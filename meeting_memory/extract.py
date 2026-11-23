"""Stage 2 of ingest: the memory layer. This is where a model IS needed.

Turning "yeah, let's just go with the the rubber one then" into
    decision | case material | "rubber case chosen, replacing the plastic proposed in meeting b"
requires reading messy conversation, resolving references ("the rubber one", "what Sarah said")
against what the team said before, and judging whether this replaces an earlier decision.
That is language understanding; regex can't do it.

Everything around that judgement is code:
  * segmentation (bounded input per call -> works with an 8-16k-context 9B model),
  * choosing which slice of the ledger to show (bounded, relevance-ranked),
  * grounding each item's quote in the transcript (items whose quote can't be found are kept
    but marked uncitable),
  * validating link targets (must exist, must be earlier, same team),
  * ordering: teams in parallel, meetings within a team strictly in time order, because each
    meeting is read against the memory of the ones before it.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import config, embed
from .ledger import EFFECTS, KINDS, context_for_extraction, fmt_item, team_roles
from .llm import complete_json, get_llm
from .quotes import snap
from .store import connect, get_turns, roles_for, team_meetings, upsert_role

SYSTEM = ("You extract structured meeting memory from raw meeting transcripts. You are precise, "
          "you never invent facts, and you reply with a single JSON object only.")

PROMPT = """TEAM {team} | MEETING {meeting_id} = meeting {seq} of the team's series (a, b, c, d in time order)
Known speaker roles in this meeting: {roles}

MEMORY SO FAR for this team (earlier meetings and earlier parts of this meeting). Ids in brackets:
{ledger}

NEW TRANSCRIPT SEGMENT ("#turn speaker: words"):
{segment}

TASK: record what this segment adds to the team's memory. Item kinds:
- decision: the team agrees or settles on something (also tentative "let's go with X")
- proposal: someone puts forward an option/idea that is not settled
- rejection: an option is dropped or ruled out (say why)
- action: someone commits or is asked to do something (owner = speaker field)
- disagreement: someone objects to or pushes back on an idea (say who and why)
- fact: a stated number, price, cost, budget, requirement, research finding
For each item:
  "kind", "topic": 2-4 words naming the subject (REUSE the exact topic name of a memory item if it is the same subject),
  "summary": one specific sentence with names of options, numbers and reasons,
  "speaker": letter of the person who said it (for actions: the owner),
  "turn": the #turn number where it is said,
  "quote": 8-25 words copied EXACTLY from that turn (keep "um", "uh", repetitions),
  "updates": id of an EARLIER memory item this one changes or refers back to, or null,
  "effect": if updates is set, one of supersedes | reverses | rejects | adopts | confirms | resolves | answers | completes.
Use "updates" whenever the segment changes, drops, confirms, reports back on, or finishes something already in memory,
even if it is referred to indirectly ("that idea", "the other one", "what we said last time").
Skip greetings, chit-chat, and technical problems with the meeting setup. At most 14 items; prefer substance.
Also return "roles": map of speaker letter to role (Project Manager, Industrial Designer, User Interface Designer, Marketing Expert) for anyone whose role this segment makes clear.

Reply with JSON: {{"roles": {{}}, "items": [{{"kind": "...", "topic": "...", "summary": "...", "speaker": "A", "turn": 12, "quote": "...", "updates": null, "effect": null}}]}}"""


def segments(meeting_id: str, target: int = config.SEGMENT_TARGET_WORDS):
    turns = get_turns(meeting_id)
    seg, words, out = [], 0, []
    for t in turns:
        seg.append(t)
        words += len(t["text"].split())
        if words >= target:
            out.append(seg)
            seg, words = [], 0
    if seg:
        if out and words < target * 0.35:
            out[-1].extend(seg)  # avoid a tiny trailing segment
        else:
            out.append(seg)
    return out


def _fmt_segment(seg) -> str:
    return "\n".join(f"#{t['idx']} {t['speaker']}: {t['text']}" for t in seg)


def extract_meeting(meeting_row, verbose: bool = True) -> dict:
    conn = connect()
    mid, team, seq = meeting_row["meeting_id"], meeting_row["team"], meeting_row["seq"]
    # idempotent re-run: clear this meeting's previous items/links
    old = [r[0] for r in conn.execute("SELECT item_id FROM memory_items WHERE meeting_id=?", (mid,))]
    for iid in old:
        conn.execute("DELETE FROM item_links WHERE from_id=? OR to_id=?", (iid, iid))
    conn.execute("DELETE FROM memory_items WHERE meeting_id=?", (mid,))
    conn.execute("DELETE FROM speaker_roles WHERE meeting_id=? AND source='llm'", (mid,))
    conn.commit()

    stats = {"meeting_id": mid, "items": 0, "dropped": 0, "uncitable": 0, "links": 0, "in": 0, "out": 0,
             "cost": 0.0, "secs": 0.0, "segments": 0}
    n_item = 0
    t0 = time.time()
    for seg in segments(mid):
        stats["segments"] += 1
        seg_text = _fmt_segment(seg)
        seg_vec = embed.embed_passages([seg_text[:4000]])[0] if embed.available() else None
        ctx = context_for_extraction(team, seq, seg_vec, cap=config.LEDGER_CONTEXT_ITEMS)
        roles_map = team_roles(team)
        ledger_txt = "\n".join(fmt_item(i, roles_map, with_quote=False) for i in ctx) or "(empty: nothing recorded yet)"
        roles = roles_for(mid)
        prompt = PROMPT.format(team=team, meeting_id=mid, seq=seq,
                               roles=", ".join(f"{k}={v}" for k, v in sorted(roles.items())) or "unknown",
                               ledger=ledger_txt, segment=seg_text)
        try:
            data, res = complete_json("extract", SYSTEM, prompt, purpose="extract", max_tokens=6000)
        except Exception as e:  # one bad segment must not kill the whole ingest
            print(f"  !! {mid} segment failed: {e}", flush=True)
            continue
        stats["in"] += res.in_tokens
        stats["out"] += res.out_tokens
        stats["cost"] += res.cost_usd

        # roles proposed by the model: only fill gaps, never override regex evidence
        for spk, role in (data.get("roles") or {}).items():
            spk = str(spk).strip().upper()[:1]
            if spk in roles or not role or role in roles.values():
                continue
            upsert_role(mid, spk, str(role), "llm", "")
            roles[spk] = str(role)

        valid_ids = {r[0] for r in conn.execute("SELECT item_id FROM memory_items WHERE team=?", (team,))}
        turn_lo, turn_hi = seg[0]["idx"], seg[-1]["idx"]
        new_vec_texts = []
        for it in data.get("items") or []:
            kind = str(it.get("kind", "")).lower().strip()
            if kind not in KINDS or not it.get("summary"):
                stats["dropped"] += 1
                continue
            try:
                turn = int(str(it.get("turn", "")).lstrip("#"))
            except ValueError:
                turn = None
            if turn is not None and not (turn_lo <= turn <= turn_hi):
                turn = None
            g = snap(mid, str(it.get("quote") or ""), turn) if it.get("quote") else None
            if g is not None and not (turn_lo - 3 <= g["turn"] <= turn_hi + 3):
                g = None
            tidx = g["turn"] if g else (turn if turn is not None else turn_lo)
            trow = conn.execute("SELECT t_sec, speaker FROM turns WHERE meeting_id=? AND idx=?", (mid, tidx)).fetchone()
            n_item += 1
            iid = f"{mid}-{n_item:02d}"
            speaker = str(it.get("speaker") or (trow["speaker"] if trow else "")).strip().upper()[:1]
            conn.execute(
                "INSERT INTO memory_items(item_id, team, meeting_id, seq, turn_idx, t_sec, kind, topic, summary, speaker, quote, quote_ok) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, team, mid, seq, tidx, trow["t_sec"] if trow else 0, kind, str(it.get("topic") or "misc")[:60],
                 str(it["summary"])[:500], speaker, g["quote"] if g else None, 1 if g else 0))
            stats["items"] += 1
            stats["uncitable"] += 0 if g else 1
            tgt, eff = it.get("updates"), str(it.get("effect") or "").lower().strip()
            if tgt and str(tgt).strip("[] ") in valid_ids and eff in EFFECTS:
                conn.execute("INSERT INTO item_links(from_id, to_id, effect, source) VALUES (?,?,?,'extract') ON CONFLICT DO NOTHING",
                             (iid, str(tgt).strip("[] "), eff))
                stats["links"] += 1
            new_vec_texts.append((iid, f"{it.get('topic')}: {it['summary']}"))
        if new_vec_texts and embed.available():
            vecs = embed.embed_passages([t for _, t in new_vec_texts])
            conn.executemany("UPDATE memory_items SET embedding=? WHERE item_id=?",
                             [(v, iid) for (iid, _), v in zip(new_vec_texts, vecs)])
        conn.commit()
    stats["secs"] = round(time.time() - t0, 1)
    conn.execute("DELETE FROM extract_log WHERE meeting_id=?", (mid,))
    conn.execute("INSERT INTO extract_log VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (mid, meeting_row["sha1"], get_llm("extract").model,
                  stats["segments"], stats["items"], stats["dropped"], stats["in"], stats["out"], stats["cost"],
                  stats["secs"], datetime.now(timezone.utc).isoformat()))
    conn.commit()
    if verbose:
        print(f"  {mid}: {stats['segments']} seg, {stats['items']} items ({stats['uncitable']} uncitable), "
              f"{stats['links']} links, {stats['secs']}s, ${stats['cost']:.3f}", flush=True)
    return stats


def extract_team(team: str, force: bool = False, verbose: bool = True, reconcile: bool = True) -> list[dict]:
    """Meetings strictly in order: meeting c is read against the memory of a and b."""
    conn = connect()
    out, dirty = [], force
    for m in team_meetings(team):
        done = conn.execute("SELECT sha1 FROM extract_log WHERE meeting_id=?", (m["meeting_id"],)).fetchone()
        if not dirty and done and done["sha1"] == m["sha1"]:
            continue
        dirty = True  # once one meeting is (re)extracted, everything after it must be too
        out.append(extract_meeting(m, verbose))
        if reconcile:  # close the books on this meeting before reading the next one
            from .reconcile import reconcile_meeting
            reconcile_meeting(m, verbose)
    rebuild_item_fts()
    return out


def rebuild_item_fts():
    """No-op on Postgres: the tsvector column is generated and indexed automatically.
    Kept so callers written for an external FTS index still work."""
    return None


def extract_all(teams: list[str] | None = None, force: bool = False, workers: int | None = None) -> list[dict]:
    from .store import all_teams
    teams = teams or all_teams()
    workers = workers or config.LLM_CONCURRENCY
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(extract_team, t, force): t for t in teams}
        for f in as_completed(futs):
            try:
                results.extend(f.result())
            except Exception as e:
                print(f"!! team {futs[f]} failed: {e}", flush=True)
    rebuild_item_fts()
    return results
