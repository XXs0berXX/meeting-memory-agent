"""Cost / latency report from measured runs, projected to 1,000 meetings."""
from __future__ import annotations

import json
from collections import defaultdict

from . import config
from .store import connect


def _calls():
    if not config.LLM_LOG_PATH.exists():
        return []
    return [json.loads(l) for l in config.LLM_LOG_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]


def cost_report(n_target: int = 1000, workers: int = 8) -> str:
    conn = connect()
    idx = json.loads((config.STORE_DIR / "index_stats.json").read_text()) if (config.STORE_DIR / "index_stats.json").exists() else {}
    m = conn.execute("SELECT COUNT(*) n, AVG(n_words) w FROM meetings").fetchone()
    ex = conn.execute("SELECT COUNT(*) n, AVG(in_tokens) i, AVG(out_tokens) o, AVG(cost_usd) c, AVG(seconds) s, "
                      "AVG(n_segments) seg, AVG(n_items) items, SUM(cost_usd) tc, MAX(model) model FROM extract_log").fetchone()
    by = defaultdict(lambda: {"n": 0, "in": 0, "out": 0, "cost": 0.0, "secs": 0.0})
    vis = [c["out_chars"] / 4 / max(c["out_tokens"], 1) for c in _calls() if c.get("out_chars") and c["purpose"] == "extract"]
    visible_ratio = sum(vis) / len(vis) if vis else 0.45  # share of output tokens that is the JSON answer (rest = thinking)
    for c in _calls():
        b = by[c["purpose"].split(":")[0]]
        b["n"] += 1
        b["in"] += c["in_tokens"]
        b["out"] += c["out_tokens"]
        b["cost"] += c["cost_usd"]
        b["secs"] += c["seconds"]

    L = ["# Cost and latency (measured on this run, projected to %d meetings)" % n_target, ""]
    L += ["## Measured", "",
          f"- Corpus: {m['n']} meetings, {m['w']:.0f} words/meeting on average",
          f"- Raw layer (code only): parse + chunk + Postgres full-text index for all meetings in {idx.get('seconds_parse_index', '?')}s; "
          f"{idx.get('chunks', '?')} chunks embedded in {idx.get('seconds_embed', '?')}s ({idx.get('embed_device', '')})"]
    if ex["n"]:
        L += [f"- Memory extraction ({ex['model']}): {ex['n']} meetings, {ex['seg']:.1f} LLM calls/meeting, "
              f"{ex['i']:.0f} input + {ex['o']:.0f} output tokens/meeting, ${ex['c']:.4f}/meeting, "
              f"{ex['s']:.0f}s/meeting sequential, {ex['items']:.0f} memory items/meeting; total ${ex['tc']:.2f}"]
    for k, v in sorted(by.items()):
        if v["n"]:
            L.append(f"- LLM purpose `{k}`: {v['n']} calls, {v['in'] / v['n']:.0f} in / {v['out'] / v['n']:.0f} out tokens per call, "
                     f"{v['secs'] / v['n']:.1f}s per call, ${v['cost']:.3f} total")
    if ex["n"]:
        L += ["", f"## Projection: ingesting {n_target} meetings", "",
              "| step | model / hardware | cost | wall time |", "|---|---|---|---|"]
        chunks_per = idx.get("chunks", 0) / max(idx.get("indexed", 1), 1)
        emb_rate = idx.get("chunks", 1) / max(idx.get("seconds_embed", 1), 1)
        L.append(f"| parse, chunk, full-text index | code | $0 | ~{idx.get('seconds_parse_index', 1) / max(idx.get('indexed', 1), 1) * n_target:.0f}s |")
        L.append(f"| embeddings ({chunks_per * n_target:,.0f} chunks) | bge-small, this laptop CPU | $0 | ~{chunks_per * n_target / emb_rate / 3600:.1f}h |")
        L.append(f"| embeddings | any GPU (~500 chunks/s) or an embeddings API | $0 to ~$1 | ~{chunks_per * n_target / 500 / 60:.0f} min |")
        tin, tout = ex["i"] * n_target, ex["o"] * n_target
        wall = ex["s"] * n_target / workers / 3600
        for name, (pi, po) in config.PRICES.items():
            w = f"~{wall:.1f}h at {workers} parallel (measured)" if name.split("-")[1] in str(ex["model"]) else "slower per call than Haiku"
            L.append(f"| memory extraction | {name} (list price) | ${tin / 1e6 * pi + tout / 1e6 * po:,.2f} | {w} |")
        vout = tout * visible_ratio
        L.append(f"| memory extraction | local 9B (Ollama, own GPU, no thinking) | $0 marginal | "
                 f"~{vout:,.0f} output tokens / your GPU's tokens/s: ~{vout / 15 / 3600:.0f}h at 15 tok/s (4GB card, partial offload), "
                 f"~{vout / 60 / 3600:.0f}h at 60 tok/s (24GB card); prompt processing extra |")
        L += ["", f"Extraction volume: {tin / 1e6:.1f}M input + {tout / 1e6:.1f}M output tokens. Teams are independent, so "
              "extraction parallelises across teams; within a team meetings run in time order. A daily job only "
              "processes new meetings (file hash check), so steady-state cost is per new meeting, not per corpus."]
    return "\n".join(L)
