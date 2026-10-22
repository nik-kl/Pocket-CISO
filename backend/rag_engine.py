"""Pocket CISO — local RAG & governance engine (Pipeline A: ingestion/diff, Pipeline B: stateless chat).

Shares ../data/pocket-ciso-db.json with the Next.js app (same schema, same audit hash-chain, same HMAC tokens)
and keeps embeddings in ../data/vectors.json (NumPy cosine search, no external vector DB).
Everything degrades gracefully: without Ollama the engine switches to deterministic hash-n-gram embeddings
and rule-based rewrite / extraction / verification, so a live demo can never crash.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import shutil
import threading
import time
import unicodedata
import uuid
import zipfile
import zlib
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx
import numpy as np

# ───────────────────────── Configuration ─────────────────────────

DATA_DIR = Path(os.getenv("POCKET_CISO_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "pocket-ciso-db.json"
VEC_PATH = DATA_DIR / "vectors.json"
LOCK_DIR = DATA_DIR / ".db.lock"  # cross-process mutex shared with the Node app (atomic mkdir)
AUTH_SECRET_PATH = DATA_DIR / ".auth-secret"
IS_PRODUCTION = os.getenv("NODE_ENV") == "production" or os.getenv("POCKET_CISO_ENV") == "production"
DEMO_MODE = not IS_PRODUCTION and (os.getenv("POCKET_CISO_DEMO") == "1" or os.getenv("POCKET_CISO_DEMO_MODE") == "1")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "14"))
EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", "20"))
EMBED_TOTAL_BUDGET = float(os.getenv("EMBED_TOTAL_BUDGET", "100"))  # whole embed() call; staging stays below the gateway timeout (180 s)
CONFLICT_LLM_TIMEOUT = float(os.getenv("CONFLICT_LLM_TIMEOUT", "20"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024
MAX_CHUNK_CHARS = 600
COLLECTION = "policies_live_v1"
INBOX_DIR = Path(os.getenv("WATCHED_INBOX_DIR", Path(__file__).resolve().parent / "watched_inbox"))
AUTO_SYNC = os.getenv("AUTO_SYNC", "0") == "1"
AUTO_SYNC_SECONDS = max(5.0, float(os.getenv("AUTO_SYNC_SECONDS", "30")))
CONNECTOR_ACTOR = {"email": "auto-connector@watched_inbox", "role": "L2"}  # clearance used for connector staging + conflict scan
CONNECTOR_AUTHOR = "Auto-Connector (watched_inbox)"
ROLE_RANK = {"L1": 1, "L2": 2, "L3": 3}
GENESIS = "0" * 64
VERSION = "0.6.0"

HASH_ID = "hash-ngram-v1"

# Similarity thresholds per embedding space (cosine). Ollama values follow the product spec; the hash space is calibrated.
PARAMS = {
    "ollama": {
        "min_score": float(os.getenv("MIN_SCORE", "0.55")),  # lowered from 0.65 for bge-m3
        "conflict_sim": float(os.getenv("CONFLICT_SIM", "0.68")),  # adjusted for bge-m3
        "align": float(os.getenv("ALIGN_SIM", "0.60")),
    },
    "hash": {"min_score": 0.25, "conflict_sim": 0.30, "align": 0.35},
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, error: str):
        super().__init__(error)
        self.status, self.code, self.error = status, code, error


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def params_for(engine_id: str) -> dict[str, float]:
    return PARAMS["ollama" if engine_id.startswith("ollama:") else "hash"]


# ───────────────────────── Text utilities (mirror src/lib/db.ts) ─────────────────────────

def norm(s: str) -> str:
    return s.lower().replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")


def tokenize(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", norm(s)) if t]


STOP = set(
    ("wie was wer wo wann wieso warum welche welcher welches bis muss musst mussen darf durfen kann konnen ich mein meine meinen mir mich wir uns das der die den dem des "
     "ein eine einen einem einer und oder im in am an auf aus bei mit nach von vom zu zur zum fur uber auch nicht sein ist sind wird werden wurde hat haben es sie er ob als wenn "
     "dass noch nur so machen gibt gilt soll sollen sollte dann dies diese ihr ihre wir man oder sowie sowohl jede jeder jedes alle dabei dafur damit durch sich "
     "eines einem einer neuen neues neuen dafür dafuer laut welcher welches "
     "a an am an and are as at be been being but by can does for from have how i if in is it may my of on or our the their them these they this to us was we what who with").split()
)

def stem(t: str) -> str:
    return t[:7] if len(t) >= 7 else t


def content_stems(text: str) -> list[str]:
    return [stem(t) for t in tokenize(text) if len(t) >= 3 and t not in STOP and not t.isdigit()]


NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def numbers_of(text: str) -> set[str]:
    return {n.replace(",", ".") for n in NUM_RE.findall(text)}


SENT_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9„\"(])")
# Abbreviations after which a period does not end a sentence ("z. B.", "bzw.", "Art. 33", "max. 90 Tage", "e.g.").
ABBREV = frozenset("z b bzw ca inkl exkl nr art abs max min mind ggf evtl vgl u a d h e g i s etc usw sog zzgl gem lt str dr st vs no "
                   "approx incl resp cf fig sec bspw ff".split())


def split_sentences(text: str) -> list[str]:
    """Sentence spans; generated table statements ("Tabelle [§…]: …") always start a new span."""
    pieces = re.split(r"\s+(?=Tabelle \[)", text)
    if len(pieces) > 1:
        return [s for p in pieces for s in split_sentences(p)]
    out: list[str] = []
    start = 0
    for m in SENT_BREAK.finditer(text):
        before = text[start : m.start()]
        word = re.search(r"([A-Za-zÄÖÜäöüß]+)\.$", before)
        if word and (word.group(1).lower() in ABBREV or len(word.group(1)) == 1):
            continue
        if re.search(r"(?:^|[:|]\s*)\d{1,3}\.$", before):  # enumeration "Action: 1. Host …"
            continue
        piece = text[start : m.start()].strip()
        if piece:
            out.append(piece)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


# ───────────────────────── Ollama client + embeddings ─────────────────────────

class OllamaClient:
    """Thin httpx wrapper. Every method returns None on failure so callers can degrade."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tags: list[str] | None = None
        self._tags_at = 0.0

    def tags(self, force: bool = False) -> list[str] | None:
        with self._lock:
            ttl = 8.0 if self._tags is not None else 2.0
            if not force and time.monotonic() - self._tags_at < ttl:
                return self._tags
            try:
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

    def chat(self, system: str, user: str, *, json_mode: bool = False, timeout: float = LLM_TIMEOUT, max_tokens: int = 256) -> str | None:
        model = self.llm_model()
        if not model:
            return None
        payload: dict[str, Any] = {
            "model": model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0.0, "seed": 7, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        try:
            r = httpx.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=timeout)
            r.raise_for_status()
            return (r.json().get("message", {}).get("content") or "").strip() or None
        except httpx.ConnectError:
            self.mark_down()
            return None
        except Exception:
            return None  # timeout / bad response: caller falls back to rules, Ollama stays "connected"


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
        return f"Ollama {llm} + {emb}"
    return f"Rule-based + {emb}" + (" (Ollama offline)" if OLLAMA.tags() is None else "")


# ───────────────────────── Persistence (shared JSON DB + vector store) ─────────────────────────

def _lock_owner(lock: Path) -> str | None:
    try:
        return (lock / "owner").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _steal_stale_lock(lock: Path, stale: float) -> None:
    """Mirror of stealStaleLock in src/lib/db.ts: an atomic rename has exactly one winner; we verify the owner we judged stale."""
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return
    if age <= stale:
        return
    seen = _lock_owner(lock)
    head = seen.split(":")[0] if seen else ""
    if head.isdigit() and _pid_alive(int(head)) and age <= stale * 10:
        return  # slow but alive
    parked = lock.with_name(f"{lock.name}.stale-{secrets.token_hex(4)}")
    try:
        os.rename(lock, parked)
    except OSError:
        return  # somebody else won the steal
    if _lock_owner(parked) == seen:
        shutil.rmtree(parked, ignore_errors=True)
        return
    try:  # we moved a fresh lock: put it back
        os.rename(parked, lock)
    except OSError:
        shutil.rmtree(parked, ignore_errors=True)


@contextmanager
def file_lock(timeout: float = 10.0, stale: float = 60.0) -> Iterator[None]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    token = f"{os.getpid()}:{secrets.token_hex(6)}"
    while True:
        try:
            os.mkdir(LOCK_DIR)
            break
        except FileExistsError:
            _steal_stale_lock(LOCK_DIR, stale)
            if time.monotonic() > deadline:
                raise ApiError(503, "DB_LOCKED", "Datenbank ist gesperrt — bitte erneut versuchen.")
            time.sleep(0.02)
    try:
        (LOCK_DIR / "owner").write_text(token, encoding="utf-8")
    except OSError:
        pass
    try:
        yield
    finally:
        if _lock_owner(LOCK_DIR) == token:  # compare before remove: never delete a lock someone else owns now
            shutil.rmtree(LOCK_DIR, ignore_errors=True)


def read_db() -> dict[str, Any]:
    try:
        db = json.loads(DB_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ApiError(503, "DB_NOT_READY", "Datenbank noch nicht initialisiert — Web-App einmal öffnen (GET /api/v1).")
    except json.JSONDecodeError:
        raise ApiError(500, "DB_CORRUPT", "pocket-ciso-db.json ist beschädigt.")
    if "meta" not in db or "live_chunks" not in db:
        raise ApiError(500, "DB_CORRUPT", "Datenbank-Schema ungültig.")
    db["meta"].pop("secret", None)
    return db


_SECRET_CACHE: tuple[int, str] | None = None  # (mtime_ns of data/.auth-secret or 0 for env secrets, secret)


def auth_secret() -> str:
    """Cached; the demo secret file is re-read only when its mtime changes (e.g. data dir wiped while running)."""
    global _SECRET_CACHE
    try:
        mtime = 0 if os.getenv("POCKET_CISO_SECRET", "").strip() else AUTH_SECRET_PATH.stat().st_mtime_ns
    except OSError:
        mtime = -1
    if _SECRET_CACHE is None or _SECRET_CACHE[0] != mtime:
        secret = _load_auth_secret()
        try:
            mtime = 0 if os.getenv("POCKET_CISO_SECRET", "").strip() else AUTH_SECRET_PATH.stat().st_mtime_ns
        except OSError:
            mtime = -1
        _SECRET_CACHE = (mtime, secret)
    return _SECRET_CACHE[1]


def _load_auth_secret() -> str:
    configured = os.getenv("POCKET_CISO_SECRET", "").strip()
    if configured:
        if len(configured) < 32:
            raise ApiError(500, "AUTH_CONFIG_INVALID", "POCKET_CISO_SECRET muss mindestens 32 Zeichen lang sein.")
        return configured
    if not DEMO_MODE:
        raise ApiError(500, "AUTH_CONFIG_MISSING", "POCKET_CISO_SECRET muss außerhalb des Demo-Modus gesetzt sein.")
    try:
        existing = AUTH_SECRET_PATH.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    except FileNotFoundError:
        pass
    except OSError as e:
        raise ApiError(500, "AUTH_SECRET_UNREADABLE", f"Auth-Secret konnte nicht gelesen werden: {type(e).__name__}") from e
    AUTH_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_hex(32)
    try:
        fd = os.open(AUTH_SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return AUTH_SECRET_PATH.read_text(encoding="utf-8").strip()
    try:
        os.write(fd, generated.encode("utf-8"))
    finally:
        os.close(fd)
    return generated


def _atomic_write(path: Path, payload: str) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # audit data: owner-only
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(tmp, path)


def write_db(db: dict[str, Any]) -> None:
    _atomic_write(DB_PATH, json.dumps(db, indent=2, ensure_ascii=False))


@contextmanager
def locked_db() -> Iterator[dict[str, Any]]:
    """Read-modify-write under the cross-process lock. Persists even when an ApiError aborts (audit entries survive)."""
    with file_lock():
        db = read_db()
        before = json.dumps(db, sort_keys=True)
        try:
            yield db
        except ApiError:
            if json.dumps(db, sort_keys=True) != before:
                write_db(db)
            raise
        if json.dumps(db, sort_keys=True) != before:
            write_db(db)


class VectorStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.engine: str | None = None
        self.vecs: dict[str, np.ndarray] = {}
        self.stash: dict[str, dict[str, np.ndarray]] = {}  # expensive Ollama vector set kept while the hash engine is active (Ollama flapping)
        self.lexical: dict[str, Any] | None = None  # BM25 terms/spans of the live chunks (built at approval time, see LexIndex)
        self._mtime: float | None = None

    def reload(self) -> None:
        try:
            mt = VEC_PATH.stat().st_mtime
        except FileNotFoundError:
            self.engine, self.vecs, self.lexical, self._mtime = None, {}, None, None
            return
        if mt == self._mtime:
            return
        try:
            raw = json.loads(VEC_PATH.read_text(encoding="utf-8"))
            self.engine = raw.get("engine")
            self.vecs = {k: np.asarray(v, dtype=np.float32) for k, v in raw.get("vectors", {}).items()}
            self.stash = {e: {k: np.asarray(v, dtype=np.float32) for k, v in vs.items()} for e, vs in raw.get("stash", {}).items()}
            self.lexical = raw.get("lexical")
        except Exception:
            self.engine, self.vecs, self.lexical = None, {}, None
        self._mtime = mt

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "engine": self.engine,
            "dim": int(next(iter(self.vecs.values())).shape[0]) if self.vecs else 0,
            "updatedAt": now_iso(),
            "vectors": {k: [round(float(x), 6) for x in v] for k, v in self.vecs.items()},
            "stash": {e: {k: [round(float(x), 6) for x in v] for k, v in vs.items()} for e, vs in self.stash.items()},
            "lexical": self.lexical,
        }
        _atomic_write(VEC_PATH, json.dumps(payload))
        self._mtime = VEC_PATH.stat().st_mtime

    def count(self) -> int:
        with self.lock:
            self.reload()
            return len(self.vecs)


VECTORS = VectorStore()


def ensure_vectors(db: dict[str, Any], force_hash: bool = False) -> tuple[str, dict[str, np.ndarray]]:
    """Make vectors.json match the DB (live chunks + pending staged hunks). Returns (engine_id, vectors)."""
    needed: dict[str, str] = {c["id"]: c["content"] for c in db["live_chunks"]}
    for d in db["staged_diffs"]:
        if d["status"] == "pending":
            for h in d["hunks"]:
                needed[f"stg:{h['id']}"] = h["newContent"]
    with VECTORS.lock:
        for attempt in range(2):
            eid = HASH_ID if (force_hash or attempt == 1) else EMBEDDER.engine_id()
            VECTORS.reload()
            changed = False
            if VECTORS.engine != eid:
                if VECTORS.engine and VECTORS.engine.startswith("ollama:") and VECTORS.vecs:
                    VECTORS.stash = {VECTORS.engine: VECTORS.vecs}  # keep the costly set; restored when Ollama returns
                restored = VECTORS.stash.pop(eid, {}) if eid.startswith("ollama:") else {}
                VECTORS.engine, VECTORS.vecs, changed = eid, restored, True
            missing = [i for i in needed if i not in VECTORS.vecs]
            if missing:
                arr = EMBEDDER.embed([needed[i] for i in missing], "document", eid)
                if arr is None:
                    continue  # Ollama died mid-way → retry in hash space
                for i, v in zip(missing, arr):
                    VECTORS.vecs[i] = v
                changed = True
            for stale in [i for i in VECTORS.vecs if i not in needed]:
                del VECTORS.vecs[stale]
                changed = True
            if changed:
                VECTORS.save()
            return eid, dict(VECTORS.vecs)
    raise ApiError(500, "EMBED_FAILED", "Embeddings konnten nicht berechnet werden.")


# ───────────────────────── Audit trail + auth (compatible with src/lib/db.ts) ─────────────────────────

def append_audit(db: dict[str, Any], actor: str, role: str, action: str, target: str, status: str, detail: str = "") -> None:
    meta = db["meta"]
    n = meta["nextAudit"]
    meta["nextAudit"] = n + 1
    prev = db["audit_logs"][-1]["hash"] if db["audit_logs"] else GENESIS
    e = {"id": f"AUD-{n:04d}", "timestamp": now_iso(), "actor": actor, "role": role, "action": action, "target": target, "status": status, "detail": detail, "prevHash": prev, "hash": "", "v": 2}
    # v2: JSON array (unambiguous field boundaries) — byte-identical to JSON.stringify in src/lib/db.ts
    e["hash"] = sha256(json.dumps([prev, e["id"], e["timestamp"], actor, role, action, target, status, detail], ensure_ascii=False, separators=(",", ":")))
    db["audit_logs"].append(e)
    meta["auditAnchor"] = hmac.new(auth_secret().encode("utf-8"), f"audit-anchor|{len(db['audit_logs'])}|{e['hash']}".encode("utf-8"), hashlib.sha256).hexdigest()


_DENIALS: dict[str, tuple[float, int]] = {}
DENIAL_WINDOW_S = 60.0


def append_denial(db: dict[str, Any], actor: str, role: str, action: str, target: str, detail: str) -> None:
    """Sampled denial audit (mirrors appendDenial in src/lib/db.ts): ≤ 1 entry per actor/action/target-prefix per minute."""
    key = f"{actor}|{action}|{target.split(':')[0]}"
    now = time.time()
    last, dropped = _DENIALS.get(key, (0.0, 0))
    if now - last < DENIAL_WINDOW_S:
        _DENIALS[key] = (last, dropped + 1)
        return
    _DENIALS[key] = (now, 0)
    suffix = f" (+{dropped} gleichartige Ablehnungen unterdrückt)" if dropped else ""
    append_audit(db, actor, role, action, target, "DENIED", detail + suffix)


def verify_token(secret: str, token: str | None) -> str | None:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] not in ("L2", "L3"):
        return None
    role, exp, sig = parts
    expected = hmac.new(secret.encode("utf-8"), f"{role}.{exp}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig.encode("utf-8"), expected.encode("utf-8")):
        return None
    try:
        return role if int(exp) > time.time() * 1000 else None
    except ValueError:
        return None


def actor_for(db: dict[str, Any], token: str | None) -> dict[str, str]:
    role = verify_token(auth_secret(), token) or "L1"
    user = next(u for u in db["users"] if u["role"] == role)
    return {"email": user["email"], "role": role}


def authorize(db: dict[str, Any], token: str | None, need: str, action: str, target: str) -> dict[str, str]:
    a = actor_for(db, token)
    if ROLE_RANK[a["role"]] < ROLE_RANK[need]:
        append_denial(db, a["email"], a["role"], "ACCESS_DENIED", f"{action}:{target}", f"Erfordert {need}")
        raise ApiError(403, "FORBIDDEN", f"Diese Aktion erfordert Clearance {need}.")
    return a


def check_role(token: str | None, need: str) -> None:
    db = read_db()
    actor = actor_for(db, token)
    if ROLE_RANK[actor["role"]] < ROLE_RANK[need]:
        raise ApiError(403, "FORBIDDEN", f"Diese Aktion erfordert Clearance {need}.")


# ───────────────────────── Document parsing + semantic chunking ─────────────────────────

ALLOWED_EXT = {".pdf", ".docx", ".md", ".txt"}


def safe_doc_name(name: str) -> str:
    base = Path(unicodedata.normalize("NFC", name).replace("\\", "/")).name.strip()
    base = re.sub(r"[^\w.\-&,() ]", "_", base)[:120]  # keeps "IAM, Endgeräte & Wechselmedien (v2).pdf" readable
    if not re.search(r"\.[A-Za-z0-9]{1,5}$", base):
        base += ".txt"
    return base or "dokument.txt"


MAX_UNZIPPED_BYTES = 64 * 1024 * 1024


def _check_zip_budget(data: bytes) -> None:
    """DOCX is a ZIP: reject archives whose declared uncompressed size is absurd *before* any parser allocates memory (zip bomb)."""
    try:
        infos = zipfile.ZipFile(io.BytesIO(data)).infolist()
    except zipfile.BadZipFile:
        raise ApiError(422, "DOCX_UNREADABLE", "DOCX konnte nicht gelesen werden: BadZipFile")
    if len(infos) > 2000 or sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
        raise ApiError(413, "DOCX_TOO_LARGE", "DOCX entpackt zu groß (möglicher Zip-Bomb).")


def parse_document(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ApiError(415, "UNSUPPORTED_TYPE", f"Dateityp {ext or '?'} nicht unterstützt (erlaubt: .pdf, .docx, .md, .txt).")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ApiError(413, "TOO_LARGE", f"Datei größer als {MAX_UPLOAD_BYTES // 1024 // 1024} MB.")
    if ext in (".md", ".txt"):
        for enc in ("utf-8-sig", "cp1252"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")
    if ext == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise ApiError(422, "PDF_ENCRYPTED", "Verschlüsselte PDFs werden nicht unterstützt.")
            return "\n\n".join(_pdf_page_text(p) for p in reader.pages[:300])
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(422, "PDF_UNREADABLE", f"PDF konnte nicht gelesen werden: {type(e).__name__}")
    _check_zip_budget(data)
    try:
        return _docx_to_text(data)
    except Exception as e:  # noqa: BLE001
        raise ApiError(422, "DOCX_UNREADABLE", f"DOCX konnte nicht gelesen werden: {type(e).__name__}")


PAGE_FOOTER = re.compile(r"\b(?:Seite|Page)\s+\d+\s+(?:von|of)\s+\d+\s*$", re.I)
LAYOUT_CELL = re.compile(r"\S+(?: {1,2}\S+)*")  # a cell = words separated by ≤ 2 spaces; columns are ≥ 3 spaces apart


def _layout_cells(line: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group(0)) for m in LAYOUT_CELL.finditer(line)]


def _join_cell(a: str, b: str) -> str:
    if not a:
        return b
    return a + b if re.search(r"\w-$", a) and b[:1].isalnum() else f"{a} {b}"  # "ERP-" + "Auftragsbearbeitung"


LAYOUT_TOL = 8  # max. distance (characters) between a cell and its column start; centred cells drift a few characters


def _layout_rows(block: list[str], starts: list[int]) -> list[str] | None:
    """Cells → columns. None when any assignment is uncertain (cell far from every column start, reaching into the next
    column, or two cells of one line in the same column): the block is then treated as running text, never as a table
    with values in the wrong column."""
    cols = [""] * len(starts)
    for line in block:
        used: set[int] = set()
        for pos, text in _layout_cells(line):
            k = min(range(len(starts)), key=lambda j: (abs(starts[j] - pos), j))
            if abs(starts[k] - pos) > LAYOUT_TOL or k in used or (k + 1 < len(starts) and pos + len(text) > starts[k + 1] + 1):
                return None
            used.add(k)
            cols[k] = _join_cell(cols[k], text)
    return [c.replace("|", "\\|").strip() for c in cols]


def _pdf_page_text(page: Any) -> str:
    """Layout-mode extraction keeps table columns: blocks with ≥ 3 aligned cells become Markdown tables (header + one row
    per blank-line separated block), two-line KPI boxes (values over UPPERCASE labels) become one-row tables, page footers
    are dropped. Falls back to plain extraction when the layout mode yields nothing."""
    try:
        raw = page.extract_text(extraction_mode="layout") or ""
    except Exception:  # noqa: BLE001 — older pypdf / odd fonts
        raw = ""
    if not raw.strip():
        return page.extract_text() or ""
    blocks: list[list[str]] = []
    cur: list[str] = []
    for line in raw.split("\n"):
        line = line.rstrip()
        if PAGE_FOOTER.search(line):
            continue
        if line.strip():
            cur.append(line)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    out: list[str] = []
    i = 0
    while i < len(blocks):
        head = blocks[i]
        widest = max(head, key=lambda ln: len(_layout_cells(ln)))
        if len(head) <= 3 and len(_layout_cells(widest)) >= 3:
            rows = []
            j = i + 1
            while j < len(blocks) and len(_layout_cells(blocks[j][0])) >= 2:
                rows.append(blocks[j])
                j += 1
            starts = [p for p, _ in _layout_cells(widest)]
            header = _layout_rows(head, starts) if rows else None
            body = [_layout_rows(r, starts) for r in rows]
            if header is not None and body and all(x is not None for x in body):
                out += ["", "| " + " | ".join(header) + " |", "|" + "---|" * len(starts)]
                out += ["| " + " | ".join(x) + " |" for x in body if x is not None] + [""]
                i = j
                continue
            if len(head) == 2:
                vals, labels = (_layout_cells(head[0]), _layout_cells(head[1]))
                letters = re.sub(r"[^A-Za-zÄÖÜäöüß]", "", head[1])
                centred = all(lp - 2 <= vp + len(vt) / 2 <= lp + len(lt) + 2 for (vp, vt), (lp, lt) in zip(vals, labels))
                if len(vals) == len(labels) and letters and letters.isupper() and centred:
                    out += ["", "| " + " | ".join(t for _, t in labels) + " |", "|" + "---|" * len(labels), "| " + " | ".join(t for _, t in vals) + " |", ""]
                    i += 1
                    continue
        out += [re.sub(r"\s{2,}", " ", ln.strip()) for ln in head] + [""]
        i += 1
    return "\n".join(out)


def _docx_paragraph_line(p: Any) -> str:
    txt = p.text.strip()
    if not txt:
        return ""
    style = (p.style.name if p.style is not None else "") or ""
    if style == "Title":
        return f"# {txt}"
    if style.startswith("Heading"):
        return f"## {txt}"
    if "List" in style:
        return f"- {txt}"
    return txt


def _docx_table_lines(table: Any) -> list[str]:
    """DOCX table → Markdown table (first row = header) so the chunker can turn rows into self-contained statements."""
    rows: list[list[str]] = []
    for row in table.rows:
        seen: set[int] = set()
        cells: list[str] = []
        for cell in row.cells:
            if id(cell._tc) in seen:  # merged cells repeat the same underlying element
                continue
            seen.add(id(cell._tc))
            cells.append(re.sub(r"\s+", " ", cell.text).replace("|", "\\|").strip())
        rows.append(cells)
    if not rows:
        return []
    lines = ["", "| " + " | ".join(rows[0]) + " |"]
    if len(rows) > 1:
        lines.append("|" + "---|" * len(rows[0]))
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return lines + [""]


def _docx_to_text(data: bytes) -> str:
    """Paragraphs and tables in document order, so a table stays under the heading it belongs to."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(data))
    lines: list[str] = []
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            lines.append(_docx_paragraph_line(Paragraph(child, doc)))
        elif child.tag.endswith("}tbl"):
            lines.extend(_docx_table_lines(Table(child, doc)))
    return "\n".join(lines)


HEAD_MD = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
HEAD_PARA = re.compile(r"^\s*§\s*(\d+(?:\.\d+)*)\.?\s+(.+?)\s*$")
HEAD_NUM = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+([A-ZÄÖÜ][^.!?;:]{2,80})\s*$")
# "2 Stunden", "250.000 EUR", "36 Stunden": KPI values in tables/boxes, not numbered headings (their value would leave the chunk text).
QUANTITY_TITLE = re.compile(r"^(stunden?|minuten?|tage[n]?|werktage[n]?|wochen?|monate[n]?|jahre[n]?|sekunden?|prozent|zeichen|versuche[n]?|mal|eur|euro|mb|gb|tb)\b", re.I)


def is_quantity_heading(number: str, title: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(\.\d{3})+", number)) or bool(QUANTITY_TITLE.match(title.strip()))


NUM_TITLE = re.compile(r"^§?\s*(\d+(?:\.\d+)*)\.?\s*[:–-]?\s*(.*)$")
BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def _pack(paragraphs: list[str]) -> list[str]:
    pieces: list[str] = []
    for p in paragraphs:
        if len(p) <= MAX_CHUNK_CHARS:
            pieces.append(p)
            continue
        cur = ""
        for s in split_sentences(p) or [p]:
            while len(s) > MAX_CHUNK_CHARS:  # hard wrap pathological sentences at word boundaries
                cut = s.rfind(" ", 0, MAX_CHUNK_CHARS) or MAX_CHUNK_CHARS
                pieces.append(s[:cut].strip())
                s = s[cut:].strip()
            if cur and len(cur) + 1 + len(s) > MAX_CHUNK_CHARS:
                pieces.append(cur)
                cur = s
            else:
                cur = f"{cur} {s}".strip()
        if cur:
            pieces.append(cur)
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        if cur and len(cur) + 1 + len(p) > MAX_CHUNK_CHARS:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def _body_paragraphs(lines: list[str]) -> list[str]:
    paras: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            joined = buf[0]
            for nxt in buf[1:]:  # "Endgeräte-" + "Richtlinie" (line-break hyphen) → "Endgeräte-Richtlinie"
                joined = joined + nxt if re.search(r"\w-$", joined) and nxt[:1].isalpha() else f"{joined} {nxt}"
            paras.append(joined)
            buf.clear()

    for ln in lines:
        if not ln.strip():
            flush()
            continue
        item = BULLET.sub("", ln).strip()
        if BULLET.match(ln) and item and item[-1] not in ".!?:;":
            item += "."
        buf.append(item)
    flush()
    return [re.sub(r"\s+", " ", p).strip() for p in paras if p.strip()]


TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_cells(row: str) -> list[str]:
    cells = re.split(r"(?<!\\)\|", row.strip().strip("|"))
    return [c.replace("\\|", "|").replace("**", "").replace("`", "").strip() for c in cells]


def _cell_pair(header: list[str], i: int, cell: str) -> str:
    if i < len(header) and header[i]:
        return f"{header[i]}: {cell}"
    return cell


def _table_statements(rows: list[str], label: str) -> list[str]:
    """Markdown table → one self-contained statement per data row: 'Tabelle [§2 Titel]: Spalte: Wert | Spalte: Wert.'"""
    has_header = len(rows) >= 2 and bool(TABLE_SEP.match(rows[1]))
    header = _split_cells(rows[0]) if has_header else []
    body = rows[2:] if has_header else rows
    out: list[str] = []
    for row in body:
        if TABLE_SEP.match(row):
            continue
        pairs = [_cell_pair(header, i, cell) for i, cell in enumerate(_split_cells(row)) if cell]
        if pairs:
            out.append(f"Tabelle [{label}]: {' | '.join(pairs).rstrip('.;')}.")
    return out


def _section_paragraphs(lines: list[str], label: str) -> list[str]:
    """Body lines of one section → paragraphs; table blocks become row statements, in document order."""
    paras: list[str] = []
    text: list[str] = []
    table: list[str] = []

    def flush_text() -> None:
        paras.extend(_body_paragraphs(text))
        text.clear()

    def flush_table() -> None:
        paras.extend(_table_statements(table, label))
        table.clear()

    for ln in lines:
        if TABLE_ROW.match(ln):
            flush_text()
            table.append(ln)
            continue
        flush_table()
        text.append(ln)
    flush_text()
    flush_table()
    return paras


def chunk_document(text: str) -> tuple[str | None, list[dict[str, str]]]:
    """Split by headings (## / §N / 1.2 Title) into atomic policy chunks of ≤ ~600 chars. Returns (title, chunks)."""
    text = unicodedata.normalize("NFKC", text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", ""))  # PDF ligatures (ﬁ → fi)
    lines_in = [ln for ln in text.split("\n") if not PAGE_FOOTER.search(ln)]
    # "1. Host & VLAN Isolation" inside a §-structured document is a list/table item, not a heading
    explicit = any(HEAD_MD.match(ln) or HEAD_PARA.match(ln) for ln in lines_in)
    title: str | None = None
    sections: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    pre: list[str] = []

    def start(raw: str, number: str | None = None) -> None:
        nonlocal cur
        m = NUM_TITLE.match(raw)
        if number is None and m:
            number, raw = m.group(1), m.group(2)
        label_title = (raw or "Abschnitt").strip(" #*:")
        cur = {"number": number or str(len(sections) + 1), "title": label_title or "Abschnitt", "lines": []}
        sections.append(cur)

    for line in lines_in:
        md = HEAD_MD.match(line)
        if md:
            if len(md.group(1)) == 1 and title is None and not sections:
                title = md.group(2).strip()
                continue
            start(md.group(2).strip())
            continue
        pm = HEAD_PARA.match(line)
        if pm:
            start(pm.group(2), pm.group(1))
            continue
        nm = None if explicit else HEAD_NUM.match(line)
        if nm and is_quantity_heading(nm.group(1), nm.group(2)):
            nm = None
        if nm:
            start(nm.group(2), nm.group(1))
            continue
        (cur["lines"] if cur else pre).append(line)

    if sum(len(x.strip()) for x in pre) >= (15 if not sections else 40):  # headingless text becomes one "§0" chunk set
        sections.insert(0, {"number": "0", "title": "Vorbemerkung", "lines": pre})

    chunks: list[dict[str, str]] = []
    for sec in sections:
        parts = [p for p in _pack(_section_paragraphs(sec["lines"], f"§{sec['number']} {sec['title']}")) if len(p) >= 15]
        for k, content in enumerate(parts, start=1):
            suffix = f" (Teil {k})" if k > 1 else ""
            chunks.append({"section": f"§{sec['number']} {sec['title']}{suffix}", "number": sec["number"], "content": content})
    return title, chunks


# ───────────────────────── Document series + versions ─────────────────────────

GENERIC = {"policy", "policies", "richtlinie", "richtlinien", "leitlinie", "guideline", "plan", "handbuch", "playbook", "runbook", "konzept", "standard", "dokument"}


def series_tokens(name: str) -> set[str]:
    stem_ = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name).lower()
    toks = [t for t in re.split(r"[^a-z0-9äöüß]+", stem_) if t]
    return {t for t in toks if not re.fullmatch(r"v?\d+(?:\.\d+)*", t) and t not in {"final", "draft", "entwurf"} and t not in GENERIC}


def same_series(a: str, b: str) -> bool:
    sa, sb = series_tokens(a), series_tokens(b)
    return bool(sa and sb) and len(sa & sb) / len(sa | sb) >= 0.5


def bump_version(name: str) -> str:
    m = re.match(r"^(.*_v)(\d+)(\.[A-Za-z0-9]+)$", name)
    if m:
        return f"{m.group(1)}{int(m.group(2)) + 1}{m.group(3)}"
    ext = (re.search(r"(\.[A-Za-z0-9]+)$", name) or [None, ""])[1]
    return f"{name[: -len(ext)] if ext else name}_v2{ext}"


# ───────────────────────── Pipeline A: staging, diff, conflict detection ─────────────────────────

def _json_from(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


UNIT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(stunden|stunde|std|tagen|tage|tag|wochen|woche|monaten|monate|monat|jahren|jahre|jahr|minuten|minute|zeichen|sekunden|prozent|%|versuche|versuchen|mal)\b", re.I)


UNIT_LABEL = {"stun": "Stunden", "tage": "Tage", "woch": "Wochen", "mona": "Monate", "jahr": "Jahre", "minu": "Minuten", "zeic": "Zeichen", "seku": "Sekunden", "proz": "%", "vers": "Versuche", "mal": "Mal"}


def quantities(text: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for val, unit in UNIT_RE.findall(text):
        u = norm(unit)
        key = "stun" if u.startswith("std") else "proz" if u in ("%", "prozent") else u[:4]
        out.setdefault(key, set()).add(val.replace(",", "."))
    return out


def topical_overlap(a: str, b: str) -> float:
    ta, tb = set(content_stems(a)), set(content_stems(b))
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def _rule_conflict(a: str, b: str) -> dict[str, Any]:
    """Deterministic evidence: same topic + same unit with different values (e.g. 12 Stunden ↔ 24 Stunden)."""
    overlap = topical_overlap(a, b)
    qa, qb = quantities(a), quantities(b)
    diffs = [(u, qa[u] - qb[u], qb[u] - qa[u]) for u in qa.keys() & qb.keys() if qa[u] - qb[u] and qb[u] - qa[u]]
    if overlap >= 0.25 and diffs:
        text = "; ".join(f"neu {', '.join(sorted(x))} {UNIT_LABEL.get(u, u)} ↔ bestehend {', '.join(sorted(y))} {UNIT_LABEL.get(u, u)}" for u, x, y in diffs)
        return {"has_conflict": True, "severity": "High" if overlap >= 0.5 else "Medium", "explanation": f"Abweichende Werte: {text}.", "conflicting_quote": b}
    if overlap >= 0.34 and not qa and not qb:  # no units on either side: generic number mismatch
        na, nb = numbers_of(a), numbers_of(b)
        if na - nb and nb - na:
            return {"has_conflict": True, "severity": "Medium", "explanation": f"Abweichende Werte: neu {', '.join(sorted(na - nb))} ↔ bestehend {', '.join(sorted(nb - na))}.", "conflicting_quote": b}
    return {"has_conflict": False, "severity": "Medium", "explanation": "", "conflicting_quote": ""}


CONFLICT_SYSTEM = "Du bist ein strenger Compliance-Prüfer für Unternehmensrichtlinien. Antworte ausschließlich mit einem einzigen JSON-Objekt, ohne weiteren Text."


def check_conflict(a: dict[str, str], b: dict[str, str]) -> tuple[dict[str, Any], str]:
    """Verification cascade → (verdict, verifier). Rules give precise numeric evidence; the LLM (temp 0.0, strict JSON)
    judges non-numeric contradictions and is only trusted when the two rules share real topical vocabulary."""
    rules = _rule_conflict(a["text"], b["text"])
    llm: dict[str, Any] | None = None
    if OLLAMA.llm_model():
        prompt = (
            "Vergleiche Regel A (neu, gestaged) und Regel B (bestehend, aus einer anderen Richtlinie). "
            "Widersprechen sie sich bei demselben Sachverhalt (z. B. unterschiedliche Zahlen, Fristen, Berechtigungen)? "
            "Unterschiedliche Themen sind KEIN Widerspruch.\n"
            f"Regel A ({a['doc']}, {a['section']}): {a['text']}\n"
            f"Regel B ({b['doc']}, {b['section']}): {b['text']}\n"
            'Antworte als JSON: {"has_conflict": true|false, "severity": "High"|"Medium", '
            '"explanation": "kurze Begründung auf Deutsch", "conflicting_quote": "wörtliches Zitat aus Regel B"}'
        )
        out = _json_from(OLLAMA.chat(CONFLICT_SYSTEM, prompt, json_mode=True, timeout=CONFLICT_LLM_TIMEOUT, max_tokens=220))
        if isinstance(out, dict) and isinstance(out.get("has_conflict"), bool):
            quote = str(out.get("conflicting_quote") or "").strip()
            if len(quote) < 15 or quote not in b["text"]:  # guard against hallucinated quotes
                quote = b["text"]
            llm = {"has_conflict": out["has_conflict"], "severity": "High" if str(out.get("severity", "")).lower() == "high" else "Medium",
                   "explanation": str(out.get("explanation") or "").strip()[:400], "conflicting_quote": quote}
    if rules["has_conflict"]:
        return rules, "llm+rules" if llm and llm["has_conflict"] else "rules"
    if llm and llm["has_conflict"]:
        ov = topical_overlap(a["text"], b["text"])
        shared_unit = bool(quantities(a["text"]).keys() & quantities(b["text"]).keys())
        if (shared_unit and ov >= 0.30) or ov >= 0.70:  # LLM-only verdicts need numeric context or very strong topical overlap
            return llm, "llm"
    return {"has_conflict": False, "severity": "Medium", "explanation": "", "conflicting_quote": ""}, "llm" if llm else "rules"


def _record_connector_ingest(db: dict[str, Any], c: dict[str, str], doc_name: str, diff: dict[str, Any]) -> None:
    db["meta"].setdefault("ingestedFiles", {})[c["file"]] = c["sha256"]
    append_audit(db, CONNECTOR_ACTOR["email"], "SYSTEM", "AUTO_CONNECTOR_SYNC", doc_name, "SUCCESS",
                 f"{c['file']} → {diff['id']} (SHA-256 {c['sha256'][:12]}…, {len(diff['hunks'])} Chunk(s), {len(diff['conflicts'])} Konflikt(e)) · Trigger: {c['trigger']}")


def stage_author_label(actor: dict[str, str], claimed: str | None, trusted: bool) -> str:
    """The author shown to the CISO comes from the verified session; a client-supplied name is only shown as a claim."""
    base = f"{actor['email']} ({actor['role']})"
    if not claimed:
        return base[:120]
    return (claimed if trusted else f"{base} · angegeben: {claimed}")[:120]


def stage_document(
    token: str | None,
    *,
    doc_name: str,
    text: str,
    title: str | None = None,
    min_role: str = "L1",
    author: str | None = None,
    replaces: str | None = None,
    as_actor: dict[str, str] | None = None,
    connector: dict[str, str] | None = None,
    partial: bool = False,
) -> dict[str, Any]:
    """`as_actor` (watched-inbox connector) skips token authorization; `connector` {file, sha256, trigger} makes the
    final write also remember the file hash and append AUTO_CONNECTOR_SYNC — all in one atomic DB update."""
    t_all = time.perf_counter()
    lat: dict[str, int] = {}

    actor = as_actor
    if actor is None:
        with locked_db() as db:  # cheap authorization (+ persisted ACCESS_DENIED audit)
            actor = authorize(db, token, "L2", "stage_file", doc_name)
    if min_role not in ROLE_RANK:
        raise ApiError(400, "BAD_ROLE", "Ungültige Min-Role.")
    if ROLE_RANK[min_role] > ROLE_RANK[actor["role"]]:
        raise ApiError(403, "FORBIDDEN", f"Min-Role {min_role} übersteigt Ihre Clearance ({actor['role']}).")

    t = time.perf_counter()
    doc_title, chunks = chunk_document(text)
    if not chunks:
        raise ApiError(422, "EMPTY_DOCUMENT", "Im Dokument wurde kein auswertbarer Text gefunden.")
    chunks = chunks[:200]
    lat["chunking"] = int((time.perf_counter() - t) * 1000)

    db = read_db()
    live = db["live_chunks"]
    visible = [c for c in live if ROLE_RANK[c["minRole"]] <= ROLE_RANK[actor["role"]]]

    if replaces:
        series_docs = {c["docName"] for c in visible if c["docName"].lower() == replaces.lower()}
        if not series_docs:
            raise ApiError(404, "REPLACES_NOT_FOUND", f"Zieldokument „{replaces}“ ist nicht in der Live-DB.")
    else:
        series_docs = {c["docName"] for c in visible if same_series(c["docName"], doc_name)}
    series = [c for c in visible if c["docName"] in series_docs]

    t = time.perf_counter()
    eid, vecs = ensure_vectors(db)
    new_vecs = EMBEDDER.embed([c["content"] for c in chunks], "document", eid)
    if new_vecs is None:
        eid, vecs = ensure_vectors(db, force_hash=True)
        new_vecs = EMBEDDER.embed([c["content"] for c in chunks], "document", eid)
    if new_vecs is None:
        raise ApiError(503, "EMBED_FAILED", "Embeddings konnten nicht berechnet werden.")
    lat["embedding"] = int((time.perf_counter() - t) * 1000)
    prm = params_for(eid)

    # Red/Green diff: align each new chunk to at most one old chunk of the same document series.
    t = time.perf_counter()
    pairs: list[tuple[float, int, int]] = []
    for i, ch in enumerate(chunks):
        for j, old in enumerate(series):
            old_num = (re.match(r"§(\d+(?:\.\d+)*)", old["section"]) or [None, None])[1]
            if old_num is not None and old_num == ch["number"]:
                pairs.append((2.0, i, j))
            elif old["id"] in vecs:
                sim = float(np.dot(new_vecs[i], vecs[old["id"]]))
                if sim >= prm["align"]:
                    pairs.append((sim, i, j))
    pairs.sort(reverse=True)
    match: dict[int, int] = {}
    used_old: set[int] = set()
    for _, i, j in pairs:
        if i not in match and j not in used_old:
            match[i] = j
            used_old.add(j)

    hunks: list[dict[str, Any]] = []
    hunk_vecs: dict[str, np.ndarray] = {}
    unchanged = 0
    for i, ch in enumerate(chunks):
        old = series[match[i]] if i in match else None
        if old and old["content"].strip() == ch["content"].strip():
            unchanged += 1
            continue
        hid = f"h-{uuid.uuid4().hex[:8]}"
        section = ch["section"]
        if section.startswith("§0 Vorbemerkung"):  # headingless text inherits the replaced chunk's section (or the title)
            section = old["section"] if old else f"§1 {(title or doc_title or doc_name)[:40]}"
        hunks.append({
            "id": hid, "section": section, "minRole": min_role,
            "oldChunkId": old["id"] if old else None, "oldDocName": old["docName"] if old else None,
            "oldSection": old["section"] if old else None, "oldContent": old["content"] if old else None,
            "newContent": ch["content"],
        })
        hunk_vecs[hid] = new_vecs[i]
    lat["diff"] = int((time.perf_counter() - t) * 1000)
    if not hunks:
        raise ApiError(409, "NO_CHANGES", "Keine Änderungen gegenüber der Live-Version erkannt.")
    # pasted rule text (partial) updates single clauses; only complete documents may remove the clauses they no longer contain
    removed_chunk_ids = [] if partial else [c["id"] for j, c in enumerate(series) if j not in used_old]

    # Cross-document conflict scanner: vector pre-selection (cosine) → LLM verdict (temp 0.0, JSON) → rule fallback.
    t = time.perf_counter()
    others = [c for c in visible if c["docName"] not in series_docs and c["id"] in vecs]
    conflicts: dict[str, dict[str, Any]] = {}
    llm_budget, deadline = 8, time.monotonic() + 55
    if others:
        mat = np.stack([vecs[c["id"]] for c in others])
        for h in hunks:
            sims = mat @ hunk_vecs[h["id"]]
            for idx in np.argsort(-sims)[:3]:
                sim, cand = float(sims[idx]), others[int(idx)]
                if sim < prm["conflict_sim"] or time.monotonic() > deadline or topical_overlap(h["newContent"], cand["content"]) < 0.25:
                    continue
                a = {"doc": doc_name, "section": h["section"], "text": h["newContent"]}
                b = {"doc": cand["docName"], "section": cand["section"], "text": cand["content"]}
                if llm_budget > 0:
                    llm_budget -= 1
                    verdict, by = check_conflict(a, b)
                else:
                    verdict, by = _rule_conflict(a["text"], b["text"]), "rules"
                if not verdict["has_conflict"]:
                    continue
                shown = round(sim, 2)  # raw cosine similarity (no rescaling)
                prev = conflicts.get(cand["id"])
                if prev and prev["similarity"] >= shown:
                    continue
                conflicts[cand["id"]] = {
                    "id": f"cf-{uuid.uuid4().hex[:8]}", "chunkId": cand["id"], "docName": cand["docName"], "section": cand["section"],
                    "quote": verdict["conflicting_quote"] or cand["content"],
                    "message": f"Semantischer Konflikt erkannt: Dieses Update widerspricht {cand['docName']} ({cand['section'].split(' ')[0]}). {verdict['explanation']}".strip(),
                    "similarity": shown, "severity": verdict["severity"].lower(), "status": "open", "ticketId": None, "verifiedBy": by,
                }
    conflict_list = sorted(conflicts.values(), key=lambda c: -c["similarity"])[:6]
    lat["conflictScan"] = int((time.perf_counter() - t) * 1000)

    lat["total"] = int((time.perf_counter() - t_all) * 1000)
    label = engine_label(eid)
    replaces_doc = None
    if series_docs and match:
        replaces_doc = Counter(series[j]["docName"] for j in match.values()).most_common(1)[0][0]
    diff = {
        "id": f"diff-{uuid.uuid4().hex[:8]}",
        "title": (title or doc_title or doc_name)[:120],
        "docName": doc_name,
        "replacesDoc": replaces_doc,
        "author": stage_author_label(actor, author, as_actor is not None),
        "uploadedAt": now_iso(),
        "summary": f"{len(hunks)} geänderte/neue Chunk(s), {unchanged} unverändert, {len(conflict_list)} Konflikt(e) · Engine: {label}",
        "sha256": sha256("\n".join(h["newContent"] for h in hunks)),
        "targetCollection": COLLECTION,
        "status": "pending", "decidedAt": None, "decidedBy": None, "decisionNote": None,
        "hunks": hunks, "removedChunkIds": removed_chunk_ids, "conflicts": conflict_list,
    }
    with file_lock():
        db = read_db()
        duplicate = next((d for d in db["staged_diffs"] if d["status"] == "pending" and d["docName"] == doc_name and d["sha256"] == diff["sha256"]), None)
        if duplicate:  # idempotent re-submit (e.g. after a gateway timeout)
            return {"ok": True, "diff": duplicate, "duplicate": True, "stats": {
                "chunks": len(chunks), "changed": len(duplicate["hunks"]), "unchanged": unchanged, "conflicts": len(duplicate["conflicts"]),
                "engineMode": label, "latencyMs": lat}}
        live_now = {c["id"]: c["content"] for c in db["live_chunks"]}
        if any(h["oldChunkId"] and live_now.get(h["oldChunkId"]) != h["oldContent"] for h in hunks):
            raise ApiError(409, "STALE_BASE", "Die Live-Version hat sich während der Analyse geändert. Bitte erneut einreichen.")
        db["staged_diffs"].append(diff)
        append_audit(db, actor["email"], actor["role"], "POLICY_STAGED", doc_name, "SUCCESS",
                     f"{len(hunks)} Chunk(s), {len(conflict_list)} Konflikt(e), SHA-256 {diff['sha256'][:12]}…, Engine {label}")
        if connector:
            _record_connector_ingest(db, connector, doc_name, diff)
        with VECTORS.lock:
            VECTORS.reload()
            if VECTORS.engine == eid:
                for hid, v in hunk_vecs.items():
                    VECTORS.vecs[f"stg:{hid}"] = v
                VECTORS.save()
        write_db(db)
    return {"ok": True, "diff": diff, "stats": {
        "chunks": len(chunks), "changed": len(hunks), "unchanged": unchanged, "conflicts": len(conflict_list),
        "engineMode": label, "latencyMs": lat,
    }}


def stage_text(token: str | None, *, title: str, target_doc: str, new_text: str, min_role: str = "L1", author: str | None = None) -> dict[str, Any]:
    """Pasted-text variant (same semantics as the Node fallback): existing target ⇒ next version replaces it, else new document."""
    target = target_doc.strip()
    if not 3 <= len(title.strip()) <= 120:  # same limits as stagePolicy in src/lib/db.ts
        raise ApiError(400, "BAD_TITLE", "Titel: 3–120 Zeichen.")
    if not re.fullmatch(r"[\w.\-&,() ]{3,120}", target):
        raise ApiError(400, "BAD_TARGET", "Zieldokument: 3–120 Zeichen (Buchstaben, Ziffern, Leerzeichen, . _ - & , ( )).")
    if not 10 <= len(new_text.strip()) <= 2000:
        raise ApiError(400, "BAD_TEXT", "Regeltext: 10–2000 Zeichen.")
    existing = next((c for c in read_db()["live_chunks"] if c["docName"].lower() == target.lower()), None)
    if existing:
        return stage_document(token, doc_name=bump_version(existing["docName"]), text=new_text, title=title, min_role=min_role, author=author, replaces=existing["docName"], partial=True)
    name = target if re.search(r"\.[A-Za-z0-9]{1,5}$", target) else f"{target}.md"
    return stage_document(token, doc_name=name, text=new_text, title=title, min_role=min_role, author=author, partial=True)


# ───────────────────────── Zero-touch connector: watched_inbox → staging queue ─────────────────────────

INBOX_LOCK = threading.Lock()  # manual trigger and background thread must never stage the same file twice


def _inbox_files() -> list[Path]:
    if not INBOX_DIR.is_dir():
        return []
    return sorted(
        p for p in INBOX_DIR.iterdir()
        if p.is_file() and not p.is_symlink() and not p.name.startswith(".") and p.suffix.lower() in ALLOWED_EXT
    )


def _remember_ingested(file: str, digest: str) -> None:
    with locked_db() as db:
        db["meta"].setdefault("ingestedFiles", {})[file] = digest


def _ingest_file(path: Path, trigger: str) -> tuple[str, Any]:
    """Dedupe by hash / pending diff, then stage. Returns ("staged", diff) | ("skipped", reason) | ("failed", error)."""
    name = path.name
    doc_name = safe_doc_name(name)
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        return "failed", f"Datei größer als {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()

    db = read_db()
    if db["meta"].get("ingestedFiles", {}).get(name) == digest:
        return "skipped", "unverändert seit dem letzten Sync"
    if any(d["status"] == "pending" and d["docName"] == doc_name for d in db["staged_diffs"]):
        return "skipped", "bereits in der Staging-Queue"

    try:
        text = parse_document(name, data)
        result = stage_document(
            None, doc_name=doc_name, text=text, min_role="L2", author=CONNECTOR_AUTHOR,
            as_actor=CONNECTOR_ACTOR, connector={"file": name, "sha256": digest, "trigger": trigger},
        )
    except ApiError as e:
        if e.code != "NO_CHANGES":
            return "failed", e.error
        _remember_ingested(name, digest)
        return "skipped", "identisch mit der Live-Version"
    return "staged", result["diff"]


def sync_watched_inbox(token: str | None, *, system: bool = False) -> dict[str, Any]:
    """Scan backend/watched_inbox/ (.pdf .docx .md .txt) and stage new/modified files for CISO approval.
    `system=True` is used by the background watcher (no user token); manual triggers require L2+."""
    if system:
        trigger = "Auto-Sync (Hintergrund-Thread)"
    else:
        with locked_db() as db:
            who = authorize(db, token, "L2", "sync_inbox", "watched_inbox")
        trigger = f"{who['email']} ({who['role']})"

    read_db()  # DB_NOT_READY / DB_CORRUPT surface as one clear error instead of one failure per file
    report: dict[str, Any] = {"inbox": "backend/watched_inbox", "scanned": 0, "staged": [], "skipped": [], "failed": [], "engineMode": engine_label()}
    with INBOX_LOCK:
        files = _inbox_files()
        report["scanned"] = len(files)
        for path in files:
            try:
                kind, detail = _ingest_file(path, trigger)
            except Exception as e:  # noqa: BLE001 — one broken file must not stop the cycle
                print(f"[pocket-ciso] watched_inbox {path.name}: {type(e).__name__}: {e}")
                kind, detail = "failed", "Datei konnte nicht verarbeitet werden."
            if kind == "staged":
                report["staged"].append({"file": path.name, "diffId": detail["id"], "conflicts": len(detail["conflicts"])})
            elif kind == "skipped":
                report["skipped"].append({"file": path.name, "reason": detail})
            else:
                report["failed"].append({"file": path.name, "error": detail})
    return {"ok": True, "report": report}


def start_inbox_watcher() -> threading.Event | None:
    """AUTO_SYNC=1 → daemon thread that runs sync_watched_inbox every AUTO_SYNC_SECONDS (default 30). Returns the stop event."""
    if not AUTO_SYNC:
        return None
    stop = threading.Event()

    def loop() -> None:
        last_error = ""
        while not stop.wait(AUTO_SYNC_SECONDS):
            try:
                out = sync_watched_inbox(None, system=True)
                last_error = ""
                if out["report"]["staged"]:
                    print(f"[pocket-ciso] auto-sync staged: {', '.join(x['file'] for x in out['report']['staged'])}")
            except Exception as e:  # noqa: BLE001 — e.g. DB not initialised yet; log each distinct problem once
                msg = f"{type(e).__name__}: {getattr(e, 'error', e)}"
                if msg != last_error:
                    print(f"[pocket-ciso] auto-sync waiting: {msg}")
                last_error = msg

    threading.Thread(target=loop, name="inbox-watcher", daemon=True).start()
    return stop


def approve_diff(token: str | None, diff_id: str, override: bool = False, override_reason: str = "") -> dict[str, Any]:
    with locked_db() as db:
        actor = authorize(db, token, "L3", "approve", diff_id)
        idx = next((i for i, d in enumerate(db["staged_diffs"]) if d["id"] == diff_id and d["status"] == "pending"), -1)
        if idx < 0:
            raise ApiError(404, "NOT_FOUND", "Pending-Diff nicht gefunden.")
        diff = db["staged_diffs"][idx]
        stale = False
        for h in diff["hunks"]:
            if not h.get("oldChunkId"):
                continue
            current = next((c for c in db["live_chunks"] if c["id"] == h["oldChunkId"]), None)
            if current is None or current["content"] != h.get("oldContent"):
                stale = True
                break
        if not stale:
            stale = any(not any(c["id"] == old_id for c in db["live_chunks"]) for old_id in diff.get("removedChunkIds", []))
        if stale:
            append_audit(db, actor["email"], actor["role"], "MERGE_BLOCKED", diff["docName"], "DENIED", "Staging-Basis ist nicht mehr aktuell")
            raise ApiError(409, "STALE_DIFF", "Die Live-Version hat sich seit dem Staging geändert. Bitte erneut diffen.")
        open_conflicts = []
        for conflict in diff["conflicts"]:
            if conflict["status"] == "open":
                open_conflicts.append(conflict)
                continue
            ticket = next((t for t in db["tickets"] if t["id"] == conflict.get("ticketId")), None)
            if ticket is None or ticket["status"] != "resolved":
                open_conflicts.append(conflict)
        if open_conflicts and not override:
            ids = ", ".join(c["id"] for c in open_conflicts)
            append_audit(db, actor["email"], actor["role"], "MERGE_BLOCKED", diff["docName"], "DENIED", f"{len(open_conflicts)} ungelöste(r) Konflikt(e): {ids}")
            raise ApiError(409, "CONFLICT_UNRESOLVED", "Ungelöste Konflikte: Sync-Ticket erstellen und erledigen oder Override mit Begründung bestätigen.")
        override_note = (override_reason or "").strip()[:500]
        if open_conflicts and len(override_note) < 10:
            raise ApiError(400, "OVERRIDE_REASON_REQUIRED", "Für einen Override ist eine Begründung (mind. 10 Zeichen) erforderlich.")

        superseded, moved = 0, 0
        ts = now_iso()
        with VECTORS.lock:
            VECTORS.reload()
            removed_ids = set(diff.get("removedChunkIds", []))
            for h in diff["hunks"]:
                if h["oldChunkId"]:
                    before = len(db["live_chunks"])
                    db["live_chunks"] = [c for c in db["live_chunks"] if c["id"] != h["oldChunkId"]]
                    superseded += before - len(db["live_chunks"])
                    VECTORS.vecs.pop(h["oldChunkId"], None)
                new_id = f"chk-{uuid.uuid4().hex[:8]}"
                db["live_chunks"].append({
                    "id": new_id, "docName": diff["docName"], "section": h["section"], "minRole": h["minRole"], "content": h["newContent"],
                    "updatedAt": ts, "sha256": sha256(f"{diff['docName']}|{h['section']}|{h['newContent']}"), "approvedBy": actor["email"],
                })
                vec = VECTORS.vecs.pop(f"stg:{h['id']}", None)
                if vec is not None:
                    VECTORS.vecs[new_id] = vec
                    moved += 1
            if removed_ids:
                before = len(db["live_chunks"])
                db["live_chunks"] = [c for c in db["live_chunks"] if c["id"] not in removed_ids]
                superseded += before - len(db["live_chunks"])
                for old_id in removed_ids:
                    VECTORS.vecs.pop(old_id, None)
            if diff.get("replacesDoc"):
                for chunk in db["live_chunks"]:
                    if chunk["docName"] == diff["replacesDoc"]:
                        chunk["docName"] = diff["docName"]
                        chunk["sha256"] = sha256(f"{chunk['docName']}|{chunk['section']}|{chunk['content']}")
            refresh_lexical(db["live_chunks"])  # BM25 terms + spans are precomputed here, never per query
            VECTORS.save()
        db["staged_diffs"].pop(idx)
        db["meta"]["liveVersion"] += 1
        if open_conflicts:
            resolution = f"Override ({len(open_conflicts)} Konflikt akzeptiert: {', '.join(c['id'] for c in open_conflicts)}; Begründung: {override_note})"
        elif diff["conflicts"]:
            resolution = "Konflikte per Sync-Ticket adressiert (" + ", ".join(str(c["ticketId"]) for c in diff["conflicts"]) + ")"
        else:
            resolution = "keine Konflikte"
        append_audit(db, actor["email"], actor["role"], "APPROVED_AND_MERGED", diff["docName"], "SUCCESS",
                     f"{len(diff['hunks'])} Chunk(s) live, {superseded} ersetzt, {moved} Vektor(en) übernommen; {resolution}")
    return {"ok": True, "merged": len(diff["hunks"]), "superseded": superseded, "docName": diff["docName"]}


# ───────────────────────── Retrieval text analysis (normalisation, stemming, compounds, glossary) ─────────────────────────

_FOLD = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "ss", "é": "e", "è": "e", "á": "a", "à": "a"})


def fold(s: str) -> str:
    """NFKC (PDF ligatures 'ﬁ' → 'fi'), case folding, umlaut and ß folding."""
    return unicodedata.normalize("NFKC", s).casefold().translate(_FOLD)


# Function words, question words, auxiliaries and modal verbs (folded, DE + EN). Negations and modal verbs matter to the
# verifier, not to retrieval.
R_STOP = frozenset("""
ab aber alle allem allen aller alles als also am an andere anderen anderer anderes auch auf aus ausser bei beim bin bis
bisher bist bitte da dabei dadurch dafur dagegen daher damit danach dann daran darauf daraus darf darfst darin darum darunter
das dass davon dazu dem den denen denn der deren des dessen dich die dies diese diesem diesen dieser dieses dir doch dort du
durch durfen durfte durften ein eine einem einen einer eines einige einmal er es etwa etwas euch euer eure fur gab gibt gilt
gelten genau gemass gem geben habe haben hat hatte hatten hier hin ich ihm ihn ihnen ihr ihre ihrem ihren ihrer im in indem
ins ist ja je jede jedem jeden jeder jedes jemand jetzt kann kannst konnen konnte konnten lt laut machen macht man mehr mein
meine meinem meinen meiner meines mich mir mit muss musst mussen musste mussten nach nachdem neben noch nun nur ob oder ohne
per pro schon sehr sein seine seinem seinen seiner seit selbst sich sie sind so solche soll sollen sollte sollten sondern
sonst sowie sowohl tun uber um und uns unser unsere unserem unseren unserer unter viel viele vom von vor wahrend war waren
warum was weil welche welchem welchen welcher welches wem wen wenn wer werde werden wessen wie wieso wieviel wieviele weshalb
wir wird wo wobei wodurch wofur woher wohin womit worden wurde wurden zu zum zur zwar zwischen eigentlich gerne bzw nicht kein
keine keinem keinen keiner keines nie niemals oft wann lange denn mal bitte darfst eigene eigenen eigener
a about above after again against all also am an and any are as at be because been before being below between both but by
can cannot could did do does doing done down during each else ever every few for from further had has have having he her
here hers him his how however i if in into is it its itself just may me might more most must my no nor not now of off on
once only or other our ours out over own per please same shall she should so some such than that the their theirs them then
there these they this those through to too under until up us very was we were what when where which while who whom whose why
will with within without would you your yours according regarding exactly exact many much often long allowed
""".split())
# Document-type words: weak evidence for a topic (weight 0.3), still useful to rank the referenced document.
GENERIC_WORDS = frozenset("richtlinie richtlinien policy policies unternehmen company firma organisation regel regeln vorgabe vorgaben dokument document".split())

UNIT_CANON: dict[str, str] = {}
for _canon, _words in {
    "h": "h std stunde stunden hour hours hr hrs", "min": "min minute minuten minutes mins", "s": "sek sekunde sekunden second seconds sec",
    "wd": "werktag werktage werktagen", "d": "tag tage tagen day days", "w": "woche wochen week weeks", "mo": "monat monate monaten month months",
    "y": "jahr jahre jahren year years", "chr": "zeichen character characters chars", "pct": "prozent percent", "eur": "eur euro",
    "mb": "mb", "gb": "gb", "tb": "tb",
}.items():
    for _w in _words.split():
        UNIT_CANON[_w] = _canon
FREQ_WORDS = {"jahrlich": "12mo", "annual": "12mo", "annually": "12mo", "yearly": "12mo", "halbjahrlich": "6mo", "semiannual": "6mo",
              "vierteljahrlich": "3mo", "quarterly": "3mo", "monatlich": "1mo", "monthly": "1mo", "wochentlich": "7d", "weekly": "7d",
              "taglich": "1d", "daily": "1d", "biennial": "24mo"}
_LEX = re.compile(r"\d+(?:[.,]\d+)*|[a-z]+|%")


def _num(tok: str) -> str:
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", tok):  # 250.000 / 50,000 → 250000 / 50000
        return re.sub(r"[.,]", "", tok).lstrip("0") or "0"
    tok = tok.replace(",", ".")
    return tok if "." in tok else (tok.lstrip("0") or "0")


def _unit_at(toks: list[str], k: int) -> str | None:
    if k >= len(toks):
        return None
    return "pct" if toks[k] == "%" else UNIT_CANON.get(toks[k])


def _qty(value: str, unit: str) -> str:
    """Canonical quantity term: years → months, weeks → days ("1 year" = "12 Monate", "2 Jahre" = "24 Monate")."""
    factor = {"y": ("mo", 12), "w": ("d", 7)}.get(unit)
    if factor:
        try:
            v = float(value) * factor[1]
            value, unit = (str(int(v)) if v.is_integer() else str(v)), factor[0]
        except ValueError:
            pass
    return f"{value}{unit}"


def _cue(unit: str) -> str:
    return {"y": "mo", "w": "d"}.get(unit, unit)


@lru_cache(maxsize=50_000)
def cistem(w: str) -> str:
    """CISTEM stemmer (Weißweiler & Fraser 2017), case-insensitive variant; input is already folded."""
    if len(w) <= 3:
        return w
    participle = (w.startswith("ge") and len(w) >= 6) or w.endswith("et")
    w = re.sub(r"^ge(.{4,})", r"\1", w)
    w = w.replace("sch", "$").replace("ei", "%").replace("ie", "&")
    w = re.sub(r"(.)\1", r"\1*", w)
    while len(w) > 3:
        if len(w) > 5:
            w2 = re.sub(r"e[mr]$", "", w)
            if w2 == w:
                w2 = re.sub(r"nd$", "", w)
            if w2 != w:
                w = w2
                continue
        w2 = w
        if participle:  # case-insensitive variant: only participles lose their final -t (gemeldet ~ melden), once
            w2, participle = re.sub(r"t$", "", w), False
        if w2 == w:
            w2 = re.sub(r"[esn]$", "", w)
        if w2 == w:
            break
        w = w2
    w = re.sub(r"(.)\*", r"\1\1", w)
    w = w.replace("$", "sch").replace("%", "ei").replace("&", "ie")
    return w[:-3] if w.endswith("ung") and len(w) >= 7 else w  # Meldung ~ melden, Leitung ~ leitet


class Vocab:
    """Compound splitting against the corpus vocabulary: longest known head, optional linking element, known or recursively
    split remainder (an unknown inflected tail ≥ 5 chars is kept): 'wiederanlaufzeiten' → wiederanlauf + zeiten."""

    def __init__(self, words: Iterable[str]) -> None:
        self.words = frozenset(w for w in words if len(w) >= 4 and w not in R_STOP)
        self.stems = frozenset(cistem(w) for w in self.words)
        self._memo: dict[str, list[str]] = {}

    def split(self, w: str) -> list[str]:
        if w not in self._memo:
            parts = self._split(w, 0)
            self._memo[w] = parts if len(parts) > 1 else []
        return self._memo[w]

    def _split(self, w: str, depth: int) -> list[str]:
        fallback: list[str] | None = None
        for i in range(len(w) - 4, 3, -1):
            head = w[:i]
            if head not in self.words:
                continue
            rest = w[i:]
            for link in ("", "s", "es", "n", "en"):
                if not rest.startswith(link) or len(rest) - len(link) < 4:
                    continue
                r = rest[len(link):]
                if r in self.words:
                    return [head, r]
                if depth < 2:
                    sub = self._split(r, depth + 1)
                    if len(sub) > 1:
                        return [head] + sub
            if fallback is None and len(rest) >= 5:
                fallback = [head, rest]
        if fallback is None and depth == 0:  # unknown head, known (inflected) tail: "firmenlaptop" → firmen + laptop
            for i in range(4, len(w) - 3):
                if cistem(w[i:]) in self.stems and w[i:] not in R_STOP:
                    return [w[:i], w[i:]]
        return fallback or [w]


def analyze(text: str, vocab: Vocab | None = None) -> list[str]:
    """Index/query terms: stems, compound parts, numbers ('50000'), quantities ('24h', '12mo'), unit cues ('u:h') and
    identifiers ('tier2', 'tls1.3', 'aes256')."""
    toks = _LEX.findall(fold(text))
    out: list[str] = []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t[0].isdigit():
            v = _num(t)
            out.append(v)
            u = _unit_at(toks, i + 1)
            if u:
                out += [_qty(v, u), "u:" + _cue(u)]
                i += 2
                continue
        elif t != "%":
            if t in FREQ_WORDS:
                out.append(FREQ_WORDS[t])
            if t in UNIT_CANON and len(t) > 3:
                out.append("u:" + _cue(UNIT_CANON[t]))
            elif len(t) >= 2 and t not in R_STOP:
                if i + 1 < n and toks[i + 1][0].isdigit() and not _unit_at(toks, i + 2):
                    out.append(t + _num(toks[i + 1]))
                out.append(cistem(t))
                if vocab is not None and len(t) >= 8:
                    out += [cistem(p) for p in vocab.split(t)]
        i += 1
    return out


def _edit_distance(a: str, b: str, cap: int) -> int:
    """Damerau-Levenshtein (optimal string alignment) with early exit above `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


GLOSSARY_PATH = Path(__file__).resolve().parent / "glossary.json"


class Glossary:
    """backend/glossary.json: groups of interchangeable domain terms (DE/EN pairs, abbreviations)."""

    def __init__(self, path: Path) -> None:
        try:
            raw = path.read_bytes()
            groups = json.loads(raw).get("groups", [])
        except (OSError, ValueError):
            raw, groups = b"", []
        self.digest = hashlib.sha256(raw).hexdigest()[:16]
        self.words = {w for g in groups for m in g for w in _LEX.findall(fold(m)) if w.isalpha()}
        self.groups: list[list[frozenset[str]]] = []
        self.single: dict[str, list[int]] = defaultdict(list)
        self.multi: list[tuple[frozenset[str], int]] = []
        for g in groups:
            members = [frozenset(t for t in analyze(m) if not t.startswith("u:")) for m in g]
            members = [m for m in members if m]
            gi = len(self.groups)
            self.groups.append(members)
            for m in members:
                if len(m) == 1:
                    self.single[next(iter(m))].append(gi)
                else:
                    self.multi.append((m, gi))


GLOSSARY = Glossary(GLOSSARY_PATH)


# ───────────────────────── Lexical index (built at approval time, persisted next to the vectors) ─────────────────────────

INDEX_VERSION = "lex-3"
BM25_K1, BM25_B = 1.2, 0.75
RRF_K = 60


def section_title(section: str) -> str:
    return re.sub(r"\s*\(Teil \d+\)$", "", re.sub(r"^§\S*\s*", "", section))


def doc_title(doc_name: str) -> str:
    return re.sub(r"[_]+", " ", re.sub(r"\.[A-Za-z0-9]{1,5}$", "", doc_name))


def corpus_signature(chunks: list[dict[str, Any]]) -> str:
    h = hashlib.sha256(f"{INDEX_VERSION}|{GLOSSARY.digest}".encode())
    for c in sorted(chunks, key=lambda x: x["id"]):
        h.update(f"\x1e{c['id']}\x1f{c['docName']}\x1f{c['section']}\x1f{c['minRole']}\x1f{sha256(c['content'])}".encode())
    return h.hexdigest()


class LexIndex:
    def __init__(self, chunks: list[dict[str, Any]], payload: dict[str, Any] | None = None) -> None:
        self.chunks = sorted(chunks, key=lambda c: c["id"])
        self.sig = corpus_signature(self.chunks)
        if payload and payload.get("sig") == self.sig:
            self.words = set(payload["words"])
            self.doc_terms = [list(t) for t in payload["terms"]]
            self.span_text = [list(s) for s in payload["spanText"]]
            self.span_terms = [[frozenset(t) for t in s] for s in payload["spanTerms"]]
            self.ctx_terms = [frozenset(t) for t in payload["ctx"]]
        else:
            self.words = {w for c in self.chunks for w in _LEX.findall(fold(f"{c['content']} {c['section']} {doc_title(c['docName'])}")) if w.isalpha()}
            vocab = Vocab(self.words | GLOSSARY.words)
            self.doc_terms, self.span_text, self.span_terms, self.ctx_terms = [], [], [], []
            for c in self.chunks:
                ctx = f"{section_title(c['section'])} {doc_title(c['docName'])}"
                self.doc_terms.append(analyze(f"{c['content']} {ctx}", vocab))
                spans = split_sentences(c["content"]) or [c["content"]]
                self.span_text.append(spans)
                self.span_terms.append([frozenset(analyze(s, vocab)) for s in spans])
                self.ctx_terms.append(frozenset(analyze(ctx, vocab)))
        self.pos = {c["id"]: i for i, c in enumerate(self.chunks)}
        # spans: stable ids "<chunk id>:<k>", instruction-like text flagged (never used in answers), section sequences across
        # neighbouring chunks ("§2 X" and "§2 X (Teil 2)") for context windows
        self.span_ids = [[f"{c['id']}:{k}" for k in range(len(self.span_text[i]))] for i, c in enumerate(self.chunks)]
        self.span_instr = [[bool(INSTRUCTION_RE.search(fold(t))) for t in spans] for spans in self.span_text]
        self.section_key = [(c["docName"], section_title(c["section"])) for c in self.chunks]
        self.section_seq: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
        part_no = [int((re.search(r"\(Teil (\d+)\)$", c["section"]) or [0, 1])[1]) for c in self.chunks]
        for i in sorted(range(len(self.chunks)), key=lambda x: (self.section_key[x], part_no[x], self.chunks[x]["id"])):
            self.section_seq[self.section_key[i]] += [(i, k) for k in range(len(self.span_text[i]))]
        self.tf = [Counter(t) for t in self.doc_terms]
        self.dl = [len(t) for t in self.doc_terms]
        self._views: dict[str, RoleView] = {}

    def payload(self) -> dict[str, Any]:
        return {"version": INDEX_VERSION, "sig": self.sig, "words": sorted(self.words), "terms": self.doc_terms, "spanText": self.span_text,
                "spanTerms": [[sorted(t) for t in s] for s in self.span_terms], "ctx": [sorted(t) for t in self.ctx_terms]}

    def view(self, role: str) -> "RoleView":
        if role not in self._views:
            self._views[role] = RoleView(self, role)
        return self._views[role]


class RoleView:
    """Everything a role's query may touch: RBAC filter BEFORE scoring — document frequencies, vocabulary (compound
    splitting, typo correction) and candidates come from the chunks the role may read only."""

    def __init__(self, idx: LexIndex, role: str) -> None:
        self.idx = idx
        self.allowed = [i for i, c in enumerate(idx.chunks) if ROLE_RANK[c["minRole"]] <= ROLE_RANK[role]]
        self.n = len(self.allowed)
        self.df: Counter[str] = Counter()
        words: set[str] = set()
        for i in self.allowed:
            self.df.update(set(idx.doc_terms[i]))
            words |= set(_LEX.findall(fold(f"{idx.chunks[i]['content']} {idx.chunks[i]['section']} {doc_title(idx.chunks[i]['docName'])}")))
        self.vocab = Vocab({w for w in words if w.isalpha()} | GLOSSARY.words)
        self.avgdl = (sum(idx.dl[i] for i in self.allowed) / self.n) if self.n else 1.0
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i in self.allowed:
            for t, f in idx.tf[i].items():
                self.postings[t].append((i, f))
        self.stems_by_initial: dict[str, list[str]] = defaultdict(list)
        for t in sorted(self.df):
            if t.isalpha() and len(t) >= 4:
                self.stems_by_initial[t[0]].append(t)

    def idf(self, t: str) -> float:
        df = self.df.get(t, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def bm25(self, weights: dict[str, float]) -> dict[int, float]:
        scores: dict[int, float] = defaultdict(float)
        for t, w in sorted(weights.items()):
            for i, f in self.postings.get(t, ()):
                norm_len = f + BM25_K1 * (1 - BM25_B + BM25_B * self.idx.dl[i] / self.avgdl)
                scores[i] += w * self.idf(t) * f * (BM25_K1 + 1) / norm_len
        return scores

    def fuzzy(self, stem_: str) -> str | None:
        """Typo tolerance: the closest known stem (edit distance ≤ 1, ≤ 2 for long words); ties → higher df, then alphabetical."""
        if len(stem_) < 4:
            return None
        cap = 2 if len(stem_) >= 8 else 1
        best: tuple[int, int, str] | None = None
        for cand in self.stems_by_initial.get(stem_[0], ()):
            d = _edit_distance(stem_, cand, cap)
            if d <= cap:
                key = (d, -self.df[cand], cand)
                if best is None or key < best:
                    best = key
        return best[2] if best else None


_LEX_CACHE: LexIndex | None = None


def lexical_index(db: dict[str, Any]) -> LexIndex:
    """mtime/signature-checked: reuses the in-memory index, else the persisted one (vectors.json → 'lexical'), else rebuilds."""
    global _LEX_CACHE
    sig = corpus_signature(db["live_chunks"])
    if _LEX_CACHE is not None and _LEX_CACHE.sig == sig:
        return _LEX_CACHE
    with VECTORS.lock:
        VECTORS.reload()
        idx = LexIndex(db["live_chunks"], VECTORS.lexical)
        if VECTORS.lexical is None or VECTORS.lexical.get("sig") != sig:
            VECTORS.lexical = idx.payload()
            VECTORS.save()
    _LEX_CACHE = idx
    return idx


def refresh_lexical(live_chunks: list[dict[str, Any]]) -> None:
    """Approval time: rebuild and persist the lexical index (caller holds VECTORS.lock and saves afterwards)."""
    global _LEX_CACHE
    idx = LexIndex(live_chunks)
    VECTORS.lexical = idx.payload()
    _LEX_CACHE = idx


# ───────────────────────── Query analysis: key terms + glossary expansion ─────────────────────────

@dataclass
class KeyTerm:
    surface: str
    kind: str  # word | phrase | ident | qty | num
    alts: list[frozenset[str]]  # matched when ANY alternative is fully contained; alts[0] is the question's own form
    weight: float = 1.0


@dataclass
class QueryTerms:
    keys: list[KeyTerm]
    weights: dict[str, float]  # BM25 query term weights (own form 1.0, compound parts 0.8, glossary/typo alternatives 0.5)
    unit_cues: set[str]


def key_terms(question: str, view: RoleView) -> QueryTerms:
    toks = _LEX.findall(fold(question))
    # German capitalises nouns: when the question uses casing, lower-case words after the first one (verbs, adjectives)
    # count half; an all-lower-case question keeps equal weights.
    words_cs = re.findall(r"[A-Za-zÄÖÜäöüß]+", question)
    folded_words = [fold(w) for w in words_cs]
    german = sum(w in DE_HINTS for w in folded_words) > sum(w in EN_HINTS for w in folded_words)
    cased = german and any(w[:1].isupper() for w in words_cs[1:])
    lower = {fold(w) for w in words_cs[1:] if w[:1].islower()} if cased else set()
    items: list[KeyTerm] = []
    cues: set[str] = set()
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t[0].isdigit():
            v = _num(t)
            u = _unit_at(toks, i + 1)
            if u:
                items.append(KeyTerm(f"{t} {toks[i + 1]}", "qty", [frozenset({_qty(v, u)})]))
                cues.add(_cue(u))
                i += 2
                continue
            items.append(KeyTerm(t, "num", [frozenset({v})]))
        elif t != "%":
            if t in UNIT_CANON and len(t) > 3:
                cues.add(_cue(UNIT_CANON[t]))
            elif t in FREQ_WORDS:
                items.append(KeyTerm(t, "word", [frozenset({cistem(t)}), frozenset({FREQ_WORDS[t]})]))
            elif len(t) >= 2:
                w = t
                if w not in R_STOP and re.search(r"ae|oe|ue", w):  # "fuer", "Geschaeftsfuehrung" typed without umlauts
                    alt = w.replace("ae", "a").replace("oe", "o").replace("ue", "u")
                    if alt in R_STOP or cistem(alt) in view.df:
                        w = alt
                if w in R_STOP or (len(w) >= 5 and cistem(w) not in view.df  # typo of a function word ("wiviele")
                                   and any(_edit_distance(w, s, 1) <= 1 for s in R_STOP if abs(len(s) - len(w)) <= 1 and len(s) >= 4)):
                    i += 1
                    continue
                if i + 1 < n and toks[i + 1][0].isdigit() and not _unit_at(toks, i + 2):
                    items.append(KeyTerm(f"{t} {toks[i + 1]}", "ident", [frozenset({w + _num(toks[i + 1])})]))
                    i += 2
                    continue
                s = cistem(w)
                alts = [frozenset({s})]
                if len(w) >= 8:
                    parts = view.vocab.split(w)
                    if parts:
                        alts.append(frozenset(cistem(p) for p in parts))
                items.append(KeyTerm(t, "word", alts, 0.3 if w in GENERIC_WORDS else 0.5 if t in lower else 1.0))
        i += 1

    # glossary: multi-word members first (consume consecutive word items), then single-term members
    merged: list[KeyTerm] = []
    k = 0
    while k < len(items):
        hit = None
        for member, gi in GLOSSARY.multi:
            span = len(member)
            window = items[k : k + span]
            if len(window) == span and all(x.kind == "word" for x in window) and {next(iter(x.alts[0])) for x in window} == set(member):
                hit = (gi, window)
                break
        if hit:
            gi, window = hit
            merged.append(KeyTerm(" ".join(x.surface for x in window), "phrase", [frozenset().union(*(x.alts[0] for x in window))] + [m for m in GLOSSARY.groups[gi]]))
            k += len(hit[1])
            continue
        merged.append(items[k])
        k += 1
    for it in merged:
        if it.kind in ("word", "ident", "num"):
            for gi in GLOSSARY.single.get(next(iter(it.alts[0])), []):
                it.alts += [m for m in GLOSSARY.groups[gi] if m not in it.alts]
        if it.kind == "word" and not any(all(x in view.df for x in a) for a in it.alts):
            near = view.fuzzy(next(iter(it.alts[0])))
            if near:
                it.alts.append(frozenset({near}))
                for gi in GLOSSARY.single.get(near, []):
                    it.alts += [m for m in GLOSSARY.groups[gi] if m not in it.alts]

    weights: dict[str, float] = {}
    for it in merged:
        own = it.alts[0]
        it.weight *= sum(view.idf(x) for x in own) / len(own)
        for a_i, alt in enumerate(it.alts):
            w = (1.0 if a_i == 0 else 0.8 if (a_i == 1 and it.kind == "word" and len(alt) > 1) else 0.5) * (0.3 if it.surface in GENERIC_WORDS else 1.0)
            for x in alt:
                weights[x] = max(weights.get(x, 0.0), w)
    return QueryTerms(merged, weights, cues)


def coverage(qt: QueryTerms, terms: frozenset[str], ctx: frozenset[str] = frozenset()) -> float:
    """Weighted share of the question's key terms present in `terms` (context-only matches — section/document title — count half)."""
    total = sum(k.weight for k in qt.keys)
    if total <= 0:
        return 0.0
    got = 0.0
    for k in qt.keys:
        if any(a <= terms for a in k.alts):
            got += k.weight
        elif any(a <= ctx for a in k.alts):
            got += 0.5 * k.weight
    return got / total


# ───────────────────────── Dense retrieval + fusion ─────────────────────────

_QVEC_CACHE: dict[tuple[str, str], np.ndarray] = {}
_DB_CACHE: dict[str, Any] = {"key": None, "db": None}


def reset_query_caches() -> None:
    _QVEC_CACHE.clear()
    _DB_CACHE.update(key=None, db=None)


def cached_db() -> dict[str, Any]:
    """Read-only DB view for queries; re-parsed only when the file's mtime/size changed."""
    try:
        st = DB_PATH.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is None or key != _DB_CACHE["key"]:
        _DB_CACHE.update(db=read_db(), key=key)
    return _DB_CACHE["db"]


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
    """Reciprocal rank fusion (k = 60); deterministic tie-break by chunk id."""
    fused: dict[int, float] = defaultdict(float)
    for sc in scores:
        if not sc:
            continue
        ranking = sorted((i for i in sc if sc[i] > 0), key=lambda i: (-sc[i], view.idx.chunks[i]["id"]))
        for r, i in enumerate(ranking):
            fused[i] += 1.0 / (RRF_K + r + 1)
    return sorted(fused, key=lambda i: (-fused[i], view.idx.chunks[i]["id"]))


# ───────────────────────── Pipeline B: stateless compliance RAG (extract first, verify every sentence) ─────────────────────────

NO_ANSWER = "Keine belegbare Antwort im freigegebenen Bestand."
COV_MIN = float(os.getenv("COV_MIN", "0.5"))  # replaced by the feature gate in step 5
MAX_PARTS = 4

# Text addressed to the assistant instead of being policy content ("ignore all previous instructions", "answer only …").
INSTRUCTION_RE = re.compile(
    r"(\bignor\w*\s+(?:alle\w*|all|any|the|previous|prior|vorherig\w*|bisherig\w*|obig\w*)\b|\bvergiss\s+(?:alle\w*|die|deine|your)\b"
    r"|\bforget\s+(?:all|everything|your|the)\b|system\s*-?\s*prompt|^\s*system\s*:|\bdu bist (?:jetzt|nun|ab sofort)\b|\byou are now\b"
    r"|\b(?:antworte|answer|respond|reply)\w*\b.{0,40}\b(?:nur|only|ausschliesslich|exclusively|everything|alles|jede\w*|every)\b"
    r"|\bact as\b|\bpretend\b|\bassistenzsystem\w*|\bjailbreak\b)", re.I)

INTERROG = (r"(?:wie|wer|wem|wen|was|wo|wohin|woher|wann|welche\w*|warum|wieso|weshalb|ob|darf|durfen|muss|mussen|ist|sind|gibt|gilt|kann|"
            r"konnen|soll|sollen|braucht|brauchen|bis|ab|innerhalb|binnen|nach|how|who|whom|what|where|when|which|why|whether|is|are|do|does|"
            r"did|can|could|must|may|should|shall|will|within)")
PART_SPLIT = re.compile(r"(?<=[?!.;:])\s+|,?\s+(?:und|sowie|oder|and|or)\s+(?=" + INTERROG + r"\b)|,\s+(?=(?:wie|wer|was|wo|wann|welche\w*|darf|durfen|muss|mussen|how|who|what|where|when|which)\b)")
QUESTION_START = re.compile(r"^\s*(?:und\s+|and\s+)?" + INTERROG + r"\b")
VALUE_Q = re.compile(r"\b(?:wie (?:lange?|viele?\w*|oft|schnell|hoch|gross|fruh)|bis wann|ab wann|ab welche\w*|bis zu welche\w*|innerhalb welche\w*|"
                     r"binnen welche\w*|in welche\w* (?:frist|zeit|intervall)|welche\w* (?:frist|intervall|wert|hohe|dauer|zeit|lange|mindestlange|gultigkeit)|"
                     r"nach wie viel\w*|wieviele?|how (?:long|many|much|often|quickly|soon|fast)|within how|what is the (?:maximum|minimum|deadline|sla|interval)|"
                     r"which (?:deadline|interval|sla)|frist|deadline|intervall|interval)\b")
WHO_Q = re.compile(r"\b(?:wer|wem|wen|who|whom)\b")
# yes/no questions about permission or obligation are answered by a normative sentence (muss, darf, verboten, …)
NORM_Q = re.compile(r"^(?:brauche\w*|can|am i|is it|are we|do i|do we)\b|\b(?:darf|durfen|muss|mussen|soll|sollen|may|must|erlaubt|zulassig|gestattet|verboten|untersagt|allowed|permitted|prohibited|forbidden|pflicht\w*|mandatory|required)\b")
PROHIBITION_RE = re.compile(r"\b(?:untersagt\w*|verbot\w*|unzulassig\w*|prohibit\w*|forbidden|never|nie|niemals)\b")
ANAPHOR = re.compile(r"^(?:er|sie|es|dies\w*|dabei|dazu|hierbei|this|these|it|they|he|she)\b", re.I)
# Named roles and bodies: acronyms (CISO, BSI, SOC) and role nouns, also as the tail of a compound ("Küchenleitung", "CISO-Office")
ENTITY_RE = re.compile(r"\b(?:[A-ZÄÖÜ]{2,}[A-Za-z0-9]*(?:-[A-Za-zÄÖÜäöü]+)*|[\wÄÖÜäöüß-]*(?i:office|team|owner|leiter\w*|leitung|lead|admin\w*|beauftragte\w*|"
                       r"commander|desk|führung|vorstand|vorgesetzte\w*|behörde\w*|counsel|betrieb|mitarbeit\w+|dienstleister\w*|führungskräfte|"
                       r"benutzer\w*|nutzer\w*|employee\w*|staff|personal))\b")
NEG_RE = re.compile(r"\b(?:nicht|kein\w*|nie|niemals|ohne|untersagt\w*|verbot\w*|unzulassig\w*|ausgeschlossen|not|no|never|without|prohibit\w*|forbidden|cannot)\b")
MODAL_CLASSES = {
    "obligation": re.compile(r"\b(?:muss|mussen|musst|zwingend|verpflichtend\w*|verpflichtet|pflicht\w*|erforderlich|notwendig|must|shall|mandatory|required|requires|obligatory)\b"),
    "permission": re.compile(r"\b(?:darf|durfen|erlaubt|gestattet|zulassig|may|allowed|permitted|permits|authorized)\b"),
    "recommendation": re.compile(r"\b(?:soll|sollen|sollte|sollten|empfohlen|empfiehlt|should|recommended)\b"),
    "possibility": re.compile(r"\b(?:kann|konnen|can|could)\b"),
}
TABLE_LABEL = re.compile(r"^Tabelle \[[^\]]*\]:\s*")
YES_NO = re.compile(r"^\s*(?:ja|nein|yes|no)\b")
# German question detection for the noun-casing weight (English nouns are lower case): German vs English function words
DE_HINTS = frozenset("der die das den dem des und ist sind wie wer was wo wann ich wir nicht muss mussen darf durfen fur mit von bei welche welcher welches nach im ein eine einen".split())
EN_HINTS = frozenset("the is are what who how where when and of for to with must may can does do which an a in on".split())


def display_span(text: str) -> str:
    """Span as shown in an answer: the generated 'Tabelle [§…]:' prefix is a chunker label, not document text."""
    return TABLE_LABEL.sub("", text)


def modal_classes(text: str) -> frozenset[str]:
    f = fold(text)
    return frozenset(k for k, rx in MODAL_CLASSES.items() if rx.search(f))


def entities(text: str) -> set[str]:
    return {e.strip("-") for e in ENTITY_RE.findall(text) if len(e.strip("-")) >= 2}


def numeric_terms(terms: Iterable[str]) -> set[str]:
    """Numbers, quantities ('24h') and identifiers ('tier2', 'tls1.3') — must be reproduced exactly."""
    return {t for t in terms if t[:1].isdigit() or re.fullmatch(r"[a-z]+\d[\d.]*", t)}


@dataclass
class Part:
    text: str
    qt: QueryTerms
    value: bool
    who: bool
    windows: list[list[tuple[int, int]]]
    norm: bool = False
    coverage: float = 0.0
    answered: bool = False
    reason: str = ""


def question_parts(question: str) -> tuple[list[str], list[str], bool]:
    """→ (question parts, context sentences, injection_removed). Instructions to the assistant are dropped before analysis."""
    pieces = [p.strip(" ,;:") for p in PART_SPLIT.split(question) if p and p.strip(" ,;:")]
    clean = [p for p in pieces if not INSTRUCTION_RE.search(fold(p))]
    removed = len(clean) < len(pieces)
    parts = [p for p in clean if QUESTION_START.match(fold(p)) or p.rstrip().endswith("?")]
    context = [p for p in clean if p not in parts]
    if not parts and context:
        parts, context = context, []
    return parts[:MAX_PARTS], context, removed


def span_windows(idx: "LexIndex", view: RoleView, chunk_ids: list[int]) -> list[list[tuple[int, int]]]:
    """Candidate windows: one span, or a span with up to two preceding spans of the same section (neighbouring chunks included)."""
    seen: set[tuple[tuple[int, int], ...]] = set()
    out: list[list[tuple[int, int]]] = []
    allowed = set(view.allowed)
    for i in chunk_ids:
        seq = [s for s in idx.section_seq[idx.section_key[i]] if s[0] in allowed and not idx.span_instr[s[0]][s[1]]]
        for pos, anchor in enumerate(seq):
            if anchor[0] != i:
                continue
            if pos + 1 < len(seq) and ANAPHOR.match(idx.span_text[seq[pos + 1][0]][seq[pos + 1][1]]) and \
                    not idx.span_text[anchor[0]][anchor[1]].startswith("Tabelle ["):
                fw = (anchor, seq[pos + 1])
                if fw not in seen:
                    seen.add(fw)
                    out.append(list(fw))
            for back in range(0, 3):
                if pos - back < 0:
                    break
                w = tuple(seq[pos - back : pos + 1])
                if back and any(idx.span_text[x][y].startswith("Tabelle [") for x, y in w):
                    break  # table rows are self-contained statements; context windows are for running text only
                if w not in seen:
                    seen.add(w)
                    out.append(list(w))
    return out


def window_terms(idx: "LexIndex", w: list[tuple[int, int]]) -> frozenset[str]:
    return frozenset().union(*(idx.span_terms[i][k] for i, k in w))


def window_ok(idx: "LexIndex", part: Part, w: list[tuple[int, int]]) -> bool:
    """A value question needs a number/quantity in the window, a 'who' question a named role or body."""
    text = " ".join(idx.span_text[i][k] for i, k in w)
    if part.value and not numeric_terms(window_terms(idx, w)) and not any(x in FREQ_WORDS for x in _LEX.findall(fold(text))):
        return False
    if part.who and not entities(text):
        return False
    return True


def select_windows(idx: "LexIndex", view: RoleView, part: Part, ranked: list[int]) -> None:
    rank_of = {i: r for r, i in enumerate(ranked)}
    cands = span_windows(idx, view, ranked)

    def key(w: list[tuple[int, int]]) -> tuple[float, float, int, int, str]:
        cov = coverage(part.qt, window_terms(idx, w), idx.ctx_terms[w[-1][0]])
        rank = min(rank_of.get(i, len(ranked)) for i, _ in w)
        text = fold(" ".join(idx.span_text[i][k] for i, k in w))
        normative = 0.08 if part.norm and (modal_classes(text) or PROHIBITION_RE.search(text)) else 0.0
        # coverage first (+ small bonuses: better fused rank, normative sentence for yes/no questions), then shorter
        # window, rank, span id (deterministic)
        return (-(cov + 0.1 / (1 + rank) + normative), -cov, len(w), rank, idx.span_ids[w[-1][0]][w[-1][1]])

    scored = []
    for w in cands:
        alone = coverage(part.qt, idx.span_terms[w[-1][0]][w[-1][1]], idx.ctx_terms[w[-1][0]])
        if alone <= 0:
            continue  # the answering span itself must match the question
        if len(w) > 1 and coverage(part.qt, window_terms(idx, w), idx.ctx_terms[w[-1][0]]) < alone + 0.1:
            continue  # a preceding span must add coverage, otherwise the shorter window wins
        scored.append((key(w), w))
    scored.sort(key=lambda x: x[0])
    good = [w for _, w in scored if window_ok(idx, part, w)]
    if not good:
        part.reason = "kein Wert/keine Rolle in den Fundstellen" if scored else "keine Fundstelle"
        return
    first = good[0]
    chosen = [first]
    got = window_terms(idx, first)
    base = coverage(part.qt, got, idx.ctx_terms[first[-1][0]])
    for w in good[1:]:
        if set(w) & set(first):
            continue
        gain = coverage(part.qt, got | window_terms(idx, w)) - coverage(part.qt, got)
        close = idx.section_key[w[-1][0]] != idx.section_key[first[-1][0]] and \
            coverage(part.qt, window_terms(idx, w), idx.ctx_terms[w[-1][0]]) >= 0.9 * base
        if gain >= 0.2 or close:  # complementary statement, or an equally good one from another section
            chosen.append(w)
            got = got | window_terms(idx, w)
            break
    part.windows = chosen
    part.coverage = max(base, coverage(part.qt, got, idx.ctx_terms[first[-1][0]]))


# ── answer sentences + verifier ──

PHRASE_SYSTEM = (
    "Du formulierst eine Antwort ausschließlich aus nummerierten, freigegebenen Textstellen. Regeln: höchstens drei Sätze; "
    "jeder Satz endet mit den Nummern der verwendeten Textstellen, z. B. [S1] oder [S1][S2]; Zahlen mit Einheiten, Verneinungen "
    "(nicht, kein, verboten), Modalverben (muss, darf, soll, kann) und Rollen oder Stellen (z. B. CISO, Geschäftsführung, BSI) "
    "werden wörtlich übernommen; nichts hinzufügen, was nicht in den Textstellen steht; Sprache der Textstellen verwenden."
)


@dataclass
class Sentence:
    text: str
    spans: list[str]  # span ids
    source: str  # llm | verbatim
    verified: bool = True
    reasons: list[str] | None = None


def verify_sentence(text: str, spans: list[str], vocab: Vocab) -> list[str]:
    """Empty list = supported. Every number with its unit and every identifier must appear identically, negation polarity and
    modality must match the best-matching cited span, named roles/bodies must appear in the cited spans, ≥ 70 % of the
    content terms must be supported."""
    if not spans:
        return ["keine Quelle"]
    st = set(analyze(text, vocab))
    src_terms = [set(analyze(s, vocab)) for s in spans]
    union = set().union(*src_terms)
    problems: list[str] = []
    missing_nums = numeric_terms(st) - numeric_terms(union)
    if missing_nums:
        problems.append("Zahl/Einheit: " + ", ".join(sorted(missing_nums)))
    words = {t for t in st if t.isalpha() and len(t) >= 3}
    expanded = set(union)
    for t in union:
        for gi in GLOSSARY.single.get(t, []):
            for m in GLOSSARY.groups[gi]:
                expanded |= m
    # every content word must come from the cited spans (or a glossary equivalent); one word of slack for inflection noise
    if len(words - expanded) > 1:
        problems.append("Inhalt nicht belegt: " + ", ".join(sorted(words - expanded)))
    if YES_NO.match(fold(text)):  # "Ja, …" / "No, …" is a verdict drawn by the model, not text of the source
        problems.append("Ja/Nein-Urteil")
    best = max(range(len(spans)), key=lambda k: (len(st & src_terms[k]), -k))
    f_text, f_best = fold(text), fold(spans[best])
    if bool(NEG_RE.search(f_text)) != bool(NEG_RE.search(f_best)):
        problems.append("Verneinung")
    if modal_classes(text) != modal_classes(spans[best]):
        problems.append("Modalität")
    src = fold(" ".join(spans))
    for e in entities(text):
        if fold(e) not in src:
            problems.append(f"Rolle/Stelle: {e}")
    return problems


def parse_phrased(out: str, span_ids: list[str]) -> list[Sentence]:
    sents: list[Sentence] = []
    for raw in split_sentences(out.replace("\n", " ")):
        refs = [int(n) for n in re.findall(r"S(\d+)", " ".join(re.findall(r"\[([^\]]*)\]", raw)))]
        text = re.sub(r"\s*\[[^\]]*\]", "", raw).strip()
        if not text:
            if sents and refs:  # citation block after the sentence end
                sents[-1].spans += [span_ids[r - 1] for r in refs if 0 < r <= len(span_ids) and span_ids[r - 1] not in sents[-1].spans]
            continue
        sents.append(Sentence(text, list(dict.fromkeys(span_ids[r - 1] for r in refs if 0 < r <= len(span_ids))), "llm"))
    return sents


def compose_sentences(q: str, spans: list[tuple[str, str]], vocab: Vocab, use_llm: bool) -> tuple[list[Sentence], str]:
    """spans = [(span_id, text)] in answer order. Verbatim by default; the LLM may only combine/rephrase them (≤ 3 sentences),
    and every LLM sentence that fails the verifier is replaced by the verbatim spans it cited."""
    verbatim = [Sentence(display_span(t), [sid], "verbatim") for sid, t in spans]
    if not use_llm or not spans:
        return verbatim, "verbatim"
    text_of = dict(spans)
    prompt = "Frage: " + q + "\nTextstellen:\n" + "\n".join(f"[S{k + 1}] {display_span(t)}" for k, (_, t) in enumerate(spans)) + "\nAntwort:"
    out = OLLAMA.chat(PHRASE_SYSTEM, prompt, timeout=LLM_TIMEOUT, max_tokens=220)
    if not out:
        return verbatim, "verbatim (LLM nicht verfügbar)"
    ids = [sid for sid, _ in spans]
    result: list[Sentence] = []
    for s in parse_phrased(out, ids)[:3]:
        used = [sid for sid in s.spans if set(analyze(s.text, vocab)) & set(analyze(text_of[sid], vocab))]
        problems = verify_sentence(s.text, [text_of[sid] for sid in used], vocab)
        if problems:
            result += [Sentence(display_span(text_of[sid]), [sid], "verbatim", False, problems) for sid in (used or []) if not any(sid in r.spans for r in result)]
            continue
        result.append(Sentence(s.text, used, "llm", True, []))
    for sid, t in spans:  # every selected span must be represented; nothing the verifier could not check is shown
        if not any(sid in r.spans for r in result):
            result.append(Sentence(display_span(t), [sid], "verbatim"))
    return result, "llm+verifier"


def _restricted_match(db: dict[str, Any], role: str, question: str) -> dict[str, Any] | None:
    """Audit only (RBAC_DENIED): would a chunk above the caller's clearance have answered? Never influences the response."""
    if role == "L3":
        return None
    idx = lexical_index(db)
    full = idx.view("L3")
    restricted = {i for i in full.allowed if ROLE_RANK[idx.chunks[i]["minRole"]] > ROLE_RANK[role]}
    if not restricted:
        return None
    qt = key_terms(question, full)
    bm = {i: s for i, s in full.bm25(qt.weights).items() if i in restricted}
    for i in sorted(bm, key=lambda i: (-bm[i], idx.chunks[i]["id"]))[:3]:
        if coverage(qt, frozenset(idx.doc_terms[i]), idx.ctx_terms[i]) >= COV_MIN:
            return idx.chunks[i]
    return None


def run_query(token: str | None, question: str) -> dict[str, Any]:
    t_all = time.perf_counter()
    q = (question or "").strip()
    if not 3 <= len(q) <= 500:
        raise ApiError(400, "BAD_QUESTION", "Die Frage muss zwischen 3 und 500 Zeichen lang sein.")
    lat: dict[str, int] = {}
    db = cached_db()
    actor = actor_for(db, token)
    role = actor["role"]

    t = time.perf_counter()
    idx = lexical_index(db)
    view = idx.view(role)  # RBAC pre-filter before any scoring
    texts, context, injection = question_parts(q)
    lat["rbac"] = int((time.perf_counter() - t) * 1000)

    t = time.perf_counter()
    ctx_weights: dict[str, float] = {}
    for c in context:
        for x, w in key_terms(c, view).weights.items():
            ctx_weights[x] = max(ctx_weights.get(x, 0.0), 0.3 * w)
    dense_all = dense_scores(db, view, texts) if texts and view.n else None
    parts: list[Part] = []
    best_dense = 0.0
    candidate_docs: list[str] = []
    for ptext in texts:
        qt = key_terms(ptext, view)
        f = fold(ptext)
        part = Part(ptext, qt, bool(VALUE_Q.search(f) or qt.unit_cues), bool(WHO_Q.search(f)), [], bool(NORM_Q.search(f)))
        if not qt.keys or not view.n:
            part.reason = "keine Suchbegriffe" if view.n else "keine Fundstelle"
            parts.append(part)
            continue
        weights = dict(ctx_weights)
        for x, w in qt.weights.items():
            weights[x] = max(weights.get(x, 0.0), w)
        bm = view.bm25(weights)
        dense = dense_scores(db, view, [ptext]) if dense_all is not None else None
        ranked = fuse(view, bm, dense)[:8]
        candidate_docs += [idx.chunks[i]["docName"] for i in ranked]
        select_windows(idx, view, part, ranked)
        dense_top = max((dense.get(i, 0.0) for w in part.windows for i, _ in w), default=0.0) if dense else None
        best_dense = max(best_dense, dense_top or 0.0)
        part.answered = bool(part.windows) and part.coverage >= COV_MIN and (dense_top is None or dense_top >= PARAMS["ollama"]["min_score"])
        if part.windows and not part.answered:
            part.reason = f"Abdeckung {part.coverage:.2f} < {COV_MIN}" if part.coverage < COV_MIN else "Dense-Score zu niedrig"
        parts.append(part)
    lat["retrieval"] = int((time.perf_counter() - t) * 1000)

    label = engine_label()
    trace: dict[str, Any] = {
        "original": q, "rewrite": " | ".join(texts) if texts else q, "rbacFilter": f"min_role <= {role}", "searched": view.n, "total": len(idx.chunks),
        "nliVerified": False, "temperature": 0.0, "confidence": 0.0, "topScore": 0.0, "latencyMs": lat, "engineMode": label,
        "rewriteBy": "rules (Teilfragen)", "answerBy": "none", "injectionRemoved": injection, "candidateDocs": list(dict.fromkeys(candidate_docs)),
        "parts": [{"text": p.text, "answered": p.answered, "coverage": round(p.coverage, 3), "value": p.value, "who": p.who, "reason": p.reason} for p in parts],
        "denseTop": round(best_dense, 3) if dense_all is not None else None, "contradiction": False, "partial": False,
    }

    def done(kind: str, segments: list[dict[str, str]], citations: dict[str, Any], scores: dict[str, float], spans: dict[str, Any]) -> dict[str, Any]:
        lat["total"] = int((time.perf_counter() - t_all) * 1000)
        plain = " ".join(s["text"].strip() for s in segments if s["type"] in ("text", "note"))
        telemetry = {"rewrittenQuery": trace["rewrite"], "rbacFilter": trace["rbacFilter"], "topScore": trace["topScore"], "nliVerified": trace["nliVerified"],
                     "latencyMs": lat, "engineMode": label, "rbacDenied": False, "requiredRole": None}
        return {"ok": True, "result": {"kind": kind, "requiredRole": None, "segments": segments, "citations": citations, "scores": scores, "spans": spans, "trace": trace},
                "answer": plain, "citations": list(citations.values()), "telemetry": telemetry}

    answered = [p for p in parts if p.answered]
    if not answered:
        hidden = _restricted_match(db, role, q)
        if hidden:
            with locked_db() as dbw:
                append_denial(dbw, actor["email"], role, "RBAC_DENIED", hidden["docName"], f"Retrieval erfordert {hidden['minRole']}")
        trace.update({"verification": "none", "candidateDocs": trace["candidateDocs"] if view.n else []})
        return done("fallback", [{"type": "text", "text": NO_ANSWER}], {}, {}, {})

    t = time.perf_counter()
    ordered: list[tuple[str, str]] = []
    for p in answered:
        for w in p.windows:
            for i, k in w:
                sid = idx.span_ids[i][k]
                if sid not in dict(ordered):
                    ordered.append((sid, idx.span_text[i][k]))
    use_llm = bool(OLLAMA.llm_model())
    sentences, answer_by = compose_sentences(q, ordered, view.vocab, use_llm)
    lat["generation"] = int((time.perf_counter() - t) * 1000)

    span_chunk = {idx.span_ids[i][k]: i for i in range(len(idx.chunks)) for k in range(len(idx.span_ids[i]))}
    segments: list[dict[str, str]] = []
    for s in sentences:
        segments.append({"type": "text", "text": (" " if segments else "") + s.text + " "})
        for sid in s.spans:
            segments.append({"type": "cite", "chunkId": idx.chunks[span_chunk[sid]]["id"], "spanId": sid})
    unanswered = [p for p in parts if not p.answered]
    for p in unanswered:
        segments.append({"type": "note", "text": f"Zu „{p.text}“ enthält der freigegebene Bestand keine belegbare Aussage."})
    cited_chunks = list(dict.fromkeys(s["chunkId"] for s in segments if s["type"] == "cite"))
    by_id = {c["id"]: c for c in idx.chunks}
    spans_out = {sid: {"chunkId": idx.chunks[span_chunk[sid]]["id"], "text": text} for sid, text in ordered}
    cov = min(p.coverage for p in answered)
    trace.update({
        "nliVerified": all(s.verified for s in sentences), "verification": "span-verifier", "answerBy": answer_by, "partial": bool(unanswered),
        "confidence": round(cov, 2), "retrievalScore": round(best_dense if dense_all is not None else cov, 3), "topScore": round(best_dense if dense_all is not None else cov, 3),
        "lexicalSupport": round(cov, 2),
        "sentences": [{"text": s.text, "spans": s.spans, "source": s.source, "verified": s.verified, "problems": s.reasons or []} for s in sentences],
    })
    scores = {cid: round(max((p.coverage for p in answered), default=0.0), 3) for cid in cited_chunks}
    return done("answer", segments, {cid: by_id[cid] for cid in cited_chunks}, scores, spans_out)


# ───────────────────────── Health ─────────────────────────

def health() -> dict[str, Any]:
    names = OLLAMA.tags()
    try:
        db: dict[str, Any] | None = read_db()
    except ApiError:
        db = None
    return {
        "ok": True, "status": "ok", "service": "pocket-ciso-rag", "version": VERSION, "python": True,
        "dbReady": db is not None,
        "ollama": {"connected": names is not None, "baseUrl": OLLAMA_BASE_URL, "llmModel": OLLAMA.llm_model(), "embedModel": OLLAMA.embed_model(), "models": names or []},
        "engineMode": engine_label(), "vectorCount": VECTORS.count(), "vectorEngine": VECTORS.engine,
        "autoSync": AUTO_SYNC, "autoSyncSeconds": AUTO_SYNC_SECONDS if AUTO_SYNC else None,
        "liveChunks": len(db["live_chunks"]) if db else 0, "pendingDiffs": sum(1 for d in db["staged_diffs"] if d["status"] == "pending") if db else 0,
    }
