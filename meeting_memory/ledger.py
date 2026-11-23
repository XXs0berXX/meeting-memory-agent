"""The memory ledger: how the system knows what is *current*.

Event-sourced. Memory items (decision / proposal / rejection / action / disagreement / fact) are
append-only facts about what was said, each pinned to (meeting, turn, verbatim quote). Changes
are separate append-only LINKS created when a later item refers back to an earlier one:

    later item --supersedes / reverses / rejects / adopts / confirms / resolves / answers / completes--> earlier item

Status is never stored; it is computed by replaying links in time order up to a cut-off
(`max_seq`). So "what did the team believe before meeting d?" is the same query as "what is
current now?", just with a different cut-off. This is plain code on purpose: once the model
has said "this replaces that", deciding what is current is bookkeeping, and bookkeeping
should be deterministic, auditable and free.

Safety net for links the model missed: items are also grouped by topic and shown as a
chronological timeline, and a decision that has a LATER decision on the same topic is flagged
`later_on_topic`, so readers (agent, brief writer) still see the newer state.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .store import connect

KINDS = ["decision", "proposal", "rejection", "action", "disagreement", "fact"]
EFFECTS = {"supersedes", "reverses", "rejects", "adopts", "confirms", "resolves", "answers", "completes"}
BASE_STATUS = {"decision": "current", "proposal": "open", "rejection": "in force", "action": "open",
               "disagreement": "open", "fact": "stated"}


def norm_topic(t: str) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", (t or "").lower()).strip()
    t = re.sub(r"\s+", " ", t)
    return re.sub(r"(?<=[a-z]{3})s\b", "", t)  # crude singular: "buttons" -> "button"


TOPIC_SIM = 0.84  # cosine threshold for merging topic labels (bge-small); 0.82 starts merging wrong pairs on TS3012


def topic_clusters(team: str) -> dict[str, str]:
    """Map every normalised topic label of a team to a canonical label. Plain code: local
    embeddings of the short labels + greedy clustering (most frequent label becomes the name).
    Fixes "speech recognition" / "voice recognition option" / "speech recognition component"
    being treated as three subjects. Falls back to exact labels if embeddings are off."""
    import numpy as np
    from . import embed
    from .store import vec
    conn = connect()
    counts: dict[str, int] = defaultdict(int)
    for r in conn.execute("SELECT topic FROM memory_items WHERE team=?", (team,)):
        counts[norm_topic(r[0])] += 1
    labels = sorted(counts, key=lambda t: (-counts[t], t))
    if not labels or not embed.available():
        return {t: t for t in labels}
    have = {r[0]: vec(r[1]) for r in conn.execute(
        f"SELECT topic, embedding FROM topic_vecs WHERE topic IN ({','.join('?' * len(labels))})", labels)}
    missing = [t for t in labels if t not in have]
    if missing:
        vecs = embed.embed_passages(missing)
        conn.executemany("INSERT INTO topic_vecs VALUES (?,?) ON CONFLICT (topic) DO UPDATE SET embedding=EXCLUDED.embedding",
                         [(t, v) for t, v in zip(missing, vecs)])
        conn.commit()
        have.update(zip(missing, vecs))
    heads: list[tuple[str, np.ndarray]] = []
    out = {}
    for t in labels:
        v = have[t]
        best = max(((float(np.dot(v, hv)), h) for h, hv in heads), default=(0.0, None))
        if best[0] >= TOPIC_SIM:
            out[t] = best[1]
        else:
            heads.append((t, v))
            out[t] = t
    return out


ITEM_COLS = "item_id, team, meeting_id, seq, turn_idx, t_sec, kind, topic, summary, speaker, quote, quote_ok"


def items(team: str, max_seq: int | None = None) -> list[dict]:
    sql = f"SELECT {ITEM_COLS} FROM memory_items WHERE team=?" + (" AND seq<=?" if max_seq else "") + " ORDER BY seq, t_sec, item_id"
    rows = connect().execute(sql, [team] + ([max_seq] if max_seq else [])).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def state(team: str, max_seq: int | None = None) -> dict[str, dict]:
    """Replay links (only those created at or before max_seq) -> status per item."""
    its = {i["item_id"]: dict(i, status=BASE_STATUS.get(i["kind"], "stated"), changed_by=[], confirmed_by=[])
           for i in items(team, max_seq)}
    links = connect().execute(
        "SELECT l.* FROM item_links l JOIN memory_items f ON f.item_id=l.from_id "
        "WHERE f.team=?" + (" AND f.seq<=?" if max_seq else "") + " ORDER BY f.seq, f.t_sec",
        [team] + ([max_seq] if max_seq else [])).fetchall()
    for l in links:
        tgt, src = its.get(l["to_id"]), its.get(l["from_id"])
        if not tgt or not src:
            continue
        e = l["effect"]
        if e in ("supersedes", "reverses"):
            tgt["status"] = "reversed" if tgt["kind"] == "rejection" else "superseded"
            tgt["changed_by"].append(src["item_id"])
        elif e == "rejects":
            tgt["status"] = "rejected"
            tgt["changed_by"].append(src["item_id"])
        elif e in ("adopts", "confirms"):
            if tgt["kind"] == "proposal":
                tgt["status"] = "adopted"
            tgt["confirmed_by"].append(src["item_id"])
        elif e == "resolves":
            tgt["status"] = "resolved"
            tgt["changed_by"].append(src["item_id"])
        elif e in ("answers", "completes"):
            tgt["status"] = "done"
            tgt["changed_by"].append(src["item_id"])
    # canonical topic per item (embedding clusters), then the safety net:
    # a newer decision on the same topic without an explicit link
    clusters = topic_clusters(team)
    for i in its.values():
        i["topic_key"] = clusters.get(norm_topic(i["topic"]), norm_topic(i["topic"]))
    by_topic = defaultdict(list)
    for i in its.values():
        if i["kind"] == "decision":
            by_topic[i["topic_key"]].append(i)
    for lst in by_topic.values():
        for earlier in lst[:-1]:
            later = [x["item_id"] for x in lst if (x["seq"], x["t_sec"]) > (earlier["seq"], earlier["t_sec"])]
            if later and earlier["status"] == "current":
                earlier["later_on_topic"] = later
    return its


def mletter(meeting_id: str) -> str:
    return meeting_id[-1]


def fmt_item(i: dict, roles: dict | None = None, with_quote: bool = True) -> str:
    spk = i.get("speaker") or "?"
    if roles and roles.get(i["meeting_id"], {}).get(spk):
        spk = f"{spk} ({roles[i['meeting_id']][spk]})"
    status = i.get("status", "")
    extra = ""
    if i.get("changed_by"):
        extra += f" -> by {','.join(i['changed_by'])}"
    if i.get("later_on_topic"):
        extra += f" [newer decision on same topic: {','.join(i['later_on_topic'])}]"
    t = (i.get("t_sec") or 0) // 60
    line = (f"[{i['item_id']}] {i['meeting_id']} ~{t}min {i['kind'].upper()} ({status}{extra}) "
            f"| {i['topic']} | {i['summary']} | by {spk}")
    if with_quote and i.get("quote"):
        line += f' | quote(turn {i["turn_idx"]}): "{i["quote"]}"'
    return line


def team_roles(team: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = defaultdict(dict)
    for r in connect().execute(
            "SELECT r.meeting_id, r.speaker, r.role FROM speaker_roles r JOIN meetings m ON m.meeting_id=r.meeting_id "
            "WHERE m.team=?", (team,)):
        out[r["meeting_id"]][r["speaker"]] = r["role"]
    return out


def timeline(team: str, item_ids: list[str] | None = None, topic_query: str | None = None,
             max_seq: int | None = None, with_quote: bool = True, limit: int = 40) -> str:
    """Chronological, topic-grouped view. Topics touched by the selected items are expanded
    to their FULL history so the reader sees the before/after of every change."""
    st = state(team, max_seq)
    roles = team_roles(team)
    chosen = set(item_ids or [])
    if topic_query:
        qt = set(norm_topic(topic_query).split())
        chosen |= {iid for iid, i in st.items() if qt & set(norm_topic(i["topic"]).split())}
    topics = {st[i]["topic_key"] for i in chosen if i in st}
    # pull in items linked to/from chosen ones, and every item sharing a topic
    for i in list(chosen):
        if i in st:
            chosen |= set(st[i]["changed_by"]) | set(st[i]["confirmed_by"])
    rows = [i for i in st.values() if i["item_id"] in chosen or i["topic_key"] in topics]
    groups: dict[str, list[dict]] = defaultdict(list)
    for i in rows:
        groups[i["topic_key"]].append(i)
    order = sorted(groups.items(), key=lambda kv: (kv[1][0]["seq"], kv[1][0]["t_sec"]))
    lines, n = [], 0
    for topic, lst in order:
        lines.append(f"## topic: {topic}")
        for i in sorted(lst, key=lambda x: (x["seq"], x["t_sec"])):
            lines.append("  " + fmt_item(i, roles, with_quote))
            n += 1
            if n >= limit:
                break
        if n >= limit:
            break
    return "\n".join(lines) if lines else "(no memory items matched)"


def context_for_extraction(team: str, before_seq: int, segment_vec=None, cap: int = 45,
                           current_meeting_ids: set | None = None) -> list[dict]:
    """Compact slice of the ledger handed to the extractor so it can link new statements to
    old ones ("let's go back to the yellow one" -> which item?). Bounded size regardless of
    corpus size: live items only, ranked by kind priority, recency and similarity."""
    st = state(team, before_seq)
    live = [i for i in st.values() if i["status"] in ("current", "open", "in force", "stated", "adopted")]
    if current_meeting_ids:
        live += [i for i in items(team) if i["meeting_id"] in current_meeting_ids and i["item_id"] not in st]
    if len(live) <= cap:
        return sorted(live, key=lambda x: (x["seq"], x["t_sec"]))
    prio = {"decision": 3, "action": 3, "disagreement": 2, "proposal": 2, "rejection": 1, "fact": 1}
    sims = {}
    if segment_vec is not None:
        rows = connect().execute("SELECT item_id, 1 - (embedding <=> ?) FROM memory_items "
                                 "WHERE team=? AND embedding IS NOT NULL", (segment_vec, team))
        sims = {r[0]: float(r[1]) for r in rows}
    max_seq = max(i["seq"] for i in live)
    scored = sorted(live, key=lambda i: -(prio.get(i["kind"], 1) + 2.0 * sims.get(i["item_id"], 0)
                                           + (1.0 if i["seq"] == max_seq else 0)))
    keep = scored[:cap]
    return sorted(keep, key=lambda x: (x["seq"], x["t_sec"]))
