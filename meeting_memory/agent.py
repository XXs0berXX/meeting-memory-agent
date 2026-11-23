"""The question-answering agent: retrieve -> read -> decide -> (loop) -> answer -> ground.

Division of labour
  code : scope (team, as-of cut-off), first-pass retrieval, executing tool calls, evidence
         de-duplication and budget, chronological ordering, citation grounding, step limits.
  model: deciding what to look for next, reading messy dialogue, judging which statement is
         the final one, composing the answer and picking the turns that prove it.

Two interchangeable loops over the same tools (search / read / timeline / answer):
  * native tool calling - OpenAI `tools` API, used with a local model behind Ollama & co.
  * JSON-in-text        - one JSON action per step; works with any model, used by the Claude CLI
                          (and as the fallback when a local model replies in text instead of a tool call).
Multi-part questions are first decomposed into standalone sub-questions, each researched on its own,
then composed into one answer from the already-verified citations.
Context per step is bounded (AGENT_MAX_EVIDENCE_WORDS) no matter how big the corpus is.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, retrieve
from .ledger import team_roles, timeline
from .llm import complete_json, get_llm, parse_json, supports_native_tools
from .quotes import ground_citations
from .store import get_turns, meeting_seq, team_meetings

SYSTEM = ("You are a careful research agent over a store of meeting transcripts. You answer only from "
          "evidence you have retrieved, you track how decisions changed over time, and you reply with "
          "exactly one JSON object per step.")

STEP_PROMPT = """QUESTION: {question}

TEAM {team}: {meetings}
Speaker roles: {roles}

MEMORY LEDGER - notes extracted at ingest, with status computed over time (superseded = replaced later).
Use it to find where things happened; the transcript is the ground truth and the only thing you may quote.
{ledger}

TRANSCRIPT EVIDENCE retrieved so far (chronological; "#turn speaker: words"):
{evidence}

ACTIONS SO FAR:
{history}

You have {left} step(s) left. Choose ONE action and reply with JSON:
{{"thought": "<one short sentence>", "action": "search", "query": "<words likely said in the meeting>", "meeting": "<optional meeting id>"}}
{{"thought": "...", "action": "read", "meeting": "<meeting id>", "turn": <turn number>}}   (shows the turns around it)
{{"thought": "...", "action": "timeline", "topic": "<topic words>"}}   (full ledger history of a topic)
{{"thought": "...", "action": "answer", "answer": "<1-3 plain sentences>", "citations": [{{"meeting_id": "<id>", "turn": <n>, "quote": "<8-30 words copied exactly from that one turn>"}}]}}
Rules:
- Answer EVERY part of the question. These questions span several meetings: check the later meetings for changes; the last decision is the final one.
- Identify people as "Speaker X (Role)" using the transcript letters and the roles listed above (one role per speaker).
- Write the answer for a reader who has never seen your notes: never mention the ledger, notes, items or steps.
- Give 1-3 citations, ideally from different meetings, each showing a key fact of your answer. Copy quotes character for character from ONE turn in the evidence above.
- Search with words people would actually say (e.g. "battery", "solar", "twelve fifty"), not with the question's wording.
- Answer as soon as the evidence covers every part of the question.{force}"""

REPAIR = """Your answer's citations could not be verified against the transcript:
{errors}
Copy each quote exactly (8-30 consecutive words from ONE turn shown in the evidence). Reply with the corrected
answer JSON: {{"action": "answer", "answer": "...", "citations": [{{"meeting_id": "...", "turn": <n>, "quote": "..."}}]}}"""

TEAM_RE = re.compile(r"\b([A-Z]{2}\d{4})\b")


class Evidence:
    """Turns gathered so far, keyed by (meeting, turn) with a priority. Rendered merged and in
    chronological order; lowest-priority turns are evicted when over the word budget."""

    def __init__(self, budget: int):
        self.prio: dict[tuple[str, int], float] = {}
        self.budget = budget
        self._text: dict[tuple[str, int], tuple[str, str]] = {}

    def add_span(self, meeting_id: str, a: int, b: int, prio: float) -> list[tuple[str, int]]:
        keys = []
        for r in get_turns(meeting_id, a, b):
            k = (meeting_id, r["idx"])
            self._text[k] = (r["speaker"], r["text"])
            self.prio[k] = max(self.prio.get(k, 0), prio)
            keys.append(k)
        self._evict()
        return keys

    def _words(self):
        return sum(len(self._text[k][1].split()) for k in self.prio)

    def _evict(self):
        while self._words() > self.budget and len(self.prio) > 1:
            k = min(self.prio, key=lambda k: self.prio[k])
            del self.prio[k]

    def render(self, only: list | None = None, max_words: int | None = None) -> str:
        pool = [k for k in (only if only is not None else self.prio) if k in self._text]
        if not pool:
            return "(nothing yet)"
        keys = sorted(set(pool), key=lambda k: ((meeting_seq(k[0]) or 0), k[0], k[1]))
        if max_words:  # trim (used for tool results in the native loop)
            kept, n = [], 0
            for k in keys:
                n += len(self._text[k][1].split())
                if n > max_words:
                    break
                kept.append(k)
            keys = kept
        out, last = [], None
        for mid, idx in keys:
            if last is None or last[0] != mid:
                out.append(f"=== {mid} ===")
            elif idx != last[1] + 1:
                out.append("   ...")
            spk, text = self._text[(mid, idx)]
            out.append(f"#{idx} {spk}: {text}")
            last = (mid, idx)
        return "\n".join(out)


def _meetings_line(team: str, max_seq: int | None) -> str:
    ms = [m for m in team_meetings(team) if max_seq is None or m["seq"] <= max_seq]
    names = ["1st", "2nd", "3rd", "4th"]
    return ", ".join(f"{m['meeting_id']} ({names[m['seq'] - 1] if m['seq'] <= 4 else m['seq']})" for m in ms)


def _roles_line(team: str, max_seq: int | None) -> str:
    tr = team_roles(team)
    parts = []
    for m in team_meetings(team):
        if max_seq is not None and m["seq"] > max_seq:
            continue
        r = tr.get(m["meeting_id"], {})
        if r:
            parts.append(f"{m['meeting_id']}: " + ", ".join(f"{k}={v}" for k, v in sorted(r.items())))
    return "; ".join(parts) or "unknown"


TOOLS = [
    {"type": "function", "function": {
        "name": "search", "description": "Hybrid search over this team's transcripts. Use words people would actually say.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "words likely said in the meeting, e.g. 'battery solar kinetic'"},
            "meeting": {"type": "string", "description": "optional meeting id to restrict to, e.g. ES2008c"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read", "description": "Read the turns around a turn number in a meeting.",
        "parameters": {"type": "object", "properties": {
            "meeting": {"type": "string"}, "turn": {"type": "integer"}}, "required": ["meeting", "turn"]}}},
    {"type": "function", "function": {
        "name": "timeline", "description": "Full memory-ledger history of a topic across meetings, with statuses.",
        "parameters": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}}},
    {"type": "function", "function": {
        "name": "answer", "description": "Give the final answer with 1-3 citations copied exactly from single turns.",
        "parameters": {"type": "object", "properties": {
            "answer": {"type": "string", "description": "1-3 plain sentences"},
            "citations": {"type": "array", "items": {"type": "object", "properties": {
                "meeting_id": {"type": "string"}, "turn": {"type": "integer"},
                "quote": {"type": "string", "description": "8-30 words copied exactly from that one turn"}},
                "required": ["meeting_id", "quote"]}}},
            "required": ["answer", "citations"]}}},
]

NATIVE_SYSTEM = (SYSTEM.replace(" and you reply with exactly one JSON object per step", "") +
                 " Use the tools to gather evidence, then call `answer`. Rules: answer every part of the question; "
                 "check later meetings for changes (the last decision is final); identify people as 'Speaker X (Role)'; "
                 "never mention your notes or tools in the answer; copy citation quotes exactly from one turn.")

NATIVE_START = """QUESTION: {question}

TEAM {team}: {meetings}
Speaker roles: {roles}

MEMORY LEDGER (notes extracted at ingest; statuses computed over time; locate things here, quote only the transcript):
{ledger}

TRANSCRIPT EVIDENCE already retrieved (chronological; "#turn speaker: words"):
{evidence}

You have at most {steps} tool calls. Call `answer` as soon as the evidence covers every part of the question."""

DECOMPOSE_SYSTEM = "You split compound questions into standalone sub-questions. Reply with one JSON object only."
DECOMPOSE = """QUESTION: {question}

List the separate things this question asks (1 to 4 parts). Rewrite each part as a standalone question that
names the team and carries any context it needs from the other parts (e.g. "...whichever of the LCD or speech
recognition they kept"). If it asks a single thing, return it unchanged as the only part.
Reply: {{"parts": ["...", "..."]}}"""

COMPOSE_SYSTEM = "You combine researched partial answers into one precise final answer. Reply with one JSON object only."
COMPOSE = """ORIGINAL QUESTION: {question}

RESEARCHED PARTS (each answer was produced from the transcripts; citations are verified quotes):
{parts}

Write the final answer to the ORIGINAL question in 1-3 plain sentences. Keep every concrete fact (options, numbers,
who as "Speaker X (Role)", which meeting, what was final). If parts conflict, prefer the later meeting.
Pick the 1-3 citation numbers that best prove the key facts, preferring different meetings.
Reply: {{"answer": "...", "citations": [1, 2]}}"""


class Session:
    """One research run over one (sub-)question: scope, tools, evidence, usage, trace."""

    def __init__(self, question: str, team: str | None, max_seq: int | None, qid: str, verbose: bool,
                 max_steps: int):
        self.question, self.team, self.max_seq, self.qid, self.verbose = question, team, max_seq, qid, verbose
        self.max_steps = max_steps
        self.ev = Evidence(config.AGENT_MAX_EVIDENCE_WORDS)
        self.seen: set[int] = set()
        self.trace: list[dict] = []
        self.history: list[str] = []
        self.usage = {"calls": 0, "in": 0, "out": 0, "cost": 0.0}
        self.ledger_txt = "(no ledger: team unknown)"

    # ---- tools (plain code) --------------------------------------------------------
    def search(self, q: str, meeting: str | None = None, k: int = 4, prio: float = 1.0) -> tuple[str, list]:
        hits = retrieve.search(q, team=self.team, meetings=[meeting] if meeting else None, max_seq=self.max_seq,
                               k=k, exclude=self.seen)
        keys = []
        for rank, h in enumerate(hits):
            self.seen.add(h["chunk_id"])
            keys += self.ev.add_span(h["meeting_id"], h["turn_start"], h["turn_end"], prio - 0.01 * rank)
        return f"{len(hits)} passages: " + ", ".join(f"{h['meeting_id']}#{h['turn_start']}-{h['turn_end']}" for h in hits), keys

    def read(self, mid: str, turn, prio: float) -> tuple[str, list]:
        try:
            turn = int(str(turn).lstrip("#"))
        except ValueError:
            turn = 0
        if self.team and not mid.startswith(self.team):
            return f"meeting {mid} is outside team {self.team}", []
        if self.max_seq is not None and (meeting_seq(mid) or 99) > self.max_seq:
            return f"meeting {mid} is after the cut-off and not available", []
        keys = self.ev.add_span(mid, max(0, turn - 3), turn + 8, prio)
        return f"added {mid} turns {max(0, turn - 3)}-{turn + 8}", keys

    def timeline(self, topic: str) -> str:
        if not self.team:
            return "(no team)"
        tl = timeline(self.team, topic_query=topic, max_seq=self.max_seq, with_quote=False, limit=25)
        if tl.startswith("##"):
            self.ledger_txt = tl
        return tl

    def bootstrap(self):
        """Step 0 (code only): ledger hits + global hybrid search + best passages of EACH meeting."""
        if self.team:
            ids = retrieve.search_items(self.question, self.team, max_seq=self.max_seq, k=10)
            self.ledger_txt = timeline(self.team, ids, max_seq=self.max_seq, with_quote=False, limit=30)
        obs, _ = self.search(self.question, k=5, prio=1.0)
        if self.team:
            ms = [m for m in team_meetings(self.team) if self.max_seq is None or m["seq"] <= self.max_seq]
            for m in ms:
                self.search(self.question, meeting=m["meeting_id"], k=2, prio=0.9)
            if ms:  # closing turns of the last meeting in scope: where long arguments get settled
                last = ms[-1]["meeting_id"]
                tail = [r["idx"] for r in get_turns(last) if len(r["text"].split()) >= 6][-14:]
                if tail:
                    self.ev.add_span(last, tail[0], tail[-1], prio=0.85)
        self.history.append(f"0. (auto) search {self.question[:60]!r} -> {obs}; plus top passages from each meeting")
        self.trace.append({"step": 0, "action": "auto_search", "obs": obs})

    def add_usage(self, res):
        self.usage["calls"] += 1
        self.usage["in"] += res.in_tokens
        self.usage["out"] += res.out_tokens
        self.usage["cost"] += res.cost_usd

    def step_prompt(self, left: int, force: str = "") -> str:
        return STEP_PROMPT.format(question=self.question, team=self.team or "unknown",
                                  meetings=_meetings_line(self.team, self.max_seq) if self.team else "",
                                  roles=_roles_line(self.team, self.max_seq) if self.team else "unknown",
                                  ledger=self.ledger_txt, evidence=self.ev.render(), history="\n".join(self.history),
                                  left=left, force=force)

    def do_action(self, step: int, a: str, args: dict) -> tuple[str, list]:
        """Execute one non-answer action; returns (observation summary, new evidence keys)."""
        if a == "search":
            obs, keys = self.search(str(args.get("query") or self.question), args.get("meeting") or None, k=4,
                                    prio=1.5 + step * 0.1)
            label = f"search {args.get('query')!r}" + (f" in {args['meeting']}" if args.get("meeting") else "")
        elif a == "read":
            obs, keys = self.read(str(args.get("meeting", "")), args.get("turn", 0), prio=3.0 + step * 0.1)
            label = f"read {args.get('meeting')}#{args.get('turn')}"
        elif a == "timeline":
            tl = self.timeline(str(args.get("topic", "")))
            obs, keys = f"ledger view replaced ({tl.count(chr(10)) + 1} lines)", []
            label = f"timeline {args.get('topic')!r}"
        else:
            obs, keys, label = "invalid action (use search/read/timeline/answer)", [], f"invalid {a!r}"
        self.history.append(f"{step}. {label} -> {obs}")
        self.trace.append({"step": step, "action": a, "args": args, "obs": self.history[-1]})
        return obs, keys


SELF_CHECK = ("NOT ACCEPTED YET. Check it: which part of the question has the weakest evidence (the stated reason? "
              "the final meeting? who said it? a number?). Search for exactly that, then answer.")


def _loop_json(sess: Session) -> dict | None:
    """JSON-in-text protocol: one JSON action per model call."""
    self_checked = False
    for step in range(1, sess.max_steps + 1):
        left = sess.max_steps - step + 1
        force = "\nThis is your LAST step: you must reply with the answer action now." if left == 1 else ""
        try:
            act, res = complete_json("agent", SYSTEM, sess.step_prompt(left, force), purpose="agent_step", max_tokens=1500)
        except Exception as e:
            sess.trace.append({"step": step, "error": str(e)})
            continue
        sess.add_usage(res)
        a = str(act.get("action", "")).lower()
        if sess.verbose:
            print(f"  [{sess.qid}] step {step}: {a} {json.dumps({k: v for k, v in act.items() if k != 'action'})[:200]}", flush=True)
        if a == "answer" and step == 1 and left > 2 and not self_checked:
            self_checked = True  # self-check loop (code-enforced, once)
            sess.history.append(f"{step}. draft answer: {str(act.get('answer'))[:300]!r} -> {SELF_CHECK}")
            sess.trace.append({"step": step, "action": "draft_answer", "raw": act})
            continue
        if a == "answer" or (left == 1 and a not in ("search", "read", "timeline")):
            sess.trace.append({"step": step, "action": "answer", "thought": act.get("thought"), "raw": act})
            return act
        sess.do_action(step, a, {k: v for k, v in act.items() if k not in ("action", "thought")})
    return None


def _loop_native(sess: Session) -> dict | None:
    """Native tool calling (OpenAI tools API). Tool results carry the newly retrieved transcript turns.
    If the model replies with text instead of a tool call, the text is parsed with the JSON protocol."""
    llm = get_llm("agent")
    msgs = [{"role": "system", "content": NATIVE_SYSTEM},
            {"role": "user", "content": NATIVE_START.format(
                question=sess.question, team=sess.team or "unknown",
                meetings=_meetings_line(sess.team, sess.max_seq) if sess.team else "",
                roles=_roles_line(sess.team, sess.max_seq) if sess.team else "unknown",
                ledger=sess.ledger_txt, evidence=sess.ev.render(), steps=sess.max_steps)}]
    self_checked, nudged = False, False
    for step in range(1, sess.max_steps + 1):
        left = sess.max_steps - step + 1
        if left == 1:
            msgs.append({"role": "user", "content": "Last step: call `answer` now."})
        try:
            msg, res = llm.chat(msgs, TOOLS, purpose="agent_step_native", max_tokens=1500)
        except Exception as e:
            sess.trace.append({"step": step, "error": str(e)})
            continue
        sess.add_usage(res)
        calls = msg.get("tool_calls") or []
        if not calls:  # text reply: try the JSON protocol, else nudge once
            try:
                act = parse_json(msg.get("content") or "")
                a = str(act.get("action", "answer" if "answer" in act else "")).lower()
                calls = [{"id": f"text{step}", "function": {"name": a, "arguments": json.dumps(
                    {k: v for k, v in act.items() if k not in ("action", "thought")})}}]
            except Exception:
                if nudged:
                    continue
                nudged = True
                msgs.append({"role": "assistant", "content": msg.get("content") or ""})
                msgs.append({"role": "user", "content": "Use one of the tools (search, read, timeline, answer)."})
                continue
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": [
            {"id": c.get("id") or f"call{step}_{i}", "type": "function", "function": c["function"]}
            for i, c in enumerate(calls)]})
        for i, c in enumerate(calls):
            name = c["function"]["name"]
            raw = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args = {}
            call_id = c.get("id") or f"call{step}_{i}"
            if sess.verbose:
                print(f"  [{sess.qid}] step {step}: tool {name} {json.dumps(args)[:200]}", flush=True)
            if name == "answer":
                if step == 1 and left > 2 and not self_checked:
                    self_checked = True
                    sess.trace.append({"step": step, "action": "draft_answer", "raw": args})
                    msgs.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": SELF_CHECK})
                    continue
                sess.trace.append({"step": step, "action": "answer", "raw": args})
                return args
            obs, keys = sess.do_action(step, name, args)
            content = obs + ("\n" + sess.ev.render(only=keys, max_words=900) if keys else "")
            if name == "timeline":
                content = sess.ledger_txt
            msgs.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": content})
    return None


def _ground(sess: Session, final: dict) -> tuple[str, list, list]:
    """Code grounds every quote; one model repair round if grounding fails."""
    team = sess.team
    answer = str(final.get("answer", "")).strip()
    cits, errors = ground_citations(final.get("citations") or [])
    if team:
        bad = [c for c in cits if not c["meeting_id"].startswith(team)]
        cits = [c for c in cits if c["meeting_id"].startswith(team)]
        errors += [f"{c['meeting_id']} is not a {team} meeting" for c in bad]
    if (errors and len(cits) < 2) or not cits:
        prompt = sess.step_prompt(1) + "\n\n" + REPAIR.format(errors="\n".join(errors) or "no citations given")
        try:
            fix, res = complete_json("agent", SYSTEM, prompt, purpose="agent_repair", max_tokens=1500)
            sess.add_usage(res)
            c2, e2 = ground_citations(fix.get("citations") or [])
            c2 = [c for c in c2 if not team or c["meeting_id"].startswith(team)]
            if len(c2) >= len(cits):
                cits, errors = c2, e2
                answer = str(fix.get("answer") or answer).strip()
            sess.trace.append({"step": "repair", "raw": fix, "errors": e2})
        except Exception as e:
            sess.trace.append({"step": "repair", "error": str(e)})
    return answer, cits, errors


def research(question: str, team: str | None, max_seq: int | None, qid: str, verbose: bool,
             max_steps: int | None = None) -> dict:
    sess = Session(question, team, max_seq, qid, verbose, max_steps or config.AGENT_MAX_STEPS)
    sess.bootstrap()
    final = _loop_native(sess) if supports_native_tools() else _loop_json(sess)
    if final is None:
        final = {"answer": "Insufficient evidence retrieved to answer.", "citations": []}
    answer, cits, errors = _ground(sess, final)
    steps = len([t for t in sess.trace if isinstance(t.get("step"), int) and t["step"] > 0])
    return {"question": question, "answer": answer, "citations": cits, "errors": errors, "steps": steps,
            "usage": sess.usage, "trace": sess.trace}


def decompose(question: str, usage: dict) -> list[str]:
    try:
        d, res = complete_json("agent", DECOMPOSE_SYSTEM, DECOMPOSE.format(question=question), purpose="decompose",
                               max_tokens=600)
        for k in ("calls", "in", "out", "cost"):
            usage[k] += {"calls": 1, "in": res.in_tokens, "out": res.out_tokens, "cost": res.cost_usd}[k]
        parts = [str(p).strip() for p in d.get("parts") or [] if str(p).strip()]
        return parts[:4] or [question]
    except Exception:
        return [question]


def answer_question(question: str, team: str | None = None, max_seq: int | None = None,
                    qid: str = "adhoc", verbose: bool = False) -> dict:
    t0 = time.time()
    team = team or (TEAM_RE.search(question).group(1) if TEAM_RE.search(question) else None)
    usage = {"calls": 0, "in": 0, "out": 0, "cost": 0.0}
    parts = decompose(question, usage) if config.AGENT_DECOMPOSE else [question]
    if verbose and len(parts) > 1:
        print(f"  [{qid}] decomposed into {len(parts)} parts:", *[f"\n    - {p}" for p in parts], flush=True)

    if len(parts) == 1:
        r = research(question, team, max_seq, qid, verbose)
        results, answer, cits, errors = [r], r["answer"], r["citations"], r["errors"]
    else:
        # each part researched on its own (smaller step budget), in parallel
        steps_each = max(3, config.AGENT_MAX_STEPS - 2)
        with ThreadPoolExecutor(max_workers=len(parts)) as ex:
            results = list(ex.map(lambda ip: research(ip[1], team, max_seq, f"{qid}.{ip[0] + 1}", verbose, steps_each),
                                  enumerate(parts)))
        pool, blocks = [], []
        for i, r in enumerate(results, 1):
            lines = [f"Part {i}: {r['question']}", f"Answer: {r['answer']}"]
            for c in r["citations"]:
                pool.append(c)
                lines.append(f'  [{len(pool)}] {c["meeting_id"]}: "{c["quote"]}"')
            blocks.append("\n".join(lines))
        answer, cits, errors = "", [], [e for r in results for e in r["errors"]]
        try:
            d, res = complete_json("agent", COMPOSE_SYSTEM, COMPOSE.format(question=question, parts="\n\n".join(blocks)),
                                   purpose="compose", max_tokens=1200)
            for k, v in (("calls", 1), ("in", res.in_tokens), ("out", res.out_tokens), ("cost", res.cost_usd)):
                usage[k] += v
            answer = str(d.get("answer", "")).strip()
            for n in d.get("citations") or []:
                try:
                    c = pool[int(n) - 1]
                except (ValueError, IndexError, TypeError):
                    continue
                if c not in cits:
                    cits.append(c)
        except Exception as e:
            errors.append(f"compose failed: {e}")
        if not answer:  # fall back to concatenating part answers
            answer = " ".join(r["answer"] for r in results)
        if not cits:  # fall back to the first citation of each part
            cits = [r["citations"][0] for r in results if r["citations"]]
    for r in results:
        for k in usage:
            usage[k] += r["usage"].get(k, 0)

    out = {
        "question_id": qid,
        "answer": answer,
        "citations": [{"meeting_id": c["meeting_id"], "quote": c["quote"]} for c in cits[:3]],
        "_meta": {"team": team, "parts": parts, "steps": sum(r["steps"] for r in results),
                  "grounding": [c["method"] for c in cits[:3]], "citation_errors": errors,
                  "protocol": "native-tools" if supports_native_tools() else "json",
                  "seconds": round(time.time() - t0, 1), "usage": usage},
    }
    config.TRACE_DIR.mkdir(parents=True, exist_ok=True)
    (config.TRACE_DIR / f"{qid}.json").write_text(json.dumps(
        {"question": question, "result": out,
         "parts": [{"question": r["question"], "answer": r["answer"], "trace": r["trace"]} for r in results]},
        indent=2, default=str), encoding="utf-8")
    return out


def answer_many(questions: list[dict], workers: int | None = None, verbose: bool = True) -> list[dict]:
    workers = workers or config.LLM_CONCURRENCY

    def one(q):
        try:
            r = answer_question(q["question"], q.get("team"), qid=q["question_id"], verbose=False)
        except Exception as e:  # one failing question must not sink the batch
            r = {"question_id": q["question_id"], "answer": f"(agent error: {e})", "citations": [],
                 "_meta": {"team": q.get("team"), "parts": [], "steps": 0, "grounding": [], "citation_errors": [str(e)],
                           "seconds": 0, "usage": {}}}
        if verbose:
            m = r["_meta"]
            print(f"{q['question_id']}: {len(m.get('parts') or [1])} parts, {m['steps']} steps, {len(r['citations'])} cits "
                  f"{m['grounding']}, {m['seconds']}s :: {r['answer'][:110]}", flush=True)
        return r

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, questions))
    return results
