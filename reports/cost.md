# Cost and latency (measured on this run, projected to 1000 meetings)

## Measured

- Corpus: 138 meetings, 4904 words/meeting on average
- Raw layer (code only): parse + chunk + Postgres full-text index for all meetings in 1.32s; 5615 chunks embedded in 1384.01s (laptop CPU (onnxruntime), 4.1 chunks/s)
- Memory extraction (haiku): 138 meetings, 2.9 LLM calls/meeting, 23474 input + 13030 output tokens/meeting, $0.0112/meeting, 60s/meeting sequential, 35 memory items/meeting; total $1.55
- LLM purpose `agent_repair`: 26 calls, 382 in / 30 out tokens per call, 0.4s per call, $0.047 total
- LLM purpose `agent_step`: 496 calls, 5776 in / 302 out tokens per call, 16.4s per call, $12.741 total
- LLM purpose `brief`: 2 calls, 17709 in / 2588 out tokens per call, 21.6s per call, $0.193 total
- LLM purpose `compose`: 23 calls, 1507 in / 190 out tokens per call, 9.9s per call, $0.182 total
- LLM purpose `decompose`: 23 calls, 802 in / 90 out tokens per call, 9.8s per call, $0.094 total
- LLM purpose `extract`: 399 calls, 8207 in / 4612 out tokens per call, 20.7s per call, $1.575 total
- LLM purpose `judge`: 55 calls, 869 in / 156 out tokens per call, 4.1s per call, $0.277 total
- LLM purpose `reconcile`: 233 calls, 7112 in / 4541 out tokens per call, 22.5s per call, $0.860 total

## Projection: ingesting 1000 meetings

| step | model / hardware | cost | wall time |
|---|---|---|---|
| parse, chunk, full-text index | code | $0 | ~10s |
| embeddings (40,688 chunks) | bge-small, this laptop CPU | $0 | ~2.8h |
| embeddings | any GPU (~500 chunks/s) or an embeddings API | $0 to ~$1 | ~1 min |
| memory extraction | claude-haiku-5-5 (list price) | $8.86 | ~2.1h at 8 parallel (measured) |
| memory extraction | claude-sonnet-5-5 (list price) | $177.25 | slower per call than Haiku |
| memory extraction | claude-opus-5-5 (list price) | $354.50 | slower per call than Haiku |
| memory extraction | local 9B (Ollama, own GPU, no thinking) | $0 marginal | ~5,863,568 output tokens / your GPU's tokens/s: ~109h at 15 tok/s (4GB card, partial offload), ~27h at 60 tok/s (24GB card); prompt processing extra |

Extraction volume: 23.5M input + 13.0M output tokens. Teams are independent, so extraction parallelises across teams; within a team meetings run in time order. A daily job only processes new meetings (file hash check), so steady-state cost is per new meeting, not per corpus.
