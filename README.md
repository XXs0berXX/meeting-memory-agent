# Meeting Memory Agent (Catalist build test)

Turns 138 AMI meeting transcripts into a **searchable, time-aware memory** and runs an **agent** over it that
answers multi-meeting questions with verbatim citations and writes a pre-meeting brief.

- `answers.json`: the submission (25 answers + the TS3012 brief), produced by `python -m meeting_memory build`
- `WRITEUP.md`: the one-page write-up (architecture, model-vs-code line, tools and where they broke, scale)
- `eval/`: 11 hand-written questions with transcript-checked gold answers, the scorer, and results of every run (v1-v4, see WRITEUP.md)
- `reports/cost.md`: measured cost/latency and the 1,000-meeting projection

## How it works (30 seconds)

```
transcripts/*.txt
   │  ingest (code only)       parse → speaker turns → overlapping turn windows → Postgres tsvector (GIN) + pgvector (HNSW)
   ▼                           + speaker-role detection (regex + propagation across a team's meetings)
raw layer  ───────────────────────────────────────────────────────────────────────────────────────────────┐
   │  extract (LLM, per ~1.8k-word segment, meetings in time order, teams in parallel)                     │
   │     reads the segment + the live slice of the team's memory → typed items                             │
   │     (decision / proposal / rejection / action / disagreement / fact), each with a verbatim quote       │
   │     (code-verified) and links to earlier items (supersedes, reverses, answers, ...)                    │
   │  reconcile (LLM, 1 call per meeting): close the books, add implicit changes the segment pass missed   │
   ▼                                                                                                       │
memory ledger (event-sourced: append-only items + links; "current" = replay links up to a cut-off)        │
   │                                                                                                       │
   ▼                                                                                                       ▼
agent:       model splits a multi-part question into standalone sub-questions → each is researched separately:
             code pre-retrieves (ledger hits + hybrid search + best passages of every meeting + closing turns,
             team-scoped) → model picks search / read / timeline / answer (native tool calls on a local model,
             JSON actions on the Claude CLI; ≤ 6 steps, bounded context) → code grounds every quote word-for-word
             (snaps near-misses to the exact transcript span, drops anything it can't find) → 1 repair round
             → model composes the final answer, choosing among the already-verified citations
brief:       ledger replayed AS OF meeting c (meeting d is never seen) → bucketed by code → model writes →
             code renders each citation from verified quotes, drops uncited points
```

## Quick start

Requires Python 3.10+, Docker (for Postgres + pgvector) and, for the demo backend, the `claude` CLI logged in
(`claude -p` must work).

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
cp .env.example .env              # all settings; defaults work as-is
docker compose up -d              # Postgres 17 + pgvector on localhost:5433

# Option A, use the memory already built (no LLM cost): restore the shipped dump
docker cp store/meeting_memory.dump meeting_memory_db:/tmp/mm.dump
docker exec meeting_memory_db pg_restore -U mm -d meeting_memory --clean --if-exists /tmp/mm.dump
# (to create the dump: docker exec meeting_memory_db pg_dump -U mm -Fc -f /tmp/mm.dump meeting_memory
#                      docker cp meeting_memory_db:/tmp/mm.dump store/meeting_memory.dump)
# In Git Bash on Windows, prefix the docker commands with MSYS_NO_PATHCONV=1 so /tmp isn't rewritten.

# Option B, build everything from the transcripts
python -m meeting_memory ingest   # code only: ~2 s parse/index + embeddings (23 min on a laptop CPU, ~1 min on a GPU)
python -m meeting_memory extract  # LLM memory extraction + reconciliation (~1 h at 8 parallel with Haiku via the CLI, ~$2)

python -m meeting_memory build    # answers the 25 questions + writes the brief -> answers.json
python -m meeting_memory validate # checks every citation in answers.json word for word
python eval/run_eval.py           # scores the agent on the 11 hand-written questions -> eval/results.md
python -m meeting_memory stats    # cost / latency report + 1,000-meeting projection
```

Ask anything live (the agent works on any team; `--as-of` time-slices memory):

```bash
python -m meeting_memory ask "Team ES2016's first full costing came out over budget. What did they cut?"
python -m meeting_memory ask "What had TS3012 decided about the case material?" --as-of TS3012c
python -m meeting_memory ledger --team TS3012 --topic "lcd screen"      # how a topic evolved, with statuses
python -m meeting_memory brief --team TS3012                            # brief before the final meeting
python -m meeting_memory brief --team ES2008 --before ES2008c           # brief before any meeting
```

Traces of every agent run (each step, query, observation, grounding result) are written to `store/traces/`.

## Switching to a local 9B model (native tool calling)

Edit `.env` (see `.env.example`):

```bash
MM_LLM_BACKEND=openai
MM_OPENAI_BASE_URL=http://localhost:11434/v1
MM_OPENAI_MODEL=qwen3:8b            # your tag: run `ollama list`; tool-capable models: https://ollama.com/search?c=tools
MM_AGENT_TOOL_MODE=native           # real tool calling via the OpenAI `tools` API (json = plain-text protocol)
```

and start Ollama with a bigger context window (its default is small and it truncates silently):
`set OLLAMA_CONTEXT_LENGTH=16384` (bash: `export ...`), then `ollama serve`.

With `native`, the agent sends `search / read / timeline / answer` as real tool definitions and executes the
model's `tool_calls`. If a step comes back as plain text instead of a tool call, that text is parsed with the
JSON protocol, so models with patchy tool support still work. The Claude CLI always uses the JSON protocol (it
can't register custom function tools without an MCP server). Everything else (retrieval, grounding, the ledger)
is identical across backends.

Small-model profile: `MM_SEGMENT_WORDS=1200`, `MM_AGENT_MAX_EVIDENCE_WORDS=3500`, `MM_LEDGER_CONTEXT_ITEMS=30`.
The memory built with Claude can be reused as-is: switch only the agent to the local model. A 9B model on a
4 GB GPU (GTX 1050 Ti) runs at roughly 5-15 tokens/s. Fine for live questions, slow for re-extracting a corpus.

## Layout

```
meeting_memory/
  store.py      Postgres + pgvector schema, thin DB layer  parse.py    transcript parsing (code)
  index.py      raw layer + role detection (code)
  embed.py      local embeddings (code, ONNX)             retrieve.py hybrid full-text + pgvector + RRF, metadata filters (code)
  quotes.py     citation verification + snapping (code)   ledger.py   event-sourced memory, status replay, topic clusters (code)
  extract.py    per-segment memory extraction (LLM)       reconcile.py end-of-meeting reconciliation (LLM)
  agent.py      the QA agent loop (LLM + code)            brief.py    time-sliced brief (LLM + code)
  llm.py        Claude CLI / OpenAI-compatible backends   report.py   cost & latency report
  cli.py        commands
eval/           my_questions.json, run_eval.py, results.md, grep_turns.py (helper used to write gold answers)
store/          meeting_memory.dump (Postgres dump of the built memory), llm_calls.jsonl (every LLM call: tokens,
                cost, latency), traces/ (every agent run, step by step)
scripts/        migrate_sqlite_to_pg.py (one-off move of the earlier SQLite store into Postgres)
docker-compose.yml, .env.example
```
