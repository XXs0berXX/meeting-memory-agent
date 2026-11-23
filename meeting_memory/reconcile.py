"""End-of-meeting reconciliation: "closing the books" on a meeting.

The per-segment extractor only sees ~1,800 words at a time, so it misses changes that are only
visible across the whole meeting, or that are implied rather than said:
  - meeting b rejects speech recognition; in meeting c nobody says "we reverse that", they just
    start arguing about where the microphone goes;
  - an action from meeting a is quietly answered in the middle of meeting c's presentation.

So after each meeting, ONE extra model call reads the meeting's extracted items (not the raw
transcript, which keeps it cheap: ~5k tokens) against the live memory from before, and proposes:
  links      - missing supersedes / reverses / adopts / answers ... edges
  decisions  - positions the team clearly holds at the end of the meeting that were never
               stated as a decision; each must point at an item of this meeting whose verbatim
               quote evidences it (so it stays citable)
Code validates every id, effect, and the time direction of every link (only later -> earlier).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np

from . import config, embed
from .ledger import EFFECTS, fmt_item, items, state, team_roles
from .llm import complete_json
from .quotes import snap
from .store import connect, get_turns, team_meetings, vec

SYSTEM = ("You maintain a team's long-term meeting memory. You are precise, conservative and never invent "
          "facts. You reply with a single JSON object only.")

PROMPT = """TEAM {team} | closing the books on meeting {mid} (meeting {seq} of the series)
Speaker roles: {roles}

EARLIER MEMORY that was still live when {mid} started (id, meeting, kind, status, topic, summary, speaker):
{before}

ITEMS RECORDED DURING {mid}, in order:
{during}

HOW {mid} ENDED (its last turns, raw transcript, "#turn speaker: words"):
{closing}

TASK
1. "links": status changes that are not yet recorded. For each, give the {mid} item that causes it ("from") and the
   EARLIER item it changes ("to", from either list above, earlier in time), with an effect:
   supersedes (a new decision replaces an old one) | reverses (a rejection or decision is undone) | rejects (a proposal is dropped)
   | adopts (a proposal is taken up) | confirms | resolves (a disagreement is settled) | answers / completes (an action is done or reported back).
   Include IMPLICIT changes: e.g. an idea rejected earlier that the team is now designing in, or a question answered in a presentation.
   Do not repeat links already shown with "->".
2. "decisions": positions the team clearly holds at the END of {mid} that are not already recorded correctly as a current
   decision item, including ones implied by how they talk. A forceful statement in the middle of an argument is NOT a decision:
   what stands at the end of the meeting counts. If the closing turns show that a recorded decision was conceded, dropped or
   left unimplemented, add a decision describing the real end state with "replaces" = that decision's id.
   Each: {{"topic": "2-4 words", "summary": "one specific sentence",
   "evidence": "<id of the {mid} item whose quote best shows it>" OR {{"turn": <closing turn number>, "quote": "<8-25 words copied exactly from that turn>"}},
   "replaces": "<earlier decision/rejection id or null>"}}.
Be conservative: only what the items and closing turns clearly support. At most 12 links and 8 decisions.

Reply with JSON: {{"links": [{{"from": "...", "to": "...", "effect": "..."}}], "decisions": [...]}}"""


def _before_items(team: str, seq: int, during_vecs, cap: int = 60):
    st = state(team, seq - 1) if seq > 1 else {}
    live = [i for i in st.values() if i["status"] in ("current", "open", "in force", "adopted")
            and i["kind"] != "fact"]
    if len(live) <= cap:
        return live
    conn = connect()
    vecs = {r[0]: vec(r[1]) for r in conn.execute(
        "SELECT item_id, embedding FROM memory_items WHERE team=? AND seq<? AND embedding IS NOT NULL", (team, seq))}
    centroid = np.mean(during_vecs, axis=0) if len(during_vecs) else None

    def score(i):
        s = {"decision": 2, "rejection": 1.5, "action": 1.5, "disagreement": 1, "proposal": 1}.get(i["kind"], 0)
        if centroid is not None and i["item_id"] in vecs:
            s += 3 * float(np.dot(vecs[i["item_id"]], centroid))
        return s + (0.5 if i["seq"] == seq - 1 else 0)

    return sorted(sorted(live, key=score, reverse=True)[:cap], key=lambda x: (x["seq"], x["t_sec"]))


def reconcile_meeting(meeting_row, verbose: bool = True) -> dict:
    conn = connect()
    mid, team, seq = meeting_row["meeting_id"], meeting_row["team"], meeting_row["seq"]
    # idempotent: drop previous reconcile output for this meeting
    old_r = [r[0] for r in conn.execute("SELECT item_id FROM memory_items WHERE meeting_id=? AND item_id LIKE ?",
                                        (mid, f"{mid}-R%"))]
    for iid in old_r:
        conn.execute("DELETE FROM item_links WHERE from_id=? OR to_id=?", (iid, iid))
        conn.execute("DELETE FROM memory_items WHERE item_id=?", (iid,))
    conn.execute("DELETE FROM item_links WHERE source='reconcile' AND from_id IN "
                 "(SELECT item_id FROM memory_items WHERE meeting_id=?)", (mid,))
    conn.commit()

    st_now = state(team, seq)
    during = [i for i in st_now.values() if i["meeting_id"] == mid]
    if not during:
        return {"meeting_id": mid, "links": 0, "decisions": 0}
    during_vecs = [vec(r[0]) for r in conn.execute(
        "SELECT embedding FROM memory_items WHERE meeting_id=? AND embedding IS NOT NULL", (mid,))]
    before = _before_items(team, seq, during_vecs)
    roles = team_roles(team)
    role_line = ", ".join(f"{k}={v}" for k, v in sorted(roles.get(mid, {}).items())) or "unknown"
    tail = [t for t in get_turns(mid) if len(t["text"].split()) >= 5][-25:]
    closing = "\n".join(f"#{t['idx']} {t['speaker']}: {t['text']}" for t in tail)
    prompt = PROMPT.format(team=team, mid=mid, seq=seq, roles=role_line, closing=closing,
                           before="\n".join(fmt_item(st_now.get(i["item_id"], i), roles, with_quote=False) for i in before)
                           or "(nothing: first meeting)",
                           during="\n".join(fmt_item(i, roles, with_quote=False) for i in sorted(during, key=lambda x: x["t_sec"])))
    t0 = time.time()
    try:
        data, res = complete_json("extract", SYSTEM, prompt, purpose="reconcile", max_tokens=4000)
    except Exception as e:
        print(f"  !! reconcile {mid} failed: {e}", flush=True)
        return {"meeting_id": mid, "links": 0, "decisions": 0}

    all_items = {i["item_id"]: i for i in items(team, seq)}
    existing = {(r[0], r[1]) for r in conn.execute("SELECT from_id, to_id FROM item_links")}
    n_links = 0
    for l in data.get("links") or []:
        f, t, e = str(l.get("from", "")).strip("[] "), str(l.get("to", "")).strip("[] "), str(l.get("effect", "")).lower()
        fi, ti = all_items.get(f), all_items.get(t)
        if not fi or not ti or e not in EFFECTS or fi["meeting_id"] != mid or (f, t) in existing:
            continue
        if (ti["seq"], ti["t_sec"]) >= (fi["seq"], fi["t_sec"]):  # links only point back in time
            continue
        conn.execute("INSERT INTO item_links(from_id, to_id, effect, source) VALUES (?,?,?,'reconcile') ON CONFLICT DO NOTHING", (f, t, e))
        existing.add((f, t))
        n_links += 1

    n_dec = 0
    new_vecs = []
    for d in data.get("decisions") or []:
        e = d.get("evidence")
        if isinstance(e, dict):  # evidence straight from a closing turn: code grounds the quote
            g = snap(mid, str(e.get("quote") or ""), e.get("turn") if isinstance(e.get("turn"), int) else None)
            trow = conn.execute("SELECT t_sec, speaker FROM turns WHERE meeting_id=? AND idx=?",
                                (mid, g["turn"])).fetchone() if g else None
            ev = {"meeting_id": mid, "seq": seq, "turn_idx": g["turn"], "t_sec": trow["t_sec"], "speaker": trow["speaker"],
                  "quote": g["quote"], "topic": d.get("topic") or "end state"} if g and trow else None
        else:
            ev = all_items.get(str(e or "").strip("[] "))
        if not ev or ev["meeting_id"] != mid or not ev.get("quote") or not d.get("summary"):
            continue
        n_dec += 1
        iid = f"{mid}-R{n_dec:02d}"
        conn.execute(
            "INSERT INTO memory_items(item_id, team, meeting_id, seq, turn_idx, t_sec, kind, topic, summary, speaker, quote, quote_ok) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
            (iid, team, mid, seq, ev["turn_idx"], ev["t_sec"], "decision", str(d.get("topic") or ev["topic"])[:60],
             "[end-of-meeting state] " + str(d["summary"])[:480], ev["speaker"], ev["quote"]))
        rep = all_items.get(str(d.get("replaces") or "").strip("[] "))
        if rep and (rep["seq"], rep["t_sec"]) <= (ev["seq"], ev["t_sec"]):
            conn.execute("INSERT INTO item_links(from_id, to_id, effect, source) VALUES (?,?,?,'reconcile') ON CONFLICT DO NOTHING",
                         (iid, rep["item_id"], "reverses" if rep["kind"] == "rejection" else "supersedes"))
            n_links += 1
        new_vecs.append((iid, f"{d.get('topic')}: {d['summary']}"))
    if new_vecs and embed.available():
        vecs = embed.embed_passages([t for _, t in new_vecs])
        conn.executemany("UPDATE memory_items SET embedding=? WHERE item_id=?",
                         [(v, iid) for (iid, _), v in zip(new_vecs, vecs)])
    conn.execute("DELETE FROM reconcile_log WHERE meeting_id=?", (mid,))
    conn.execute("INSERT INTO reconcile_log VALUES (?,?,?,?,?,?,?,?)",
                 (mid, n_links, n_dec, res.in_tokens, res.out_tokens, res.cost_usd, round(time.time() - t0, 1),
                  datetime.now(timezone.utc).isoformat()))
    conn.commit()
    if verbose:
        print(f"  reconcile {mid}: +{n_links} links, +{n_dec} end-of-meeting decisions ({time.time() - t0:.0f}s)", flush=True)
    return {"meeting_id": mid, "links": n_links, "decisions": n_dec}


def reconcile_team(team: str, force: bool = False) -> list[dict]:
    conn = connect()
    out = []
    for m in team_meetings(team):  # strictly in time order
        done = conn.execute("SELECT 1 FROM reconcile_log WHERE meeting_id=?", (m["meeting_id"],)).fetchone()
        if done and not force:
            continue
        out.append(reconcile_meeting(m))
    return out


def reconcile_all(teams=None, force: bool = False, workers: int | None = None) -> list[dict]:
    from .extract import rebuild_item_fts
    from .store import all_teams
    teams = teams or all_teams()
    res = []
    with ThreadPoolExecutor(max_workers=workers or config.LLM_CONCURRENCY) as ex:
        futs = {ex.submit(reconcile_team, t, force): t for t in teams}
        for f in as_completed(futs):
            try:
                res.extend(f.result())
            except Exception as e:
                print(f"!! reconcile {futs[f]} failed: {e}", flush=True)
    rebuild_item_fts()
    return res
