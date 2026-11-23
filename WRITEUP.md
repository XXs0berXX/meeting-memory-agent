# Meeting memory agent: write-up

**The idea.** 138 meetings (676k words) don't fit in one prompt, and plain search can't tell which of two
conflicting statements is the current one. So there are two layers: a **raw layer** built by code (every turn,
searchable) and a **memory ledger** built by a model (what was decided, changed, rejected, assigned), where every
record carries a verbatim quote. Everything is stored in Postgres + pgvector (Docker).

**Flow 1: saving a meeting.**
1. *Parse, chunk, index (code).* Transcript → speaker turns → ~180-word windows, indexed for keyword search
   (`tsvector`) and meaning search (bge-small embeddings in pgvector). Roles come from self-introductions ("I'm the
   project manager"), pooled across a team's meetings.
2. *Extract (model).* Meetings are read in order. For each ~1,800-word segment the model sees the segment plus the
   relevant part of the team's memory so far, and returns records (decision / proposal / rejection / action /
   disagreement) with a quote and **links** to the earlier records they change ("supersedes", "reverses", "answers").
3. *Close the books (model).* One call per meeting reads its records and its closing turns, and adds what no single
   segment shows, e.g. speech recognition rejected in b, then designed in during c, recorded as `reverses`.

Code checks every quote and link (the quote must exist word for word, links only point back in time). Records are
never edited; status ("in force", "reversed") is computed by replaying links up to a cut-off, so "what was true
before meeting d" and "what is true now" are the same query.

**Flow 2: answering a question.** The model splits a multi-part question into sub-questions. For each, code
pre-retrieves passages (keyword + meaning search, filtered to the team and the as-of cut-off, plus the ledger's
history of the topic and the best passage from every meeting). The model then searches, reads or checks the
timeline (≤6 steps) and answers with citations. **Code grounds every citation**: exact match inside one speaker
turn; if the model paraphrased, it swaps in the exact transcript words; if nothing matches, the citation is dropped
and the model gets one retry. A last call combines the sub-answers using only the verified citations. *The brief*
replays the ledger as of TS3012c (meeting d is never seen) and lets the model write it; code renders the citations.

**Model vs code.** If a mistake can be detected mechanically, code owns it: parsing, search, filters, time cut-offs,
quote checking, link checks, status replay, roles, budgets. The model only does what needs language: turning messy
speech into records, judging that X replaces Y, choosing what to search next, writing. Every model output passes a
code check before it is trusted.

**Tools and where they broke.** *Claude CLI* as the model endpoint: ~3 s per call, thinking tokens tripled the cost
until `--effort low`, and its usage limit stopped one eval run. *CPU embeddings*: 23 min for the corpus, the slowest
step (~1 min on a GPU). *Extraction*: topic names fragmented ("speech" vs "voice recognition"), fixed by clustering
embeddings; implicit reversals were missed, fixed by the close-the-books pass. *pgvector*: team-filtered search
returned almost nothing until iterative scan was enabled. *Local 9B*: same code via Ollama, with native tool calls
and a JSON fallback; every call is ≤10k tokens so it fits a small model.

**Results.** 11 own questions on teams not in the official 25, with gold answers checked against the transcripts
(`eval/`). Scores: 8.5 → 9.5 → 9.0 → 8.0 out of 11 across versions; the judge varies ±1 between identical runs, so
the clearer signal is key facts found (91% → 97% with question splitting). All 31-32 citations valid in every run.
`answers.json`: 25/25 answered, **73/73 answer and 47/47 brief citations verified**. *Where it fails:*
**E10**: a forceful line mid-argument ("only kinetic") was stored as a decision; the memory was wrong, not the agent.
Fixed by having the close-the-books pass read the meeting's ending. **E09**: the agent can stop at the first
plausible reason (cost) instead of the stated one (90% vs 60% user preference). **E11**: misses a feature the
transcript never calls "voice", a vocabulary gap search didn't bridge.

**At scale.** Each meeting is read by the model once; a new meeting costs ~1 minute and ~1.5 cents (Haiku), and only a
changed file is re-processed. **1,000 meetings: ~$12 and ~2-3 h** at 8 parallel with Haiku (extraction $9 + closing
the books $3; ~$180 with Sonnet; ~100 h on a local 9B on a 4 GB GPU, so extract with a cheap API model and run the
agent locally). **10,000 meetings:** cost grows linearly (~$120), teams run in parallel, and every search is filtered to one team first, so answering
cost depends on team size, not corpus size. Production would add a job queue, linking the same person across
teams, and an eval set that grows with every reported miss. Details: `reports/cost.md`.
