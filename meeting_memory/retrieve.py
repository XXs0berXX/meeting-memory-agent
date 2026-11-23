"""Hybrid retrieval over the raw layer, executed inside Postgres. Plain code, no LLM.

  lexical : tsvector @@ tsquery, ranked by ts_rank_cd      (GIN index)
  dense   : pgvector cosine distance `embedding <=> query`  (HNSW index)
  fusion  : Reciprocal Rank Fusion in Python
Every query is filtered by metadata first (team, meeting, as-of time).

Why both: lexical nails exact terms ("titanium", "twelve fifty", "kinetic"); dense catches paraphrase
("find it when it's lost" ~ "beeper", "whistle"). The question itself is embedded with the same model
as the chunks and looked up in pgvector.

Filtered ANN caveat: an HNSW index returns its top candidates first and filters after, so a selective
filter (one team out of thousands) can starve the result set. We enable pgvector's iterative index
scan (`hnsw.iterative_scan`), which keeps walking the graph until enough rows pass the filter.
"""
from __future__ import annotations

import re

from . import embed
from .store import connect

STOP = set("""a an and are as at be but by can could did do does for from had has have how i in into is it
its of on or our over so than that the their them then there these they this to up us was we were what when
where which who whom why will with would you your team teams meeting meetings remote control did they""".split())

_tuned: set[int] = set()


def _tune(db):
    """Per-connection pgvector settings for filtered ANN (ignored on old pgvector versions)."""
    if id(db) in _tuned:
        return
    for stmt in ("SET hnsw.iterative_scan = relaxed_order", "SET hnsw.ef_search = 100"):
        try:
            db.execute(stmt)
        except Exception:
            pass
    _tuned.add(id(db))


def _tsquery(q: str) -> str:
    toks = [t for t in re.findall(r"[a-zA-Z0-9]+", q.lower()) if t not in STOP and len(t) > 1]
    return " | ".join(dict.fromkeys(toks))


def _scope_sql(team, meetings, max_seq, alias="c"):
    where, args = [], []
    if team:
        where.append(f"{alias}.team=?")
        args.append(team)
    if meetings:
        where.append(f"{alias}.meeting_id IN ({','.join('?' * len(meetings))})")
        args += list(meetings)
    if max_seq is not None:
        where.append(f"{alias}.seq<=?")
        args.append(max_seq)
    return (" AND " + " AND ".join(where)) if where else "", args


def lexical(query: str, team=None, meetings=None, max_seq=None, k: int = 40) -> list[int]:
    tq = _tsquery(query)
    if not tq:
        return []
    scope, args = _scope_sql(team, meetings, max_seq)
    sql = (f"SELECT c.chunk_id FROM chunks c, to_tsquery('english', ?) q "
           f"WHERE c.tsv @@ q {scope} ORDER BY ts_rank_cd(c.tsv, q) DESC LIMIT ?")
    return [r[0] for r in connect().execute(sql, [tq, *args, k])]


def dense(query: str, team=None, meetings=None, max_seq=None, k: int = 40) -> list[int]:
    if not embed.available():
        return []
    db = connect()
    _tune(db)
    scope, args = _scope_sql(team, meetings, max_seq)
    sql = (f"SELECT c.chunk_id FROM chunks c WHERE c.embedding IS NOT NULL {scope} "
           f"ORDER BY c.embedding <=> ? LIMIT ?")
    return [r[0] for r in db.execute(sql, [*args, embed.embed_query(query), k])]


def rrf(*rankings: list, k: int = 60) -> list[tuple]:
    scores: dict = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def search(query: str, team=None, meetings=None, max_seq=None, k: int = 6, exclude: set | None = None) -> list[dict]:
    """Top-k chunks as dicts, de-duplicating heavily overlapping windows."""
    fused = rrf(lexical(query, team, meetings, max_seq), dense(query, team, meetings, max_seq))
    if not fused:
        return []
    conn = connect()
    ids = [cid for cid, _ in fused if not (exclude and cid in exclude)][: k * 4]
    rows = {r["chunk_id"]: r for r in conn.execute(
        f"SELECT chunk_id, meeting_id, seq, turn_start, turn_end FROM chunks WHERE chunk_id IN ({','.join('?' * len(ids))})",
        ids)} if ids else {}
    score = dict(fused)
    out, covered = [], {}
    for cid in ids:
        r = rows.get(cid)
        if r is None:
            continue
        span = set(range(r["turn_start"], r["turn_end"] + 1))
        prev = covered.setdefault(r["meeting_id"], set())
        if prev and len(span & prev) > 0.6 * len(span):
            continue
        prev |= span
        out.append({"chunk_id": cid, "meeting_id": r["meeting_id"], "seq": r["seq"], "turn_start": r["turn_start"],
                    "turn_end": r["turn_end"], "score": round(score[cid], 4)})
        if len(out) >= k:
            break
    return out


# ---- memory-layer search (over extracted items) --------------------------------------

def search_items(query: str, team: str, max_seq=None, k: int = 15) -> list[str]:
    db = connect()
    scope, args = _scope_sql(team, None, max_seq, alias="m")
    lex = []
    tq = _tsquery(query)
    if tq:
        lex = [r[0] for r in db.execute(
            f"SELECT m.item_id FROM memory_items m, to_tsquery('english', ?) q WHERE m.tsv @@ q {scope} "
            f"ORDER BY ts_rank_cd(m.tsv, q) DESC LIMIT 40", [tq, *args])]
    sem = []
    if embed.available():
        _tune(db)
        sem = [r[0] for r in db.execute(
            f"SELECT m.item_id FROM memory_items m WHERE m.embedding IS NOT NULL {scope} "
            f"ORDER BY m.embedding <=> ? LIMIT 40", [*args, embed.embed_query(query)])]
    return [iid for iid, _ in rrf(lex, sem)][:k]
