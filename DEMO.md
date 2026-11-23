# Walkthrough: live demo or 3-5 minute recording

The PDF accepts either a recording **or** coming ready to demo live. Everything below runs from this repo as-is.
A screen-and-voice recording has to be made by you (an agent can't capture your screen or voice). To record it,
run the commands below in order with OBS or Windows Game Bar (Win+G) running, and read the narration. For the live
version, the same commands answer the reviewers' new questions: `python -m meeting_memory ask "<their question>"`
(about 15-30 s each, with every citation verified on screen).

Pre-flight (30 s): `python -m meeting_memory validate` should print "73 citations verified, 0 failed | brief: 47/47".

## Script

**0:00 The problem and the shape of the answer (30s)**
"138 meetings, 676k words. Pasting it all into one prompt is the wrong answer. So there are two layers: a raw
layer built by plain code (turns, Postgres full-text and pgvector search), and a memory ledger built by a model that reads each meeting
in order and records decisions, proposals, rejections, actions and disagreements. Each record carries a verbatim
quote and links to whatever it changes."

**0:30 Memory over time: show it, don't tell it (60s)**
```bash
python -m meeting_memory ledger --team TS3012 --topic "speech voice recognition"
```
Point at: meeting b REJECTION "(reversed -> by TS3012c-R01)", and the end-of-meeting decision in c,
"the team is including speech recognition". Nobody said "we reverse that"; they started arguing where the
microphone goes. The reconciliation pass caught it. Status is computed by replaying links, never stored:
```bash
python -m meeting_memory ledger --team TS3012 --topic "speech voice recognition" --as-of TS3012b
```
The same rejection shows "in force" here, because as of meeting b it was.

**1:30 The agent, live (60s)**
```bash
python -m meeting_memory ask "Team TS3012 agreed on an LCD and speech recognition. What happened to them in the end, and why?"
```
Narrate the steps it prints. First it splits the question into sub-questions ("what happened to the LCD",
"what happened to speech recognition", "why"), each researched separately. Code pre-retrieves (ledger hits plus the
best passages from every meeting). The model drafts an answer, code pushes back once ("check your weakest part"),
the model searches, then answers; a last call composes one answer from the verified citations. Every quote
says `exact` or `snapped`: code verified it word for word against one speaker turn.
Open `store/traces/adhoc.json` to show the full trace.

**2:30 The brief (40s)**
```bash
python -m meeting_memory brief --team TS3012
```
"Built only from a, b and c. Meeting d is in the store, but the brief is cut off before it. Decided / changed /
open actions with owners / disagreements. Each citation is rendered by code from a verified quote, and points
without one are dropped."

**3:10 Model vs code (30s)**
Show the table in WRITEUP.md. The rule: if a mistake can be detected mechanically, code owns it.

**3:40 Proof and honesty (40s)**
Show `eval/results.md`: 11 hand-written questions on teams that are not in the official 25, baseline vs after
fixes, and the three that still fail and why. `python -m meeting_memory validate`: every citation verified.
`reports/cost.md`: 1,000 meetings cost about $12 with Haiku (about 1.5 cents per meeting), and the bottleneck is CPU embeddings.

**4:20 Scale and the local 9B (30s)**
Show `.env.example`: switching `MM_LLM_BACKEND=openai` points the same code at Ollama, and the agent then uses
native tool calls. Bounded context per call (segment plus ledger slice, about 8-10k tokens), so it fits a 9B model.
Storage is already Postgres + pgvector in Docker. At 10k meetings: team-filtered retrieval, incremental per-meeting
ingest, ledger slices chosen by relevance. Next steps would be a job queue and entity resolution across teams.
