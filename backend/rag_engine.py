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
import threading
import time
import uuid
import zlib
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx
import numpy as np

# ───────────────────────── Configuration ─────────────────────────

DATA_DIR = Path(os.getenv("POCKET_CISO_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "pocket-ciso-db.json"
VEC_PATH = DATA_DIR / "vectors.json"
LOCK_DIR = DATA_DIR / ".db.lock"  # cross-process mutex shared with the Node app (atomic mkdir)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "llama3.2:3b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
REWRITE_TIMEOUT = float(os.getenv("REWRITE_TIMEOUT", "8"))
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "14"))
EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", "20"))
CONFLICT_LLM_TIMEOUT = float(os.getenv("CONFLICT_LLM_TIMEOUT", "20"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024
MAX_CHUNK_CHARS = 600
LEX_MIN = float(os.getenv("LEX_MIN", "0.34"))  # min. share of question terms (incl. synonyms) a chunk must contain
COLLECTION = "policies_live_v1"
ROLE_RANK = {"L1": 1, "L2": 2, "L3": 3}
GENESIS = "0" * 64
VERSION = "0.5.0"

HASH_ID = "hash-ngram-v1"
NO_ANSWER = "Keine eindeutige Richtlinie gefunden — Halluzinationsschutz aktiv (Konfidenz < 85%)."

# Similarity thresholds per embedding space (cosine). Ollama values follow the product spec; the hash space is calibrated.
PARAMS = {
    "ollama": {
        "min_score": float(os.getenv("MIN_SCORE", "0.65")),
        "conflict_sim": float(os.getenv("CONFLICT_SIM", "0.72")),
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


def scale_score(cos: float, thr: float, at_thr: float) -> float:
    """Map a raw cosine so that `thr` lands exactly on `at_thr` (e.g. 0.65 → 85 % confidence)."""
    if cos >= thr:
        return round(at_thr + (0.99 - at_thr) * min(1.0, (cos - thr) / max(1e-6, 1.0 - thr)), 2)
    return round(max(0.0, at_thr * cos / max(thr, 1e-6)), 2)


# ───────────────────────── Text utilities (mirror src/lib/db.ts) ─────────────────────────

def norm(s: str) -> str:
    return s.lower().replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")


def tokenize(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", norm(s)) if t]


STOP = set(
    ("wie was wer wo wann wieso warum welche welcher welches bis muss musst mussen darf durfen kann konnen ich mein meine meinen mir mich wir uns das der die den dem des "
     "ein eine einen einem einer und oder im in am an auf aus bei mit nach von vom zu zur zum fur uber auch nicht sein ist sind wird werden wurde hat haben es sie er ob als wenn "
     "dass noch nur so machen gibt gilt soll sollen sollte dann dies diese ihr ihre wir man oder sowie sowohl jede jeder jedes alle dabei dafur damit durch sich").split()
)

SYN: dict[str, list[str]] = {
    "zug": ["bahn", "offentlich", "verkehrsmittel", "personenverkehr"],
    "bahn": ["zug", "offentlich", "verkehrsmittel"],
    "unterwegs": ["offentlich", "bahn", "mobile"],
    "homeoffice": ["mobile", "remote"],
    "kennwort": ["passwort"],
    "vorfall": ["sicherheitsvorfall", "incident"],
    "incident": ["sicherheitsvorfall", "vorfall"],
    "gemeldet": ["melden", "meldung", "meldepflicht"],
    "melden": ["meldung", "meldepflicht"],
    "meldung": ["melden", "meldepflicht"],
    "meldepflicht": ["melden", "meldung"],
    "chatgpt": ["generative", "ki", "kunstliche"],
    "ki": ["kunstliche", "intelligenz", "generative"],
    "nutzen": ["verwendung", "einsatz", "verwenden"],
}

REWRITES = {
    " ".join(tokenize("Darf ich das auch im Zug machen?")): "Ist das Arbeiten mit vertraulichen Daten im öffentlichen Personenverkehr (Zug) laut Mobile-Work-Policy erlaubt?",
    " ".join(tokenize("Wie lang muss mein Passwort sein?")): "Welche Mindestlänge und welche Wechselintervalle gelten laut aktueller Passwort-Policy für Benutzerpasswörter?",
    " ".join(tokenize("Bis wann muss ein Sicherheitsvorfall nach NIS-2 gemeldet werden?")): "Welche Meldefristen gelten für erhebliche Sicherheitsvorfälle gegenüber dem BSI gemäß NIS-2 und Incident-Response-Runbook?",
    " ".join(tokenize("Darf ich private KI-Tools wie ChatGPT für Kundenangebote nutzen?")): "Ist die Nutzung nicht freigegebener generativer KI-Dienste zur Verarbeitung von Kundendaten laut Unternehmensrichtlinie erlaubt?",
}


def stem(t: str) -> str:
    return t[:7] if len(t) >= 7 else t


def content_stems(text: str) -> list[str]:
    return [stem(t) for t in tokenize(text) if len(t) >= 3 and t not in STOP and not t.isdigit()]


NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def numbers_of(text: str) -> set[str]:
    return {n.replace(",", ".") for n in NUM_RE.findall(text)}


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9„\"(])", text) if s.strip()]


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
        return self._match(names, LLM_MODEL) or next((n for n in names if "embed" not in n.lower()), None)

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
            for i in range(0, len(texts), 16):
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


def hash_embed(text: str, expand: bool = False) -> np.ndarray:
    """Deterministic TF + char-4-gram feature hashing (offline embedding space)."""
    toks = content_stems(text)
    if expand:
        for t in list(toks):
            toks += [stem(s) for s in SYN.get(t, [])]
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
            return np.stack([hash_embed(t, expand=(kind == "query")) for t in texts])
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

@contextmanager
def file_lock(timeout: float = 10.0, stale: float = 60.0) -> Iterator[None]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.mkdir(LOCK_DIR)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(LOCK_DIR) > stale:
                    os.rmdir(LOCK_DIR)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise ApiError(503, "DB_LOCKED", "Datenbank ist gesperrt — bitte erneut versuchen.")
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            os.rmdir(LOCK_DIR)
        except OSError:
            pass


def read_db() -> dict[str, Any]:
    try:
        db = json.loads(DB_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ApiError(503, "DB_NOT_READY", "Datenbank noch nicht initialisiert — Web-App einmal öffnen (GET /api/v1).")
    except json.JSONDecodeError:
        raise ApiError(500, "DB_CORRUPT", "pocket-ciso-db.json ist beschädigt.")
    if "meta" not in db or "live_chunks" not in db:
        raise ApiError(500, "DB_CORRUPT", "Datenbank-Schema ungültig.")
    return db


def _atomic_write(path: Path, payload: str) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(payload, encoding="utf-8")
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
        self._mtime: float | None = None

    def reload(self) -> None:
        try:
            mt = VEC_PATH.stat().st_mtime
        except FileNotFoundError:
            self.engine, self.vecs, self._mtime = None, {}, None
            return
        if mt == self._mtime:
            return
        try:
            raw = json.loads(VEC_PATH.read_text(encoding="utf-8"))
            self.engine = raw.get("engine")
            self.vecs = {k: np.asarray(v, dtype=np.float32) for k, v in raw.get("vectors", {}).items()}
        except Exception:
            self.engine, self.vecs = None, {}
        self._mtime = mt

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "engine": self.engine,
            "dim": int(next(iter(self.vecs.values())).shape[0]) if self.vecs else 0,
            "updatedAt": now_iso(),
            "vectors": {k: [round(float(x), 6) for x in v] for k, v in self.vecs.items()},
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
                VECTORS.engine, VECTORS.vecs, changed = eid, {}, True
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
    e = {"id": f"AUD-{n:04d}", "timestamp": now_iso(), "actor": actor, "role": role, "action": action, "target": target, "status": status, "detail": detail, "prevHash": prev, "hash": ""}
    e["hash"] = sha256("|".join([prev, e["id"], e["timestamp"], actor, role, action, target, status, detail]))
    db["audit_logs"].append(e)


def verify_token(secret: str, token: str | None) -> str | None:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] not in ("L2", "L3"):
        return None
    role, exp, sig = parts
    expected = hmac.new(secret.encode("utf-8"), f"{role}.{exp}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        return role if int(exp) > time.time() * 1000 else None
    except ValueError:
        return None


def actor_for(db: dict[str, Any], token: str | None) -> dict[str, str]:
    role = verify_token(db["meta"]["secret"], token) or "L1"
    user = next(u for u in db["users"] if u["role"] == role)
    return {"email": user["email"], "role": role}


def authorize(db: dict[str, Any], token: str | None, need: str, action: str, target: str) -> dict[str, str]:
    a = actor_for(db, token)
    if ROLE_RANK[a["role"]] < ROLE_RANK[need]:
        append_audit(db, a["email"], a["role"], "ACCESS_DENIED", f"{action}:{target}", "DENIED", f"Erfordert {need}")
        raise ApiError(403, "FORBIDDEN", f"Diese Aktion erfordert Clearance {need}.")
    return a


# ───────────────────────── Document parsing + semantic chunking ─────────────────────────

ALLOWED_EXT = {".pdf", ".docx", ".md", ".txt"}


def safe_doc_name(name: str) -> str:
    base = Path(name.replace("\\", "/")).name.strip()
    base = re.sub(r"[^\w.\- ]", "_", base)[:120]
    if not re.search(r"\.[A-Za-z0-9]{1,5}$", base):
        base += ".txt"
    return base or "dokument.txt"


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
            return "\n\n".join((p.extract_text() or "") for p in reader.pages[:300])
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ApiError(422, "PDF_UNREADABLE", f"PDF konnte nicht gelesen werden: {type(e).__name__}")
    try:
        from docx import Document

        doc = Document(io.BytesIO(data))
        lines: list[str] = []
        for p in doc.paragraphs:
            txt = p.text.strip()
            if not txt:
                lines.append("")
                continue
            style = (p.style.name if p.style is not None else "") or ""
            if style == "Title":
                lines.append(f"# {txt}")
            elif style.startswith("Heading"):
                lines.append(f"## {txt}")
            elif "List" in style:
                lines.append(f"- {txt}")
            else:
                lines.append(txt)
        for table in doc.tables:
            for row in table.rows:
                lines.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        raise ApiError(422, "DOCX_UNREADABLE", f"DOCX konnte nicht gelesen werden: {type(e).__name__}")


HEAD_MD = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
HEAD_PARA = re.compile(r"^\s*§\s*(\d+(?:\.\d+)*)\.?\s+(.+?)\s*$")
HEAD_NUM = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+([A-ZÄÖÜ][^.!?;:]{2,80})\s*$")
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
            paras.append(" ".join(buf))
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


def chunk_document(text: str) -> tuple[str | None, list[dict[str, str]]]:
    """Split by headings (## / §N / 1.2 Title) into atomic policy chunks of ≤ ~600 chars. Returns (title, chunks)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
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

    for line in text.split("\n"):
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
        nm = HEAD_NUM.match(line)
        if nm:
            start(nm.group(2), nm.group(1))
            continue
        (cur["lines"] if cur else pre).append(line)

    if sum(len(x.strip()) for x in pre) >= (15 if not sections else 40):  # headingless text becomes one "§0" chunk set
        sections.insert(0, {"number": "0", "title": "Vorbemerkung", "lines": pre})

    chunks: list[dict[str, str]] = []
    for sec in sections:
        parts = [p for p in _pack(_body_paragraphs(sec["lines"])) if len(p) >= 15]
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


def stage_document(
    token: str | None,
    *,
    doc_name: str,
    text: str,
    title: str | None = None,
    min_role: str = "L1",
    author: str | None = None,
    replaces: str | None = None,
) -> dict[str, Any]:
    t_all = time.perf_counter()
    lat: dict[str, int] = {}

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
    assert new_vecs is not None
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
                shown = scale_score(sim, prm["conflict_sim"], 0.72)
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
        "author": (author or f"{actor['email']} ({actor['role']})")[:80],
        "uploadedAt": now_iso(),
        "summary": f"{len(hunks)} geänderte/neue Chunk(s), {unchanged} unverändert, {len(conflict_list)} Konflikt(e) · Engine: {label}",
        "sha256": sha256("\n".join(h["newContent"] for h in hunks)),
        "targetCollection": COLLECTION,
        "status": "pending", "decidedAt": None, "decidedBy": None, "decisionNote": None,
        "hunks": hunks, "conflicts": conflict_list,
    }
    with file_lock():
        db = read_db()
        db["staged_diffs"].append(diff)
        append_audit(db, actor["email"], actor["role"], "POLICY_STAGED", doc_name, "SUCCESS",
                     f"{len(hunks)} Chunk(s), {len(conflict_list)} Konflikt(e), SHA-256 {diff['sha256'][:12]}…, Engine {label}")
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
    if not re.fullmatch(r"[\w.\- ]{3,120}", target):
        raise ApiError(400, "BAD_TARGET", "Zieldokument: 3–120 Zeichen (Buchstaben, Ziffern, . _ -).")
    existing = next((c for c in read_db()["live_chunks"] if c["docName"].lower() == target.lower()), None)
    if existing:
        return stage_document(token, doc_name=bump_version(existing["docName"]), text=new_text, title=title, min_role=min_role, author=author, replaces=existing["docName"])
    name = target if re.search(r"\.[A-Za-z0-9]{1,5}$", target) else f"{target}.md"
    return stage_document(token, doc_name=name, text=new_text, title=title, min_role=min_role, author=author)


def approve_diff(token: str | None, diff_id: str, override: bool = False) -> dict[str, Any]:
    with locked_db() as db:
        actor = authorize(db, token, "L3", "approve", diff_id)
        idx = next((i for i, d in enumerate(db["staged_diffs"]) if d["id"] == diff_id and d["status"] == "pending"), -1)
        if idx < 0:
            raise ApiError(404, "NOT_FOUND", "Pending-Diff nicht gefunden.")
        diff = db["staged_diffs"][idx]
        open_conflicts = [c for c in diff["conflicts"] if c["status"] == "open"]
        if open_conflicts and not override:
            append_audit(db, actor["email"], actor["role"], "MERGE_BLOCKED", diff["docName"], "DENIED", f"{len(open_conflicts)} ungelöste(r) Konflikt(e)")
            raise ApiError(409, "CONFLICT_UNRESOLVED", "Ungelöste Konflikte: Sync-Ticket erstellen oder Override bestätigen.")

        superseded, moved = 0, 0
        ts = now_iso()
        with VECTORS.lock:
            VECTORS.reload()
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
            VECTORS.save()
        db["staged_diffs"].pop(idx)
        db["meta"]["liveVersion"] += 1
        if open_conflicts:
            resolution = f"Override ({len(open_conflicts)} Konflikt akzeptiert)"
        elif diff["conflicts"]:
            resolution = "Konflikte per Sync-Ticket adressiert (" + ", ".join(str(c["ticketId"]) for c in diff["conflicts"]) + ")"
        else:
            resolution = "keine Konflikte"
        append_audit(db, actor["email"], actor["role"], "APPROVED_AND_MERGED", diff["docName"], "SUCCESS",
                     f"{len(diff['hunks'])} Chunk(s) live, {superseded} ersetzt, {moved} Vektor(en) übernommen; {resolution}")
    return {"ok": True, "merged": len(diff["hunks"]), "superseded": superseded, "docName": diff["docName"]}


# ───────────────────────── Pipeline B: stateless 4-stage compliance RAG ─────────────────────────

REWRITE_SYSTEM = (
    "Du bist ein Query-Rewriter für ein Compliance-RAG-System. Du beantwortest NIEMALS Fragen. "
    "Formuliere die Nutzerfrage als eine einzige, eigenständige, explizite deutsche Suchanfrage zu Unternehmensrichtlinien um "
    "und behalte alle Schlüsselbegriffe bei. Antworte NUR mit der Suchanfrage in einem Satz.\n"
    "Beispiel 1: Frage: Darf ich Daten per USB-Stick mitnehmen? → Suchanfrage: Ist die Mitnahme vertraulicher Daten auf USB-Speichermedien laut Richtlinie zulässig?\n"
    "Beispiel 2: Frage: Wie oft Backup? → Suchanfrage: In welchen Intervallen müssen Datensicherungen laut Backup-Richtlinie erstellt werden?"
)
ANSWER_SYSTEM = (
    "Du bist Pocket CISO, ein Compliance-Assistent. Beantworte die Frage in 1 bis 3 knappen deutschen Sätzen und verwende dafür "
    "ausschließlich Informationen aus den nummerierten Kontext-Abschnitten. Hänge an jeden Satz die Quelle in eckigen Klammern an, "
    "z. B. [C1]. Übernimm Zahlen und Fristen exakt aus dem Kontext und füge nichts hinzu."
)


REFUSAL_RE = re.compile(r"^(ich kann|ich habe|leider|entschuldig|es tut mir leid|tut mir leid|als ki|sorry|i cannot|i can't)", re.I)


def question_groups(text: str) -> list[list[str]]:
    """Content-term groups (term + synonyms, as stems) used for lexical support checks."""
    seen: set[str] = set()
    groups: list[list[str]] = []
    for t in tokenize(text):
        if len(t) < 3 or t in STOP or t.isdigit() or t in seen:
            continue
        seen.add(t)
        groups.append([stem(t)] + [stem(norm(x)) for x in SYN.get(t, [])])
    return groups


def lexical_support(groups: list[list[str]], chunk: dict[str, Any]) -> float:
    if not groups:
        return 0.0
    toks = {stem(t) for t in tokenize(f"{chunk['docName']} {chunk['section']} {chunk['content']}")}
    return sum(1 for g in groups if any(x in toks for x in g)) / len(groups)


def plausible_rewrite(question: str, line: str) -> bool:
    """Small models sometimes answer/refuse instead of rewriting — keep only rewrites that preserve the question's terms."""
    if REFUSAL_RE.match(line.strip()):
        return False
    groups = question_groups(question)
    if not groups:
        return True
    keep = {stem(t) for t in tokenize(line)}
    return sum(1 for g in groups if any(x in keep for x in g)) / len(groups) >= 0.5


def rewrite_query(question: str) -> tuple[str, str]:
    known = REWRITES.get(" ".join(tokenize(question)))
    out = OLLAMA.chat(REWRITE_SYSTEM, f"Frage: {question} → Suchanfrage:", timeout=REWRITE_TIMEOUT, max_tokens=80)
    if out:
        line = out.strip().splitlines()[0].strip().strip("\"'„“”` ")
        line = re.sub(r"^(suchanfrage|antwort)\s*:\s*", "", line, flags=re.I)
        if 8 <= len(line) <= 300 and plausible_rewrite(question, line):
            return line, "llm"
    if known:
        return known, "rules"
    kw = ", ".join(dict.fromkeys(content_stems(question)))
    return f"Welche freigegebene Richtlinie regelt: „{question}“?" + (f" [Suchbegriffe: {kw}]" if kw else ""), "rules"


def extractive_answer(question: str, ranked: list[tuple[dict[str, Any], float]]) -> list[dict[str, str]]:
    q = set(content_stems(question))
    segs: list[dict[str, str]] = []
    for i, (c, _s) in enumerate(ranked[:2]):
        text = c["content"]
        if len(text) > 320:
            sents = split_sentences(text)
            order = sorted(range(len(sents)), key=lambda k: -len(q & set(content_stems(sents[k]))))[:2]
            text = " ".join(sents[k] for k in sorted(order))
        segs.append({"type": "text", "text": (" " if i else "") + text + " "})
        segs.append({"type": "cite", "chunkId": c["id"]})
    return segs


CITE_RE = re.compile(r"\[\s*((?:C\d+\s*[,;]?\s*)+)\]")


def parse_llm_answer(text: str, ctx: list[dict[str, Any]]) -> list[dict[str, str]] | None:
    cleaned = re.sub(r"\[[^\]]*§[^\]]*\]", "", text).strip()
    segs: list[dict[str, str]] = []
    pos = 0
    for m in CITE_RE.finditer(cleaned):
        chunk_text = cleaned[pos : m.start()]
        if chunk_text.strip():
            segs.append({"type": "text", "text": chunk_text.rstrip() + " "})
        for n in re.findall(r"C(\d+)", m.group(1)):
            k = int(n) - 1
            if 0 <= k < len(ctx) and not any(s.get("chunkId") == ctx[k]["id"] for s in segs[-2:]):
                segs.append({"type": "cite", "chunkId": ctx[k]["id"]})
        pos = m.end()
    tail = cleaned[pos:].strip()
    if tail:
        segs.append({"type": "text", "text": (" " if segs else "") + tail})
    if not any(s["type"] == "text" for s in segs):
        return None
    if not any(s["type"] == "cite" for s in segs):  # model forgot citations → attach the best source
        segs.append({"type": "text", "text": " "})
        segs.append({"type": "cite", "chunkId": ctx[0]["id"]})
    return segs


def nli_check(segments: list[dict[str, str]], chunks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Lexical NLI grounding: every number must occur in the cited chunks and ≥ 60 % of content words must be supported."""
    answer = re.sub(r"§\s*\d+(?:\.\d+)*", "", " ".join(s["text"] for s in segments if s["type"] == "text"))  # section refs are citations, not claims
    cited_chunks = [chunks[s["chunkId"]] for s in segments if s["type"] == "cite" and s["chunkId"] in chunks]
    cited = [c["content"] for c in cited_chunks]
    ctx = " ".join(cited + [f"{c['docName']} {c['section']}" for c in cited_chunks])
    unsupported = sorted(numbers_of(answer) - numbers_of(ctx))
    a_stems, c_stems = content_stems(answer), set(content_stems(ctx))
    coverage = (sum(1 for s in a_stems if s in c_stems) / len(a_stems)) if a_stems else 1.0
    thin = len(a_stems) < 2  # "Nein." / "Ja." are unverifiable claims
    return {"verified": bool(cited) and not thin and not unsupported and coverage >= 0.6, "unsupportedNumbers": unsupported, "coverage": round(coverage, 2)}


def run_query(token: str | None, question: str) -> dict[str, Any]:
    t_all = time.perf_counter()
    q = (question or "").strip()
    if not 3 <= len(q) <= 500:
        raise ApiError(400, "BAD_QUESTION", "Die Frage muss zwischen 3 und 500 Zeichen lang sein.")
    lat: dict[str, int] = {}
    db = read_db()
    actor = actor_for(db, token)
    role = actor["role"]

    # Step 1 — isolated query rewrite (no history exists anywhere in this function by design)
    t = time.perf_counter()
    rewrite, rewrite_by = rewrite_query(q)
    lat["rewrite"] = int((time.perf_counter() - t) * 1000)

    # Step 2 — RBAC pre-filter BEFORE ranking
    t = time.perf_counter()
    live = db["live_chunks"]
    accessible = [c for c in live if ROLE_RANK[c["minRole"]] <= ROLE_RANK[role]]
    restricted = [c for c in live if ROLE_RANK[c["minRole"]] > ROLE_RANK[role]]
    lat["rbac"] = int((time.perf_counter() - t) * 1000)

    # Step 3 — hybrid retrieval: dense cosine (original ∨ rewrite) gated by lexical support, then threshold gate
    t = time.perf_counter()
    eid, vecs = ensure_vectors(db)
    qm = EMBEDDER.embed([q, rewrite], "query", eid)
    if qm is None:  # Ollama went away between sync and query
        eid, vecs = ensure_vectors(db, force_hash=True)
        qm = EMBEDDER.embed([q, rewrite], "query", eid)
    prm = params_for(eid)
    thr = prm["min_score"]
    groups = question_groups(q)

    def rank(chunks: list[dict[str, Any]]) -> list[tuple[dict[str, Any], float, float]]:
        rows = [(c, float(np.max(qm @ vecs[c["id"]])), lexical_support(groups, c)) for c in chunks if c["id"] in vecs]
        return sorted(rows, key=lambda r: -(r[1] + 0.15 * r[2]))

    def passes(row: tuple[dict[str, Any], float, float]) -> bool:
        return row[1] >= thr and row[2] >= LEX_MIN

    acc, res = rank(accessible), rank(restricted)
    ok_acc = [r for r in acc if passes(r)]
    ok_res = [r for r in res if passes(r)]
    best = ok_acc[0][1] if ok_acc else (max((r[1] for r in acc), default=0.0))
    lat["retrieval"] = int((time.perf_counter() - t) * 1000)
    label = engine_label(eid)
    trace = {
        "original": q, "rewrite": rewrite, "rbacFilter": f"min_role <= {role}", "searched": len(accessible), "total": len(live),
        "nliVerified": False, "temperature": 0.0, "confidence": 0.0, "topScore": round(best, 3), "latencyMs": lat,
        "engineMode": label, "rewriteBy": rewrite_by, "answerBy": "none",
    }

    def done(kind: str, segments: list[dict[str, str]], citations: dict[str, Any], scores: dict[str, float], required: str | None = None) -> dict[str, Any]:
        lat["total"] = int((time.perf_counter() - t_all) * 1000)
        plain = " ".join(s["text"].strip() for s in segments if s["type"] == "text")
        telemetry = {
            "rewrittenQuery": rewrite, "rbacFilter": trace["rbacFilter"], "topScore": trace["topScore"], "nliVerified": trace["nliVerified"],
            "latencyMs": lat, "engineMode": label, "rbacDenied": kind == "denied", "requiredRole": required,
        }
        return {"ok": True, "result": {"kind": kind, "requiredRole": required, "segments": segments, "citations": citations, "scores": scores, "trace": trace},
                "answer": plain, "citations": list(citations.values()), "telemetry": telemetry}

    if not ok_acc and ok_res:
        with locked_db() as dbw:
            append_audit(dbw, actor["email"], role, "RBAC_DENIED", ok_res[0][0]["docName"], "DENIED", f"Retrieval erfordert {ok_res[0][0]['minRole']}")
        trace["topScore"] = round(ok_res[0][1], 3)
        return done("denied", [{"type": "text", "text": f"Zu dieser Frage existieren freigegebene Dokumente, die Ihre aktuelle Clearance ({role}) nicht einsehen darf."}], {}, {}, ok_res[0][0]["minRole"])

    if not ok_acc:
        trace["confidence"] = min(0.84, scale_score(best, thr, 0.85))  # a fallback never displays ≥ 85 %
        return done("fallback", [{"type": "text", "text": NO_ANSWER}], {}, {})

    # Step 4 — grounded generation (temp 0.0) + NLI verification
    t = time.perf_counter()
    best_lex = ok_acc[0][2]
    top = [(c, d) for c, d, lx in ok_acc if d >= best - 0.05 and lx >= best_lex - 1e-6][:3]
    ctx = [c for c, _ in top]
    segments: list[dict[str, str]] | None = None
    answer_by = "rules"
    prompt = "Kontext:\n" + "\n".join(f"[C{i + 1}] {c['docName']} {c['section']}: {c['content']}" for i, c in enumerate(ctx)) + f"\n\nFrage: {q}\nAntwort:"
    llm_out = OLLAMA.chat(ANSWER_SYSTEM, prompt, timeout=LLM_TIMEOUT, max_tokens=260)
    if llm_out and "KEINE_ANTWORT" not in llm_out.upper():
        segments, answer_by = parse_llm_answer(llm_out, ctx), "llm"
    lat["generation"] = int((time.perf_counter() - t) * 1000)

    t = time.perf_counter()
    by_id = {c["id"]: c for c in ctx}
    nli = nli_check(segments, by_id) if segments else {"verified": False}
    if not nli["verified"]:  # hallucination guard: replace with the guaranteed-grounded extractive answer
        segments, answer_by = extractive_answer(q + " " + rewrite, top), "rules" if not llm_out else "rules (NLI-Fallback)"
        nli = nli_check(segments, by_id)
    lat["nli"] = int((time.perf_counter() - t) * 1000)

    cited_ids = list(dict.fromkeys(s["chunkId"] for s in segments if s["type"] == "cite"))
    scores = {c["id"]: round(s, 3) for c, s in top if c["id"] in cited_ids}
    citations = {i: by_id[i] for i in cited_ids if i in by_id}
    trace.update({"nliVerified": bool(nli["verified"]), "confidence": scale_score(best, thr, 0.85), "answerBy": answer_by})
    return done("answer", segments, citations, scores)


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
        "liveChunks": len(db["live_chunks"]) if db else 0, "pendingDiffs": sum(1 for d in db["staged_diffs"] if d["status"] == "pending") if db else 0,
    }
