"""Local dense embeddings (bge-small via fastembed/ONNX, CPU). No API calls, no per-token cost.
Optional: if disabled or unavailable the system falls back to full-text-only retrieval."""
from __future__ import annotations

import re
import threading

import numpy as np

from . import config

_model = None
_lock = threading.Lock()
_FILLER = re.compile(r"\b(um+|uh+|mm+-?hmm+|hmm+|mm+|er+|ah+)\b[,.]?\s*", re.I)


def available() -> bool:
    if not config.USE_EMBEDDINGS:
        return False
    try:
        _get()
        return True
    except Exception:
        return False


def _get():
    global _model
    with _lock:
        if _model is None:
            from fastembed import TextEmbedding
            _model = TextEmbedding(model_name=config.EMBED_MODEL)
        return _model


def clean(text: str) -> str:
    return _FILLER.sub("", text)


def embed_passages(texts: list[str], batch_size: int = 64) -> np.ndarray:
    model = _get()
    with _lock:
        vecs = list(model.passage_embed([clean(t) for t in texts], batch_size=batch_size))
    return _norm(np.vstack(vecs)) if vecs else np.zeros((0, 384), dtype=np.float32)


def embed_query(text: str) -> np.ndarray:
    model = _get()
    with _lock:
        v = next(iter(model.query_embed([text])))
    return _norm(np.asarray(v)[None, :])[0]


def _norm(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1
    return m / n
