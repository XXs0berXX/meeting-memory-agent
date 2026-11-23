"""Central configuration. Everything is overridable with environment variables so the
same code runs against the Claude CLI today and a local 9B model tomorrow."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines, # comments). Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")
DATA_DIR = Path(os.environ.get("MM_DATA_DIR", ROOT / "Catalist_AI_Intern_Build_Test_Data"))
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
QUESTIONS_PATH = DATA_DIR / "questions.json"
STORE_DIR = Path(os.environ.get("MM_STORE_DIR", ROOT / "store"))
DB_PATH = STORE_DIR / "memory.db"   # legacy SQLite store, not shipped (only read by scripts/migrate_sqlite_to_pg.py)

# ---- Postgres + pgvector (docker compose up -d) -------------------------------------
PG_DSN = os.environ.get(
    "MM_PG_DSN",
    f"postgresql://{os.environ.get('PG_USER', 'mm')}:{os.environ.get('PG_PASSWORD', 'mm')}"
    f"@{os.environ.get('PG_HOST', 'localhost')}:{os.environ.get('PG_PORT', '5433')}/{os.environ.get('PG_DB', 'meeting_memory')}")
EMBED_DIM = 384
LLM_LOG_PATH = STORE_DIR / "llm_calls.jsonl"
TRACE_DIR = STORE_DIR / "traces"

# ---- LLM backend -------------------------------------------------------------------
# "claude_cli"  -> shells out to `claude -p` (stateless JSON endpoint, no tools)
# "openai"      -> any OpenAI-compatible /v1/chat/completions server
#                  (Ollama, llama.cpp server, LM Studio, vLLM) for the local 9B model
LLM_BACKEND = os.environ.get("MM_LLM_BACKEND", "claude_cli")

CLAUDE_BIN = os.environ.get("MM_CLAUDE_BIN", "claude")
# model used by the question-answering / brief agent
CLAUDE_AGENT_MODEL = os.environ.get("MM_CLAUDE_AGENT_MODEL", "sonnet")
# model used for bulk per-segment memory extraction at ingest time
CLAUDE_EXTRACT_MODEL = os.environ.get("MM_CLAUDE_EXTRACT_MODEL", "haiku")
CLAUDE_AGENT_EFFORT = os.environ.get("MM_CLAUDE_AGENT_EFFORT", "medium")
CLAUDE_EXTRACT_EFFORT = os.environ.get("MM_CLAUDE_EXTRACT_EFFORT", "low")
CLAUDE_TIMEOUT_S = int(os.environ.get("MM_CLAUDE_TIMEOUT_S", "300"))

OPENAI_BASE_URL = os.environ.get("MM_OPENAI_BASE_URL", "http://localhost:11434/v1")
OPENAI_API_KEY = os.environ.get("MM_OPENAI_API_KEY", "not-needed")
OPENAI_AGENT_MODEL = os.environ.get("MM_OPENAI_AGENT_MODEL", os.environ.get("MM_OPENAI_MODEL", "qwen3.5:9b"))
OPENAI_EXTRACT_MODEL = os.environ.get("MM_OPENAI_EXTRACT_MODEL", OPENAI_AGENT_MODEL)
OPENAI_JSON_MODE = os.environ.get("MM_OPENAI_JSON_MODE", "1") == "1"
# agent protocol for the OpenAI-compatible backend:
#   "native" -> real tool calling (tools=[...] in the request; Ollama supports this for tool-capable models
#               such as qwen3 / llama3.1 / mistral-nemo); falls back to the JSON protocol for any step where the
#               model replies with text instead of a tool call
#   "json"   -> the plain JSON-in-text protocol (works with any model, and is what the Claude CLI uses)
AGENT_TOOL_MODE = os.environ.get("MM_AGENT_TOOL_MODE", "native")
OPENAI_TIMEOUT_S = int(os.environ.get("MM_OPENAI_TIMEOUT_S", "600"))

LLM_CONCURRENCY = int(os.environ.get("MM_LLM_CONCURRENCY", "6"))

# ---- Retrieval / chunking ----------------------------------------------------------
EMBED_MODEL = os.environ.get("MM_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
USE_EMBEDDINGS = os.environ.get("MM_USE_EMBEDDINGS", "1") == "1"
CHUNK_TARGET_WORDS = int(os.environ.get("MM_CHUNK_WORDS", "180"))      # retrieval unit: a window of consecutive turns
SEGMENT_TARGET_WORDS = int(os.environ.get("MM_SEGMENT_WORDS", "1800"))  # extraction unit: what one LLM call reads at ingest

# ---- Agent budgets (sized so a 9B model with an 8-16k context still fits) -----------
AGENT_MAX_STEPS = int(os.environ.get("MM_AGENT_MAX_STEPS", "6"))
# split multi-part questions into sub-questions, research each, then compose (1 extra LLM call per question)
AGENT_DECOMPOSE = os.environ.get("MM_AGENT_DECOMPOSE", "1") == "1"
AGENT_MAX_EVIDENCE_WORDS = int(os.environ.get("MM_AGENT_MAX_EVIDENCE_WORDS", "5000"))
LEDGER_CONTEXT_ITEMS = int(os.environ.get("MM_LEDGER_CONTEXT_ITEMS", "45"))

# ---- Pricing (USD per 1M tokens) used for the cost projection -----------------------
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
