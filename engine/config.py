"""Configuration from the environment (paths, models, timeouts, thresholds), ApiError and small shared helpers."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path


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
# chat context and residency: judge/phrasing prompts are < 2k tokens, so 4096 is enough and keeps a 14B model at ~10 GB
# instead of its 32k default; keep_alive 30m avoids the cold start (qwen3:14b ~16 s) between queries
LLM_NUM_CTX = int(os.getenv("LLM_NUM_CTX", "4096"))
LLM_KEEP_ALIVE = os.getenv("LLM_KEEP_ALIVE", "30m")
EMBED_TIMEOUT = float(os.getenv("EMBED_TIMEOUT", "20"))
EMBED_TOTAL_BUDGET = float(os.getenv("EMBED_TOTAL_BUDGET", "100"))  # whole embed() call; staging stays below the gateway timeout (180 s)
CONFLICT_LLM_TIMEOUT = float(os.getenv("CONFLICT_LLM_TIMEOUT", "20"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024
MAX_CHUNK_CHARS = 600
COLLECTION = "policies_live_v1"
INBOX_DIR = Path(os.getenv("WATCHED_INBOX_DIR", DATA_DIR / "inbox"))  # connector folder, gitignored with data/
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
        "conflict_sim": float(os.getenv("CONFLICT_SIM", "0.68")),  # adjusted for bge-m3
        "align": float(os.getenv("ALIGN_SIM", "0.60")),
    },
    "hash": {"conflict_sim": 0.30, "align": 0.35},
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
