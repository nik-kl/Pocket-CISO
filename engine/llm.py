"""Ollama client (chat, embeddings, model detection), hash-n-gram fallback embeddings and the engine label."""
from __future__ import annotations

import httpx
import logging
import math
import numpy as np
import threading
import time
import zlib
from collections import Counter
from typing import Any

from .config import (
    EMBED_MODEL, EMBED_TIMEOUT, EMBED_TOTAL_BUDGET, HASH_ID, JUDGE_MODEL, LLM_API, LLM_API_KEY, LLM_KEEP_ALIVE, LLM_MODEL,
    LLM_NUM_CTX, LLM_TIMEOUT, OLLAMA_BASE_URL,
)

from .search.normalize import content_stems

OPENAI = LLM_API == "openai"
HEADERS = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
# not part of the OpenAI API; a server that rejects one gets it dropped (one warning) instead of every call failing (= NEIN)
OPENAI_OPTIONAL = {"reasoning_effort": "none",  # thinking models would spend the 3-token judge budget on reasoning
                   "num_ctx": LLM_NUM_CTX}
log = logging.getLogger(__name__)


class OllamaClient:
    """Thin httpx wrapper. Every method returns None on failure so callers can degrade."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tags: list[str] | None = None
        self._tags_at = 0.0
        self._dropped: set[str] = set()  # OPENAI_OPTIONAL parameters the server rejected

    def tags(self, force: bool = False) -> list[str] | None:
        with self._lock:
            ttl = 8.0 if self._tags is not None else 2.0
            if not force and time.monotonic() - self._tags_at < ttl:
                return self._tags
            try:
                if OPENAI:
                    r = httpx.get(f"{OLLAMA_BASE_URL}/models", headers=HEADERS, timeout=2.0)
                    r.raise_for_status()
                    self._tags = [m["id"] for m in r.json().get("data", [])]
                else:
                    r = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=2.0)
                    r.raise_for_status()
                    self._tags = [m["name"] for m in r.json().get("models", [])]
            except Exception:
                self._tags = None
            self._tags_at = time.monotonic()
            return self._tags

    def mark_down(self) -> None:
        with self._lock:
            self._tags, self._tags_at = None, time.monotonic()

    @staticmethod
    def _match(names: list[str], wanted: str) -> str | None:
        for n in names:
            if n == wanted or n.split(":")[0] == wanted.split(":")[0] and (":" not in wanted or n == wanted):
                return n
        return None

    def llm_model(self) -> str | None:
        names = self.tags()
        if not names:
            return None
        return self._match(names, LLM_MODEL) or next((n for n in names if not any(k in n.lower() for k in ("embed", "bge"))), None)

    def judge_model(self) -> str | None:
        names = self.tags()
        if not names:
            return None
        return self._match(names, JUDGE_MODEL) or self.llm_model()

    def embed_model(self) -> str | None:
        names = self.tags()
        if not names:
            return None
        return self._match(names, EMBED_MODEL) or next((n for n in names if "embed" in n.lower()), None)

    def embed(self, texts: list[str]) -> np.ndarray | None:
        model = self.embed_model()
        if not model or not texts:
            return None
        try:
            out: list[list[float]] = []
            budget_end = time.monotonic() + EMBED_TOTAL_BUDGET
            for i in range(0, len(texts), 16):
                if time.monotonic() > budget_end:
                    raise TimeoutError("embedding budget exceeded")
                batch = texts[i : i + 16]
                if OPENAI:
                    r = httpx.post(f"{OLLAMA_BASE_URL}/embeddings", headers=HEADERS, json={"model": model, "input": batch}, timeout=EMBED_TIMEOUT)
                    r.raise_for_status()
                    out.extend(d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"]))
                    continue
                r = httpx.post(f"{OLLAMA_BASE_URL}/api/embed", json={"model": model, "input": batch}, timeout=EMBED_TIMEOUT)
                if r.status_code == 404:  # older Ollama: single-prompt endpoint
                    for t in batch:
                        r1 = httpx.post(f"{OLLAMA_BASE_URL}/api/embeddings", json={"model": model, "prompt": t}, timeout=EMBED_TIMEOUT)
                        r1.raise_for_status()
                        out.append(r1.json()["embedding"])
                    continue
                r.raise_for_status()
                out.extend(r.json()["embeddings"])
            arr = np.asarray(out, dtype=np.float32)
            if arr.ndim != 2 or arr.shape[0] != len(texts):
                return None
            return arr / np.maximum(np.linalg.norm(arr, axis=1, keepdims=True), 1e-9)
        except Exception:
            self.mark_down()
            return None

    def chat(self, system: str, user: str, *, json_mode: bool = False, timeout: float = LLM_TIMEOUT, max_tokens: int = 256,
             judge: bool = False) -> str | None:
        """judge=True: answerability/conflict decisions on JUDGE_MODEL; otherwise phrasing on LLM_MODEL."""
        model = self.judge_model() if judge else self.llm_model()
        if not model:
            return None
        if OPENAI:
            return self._chat_openai(model, system, user, timeout, max_tokens)
        payload: dict[str, Any] = {
            "model": model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0.0, "seed": 7, "num_predict": max_tokens, "num_ctx": LLM_NUM_CTX},
            "keep_alive": LLM_KEEP_ALIVE,
        }
        if json_mode:
            payload["format"] = "json"
        if model.startswith("qwen3"):  # thinking model: no reasoning trace (3-token judge budget, latency); other models get no think field
            payload["think"] = False
        try:
            r = httpx.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=timeout)
            r.raise_for_status()
            return (r.json().get("message", {}).get("content") or "").strip() or None
        except httpx.ConnectError:
            self.mark_down()
            return None
        except Exception:
            return None  # timeout / bad response: caller falls back to rules, Ollama stays "connected"

    def _chat_openai(self, model: str, system: str, user: str, timeout: float, max_tokens: int) -> str | None:
        # no response_format: servers disagree on json_object support; callers already extract JSON from plain text
        payload = {"model": model, "stream": False, "temperature": 0.0, "seed": 7, "max_tokens": max_tokens,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                   **{k: v for k, v in OPENAI_OPTIONAL.items() if k not in self._dropped}}
        try:
            while True:
                r = httpx.post(f"{OLLAMA_BASE_URL}/chat/completions", headers=HEADERS, json=payload, timeout=timeout)
                rejected = [k for k in OPENAI_OPTIONAL if k in payload and k in r.text] if r.status_code in (400, 422) else []
                if not rejected:
                    break
                log.warning("LLM server rejected %s; sending without it from now on", ", ".join(rejected))
                self._dropped.update(rejected)
                for k in rejected:
                    payload.pop(k)
            r.raise_for_status()
            return ((r.json().get("choices") or [{}])[0].get("message", {}).get("content") or "").strip() or None
        except httpx.ConnectError:
            self.mark_down()
            return None
        except Exception:
            return None


OLLAMA = OllamaClient()

HASH_DIM = 1024


def _bucket(token: str) -> tuple[int, float]:
    h = zlib.crc32(token.encode("utf-8"))
    return h % HASH_DIM, 1.0 if (h >> 16) & 1 else -1.0


def hash_embed(text: str) -> np.ndarray:
    """Deterministic TF + char-4-gram feature hashing (offline space; staging alignment and conflict pre-selection only)."""
    toks = content_stems(text)
    v = np.zeros(HASH_DIM, dtype=np.float32)
    for t, c in Counter(toks).items():
        i, s = _bucket("w:" + t)
        v[i] += s * (1.0 + math.log(c))
        if len(t) >= 5:
            for k in range(len(t) - 3):
                i, s = _bucket("g:" + t[k : k + 4])
                v[i] += s * 0.15
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


class Embedder:
    def engine_id(self) -> str:
        model = OLLAMA.embed_model()
        return f"ollama:{model}" if model else HASH_ID

    def embed(self, texts: list[str], kind: str, engine_id: str) -> np.ndarray | None:
        if not texts:
            return np.zeros((0, HASH_DIM), dtype=np.float32)
        if engine_id == HASH_ID:
            return np.stack([hash_embed(t) for t in texts])
        prefix = ""
        if "nomic" in engine_id:
            prefix = "search_query: " if kind == "query" else "search_document: "
        return OLLAMA.embed([prefix + t for t in texts])


EMBEDDER = Embedder()


def engine_label(engine_id: str | None = None) -> str:
    llm = OLLAMA.llm_model()
    eid = engine_id or EMBEDDER.engine_id()
    emb = eid.split(":", 1)[1] if eid.startswith("ollama:") else "Hash-Ngram"
    if llm:
        judge = OLLAMA.judge_model()
        return f"{'OpenAI-API' if OPENAI else 'Ollama'} {llm}" + (f" (Judge {judge})" if judge and judge != llm else "") + f" + {emb}"
    return f"Rule-based + {emb}" + (" (Ollama offline)" if OLLAMA.tags() is None else "")
