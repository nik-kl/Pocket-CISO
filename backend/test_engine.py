"""Engine unit tests (standard library only, no Ollama needed).

    backend/.venv/bin/python -m unittest backend/test_engine.py
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
from pathlib import Path

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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rag_engine as rag  # noqa: E402

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


class AuditParity(unittest.TestCase):
    """Fixed vectors computed with Node (crypto.createHash / createHmac over JSON.stringify of the nine fields)."""

    FIELDS = ["0" * 64, "AUD-0001", "2026-01-02T03:04:05.678Z", "ciso@company.at", "L3", "APPROVED_AND_MERGED",
              "Richtlinie für IAM, Endgeräte & Wechselmedien.pdf", "SUCCESS", "3 Chunk(s) live; Begründung: „Test“ | ok"]

    def test_v2_hash_matches_node(self) -> None:
        h = rag.sha256(json.dumps(self.FIELDS, ensure_ascii=False, separators=(",", ":")))
        self.assertEqual(h, "76ae20ba85937bd4ee5464c6d045adabbee5b496392d90d215c256138bb32bb5")

    def test_legacy_hash_matches_node(self) -> None:
        self.assertEqual(rag.sha256("|".join(self.FIELDS)), "700994e4060b0fceaeb6fe800289d3efc8d4956288645d17a3cf0df18799eca2")

    def test_append_audit_anchor(self) -> None:
        db = {"meta": {"nextAudit": 1}, "audit_logs": []}
        rag.append_audit(db, "ciso@company.at", "L3", "X", "t", "SUCCESS", "d")
        e = db["audit_logs"][0]
        expected = rag.sha256(json.dumps([e["prevHash"], e["id"], e["timestamp"], e["actor"], e["role"], e["action"], e["target"], e["status"], e["detail"]], ensure_ascii=False, separators=(",", ":")))
        self.assertEqual(e["hash"], expected)
        anchor = hmac.new(SECRET.encode(), f"audit-anchor|1|{e['hash']}".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(db["meta"]["auditAnchor"], anchor)


class Chunker(unittest.TestCase):
    def test_kpi_line_is_content(self) -> None:
        _, chunks = rag.chunk_document("§ 1 Ziele\nDie Wiederanlaufzeit beträgt\n2 Stunden\nfür kritische Systeme.\n")
        self.assertEqual(len(chunks), 1)
        self.assertIn("2 Stunden", chunks[0]["content"])

    def test_markdown_table_rows_become_statements(self) -> None:
        _, chunks = rag.chunk_document("## §2 Ziele\n| Tier | RTO |\n|---|---|\n| Tier 1 | 2 Stunden |\n| Tier 2 | 12 Stunden |\n")
        text = chunks[0]["content"]
        self.assertIn("Tier: Tier 1 | RTO: 2 Stunden", text)
        self.assertIn("Tier: Tier 2 | RTO: 12 Stunden", text)




class Rbac(unittest.TestCase):
    def test_l1_gets_nothing_from_l2_documents(self) -> None:
        fresh_db([("Geheim_Policy.md", "§1 Fristen", "L2", "Kritische Systeme sind innerhalb von 2 Stunden wiederherzustellen.")])
        res = rag.run_query(token("L1"), "Innerhalb welcher Zeit sind kritische Systeme wiederherzustellen?")
        blob = json.dumps(res, ensure_ascii=False)
        self.assertEqual(res["result"]["kind"], "fallback")
        for marker in ("Geheim_Policy", "chk-t000", "2 Stunden wiederherzustellen"):
            self.assertNotIn(marker, blob)


if __name__ == "__main__":
    unittest.main()
