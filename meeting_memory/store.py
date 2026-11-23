"""Postgres + pgvector memory store (run it with `docker compose up -d`).

Two layers live side by side:
  1. Raw layer   - meetings, speaker turns, overlapping retrieval chunks. Full-text search is a generated
                   tsvector column with a GIN index; dense search is a vector(384) column with an HNSW
                   index. Built by plain code, cheap, deterministic.
  2. Memory layer- typed memory items (decision / proposal / rejection / action / disagreement / fact),
                   each anchored to a verbatim quote and embedded, plus an append-only table of links
                   between items (supersedes, reverses, resolves...). "What is current" is computed by
                   replaying links up to a point in time.

Every query filters by team (and optionally meeting / as-of) BEFORE ranking, which is what keeps
retrieval cheap at 10k+ meetings: one team's few hundred chunks are ranked, not the corpus.

The rest of the code talks to the DB through a thin wrapper that keeps a small, portable call style
(`?` placeholders, rows readable by name or position).
"""
from __future__ import annotations

import threading

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from psycopg.types.numeric import FloatLoader

from . import config

D = config.EMBED_DIM
SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS meetings (
    meeting_id TEXT PRIMARY KEY, team TEXT NOT NULL, letter TEXT, seq INTEGER,
    n_turns INTEGER, n_words INTEGER, path TEXT, sha1 TEXT, indexed_at TEXT
);
CREATE INDEX IF NOT EXISTS meetings_team ON meetings(team, seq);

CREATE TABLE IF NOT EXISTS turns (
    meeting_id TEXT NOT NULL, idx INTEGER NOT NULL, t_sec INTEGER, t_str TEXT,
    speaker TEXT, text TEXT, PRIMARY KEY (meeting_id, idx)
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id BIGSERIAL PRIMARY KEY, meeting_id TEXT NOT NULL, team TEXT NOT NULL, seq INTEGER,
    turn_start INTEGER, turn_end INTEGER, text TEXT,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(text, ''))) STORED,
    embedding vector({D})
);
CREATE INDEX IF NOT EXISTS chunks_team ON chunks(team, seq);
CREATE INDEX IF NOT EXISTS chunks_meeting ON chunks(meeting_id);
CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS chunks_emb ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS speaker_roles (
    meeting_id TEXT NOT NULL, speaker TEXT NOT NULL, role TEXT, source TEXT, evidence TEXT,
    PRIMARY KEY (meeting_id, speaker)
);

CREATE TABLE IF NOT EXISTS memory_items (
    item_id TEXT PRIMARY KEY, team TEXT NOT NULL, meeting_id TEXT NOT NULL, seq INTEGER,
    turn_idx INTEGER, t_sec INTEGER, kind TEXT, topic TEXT, summary TEXT, speaker TEXT,
    quote TEXT, quote_ok INTEGER,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(topic, '') || ' ' || coalesce(summary, ''))) STORED,
    embedding vector({D})
);
CREATE INDEX IF NOT EXISTS items_team ON memory_items(team, seq, t_sec);
CREATE INDEX IF NOT EXISTS items_tsv ON memory_items USING GIN (tsv);
CREATE INDEX IF NOT EXISTS items_emb ON memory_items USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS item_links (
    from_id TEXT NOT NULL, to_id TEXT NOT NULL, effect TEXT NOT NULL, source TEXT DEFAULT 'extract',
    PRIMARY KEY (from_id, to_id)
);
CREATE INDEX IF NOT EXISTS links_to ON item_links(to_id);

CREATE TABLE IF NOT EXISTS topic_vecs (topic TEXT PRIMARY KEY, embedding vector({D}));

CREATE TABLE IF NOT EXISTS reconcile_log (
    meeting_id TEXT PRIMARY KEY, n_links INTEGER, n_decisions INTEGER, in_tokens INTEGER, out_tokens INTEGER,
    cost_usd REAL, seconds REAL, finished_at TEXT
);

CREATE TABLE IF NOT EXISTS extract_log (
    meeting_id TEXT PRIMARY KEY, sha1 TEXT, model TEXT, n_segments INTEGER, n_items INTEGER,
    n_dropped INTEGER, in_tokens INTEGER, out_tokens INTEGER, cost_usd REAL, seconds REAL,
    finished_at TEXT
);
"""


class Row(tuple):
    """Row readable by position (r[0]) or column name (r["team"]), like sqlite3.Row."""

    def __new__(cls, names, values):
        r = super().__new__(cls, values)
        r._names = names
        return r

    def __getitem__(self, k):
        if isinstance(k, str):
            return tuple.__getitem__(self, self._names.index(k))
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._names)


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


def _sql(q: str) -> str:
    """Portable placeholders: code writes `?`, psycopg wants `%s` (and literal % doubled)."""
    return q.replace("%", "%%").replace("?", "%s")


class DB:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def execute(self, q: str, params=None):
        cur = self.conn.cursor(row_factory=_row_factory)
        cur.execute(_sql(q) if params is not None else q, params)
        return cur

    def executemany(self, q: str, seq):
        with self.conn.cursor() as cur:
            cur.executemany(_sql(q), list(seq))

    def commit(self):  # autocommit connection; kept so call sites read naturally
        pass


_local = threading.local()
_schema_lock = threading.Lock()
_schema_done = False


def connect() -> DB:
    """One autocommit connection per thread."""
    global _schema_done
    db = getattr(_local, "db", None)
    if db is None:
        conn = psycopg.connect(config.PG_DSN, autocommit=True)
        with _schema_lock:
            if not _schema_done:
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                register_vector(conn)
                conn.execute(SCHEMA)
                _schema_done = True
        register_vector(conn)
        conn.adapters.register_loader("numeric", FloatLoader)  # AVG/SUM -> float, as in SQLite
        db = DB(conn)
        _local.db = db
    return db


def vec(v) -> np.ndarray:
    """Normalise anything vector-like to float32 numpy (what pgvector's adapter sends/returns)."""
    if hasattr(v, "to_numpy"):  # pgvector.Vector
        v = v.to_numpy()
    return np.asarray(v, dtype=np.float32)


# ---- small read helpers -------------------------------------------------------------

def team_meetings(team: str) -> list[Row]:
    return connect().execute("SELECT * FROM meetings WHERE team=? ORDER BY seq", (team,)).fetchall()


def meeting_seq(meeting_id: str) -> int | None:
    r = connect().execute("SELECT seq FROM meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
    return r["seq"] if r else None


def get_turns(meeting_id: str, start: int = 0, end: int | None = None) -> list[Row]:
    end = 10**9 if end is None else end
    return connect().execute(
        "SELECT * FROM turns WHERE meeting_id=? AND idx BETWEEN ? AND ? ORDER BY idx",
        (meeting_id, start, end),
    ).fetchall()


def roles_for(meeting_id: str) -> dict[str, str]:
    rows = connect().execute("SELECT speaker, role FROM speaker_roles WHERE meeting_id=?", (meeting_id,))
    return {r["speaker"]: r["role"] for r in rows if r["role"]}


def all_teams() -> list[str]:
    return [r[0] for r in connect().execute("SELECT DISTINCT team FROM meetings ORDER BY team")]


def upsert_role(meeting_id: str, speaker: str, role: str, source: str, evidence: str = "") -> None:
    connect().execute(
        "INSERT INTO speaker_roles VALUES (?,?,?,?,?) ON CONFLICT (meeting_id, speaker) DO UPDATE "
        "SET role=EXCLUDED.role, source=EXCLUDED.source, evidence=EXCLUDED.evidence",
        (meeting_id, speaker, role, source, evidence))
