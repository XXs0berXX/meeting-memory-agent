"""One-off: copy the store built earlier in SQLite (store/memory.db) into Postgres + pgvector.

Copies the raw layer (meetings, turns, chunks + embeddings) and the memory layer (items + embeddings,
links, roles, logs) as-is, so no LLM call or embedding has to be redone.
The SQLite file is not shipped (the Postgres dump store/meeting_memory.dump replaces it); this script is
kept to document how the data moved. usage: docker compose up -d && python scripts/migrate_sqlite_to_pg.py
"""
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from meeting_memory import config  # noqa: E402
from meeting_memory.store import connect  # noqa: E402


def f32(b):
    return np.frombuffer(b, dtype=np.float32) if b is not None else None


def main():
    src = sqlite3.connect(config.DB_PATH)
    dst = connect()
    t0 = time.time()
    dst.execute("TRUNCATE meetings, turns, chunks, speaker_roles, memory_items, item_links, topic_vecs, "
                "extract_log, reconcile_log RESTART IDENTITY")

    def copy(table, cols, rows, transform=None):
        rows = [transform(r) if transform else r for r in rows]
        q = f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(cols.split(',')))})"
        for i in range(0, len(rows), 2000):
            dst.executemany(q, rows[i:i + 2000])
        print(f"  {table}: {len(rows)} rows")

    copy("meetings", "meeting_id, team, letter, seq, n_turns, n_words, path, sha1, indexed_at",
         src.execute("SELECT * FROM meetings"))
    copy("turns", "meeting_id, idx, t_sec, t_str, speaker, text", src.execute("SELECT * FROM turns"))
    copy("chunks", "chunk_id, meeting_id, team, seq, turn_start, turn_end, text, embedding",
         src.execute("SELECT c.chunk_id, c.meeting_id, c.team, c.seq, c.turn_start, c.turn_end, c.text, v.vec "
                     "FROM chunks c LEFT JOIN chunk_vecs v ON v.chunk_id=c.chunk_id"),
         lambda r: (*r[:7], f32(r[7])))
    dst.execute("SELECT setval('chunks_chunk_id_seq', (SELECT MAX(chunk_id) FROM chunks))")
    copy("speaker_roles", "meeting_id, speaker, role, source, evidence", src.execute("SELECT * FROM speaker_roles"))
    copy("memory_items", "item_id, team, meeting_id, seq, turn_idx, t_sec, kind, topic, summary, speaker, quote, quote_ok, embedding",
         src.execute("SELECT item_id, team, meeting_id, seq, turn_idx, t_sec, kind, topic, summary, speaker, quote, quote_ok, vec "
                     "FROM memory_items"), lambda r: (*r[:12], f32(r[12])))
    copy("item_links", "from_id, to_id, effect, source", src.execute("SELECT from_id, to_id, effect, source FROM item_links"))
    copy("topic_vecs", "topic, embedding", src.execute("SELECT topic, vec FROM topic_vecs"), lambda r: (r[0], f32(r[1])))
    copy("extract_log", "meeting_id, sha1, model, n_segments, n_items, n_dropped, in_tokens, out_tokens, cost_usd, seconds, finished_at",
         src.execute("SELECT * FROM extract_log"))
    copy("reconcile_log", "meeting_id, n_links, n_decisions, in_tokens, out_tokens, cost_usd, seconds, finished_at",
         src.execute("SELECT * FROM reconcile_log"))
    print(f"done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
