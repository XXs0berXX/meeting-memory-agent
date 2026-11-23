"""Pre-meeting brief, written AS OF the moment before the target meeting.

Time-slicing matters: TS3012's final meeting (d) is in the corpus, but a brief "to read before
the final meeting" must not know what happens in it. All ledger state and transcript evidence
is cut off at the meeting before the target (code-enforced via max_seq), so the brief reflects
exactly what the team knew walking in.

code : cut-off, ledger replay (decided / changed / open), bucketing, picking the closing turns
       of the last meeting (where tasks get handed out), rendering citations from grounded
       quotes, dropping any point that ends up with no valid citation.
model: judging what matters, merging duplicates, phrasing, and noticing where the notes and the
       transcript disagree.
"""
from __future__ import annotations

import json

from .ledger import fmt_item, norm_topic, state, team_roles
from .llm import complete_json
from .quotes import ground_citations
from .store import connect, get_turns, team_meetings

SYSTEM = ("You write crisp, factual pre-meeting briefs from structured meeting memory. You never "
          "invent facts; every point cites evidence. You reply with one JSON object only.")

PROMPT = """Write the brief Team {team} should read before their next meeting ({target}).
It covers meetings {covered}. Nothing after {last} has happened yet.
Speaker roles: {roles}

MEMORY ITEMS (status computed by replaying later changes; "superseded"/"rejected"/"reversed" = no longer current,
"newer decision on same topic" = check which one stands). Format: [id] meeting ~minute KIND (status) | topic | summary | by speaker | quote

-- DECISIONS --
{decisions}

-- CHANGES OVER TIME (topics whose position moved; items in time order) --
{changes}

-- ACTIONS / COMMITMENTS --
{actions}

-- DISAGREEMENTS / OBJECTIONS --
{disagreements}

-- KEY FACTS AND NUMBERS --
{facts}

-- CLOSING OF {last} (transcript; this is usually where tasks for the next meeting are handed out) --
{closing}

Write 4 sections, each a list of short points (one or two sentences, concrete: options, numbers, who):
1. "Decided so far" - the CURRENT position on each important topic (only what still stands after later changes).
2. "What changed along the way" - each position that moved: what it was, what it became, when (meeting) and why.
3. "Open actions and owners" - what each person must bring or do for {target}, plus older commitments not yet reported back. Name owners as "Speaker X (Role)".
4. "Still open / disagreements" - unresolved questions and disagreements, with who holds which view.
Every point must cite 1-2 pieces of evidence: either memory item ids (e.g. "{example_id}") or transcript turns from the closing section
as {{"meeting_id": "...", "turn": N, "quote": "exact words from that turn"}}. Prefer item ids.

Reply with JSON: {{"sections": [{{"heading": "Decided so far", "points": [{{"text": "...", "cite": ["<item id>", {{"meeting_id": "...", "turn": 0, "quote": "..."}}]}}]}}]}}"""


def _fmt(items, roles):
    return "\n".join(fmt_item(i, roles, with_quote=True) for i in items) or "(none)"


def make_brief(team: str, before_meeting: str | None = None) -> dict:
    meetings = team_meetings(team)
    if not meetings:
        raise ValueError(f"unknown team {team}")
    if before_meeting:
        target = next(m for m in meetings if m["meeting_id"] == before_meeting)
    else:  # default: before the team's final meeting on record
        target = meetings[-1]
    as_of = target["seq"] - 1
    if as_of < 1:
        raise ValueError("nothing has happened before the first meeting")
    covered = [m["meeting_id"] for m in meetings if m["seq"] <= as_of]
    last = covered[-1]

    st = state(team, as_of)
    roles = team_roles(team)
    its = sorted(st.values(), key=lambda i: (i["seq"], i["t_sec"]))
    decisions = [i for i in its if i["kind"] == "decision"]
    actions = [i for i in its if i["kind"] == "action"]
    disagreements = [i for i in its if i["kind"] == "disagreement"]
    facts = [i for i in its if i["kind"] == "fact"]
    # topics where something was superseded / rejected / reversed / overtaken
    moved = {i["topic_key"] for i in its
             if i["status"] in ("superseded", "rejected", "reversed", "adopted") or i.get("later_on_topic")}
    changes = [i for i in its if i["topic_key"] in moved and i["kind"] in ("decision", "proposal", "rejection")]

    closing_turns = get_turns(last)[-28:]
    closing = "\n".join(f"#{t['idx']} {t['speaker']}: {t['text']}" for t in closing_turns)
    role_line = "; ".join(f"{k}={v}" for k, v in sorted(roles.get(last, {}).items())) or "unknown"

    prompt = PROMPT.format(team=team, target=target["meeting_id"], covered=", ".join(covered), last=last,
                           roles=role_line, decisions=_fmt(decisions, roles), changes=_fmt(changes, roles),
                           actions=_fmt(actions, roles), disagreements=_fmt(disagreements, roles),
                           facts=_fmt(facts[-40:], roles), closing=closing,
                           example_id=decisions[-1]["item_id"] if decisions else f"{last}-01")
    data, res = complete_json("agent", SYSTEM, prompt, purpose="brief", max_tokens=6000)

    lines = [f"BRIEF FOR TEAM {team}, before meeting {target['meeting_id']} (built only from {', '.join(covered)}).",
             f"Who is who: {role_line}.", ""]
    n_points = n_dropped = 0
    for sec in data.get("sections") or []:
        pts_out = []
        for p in sec.get("points") or []:
            cits = []
            for c in p.get("cite") or []:
                if isinstance(c, str):
                    it = st.get(c.strip("[] "))
                    if it and it.get("quote"):
                        cits.append({"meeting_id": it["meeting_id"], "quote": it["quote"], "turn": it["turn_idx"]})
                elif isinstance(c, dict):
                    cits.append(c)
            good, _ = ground_citations(cits, max_n=2)
            good = [g for g in good if g["meeting_id"] in covered]
            if not good:
                n_dropped += 1
                continue
            refs = "; ".join(f'{g["meeting_id"]}: "{g["quote"]}"' for g in good)
            pts_out.append(f"- {p.get('text', '').strip()} [{refs}]")
            n_points += 1
        if pts_out:
            lines.append(sec.get("heading", "").strip().upper())
            lines += pts_out
            lines.append("")
    text = "\n".join(lines).strip()
    return {"team": team, "before": target["meeting_id"], "covered": covered, "brief": text,
            "points": n_points, "dropped_uncited": n_dropped, "usage": {"in": res.in_tokens, "out": res.out_tokens,
                                                                        "cost": res.cost_usd, "secs": res.seconds}}
