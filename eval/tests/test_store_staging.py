"""Store and Pipeline A: audit chain parity with Node, approval/merge, RBAC, Ollama request payload."""
from __future__ import annotations

import hashlib
import hmac
import json
import unittest

import httpx
from unittest import mock

from helpers import SECRET, fresh_db, rag, token


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


class Approval(unittest.TestCase):
    DOC = "## §1 Zweck\nDiese Richtlinie regelt Backups.\n\n## §2 Fristen\nBackups werden täglich erstellt.\n\n## §3 Aufbewahrung\nBackups werden 30 Tage aufbewahrt.\n"

    def setUp(self) -> None:
        fresh_db([])
        rag.VECTORS.__init__()
        rag.stage_document(token("L2"), doc_name="Backup_Richtlinie.md", text=self.DOC, min_role="L2")
        diff = rag.read_db()["staged_diffs"][0]
        rag.approve_diff(token("L3"), diff["id"])

    def sections(self) -> list[str]:
        return sorted(c["section"] for c in rag.read_db()["live_chunks"])

    def test_partial_paste_removes_nothing(self) -> None:
        out = rag.stage_text(token("L2"), title="Fristen neu", target_doc="Backup_Richtlinie.md", new_text="## §2 Fristen\nBackups werden stündlich erstellt.", min_role="L2")
        self.assertEqual(out["diff"]["removedChunkIds"], [])
        rag.approve_diff(token("L3"), out["diff"]["id"])
        self.assertEqual(self.sections(), ["§1 Zweck", "§2 Fristen", "§3 Aufbewahrung"])
        self.assertTrue(any("stündlich" in c["content"] for c in rag.read_db()["live_chunks"]))

    def test_full_upload_lists_and_removes_dropped_clauses(self) -> None:
        text = "## §1 Zweck\nDiese Richtlinie regelt Backups.\n\n## §2 Fristen\nBackups werden stündlich erstellt.\n"
        out = rag.stage_document(token("L2"), doc_name="Backup_Richtlinie_v2.md", text=text, min_role="L2")
        removed = out["diff"]["removedChunkIds"]
        live = {c["id"]: c for c in rag.read_db()["live_chunks"]}
        self.assertEqual([live[i]["section"] for i in removed], ["§3 Aufbewahrung"])
        rag.approve_diff(token("L3"), out["diff"]["id"])
        self.assertEqual(self.sections(), ["§1 Zweck", "§2 Fristen"])

    def test_approval_requires_l3(self) -> None:
        out = rag.stage_text(token("L2"), title="Fristen neu", target_doc="Backup_Richtlinie.md", new_text="## §2 Fristen\nBackups werden wöchentlich erstellt.", min_role="L2")
        with self.assertRaises(rag.ApiError) as ctx:
            rag.approve_diff(token("L2"), out["diff"]["id"])
        self.assertEqual(ctx.exception.status, 403)


class Rbac(unittest.TestCase):
    def test_l1_gets_nothing_from_l2_documents(self) -> None:
        fresh_db([("Geheim_Policy.md", "§1 Fristen", "L2", "Kritische Systeme sind innerhalb von 2 Stunden wiederherzustellen.")])
        res = rag.run_query(token("L1"), "Innerhalb welcher Zeit sind kritische Systeme wiederherzustellen?")
        blob = json.dumps(res, ensure_ascii=False)
        self.assertEqual(res["result"]["kind"], "fallback")
        for marker in ("Geheim_Policy", "chk-t000", "2 Stunden wiederherzustellen"):
            self.assertNotIn(marker, blob)


class OllamaPayload(unittest.TestCase):
    def test_think_off_only_for_qwen3(self) -> None:
        for model, think in (("qwen3:14b", False), ("qwen2.5:7b", None)):
            with mock.patch.object(rag.OLLAMA, "llm_model", return_value=model), mock.patch.object(httpx, "post") as post:
                post.return_value.json.return_value = {"message": {"content": "JA"}}
                self.assertEqual(rag.OLLAMA.chat("s", "u"), "JA")
                self.assertEqual(post.call_args.kwargs["json"].get("think"), think)
                self.assertEqual(post.call_args.kwargs["json"]["options"]["num_ctx"], rag.LLM_NUM_CTX)
                self.assertEqual(post.call_args.kwargs["json"]["keep_alive"], rag.LLM_KEEP_ALIVE)
