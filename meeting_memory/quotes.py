"""Citation grounding. Pure code, and deliberately so.

Models (especially small ones) are unreliable at copying text verbatim: they fix grammar, drop
"um"s, merge two turns. The grader checks quotes word for word, so instead of hoping, we:
  1. verify  - exact match (case/space-insensitive) inside a single speaker turn, >= 6 words;
  2. snap    - if not exact, align the model's approximate quote against the turns near the
               turn it pointed at and replace it with the exact transcript span;
  3. reject  - if nothing aligns well, the citation is dropped (never invented).
The model chooses WHAT to cite; code guarantees the citation is real.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .store import connect

MIN_WORDS = 6
MAX_WORDS = 40
_WS = re.compile(r"\s+")
_TOK = re.compile(r"[a-z0-9']+")


def norm(s: str) -> str:
    """Normalisation the grader uses: case and whitespace ignored, punctuation kept."""
    return _WS.sub(" ", s.strip().lower())


def _toks(s: str) -> list[str]:
    return _TOK.findall(s.lower())


def verify(meeting_id: str, quote: str) -> int | None:
    """Return the turn idx containing the quote exactly (normalised), else None."""
    q = norm(quote)
    if len(q.split()) < MIN_WORDS:
        return None
    for r in connect().execute("SELECT idx, text FROM turns WHERE meeting_id=? ORDER BY idx", (meeting_id,)):
        if q in norm(r["text"]):
            return r["idx"]
    return None


def _best_window(turn_text: str, quote: str) -> tuple[float, str]:
    """Best-matching contiguous word window of the turn for an approximate quote."""
    words = turn_text.split()
    wt = [" ".join(_toks(w)) for w in words]
    qt = _toks(quote)
    if not words or not qt:
        return 0.0, ""
    n = len(qt)
    best = (0.0, 0, 0)
    for L in {max(MIN_WORDS, n - 3), n - 1, n, n + 1, n + 3}:
        if L <= 0:
            continue
        L = min(L, len(words))
        for i in range(0, len(words) - L + 1):
            cand = [t for t in wt[i : i + L] if t]
            sm = SequenceMatcher(None, cand, qt, autojunk=False)
            r = sm.ratio()
            if r > best[0]:
                best = (r, i, i + L)
    r, i, j = best
    # make sure the snapped span satisfies the minimum length
    while j - i < MIN_WORDS and (i > 0 or j < len(words)):
        if j < len(words):
            j += 1
        else:
            i -= 1
    return r, " ".join(words[i:j])


def snap(meeting_id: str, quote: str, turn_hint: int | None = None, threshold: float = 0.72) -> dict | None:
    """Ground an approximate quote. Returns {meeting_id, quote, turn, method} or None."""
    quote = (quote or "").strip().strip('"').strip("“”").strip()
    if not quote:
        return None
    idx = verify(meeting_id, quote)
    if idx is not None and len(quote.split()) <= MAX_WORDS:
        return {"meeting_id": meeting_id, "quote": quote, "turn": idx, "method": "exact"}

    conn = connect()
    rows = []
    if turn_hint is not None:
        rows = conn.execute(
            "SELECT idx, text FROM turns WHERE meeting_id=? AND idx BETWEEN ? AND ? ORDER BY idx",
            (meeting_id, turn_hint - 3, turn_hint + 3),
        ).fetchall()
    cands = _score_rows(rows, quote)
    if not cands or cands[0][0] < threshold:
        # fall back to the whole meeting, pre-filtered by token overlap to stay fast
        qset = set(_toks(quote))
        allrows = conn.execute("SELECT idx, text FROM turns WHERE meeting_id=?", (meeting_id,)).fetchall()
        pre = sorted(allrows, key=lambda r: -len(qset & set(_toks(r["text"]))))[:12]
        cands = sorted(cands + _score_rows(pre, quote), key=lambda c: -c[0])
    if not cands or cands[0][0] < threshold:
        return None
    r, idx, span = cands[0]
    span_words = span.split()
    if len(span_words) > MAX_WORDS:
        span = " ".join(span_words[:MAX_WORDS])
    if verify(meeting_id, span) is None:
        return None
    return {"meeting_id": meeting_id, "quote": span, "turn": idx, "method": f"snapped({r:.2f})"}


def _score_rows(rows, quote):
    out = []
    for row in rows:
        r, span = _best_window(row["text"], quote)
        if span:
            out.append((r, row["idx"], span))
    out.sort(key=lambda c: -c[0])
    return out


def ground_citations(cits: list[dict], max_n: int = 3) -> tuple[list[dict], list[str]]:
    """Snap a list of model citations; return (valid, error messages)."""
    good, errors, seen = [], [], set()
    for c in cits or []:
        mid = str(c.get("meeting_id") or c.get("meeting") or "").strip()
        q = str(c.get("quote") or "")
        hint = c.get("turn")
        try:
            hint = int(str(hint).lstrip("#t")) if hint is not None else None
        except ValueError:
            hint = None
        if not mid or not q:
            errors.append(f"citation missing meeting_id or quote: {c}")
            continue
        g = snap(mid, q, hint)
        if g is None:
            errors.append(f"quote not found in {mid}: {q[:80]!r}")
            continue
        key = (g["meeting_id"], norm(g["quote"]))
        if key in seen:
            continue
        seen.add(key)
        good.append(g)
    return good[:max_n], errors
