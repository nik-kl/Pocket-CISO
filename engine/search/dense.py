"""Dense retrieval (bge-m3 via Ollama), reciprocal-rank fusion and document-diversified candidates."""
from __future__ import annotations

import numpy as np
from collections import Counter, defaultdict
from typing import Any

from ..llm import EMBEDDER, OLLAMA
from .lexical import RRF_K, RoleView
from ..store import _DB_CACHE, ensure_vectors


_QVEC_CACHE: dict[tuple[str, str], np.ndarray] = {}


def reset_query_caches() -> None:
    _QVEC_CACHE.clear()
    _DB_CACHE.update(key=None, db=None)


def query_vectors(texts: list[str], eid: str) -> np.ndarray | None:
    missing = [t for t in texts if (eid, t) not in _QVEC_CACHE]
    if missing:
        arr = EMBEDDER.embed(missing, "query", eid)
        if arr is None:
            return None
        for t, v in zip(missing, arr):
            if len(_QVEC_CACHE) > 2000:
                _QVEC_CACHE.clear()
            _QVEC_CACHE[(eid, t)] = v
    return np.stack([_QVEC_CACHE[(eid, t)] for t in texts])


def dense_scores(db: dict[str, Any], view: RoleView, texts: list[str]) -> dict[int, float] | None:
    """Cosine of each allowed chunk (max over the query texts); None when no Ollama embedding model is available."""
    if not OLLAMA.embed_model():
        return None
    eid, vecs = ensure_vectors(db)
    if not eid.startswith("ollama:"):
        return None
    qm = query_vectors(texts, eid)
    if qm is None:
        return None
    out: dict[int, float] = {}
    for i in view.allowed:
        v = vecs.get(view.idx.chunks[i]["id"])
        if v is not None:
            out[i] = float(np.max(qm @ v))
    return out


def fuse(view: RoleView, *scores: dict[int, float] | None) -> list[int]:
    """Reciprocal rank fusion (k = 60); ties broken by the stable chunk order (index position), never by the chunk id."""
    fused: dict[int, float] = defaultdict(float)
    for sc in scores:
        if not sc:
            continue
        ranking = sorted((i for i in sc if sc[i] > 0), key=lambda i: (-sc[i], i))
        for r, i in enumerate(ranking):
            fused[i] += 1.0 / (RRF_K + r + 1)
    return sorted(fused, key=lambda i: (-fused[i], i))


CAND_K, CAND_PER_DOC = 12, 3


def diversify(idx: "LexIndex", fused: list[int], k: int = CAND_K, per_doc: int = CAND_PER_DOC) -> list[int]:
    """Candidates diversified by document (class 5): fused order, at most `per_doc` chunks per document, `k` in total, so a
    large document on the topic cannot push other documents (and their contradicting values) out of the candidate list."""
    out: list[int] = []
    n: Counter[str] = Counter()
    for i in fused:
        d = idx.chunks[i]["docName"]
        if n[d] < per_doc:
            out.append(i)
            n[d] += 1
            if len(out) >= k:
                break
    return out
