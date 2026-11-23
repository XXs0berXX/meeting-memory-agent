"""Stage 1 of ingest: the raw layer. 100% plain code, no LLM.

parse -> speaker turns -> overlapping turn-window chunks -> Postgres full-text (tsvector) + pgvector -> role heuristic.
Idempotent and incremental: a meeting is only re-indexed if its file hash changed, so a daily
job over a growing corpus only pays for the new meetings.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from . import config, embed
from .parse import Meeting, iter_transcripts, parse_transcript
from .store import connect, upsert_role

ROLE_PATTERNS = {
    "Project Manager": r"project manager",
    "Industrial Designer": r"industrial design(er)?",
    "User Interface Designer": r"(user )?interface design(er)?",
    "Marketing Expert": r"marketing( expert)?",
}
SELF_ID = re.compile(
    # "I'm the Industrial Designer", "I'll be your Project Manager", "my name is X, I'm the ..."
    # (not "as the Marketing Manager says", which usually refers to someone else)
    r"(i'?m|i am|i'll be|i will be|i'm going to be|myself as|me as|my role is)"
    r"\s+(?:\w+\s+){0,2}?(?:the |your |our |a )?(project manager|industrial designer|user interface designer|"
    r"interface designer|marketing expert|marketing manager|marketing)",
    re.I,
)


def chunk_turns(m: Meeting, target: int = config.CHUNK_TARGET_WORDS):
    """Windows of consecutive turns of ~target words with ~50% overlap. Turn boundaries are
    kept so every retrieved chunk can be cited at turn granularity."""
    turns, out, i = m.turns, [], 0
    while i < len(turns):
        j, words = i, 0
        while j < len(turns) and (words < target or j == i):
            words += turns[j].n_words
            j += 1
        out.append((i, j - 1))
        if j >= len(turns):
            break
        # next window starts around the middle of this one
        half, acc, k = words / 2, 0, i
        while k < j - 1 and acc < half:
            acc += turns[k].n_words
            k += 1
        i = max(k, i + 1)
    return out


def chunk_text(m: Meeting, a: int, b: int) -> str:
    return "\n".join(f"{t.speaker}: {t.text}" for t in m.turns[a : b + 1])


def guess_roles(m: Meeting) -> dict[str, tuple[str, str]]:
    """Self-identification regex ("I'm the Industrial Designer"). Cheap, high precision,
    low recall; the LLM extraction pass and cross-meeting propagation fill the gaps."""
    found: dict[str, tuple[str, str]] = {}
    for t in m.turns:
        if t.speaker in found:
            continue
        mm = SELF_ID.search(t.text)
        if not mm:
            continue
        phrase = mm.group(2).lower().replace("marketing manager", "marketing")
        for role, pat in ROLE_PATTERNS.items():
            if re.fullmatch(pat, phrase):
                if role not in {r for r, _ in found.values()}:
                    found[t.speaker] = (role, t.text[:200])
                break
    return found


def index_all(force: bool = False, verbose: bool = True) -> dict:
    conn = connect()
    stats = {"indexed": 0, "skipped": 0, "chunks": 0, "turns": 0, "words": 0, "seconds_parse_index": 0.0,
             "seconds_embed": 0.0}
    use_vec = embed.available()
    new_chunks: list[tuple[int, str]] = []
    t0 = time.time()
    for path in iter_transcripts(config.TRANSCRIPTS_DIR):
        m = parse_transcript(path)
        row = conn.execute("SELECT sha1 FROM meetings WHERE meeting_id=?", (m.meeting_id,)).fetchone()
        if row and row["sha1"] == m.sha1 and not force:
            stats["skipped"] += 1
            continue
        conn.execute("DELETE FROM chunks WHERE meeting_id=?", (m.meeting_id,))  # tsvector + vector go with the row
        conn.execute("DELETE FROM turns WHERE meeting_id=?", (m.meeting_id,))
        conn.executemany(
            "INSERT INTO turns VALUES (?,?,?,?,?,?)",
            [(m.meeting_id, t.idx, t.t_sec, t.t_str, t.speaker, t.text) for t in m.turns],
        )
        for a, b in chunk_turns(m):
            text = chunk_text(m, a, b)
            cid = conn.execute(
                "INSERT INTO chunks(meeting_id, team, seq, turn_start, turn_end, text) VALUES (?,?,?,?,?,?) RETURNING chunk_id",
                (m.meeting_id, m.team, m.seq, a, b, text),
            ).fetchone()[0]
            new_chunks.append((cid, text))
            stats["chunks"] += 1
        for spk, (role, ev) in guess_roles(m).items():
            upsert_role(m.meeting_id, spk, role, "regex", ev)
        conn.execute(
            "INSERT INTO meetings VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (meeting_id) DO UPDATE SET "
            "team=EXCLUDED.team, letter=EXCLUDED.letter, seq=EXCLUDED.seq, n_turns=EXCLUDED.n_turns, "
            "n_words=EXCLUDED.n_words, path=EXCLUDED.path, sha1=EXCLUDED.sha1, indexed_at=EXCLUDED.indexed_at",
            (m.meeting_id, m.team, m.letter, m.seq, len(m.turns), m.n_words, m.path, m.sha1,
             datetime.now(timezone.utc).isoformat()),
        )
        stats["indexed"] += 1
        stats["turns"] += len(m.turns)
        stats["words"] += m.n_words
    conn.commit()
    stats["seconds_parse_index"] = round(time.time() - t0, 2)

    if use_vec and new_chunks:
        t1 = time.time()
        B = 256
        for s in range(0, len(new_chunks), B):
            batch = new_chunks[s : s + B]
            vecs = embed.embed_passages([t for _, t in batch])
            conn.executemany("UPDATE chunks SET embedding=? WHERE chunk_id=?",
                             [(v, cid) for (cid, _), v in zip(batch, vecs)])
            conn.commit()
            if verbose:
                print(f"  embedded {min(s + B, len(new_chunks))}/{len(new_chunks)} chunks", flush=True)
        stats["seconds_embed"] = round(time.time() - t1, 2)
    propagate_roles()
    if stats["indexed"]:
        import json
        (config.STORE_DIR / "index_stats.json").write_text(json.dumps(stats, indent=2))
    return stats


def propagate_roles():
    """Speaker letters are stable within a team (checked: 17 teams self-identify in several meetings,
    0 true conflicts). So roles are decided once per TEAM by a vote over all evidence - regex
    self-introductions weigh 3, LLM-stated roles 1 - with each role used at most once, then applied
    to every meeting of the team. A meeting's own regex evidence always wins for that meeting."""
    conn = connect()
    teams = [r[0] for r in conn.execute("SELECT DISTINCT team FROM meetings")]
    for team in teams:
        meetings = [r[0] for r in conn.execute("SELECT meeting_id FROM meetings WHERE team=? ORDER BY seq", (team,))]
        votes: dict[tuple[str, str], float] = {}
        for mid in meetings:
            for r in conn.execute("SELECT speaker, role, source FROM speaker_roles WHERE meeting_id=? AND source IN ('regex','llm')", (mid,)):
                key = (r["speaker"], r["role"])
                votes[key] = votes.get(key, 0) + (3 if r["source"] == "regex" else 1)
        team_map: dict[str, str] = {}
        for (spk, role), v in sorted(votes.items(), key=lambda kv: -kv[1]):
            if spk not in team_map and role not in team_map.values():
                team_map[spk] = role
        for mid in meetings:
            regex = {r["speaker"]: r["role"] for r in conn.execute(
                "SELECT speaker, role FROM speaker_roles WHERE meeting_id=? AND source='regex'", (mid,))}
            speakers = [r[0] for r in conn.execute("SELECT DISTINCT speaker FROM turns WHERE meeting_id=?", (mid,))]
            for spk in speakers:
                if spk in regex:
                    continue
                role = team_map.get(spk)
                if role and role not in regex.values():
                    upsert_role(mid, spk, role, "team-vote", f"team-level vote over {team} evidence")
    conn.commit()


def rebuild_roles():
    """Recompute roles from all evidence: regex (high precision) > LLM-stated (from extraction)
    > propagated (same letter in the team's other meetings). Cheap; run after extraction."""
    from .parse import Meeting, Turn
    conn = connect()
    conn.execute("DELETE FROM speaker_roles WHERE source IN ('regex','propagated','team-vote')")
    for (mid,) in conn.execute("SELECT meeting_id FROM meetings").fetchall():
        m = Meeting(mid, "", "", 0, "", "")
        m.turns = [Turn(r["idx"], r["t_sec"], r["t_str"], r["speaker"], r["text"])
                   for r in conn.execute("SELECT * FROM turns WHERE meeting_id=? ORDER BY idx", (mid,))]
        found = guess_roles(m)
        llm = {r["speaker"]: r["role"] for r in conn.execute(
            "SELECT speaker, role FROM speaker_roles WHERE meeting_id=? AND source='llm'", (mid,))}
        for spk, role in llm.items():  # LLM claims are kept as votes; regex for the same speaker replaces them
            if spk in found:
                conn.execute("DELETE FROM speaker_roles WHERE meeting_id=? AND speaker=?", (mid, spk))
        for spk, (role, ev) in found.items():
            upsert_role(mid, spk, role, "regex", ev)
    conn.commit()
    propagate_roles()
