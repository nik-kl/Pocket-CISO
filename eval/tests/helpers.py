"""Shared test setup: throw-away data dir, production-mode env (set BEFORE the engine is imported), signed tokens,
an in-memory DB fixture and a deterministic LLM stub. Tests: engine/.venv/bin/python -m unittest discover -s eval/tests
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="pocket-ciso-test-")
os.environ.update({
    "POCKET_CISO_DATA_DIR": str(Path(_TMP) / "data"),
    "WATCHED_INBOX_DIR": str(Path(_TMP) / "inbox"),
    "POCKET_CISO_ENV": "production",
    "POCKET_CISO_SECRET": "pocket-ciso-eval-secret-0123456789abcdef",
    "OLLAMA_BASE_URL": "http://127.0.0.1:9",
})
for _k in ("POCKET_CISO_DEMO", "POCKET_CISO_DEMO_MODE", "AUTO_SYNC"):
    os.environ.pop(_k, None)
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "eval")]

import engine as rag  # noqa: E402
from eval import make_pdf  # noqa: E402,F401  (re-exported for the PDF tests)

SECRET = os.environ["POCKET_CISO_SECRET"]


def token(role: str) -> str | None:
    if role == "L1":
        return None
    payload = f"{role}.{int(time.time() * 1000) + 3_600_000}"
    return f"{payload}.{hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()}"


def fresh_db(chunks: list[dict]) -> None:
    rag.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for p in rag.DATA_DIR.iterdir():
        if p.is_file():
            p.unlink()
    db = {
        "meta": {"liveVersion": 1, "nextTicket": 4712, "nextAudit": 1, "seededAt": rag.now_iso(), "ingestedFiles": {}},
        "users": [
            {"email": "employee@company.at", "role": "L1", "clearance": "Employee", "requiresPassword": False, "passwordHash": None},
            {"email": "admin@company.at", "role": "L2", "clearance": "IT-Admin", "requiresPassword": True, "passwordHash": "env-managed"},
            {"email": "ciso@company.at", "role": "L3", "clearance": "CISO", "requiresPassword": True, "passwordHash": "env-managed"},
        ],
        "live_chunks": [], "staged_diffs": [], "audit_logs": [], "tickets": [],
    }
    for i, (doc, section, role, content) in enumerate(chunks):
        db["live_chunks"].append({"id": f"chk-t{i:03d}", "docName": doc, "section": section, "minRole": role, "content": content,
                                  "updatedAt": rag.now_iso(), "sha256": rag.sha256(f"{doc}|{section}|{content}"), "approvedBy": "ciso@company.at"})
    rag.write_db(db)


def pick(word: str):
    """Evidence-judge reply for stub_llm: the number of the first listed span containing `word` ("0" when none does)."""
    def reply(user: str) -> str:
        return next((line[1:line.index("]")] for line in user.splitlines() if line.startswith("[") and word in line), "0")
    return reply


@contextmanager
def stub_llm(judge: str | None | Callable[[str], str | None] = "JA"):
    """Deterministic stand-in for Ollama: the answerability judge replies `judge` (None = no reply; a callable gets the
    prompt — evidence judge: span number), phrasing never replies, so answers stay verbatim."""
    def chat(system: str, user: str, **_: object) -> str | None:
        if system not in (rag.JUDGE_SYSTEM, rag.EVIDENCE_SYSTEM):
            return None
        return judge(user) if callable(judge) else judge
    with mock.patch.object(rag.OLLAMA, "llm_model", return_value="stub"), mock.patch.object(rag.OLLAMA, "chat", side_effect=chat):
        yield
