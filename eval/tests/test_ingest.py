"""Parsing and chunking: generated PDFs (layout tables, rows without blank lines), Markdown chunker, document names."""
from __future__ import annotations

import unittest

from helpers import ROOT, fresh_db, make_pdf, rag, token


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


    def test_headings_and_bullets(self) -> None:
        title, chunks = rag.chunk_document("# Titel\n## §1 Zweck\nDieser Text beschreibt den Zweck.\n## §2 Regeln\n- erste Regel\n- zweite Regel\n")
        self.assertEqual(title, "Titel")
        self.assertEqual([c["section"] for c in chunks], ["§1 Zweck", "§2 Regeln"])
        self.assertIn("erste Regel. zweite Regel.", chunks[1]["content"])

    def test_numbered_lines_are_not_headings_in_paragraph_documents(self) -> None:
        _, chunks = rag.chunk_document("§ 1 Ablauf\nDie Schritte sind:\n1. Host Isolation\n2. Volatile Capture\n§ 2 Fristen\nInnerhalb von 4 Stunden.\n")
        self.assertEqual([c["section"] for c in chunks], ["§1 Ablauf", "§2 Fristen"])

    def test_chunking_is_deterministic(self) -> None:
        text = (ROOT / "eval" / "corpus" / "seed" / "Passwort_Richtlinie_v4.md").read_text(encoding="utf-8")
        self.assertEqual(rag.chunk_document(text), rag.chunk_document(text))


class PdfLayoutTables(unittest.TestCase):
    """PDFs generated here (not from the corpus): aligned tables become row statements; running text with multiple spaces
    never becomes a table, and no value may land in another column."""

    def test_wide_courier_table_with_wrapped_cells(self) -> None:
        pdf = make_pdf([[(50, "Section 1 Backup Targets")], [],
                         [(50, "System"), (200, "Restore Time"), (330, "Data Loss"), (450, "Owner")], [],
                         [(50, "Payroll"), (200, "4 Stunden"), (330, "30 Minuten"), (450, "Finance Lead")], [(50, "database"), (450, "Team")], [],
                         [(50, "Web Shop"), (200, "8 Stunden"), (330, "1 Stunde"), (450, "IT Ops")], [],
                         [(50, "Plain text follows here as a normal paragraph line.")]])
        _, chunks = rag.chunk_document(rag.parse_document("t.pdf", pdf))
        text = " ".join(c["content"] for c in chunks)
        self.assertIn("System: Payroll database | Restore Time: 4 Stunden | Data Loss: 30 Minuten | Owner: Finance Lead Team", text)
        self.assertIn("System: Web Shop | Restore Time: 8 Stunden | Data Loss: 1 Stunde | Owner: IT Ops", text)

    ROWS = [[(50, "Geraet"), (200, "Pruefintervall"), (350, "Verantwortlich")],
            [(50, "Kran 3"), (200, "6 Monate"), (350, "Hallenmeister")],
            [(50, "Gabelstapler"), (200, "12 Monate"), (350, "Fuhrparkleitung")], [(50, "und Anhaenger"), (350, "Nord")],
            [(50, "Hebebuehne"), (200, "3 Monate"), (350, "Werkstatt")]]

    def test_rows_without_blank_lines_are_split_by_first_column(self) -> None:
        # Word export pattern: no blank line between rows; wrapped cells (lower-case continuation line) stay in their row
        tight = make_pdf(self.ROWS)
        spaced = make_pdf([self.ROWS[0], [], self.ROWS[1], [], self.ROWS[2], self.ROWS[3], [], self.ROWS[4]])
        header_apart = make_pdf([self.ROWS[0], []] + self.ROWS[1:])
        for pdf in (tight, spaced, header_apart):
            _, chunks = rag.chunk_document(rag.parse_document("t.pdf", pdf))
            text = " ".join(c["content"] for c in chunks)
            self.assertIn("Geraet: Kran 3 | Pruefintervall: 6 Monate | Verantwortlich: Hallenmeister.", text)
            self.assertIn("Geraet: Gabelstapler und Anhaenger | Pruefintervall: 12 Monate | Verantwortlich: Fuhrparkleitung Nord.", text)
            self.assertIn("Geraet: Hebebuehne | Pruefintervall: 3 Monate | Verantwortlich: Werkstatt.", text)

    def test_uncertain_row_boundary_falls_back_to_text(self) -> None:
        # a capitalised partial line could be a new row with an empty cell or a wrapped first cell → running text
        partial = make_pdf(self.ROWS[:3] + [[(50, "Leiter"), (350, "Nord")]] + self.ROWS[4:])
        # a second number under a cell that already has one is a merged row, never one wrapped cell
        merged = make_pdf(self.ROWS[:2] + [[(50, "im Lager"), (200, "9 Monate"), (350, "Lager")]] + self.ROWS[2:])
        for pdf in (partial, merged):
            raw = rag.parse_document("t.pdf", pdf)
            self.assertNotIn("|", raw)
            self.assertIn("Kran 3 6 Monate Hallenmeister", raw)

    def test_justified_prose_is_not_a_table(self) -> None:
        pdf = make_pdf([[(50, "Section 2 Rules")], [],
                         [(50, "Die   Richtlinie   gilt   fuer   alle   Mitarbeitenden   und   Dienstleister.")],
                         [(50, "Ausnahmen   beduerfen   der   schriftlichen   Freigabe   durch   den   CISO.")], [],
                         [(50, "Passwoerter   muessen   mindestens   14   Zeichen   lang   sein   und   werden")],
                         [(50, "nicht   periodisch   gewechselt,   ausser   bei   Verdacht.")], [],
                         [(50, "Klasse"), (140, "Frist"), (210, "Verantwortlich")], [],
                         [(50, "Hoch"), (140, "2 Tage"), (210, "CISO")], [],
                         [(50, "Niedrig"), (140, "10 Tage"), (210, "Fachbereich")]], font="Helvetica")
        raw = rag.parse_document("t.pdf", pdf)
        self.assertIn("Die Richtlinie gilt fuer alle Mitarbeitenden und Dienstleister.", raw)
        self.assertIn("Passwoerter muessen mindestens 14 Zeichen lang sein und werden", raw)
        table_lines = [ln for ln in raw.splitlines() if ln.startswith("|") and "---" not in ln]
        self.assertEqual(table_lines, ["| Klasse | Frist | Verantwortlich |", "| Hoch | 2 Tage | CISO |", "| Niedrig | 10 Tage | Fachbereich |"])

    def test_misaligned_rows_fall_back_to_text(self) -> None:
        pdf = make_pdf([[(50, "Alpha"), (200, "Beta"), (350, "Gamma")], [],
                         [(50, "eins"), (120, "zwei drei vier fuenf sechs"), (350, "sieben")], [],
                         [(50, "acht"), (200, "neun"), (350, "zehn")]])
        raw = rag.parse_document("t.pdf", pdf)
        self.assertNotIn("|", raw)  # one row cannot be assigned safely → the whole block stays running text
        self.assertIn("eins zwei drei vier fuenf sechs sieben", raw)


    def test_centred_columns_within_tolerance(self) -> None:
        # pattern of the corpus risk matrix: header cells centred, drifting up to 6 characters from the row cells
        pdf = make_pdf([[(60, "Matrix"), (200, "Gering"), (330, "Mittel"), (460, "Hoch")],
                         [(60, "Wahrsch."), (180, "(kaum spuerbar)"), (318, "(Stoerfaktor)"), (430, "(Geschaeftskritisch)")], [],
                         [(60, "Selten"), (200, "1 (Gering)"), (330, "2 (Gering)"), (460, "3 (Gering)")], [],
                         [(60, "Haeufig"), (200, "4 (Moderat)"), (330, "8 (Kritisch)"), (460, "12 (Kritisch)")]])
        raw = rag.parse_document("t.pdf", pdf)
        self.assertIn("| Matrix Wahrsch. | Gering (kaum spuerbar) | Mittel (Stoerfaktor) | Hoch (Geschaeftskritisch) |", raw)
        self.assertIn("| Haeufig | 4 (Moderat) | 8 (Kritisch) | 12 (Kritisch) |", raw)

    def test_kpi_box_values_under_labels(self) -> None:
        pdf = make_pdf([[(80, "2 Stunden"), (270, "250.000 EUR"), (460, "6 Monate")], [(60, "ZIEL-RTO TIER 1"), (260, "SCHADEN / TAG"), (450, "REVISION")], [],
                         [(50, "Fliesstext danach.")]])
        _, chunks = rag.chunk_document(rag.parse_document("t.pdf", pdf))
        self.assertIn("ZIEL-RTO TIER 1: 2 Stunden | SCHADEN / TAG: 250.000 EUR | REVISION: 6 Monate", chunks[0]["content"])
        shifted = make_pdf([[(80, "2 Stunden"), (380, "250.000 EUR"), (460, "6 Monate")], [(60, "ZIEL-RTO TIER 1"), (260, "SCHADEN / TAG"), (450, "REVISION")]])
        self.assertNotIn("|", rag.parse_document("t.pdf", shifted))  # a value not under its label → running text


class DocumentNames(unittest.TestCase):
    def test_same_rule_as_node(self) -> None:
        # src/lib/db/staging.ts safeDocName keeps the same characters: letters, digits, space . _ - & , ( )
        self.assertEqual(rag.safe_doc_name("Richtlinie für IAM, Endgeräte & Wechselmedien (v2).pdf"), "Richtlinie für IAM, Endgeräte & Wechselmedien (v2).pdf")
        self.assertEqual(rag.safe_doc_name("a/b\\c<d>|e?.md"), "c_d__e_.md")

    def test_node_uses_the_same_character_class(self) -> None:
        ts = (ROOT / "src" / "lib" / "db" / "staging.ts").read_text(encoding="utf-8")
        self.assertIn('replace(/[^\\p{L}\\p{N}_.\\-&,() ]/gu, "_")', ts)
        self.assertIn("const DOC_NAME_RE = /^[\\p{L}\\p{N}_.\\-&,() ]{3,120}$/u;", ts)
        self.assertIn('re.sub(r"[^\\w.\\-&,() ]", "_", base)', (ROOT / "engine" / "ingest" / "parse.py").read_text(encoding="utf-8"))

    def test_legacy_underscore_name_is_the_same_series(self) -> None:
        self.assertTrue(rag.same_series("Business Impact Analyse _BIA_ _ Kritikalitätskatalog 2026.pdf", "Business Impact Analyse (BIA) & Kritikalitätskatalog 2026.pdf"))
        self.assertTrue(rag.same_series("Richtlinie für IAM_ Endgeräte _ Wechselmedien.pdf", "Richtlinie für IAM, Endgeräte & Wechselmedien.pdf"))

    def test_reimport_replaces_legacy_document_without_duplicate(self) -> None:
        fresh_db([])
        rag.VECTORS.__init__()
        legacy, new = "Backup _Daten_ _ Archiv.md", "Backup (Daten) & Archiv.md"
        rag.stage_document(token("L2"), doc_name=legacy, text="## §1 Zweck\nBackups werden täglich erstellt.\n", min_role="L2")
        rag.approve_diff(token("L3"), rag.read_db()["staged_diffs"][0]["id"])
        out = rag.stage_document(token("L2"), doc_name=new, text="## §1 Zweck\nBackups werden stündlich erstellt.\n", min_role="L2")
        self.assertEqual(out["diff"]["replacesDoc"], legacy)
        rag.approve_diff(token("L3"), out["diff"]["id"])
        self.assertEqual({c["docName"] for c in rag.read_db()["live_chunks"]}, {new})
