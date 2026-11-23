"""LLM backends behind one tiny interface: complete(system, prompt) -> text (+ usage).

Two agent protocols. With the OpenAI-compatible backend (a local model behind Ollama / llama.cpp /
LM Studio / vLLM) and MM_AGENT_TOOL_MODE=native, the agent uses native tool calling via chat(tools=...).
The Claude CLI can't register custom tools without an MCP server, so there (and with
MM_AGENT_TOOL_MODE=json) the agent speaks a small JSON action protocol in plain text. A native-mode
reply that arrives as text instead of a tool call is parsed with the JSON protocol as a fallback.
Every call is logged (tokens, cost, latency) to store/llm_calls.jsonl for the cost report.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass

from . import config

_log_lock = threading.Lock()


@dataclass
class LLMResult:
    text: str
    model: str
    in_tokens: int = 0
    out_tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0


class LLMError(RuntimeError):
    pass


def _log(purpose: str, backend: str, res: LLMResult, ok: bool = True):
    config.STORE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "purpose": purpose, "backend": backend, "model": res.model,
           "in_tokens": res.in_tokens, "out_tokens": res.out_tokens, "cost_usd": res.cost_usd,
           "seconds": round(res.seconds, 2), "ok": ok, "out_chars": len(res.text)}
    with _log_lock, open(config.LLM_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


class ClaudeCLI:
    """`claude -p` as a stateless completion endpoint: no tools, custom system prompt, no
    session persistence, run from an empty temp dir so no project context leaks in."""

    name = "claude_cli"

    def __init__(self, model: str, effort: str | None = None):
        self.model = model
        self.effort = effort
        self.bin = shutil.which(config.CLAUDE_BIN) or config.CLAUDE_BIN
        self.cwd = tempfile.mkdtemp(prefix="mm_claude_")

    def complete(self, system: str, prompt: str, purpose: str = "", max_tokens: int = 4000) -> LLMResult:
        cmd = [self.bin, "-p", "--model", self.model, "--tools", "", "--system-prompt", system,
               "--output-format", "json", "--no-session-persistence", "--strict-mcp-config"]
        if self.effort:
            cmd += ["--effort", self.effort]
        last_err = None
        for attempt in range(3):
            t0 = time.time()
            try:
                p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=config.CLAUDE_TIMEOUT_S, cwd=self.cwd)
                if p.returncode != 0:
                    raise LLMError(f"claude exit {p.returncode}: {(p.stderr or p.stdout)[:400]}")
                data = json.loads(p.stdout)
                if data.get("is_error"):
                    raise LLMError(f"claude error: {str(data.get('result'))[:400]}")
                u = data.get("usage") or {}
                in_tok = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + \
                         (u.get("cache_read_input_tokens") or 0)
                mu = data.get("modelUsage") or {}
                real_model = next(iter(mu), self.model)
                res = LLMResult(data.get("result") or "", real_model, in_tok, u.get("output_tokens") or 0,
                                data.get("total_cost_usd") or 0.0, time.time() - t0)
                _log(purpose, self.name, res)
                return res
            except (subprocess.TimeoutExpired, json.JSONDecodeError, LLMError) as e:
                last_err = e
                time.sleep(3 * (attempt + 1))
        _log(purpose, self.name, LLMResult("", self.model), ok=False)
        raise LLMError(f"claude CLI failed after retries: {last_err}")


class OpenAICompat:
    """Any OpenAI-compatible chat endpoint. Default points at Ollama on localhost."""

    name = "openai"

    def __init__(self, model: str):
        self.model = model
        self.url = config.OPENAI_BASE_URL.rstrip("/") + "/chat/completions"

    def complete(self, system: str, prompt: str, purpose: str = "", max_tokens: int = 4000) -> LLMResult:
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if config.OPENAI_JSON_MODE:
            body["response_format"] = {"type": "json_object"}
        last_err = None
        for attempt in range(3):
            t0 = time.time()
            try:
                req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST", headers={
                    "Content-Type": "application/json", "Authorization": f"Bearer {config.OPENAI_API_KEY}"})
                with urllib.request.urlopen(req, timeout=config.OPENAI_TIMEOUT_S) as r:
                    data = json.loads(r.read().decode())
                text = data["choices"][0]["message"].get("content") or ""
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()  # reasoning models
                u = data.get("usage") or {}
                res = LLMResult(text, self.model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), 0.0,
                                time.time() - t0)
                _log(purpose, self.name, res)
                return res
            except Exception as e:  # network / server errors: retry
                last_err = e
                time.sleep(3 * (attempt + 1))
        _log(purpose, self.name, LLMResult("", self.model), ok=False)
        raise LLMError(f"OpenAI-compatible endpoint failed: {last_err}")

    def chat(self, messages: list[dict], tools: list[dict] | None = None, purpose: str = "",
             max_tokens: int = 2000) -> tuple[dict, LLMResult]:
        """Multi-turn chat with native tool calling (OpenAI `tools` format; supported by Ollama for
        tool-capable models). Returns the assistant message dict (may contain `tool_calls`)."""
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens, "messages": messages}
        if tools:
            body["tools"] = tools
        last_err = None
        for attempt in range(3):
            t0 = time.time()
            try:
                req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST", headers={
                    "Content-Type": "application/json", "Authorization": f"Bearer {config.OPENAI_API_KEY}"})
                with urllib.request.urlopen(req, timeout=config.OPENAI_TIMEOUT_S) as r:
                    data = json.loads(r.read().decode())
                msg = data["choices"][0]["message"]
                msg["content"] = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
                u = data.get("usage") or {}
                res = LLMResult(msg["content"], self.model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                                0.0, time.time() - t0)
                _log(purpose, self.name, res)
                return msg, res
            except Exception as e:
                last_err = e
                time.sleep(3 * (attempt + 1))
        _log(purpose, self.name, LLMResult("", self.model), ok=False)
        raise LLMError(f"OpenAI-compatible endpoint failed: {last_err}")


def supports_native_tools() -> bool:
    """Native tool calling is used only with an OpenAI-compatible server (e.g. Ollama) and
    MM_AGENT_TOOL_MODE=native. The Claude CLI cannot register custom function tools (that
    would need an MCP server), so it always uses the JSON-in-text protocol."""
    return config.LLM_BACKEND == "openai" and config.AGENT_TOOL_MODE == "native"


_cache: dict[str, object] = {}


def get_llm(role: str = "agent"):
    """role: 'agent' (question answering, briefs) or 'extract' (bulk ingest extraction)."""
    key = f"{config.LLM_BACKEND}:{role}"
    if key not in _cache:
        if config.LLM_BACKEND == "claude_cli":
            if role == "agent":
                _cache[key] = ClaudeCLI(config.CLAUDE_AGENT_MODEL, config.CLAUDE_AGENT_EFFORT)
            else:
                _cache[key] = ClaudeCLI(config.CLAUDE_EXTRACT_MODEL, config.CLAUDE_EXTRACT_EFFORT)
        elif config.LLM_BACKEND == "openai":
            model = config.OPENAI_AGENT_MODEL if role == "agent" else config.OPENAI_EXTRACT_MODEL
            _cache[key] = OpenAICompat(model)
        else:
            raise ValueError(f"unknown MM_LLM_BACKEND {config.LLM_BACKEND}")
    return _cache[key]


def parse_json(text: str) -> dict:
    """Tolerant JSON extraction: strips code fences / prose, fixes trailing commas.
    Small models often wrap JSON in chatter; we don't fail the whole step for that."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.M)
    start = t.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in model output: {text[:200]!r}")
    depth, in_str, esc, end = 0, False, False, None
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    blob = t[start:end] if end else t[start:]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        fixed = re.sub(r",\s*([}\]])", r"\1", blob)
        return json.loads(fixed)


def complete_json(role: str, system: str, prompt: str, purpose: str, max_tokens: int = 4000) -> tuple[dict, LLMResult]:
    """Call the model and parse JSON; one corrective retry if the output isn't valid JSON."""
    llm = get_llm(role)
    res = llm.complete(system, prompt, purpose=purpose, max_tokens=max_tokens)
    try:
        return parse_json(res.text), res
    except (ValueError, json.JSONDecodeError):
        res2 = llm.complete(system, prompt + "\n\nIMPORTANT: your previous reply was not valid JSON. "
                            "Reply with ONE valid JSON object only.", purpose=purpose + ":retry",
                            max_tokens=max_tokens)
        res2.in_tokens += res.in_tokens
        res2.out_tokens += res.out_tokens
        res2.cost_usd += res.cost_usd
        res2.seconds += res.seconds
        return parse_json(res2.text), res2
