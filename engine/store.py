"""Shared JSON DB (cross-process lock with the Node app), vector store, audit trail, HMAC tokens and RBAC checks."""
from __future__ import annotations

import hashlib
import hmac
import json
import numpy as np
import os
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import (
    AUTH_SECRET_PATH, ApiError, DATA_DIR, DB_PATH, DEMO_MODE, GENESIS, HASH_ID, LOCK_DIR,
    ROLE_RANK, VEC_PATH, now_iso, sha256,
)
from .llm import EMBEDDER


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
    """Mirror of stealStaleLock in src/lib/db/store.ts: an atomic rename has exactly one winner; we verify the owner we judged stale."""
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


def append_audit(db: dict[str, Any], actor: str, role: str, action: str, target: str, status: str, detail: str = "") -> None:
    meta = db["meta"]
    n = meta["nextAudit"]
    meta["nextAudit"] = n + 1
    prev = db["audit_logs"][-1]["hash"] if db["audit_logs"] else GENESIS
    e = {"id": f"AUD-{n:04d}", "timestamp": now_iso(), "actor": actor, "role": role, "action": action, "target": target, "status": status, "detail": detail, "prevHash": prev, "hash": "", "v": 2}
    # v2: JSON array (unambiguous field boundaries) — byte-identical to JSON.stringify in src/lib/db/audit.ts
    e["hash"] = sha256(json.dumps([prev, e["id"], e["timestamp"], actor, role, action, target, status, detail], ensure_ascii=False, separators=(",", ":")))
    db["audit_logs"].append(e)
    meta["auditAnchor"] = hmac.new(auth_secret().encode("utf-8"), f"audit-anchor|{len(db['audit_logs'])}|{e['hash']}".encode("utf-8"), hashlib.sha256).hexdigest()


_DENIALS: dict[str, tuple[float, int]] = {}
DENIAL_WINDOW_S = 60.0


def append_denial(db: dict[str, Any], actor: str, role: str, action: str, target: str, detail: str) -> None:
    """Sampled denial audit (mirrors appendDenial in src/lib/db/auth.ts): ≤ 1 entry per actor/action/target-prefix per minute."""
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


_DB_CACHE: dict[str, Any] = {"key": None, "db": None}


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
