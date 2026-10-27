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
        text = (ROOT / "dummy-data" / "comp_1_policy-samples" / "docs" / "Passwort_Richtlinie_v4.md").read_text(encoding="utf-8")
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


def ruled_pdf(story_of) -> bytes:
    """Realistically typeset PDF via reportlab (dev dependency, engine/requirements-dev.txt): story_of(styles) → flowables."""
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4).build(story_of(getSampleStyleSheet()))
    return buf.getvalue()


def _grid(data, widths, repeat=1, spans=()):
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    t = Table(data, colWidths=widths, repeatRows=repeat)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP")] +
                          [("SPAN", a, b) for a, b in spans]))
    return t


try:
    import reportlab  # noqa: F401
    HAVE_REPORTLAB = True
except ImportError:
    HAVE_REPORTLAB = False


@unittest.skipUnless(HAVE_REPORTLAB, "reportlab (engine/requirements-dev.txt) not installed")
class PdfRuledTables(unittest.TestCase):
    """pdfplumber on ruled tables (class b): every row on its own with the right columns (invented content)."""

    @staticmethod
    def rows(pdf: bytes) -> list[dict[str, str]]:
        """Data rows of the parsed Markdown tables as {header: cell}."""
        out, header = [], None
        for ln in rag.parse_document("Test.pdf", pdf).splitlines():
            if ln.startswith("|---"):
                continue
            if not ln.startswith("| "):
                header = None
                continue
            cells = [c.strip() for c in ln.strip("| ").split(" | ")]
            if header is None:
                header = cells
            else:
                out.append(dict(zip(header, cells)))
        return out

    def test_wrapped_cells_and_header_with_running_text(self) -> None:
        def story(st):
            from reportlab.platypus import Paragraph as P, Spacer
            cell = st["BodyText"]
            rows = [[P("Futterart", cell), P("Fütterungs&shy;intervall laut Tierarzt", cell), P("Zuständige Pflegekraft", cell)],
                    [P("Frischfisch für Pinguine und Seehunde aus dem Kühlhaus", cell), P("2 Mal täglich", cell), P("Revier Nord mit Vertretung", cell)],
                    [P("Heu", cell), P("alle 3 Tage nach Sichtprüfung des Lagers", cell), P("Revier Süd", cell)],
                    [P("Insekten", cell), P("wöchentlich", cell), P("Terrarienteam", cell)]]
            return [P("§ 1 Fütterung", st["Heading2"]), P("Die folgende Übersicht regelt die Fütterung im Tierpark. Abweichungen "
                    "genehmigt die Zooleitung schriftlich.", cell), Spacer(1, 8), _grid(rows, [170, 150, 140]), Spacer(1, 8),
                    P("Nach jeder Fütterung wird das Futterbuch abgezeichnet.", cell)]
        h = ("Futterart", "Fütterungsintervall laut Tierarzt", "Zuständige Pflegekraft")  # wrapped header cells joined
        self.assertEqual(self.rows(ruled_pdf(story)), [
            dict(zip(h, ("Frischfisch für Pinguine und Seehunde aus dem Kühlhaus", "2 Mal täglich", "Revier Nord mit Vertretung"))),
            dict(zip(h, ("Heu", "alle 3 Tage nach Sichtprüfung des Lagers", "Revier Süd"))),
            dict(zip(h, ("Insekten", "wöchentlich", "Terrarienteam")))])
        text = rag.parse_document("Test.pdf", ruled_pdf(story))
        flat = " ".join(text.split())
        self.assertIn("Abweichungen genehmigt die Zooleitung schriftlich.", flat)  # running text around the table stays
        self.assertIn("Nach jeder Fütterung wird das Futterbuch abgezeichnet.", flat)
        _, chunks = rag.chunk_document(text)  # → self-contained statements in the existing row format
        self.assertIn("Tabelle [§1 Fütterung]: Futterart: Heu | Fütterungsintervall laut Tierarzt: alle 3 Tage nach Sichtprüfung des "
                      "Lagers | Zuständige Pflegekraft: Revier Süd.", " ".join(c["content"] for c in chunks))

    def test_table_across_page_break(self) -> None:
        for repeat in (1, 0):  # header repeated on the next page, or not
            def story(st, repeat=repeat):
                from reportlab.platypus import Paragraph as P, Spacer
                rows = [["Gehege", "Reinigung", "Kontrolle"]] + [[f"Gehege {n}", f"{n} Tage", f"Team {n}"] for n in range(1, 61)]
                return [P("§ 2 Reinigung", st["Heading2"]), P("Einleitender Text zur Reinigung.", st["BodyText"]), Spacer(1, 300),
                        _grid(rows, [150, 150, 150], repeat=repeat)]
            rows = self.rows(ruled_pdf(story))
            self.assertEqual(rows, [{"Gehege": f"Gehege {n}", "Reinigung": f"{n} Tage", "Kontrolle": f"Team {n}"} for n in range(1, 61)], repeat)

    def test_two_row_header_and_row_span(self) -> None:
        def story(st):
            rows = [["Artikel", "Preis", ""], ["", "netto", "brutto"], ["Brezel", "1 EUR", "1,07 EUR"], ["Semmel", "2 EUR", "2,14 EUR"]]
            return [_grid(rows, [150, 150, 150], repeat=2, spans=[((1, 0), (2, 0)), ((0, 0), (0, 1))])]
        self.assertEqual(self.rows(ruled_pdf(story)), [{"Artikel": "Brezel", "Preis netto": "1 EUR", "Preis brutto": "1,07 EUR"},
                                                       {"Artikel": "Semmel", "Preis netto": "2 EUR", "Preis brutto": "2,14 EUR"}])


class TextExports(unittest.TestCase):
    """Plain-text/Markdown export classes: HTML remnants, pipe tables with blank lines and wrapped rows, ASCII tables
    without outer pipes (invented content)."""

    @staticmethod
    def statements(name: str, text: str) -> str:
        _, chunks = rag.chunk_document(rag.parse_document(name, text.encode("utf-8-sig")))
        return " ".join(c["content"] for c in chunks)

    def test_html_remnants_and_crlf(self) -> None:
        raw = rag.parse_document("faq.txt", "Frage: Kaffee &amp; Tee?<br>Antwort:&nbsp;Nur in der K&uuml;che &gt; 2. Stock.\r\n".encode("utf-8-sig"))
        self.assertEqual(raw.strip(), "Frage: Kaffee & Tee?\nAntwort: Nur in der Küche > 2. Stock.")

    def test_pipe_table_with_blank_lines_and_wrapped_cells(self) -> None:
        text = ("## 1 Geräte\n\n| Gerät      | Zweck                   | Wartung |\n|------------|-------------------------|---------|\n\n"
                "| Ofen 1     | Brot, Brötchen,         | 6 Monate |\n|            | Gebäck                  |          |\n\n"
                "| Kühlraum   | Lagerung                | 12 Monate |\n")
        st = self.statements("geraete.md", text)
        self.assertIn("Gerät: Ofen 1 | Zweck: Brot, Brötchen, Gebäck | Wartung: 6 Monate", st)
        self.assertIn("Gerät: Kühlraum | Zweck: Lagerung | Wartung: 12 Monate", st)

    def test_continuation_row_without_blank_lines(self) -> None:
        text = "| Raum | Schlüssel |\n|---|---|\n| Archiv | Hausmeister |\n|  | und Empfang |\n| Labor | Laborleitung |\n"
        st = self.statements("raeume.md", text)
        self.assertIn("Raum: Archiv | Schlüssel: Hausmeister und Empfang", st)
        self.assertIn("Raum: Labor | Schlüssel: Laborleitung", st)

    def test_ascii_table_without_outer_pipes(self) -> None:
        text = ("Aufbewahrung der Unterlagen\n\nUnterlage            | Frist\n---------------------+--------------\n"
                "Stundenzettel        | 2 Jahre\nUrlaubsantrag        | 1 Jahr nach Ende\n\nWeiterer Text.\n")
        st = self.statements("fristen.txt", text)
        self.assertIn("Unterlage: Stundenzettel | Frist: 2 Jahre", st)
        self.assertIn("Unterlage: Urlaubsantrag | Frist: 1 Jahr nach Ende", st)


@unittest.skipUnless(HAVE_REPORTLAB, "reportlab (engine/requirements-dev.txt) not installed")
class PdfTwoColumns(unittest.TestCase):
    def test_columns_are_read_one_after_the_other(self) -> None:
        import io
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import BaseDocTemplate, Frame, FrameBreak, PageTemplate, Paragraph
        st = getSampleStyleSheet()["BodyText"]
        left = ("Gäste melden sich am Empfang im Erdgeschoss an und erhalten dort einen Gästeausweis mit Foto. " * 4).strip()
        right = ("Das Rauchen ist nur auf der Dachterrasse erlaubt, niemals in den Werkstätten oder Lagern. " * 4).strip()
        buf = io.BytesIO()
        doc = BaseDocTemplate(buf, pagesize=A4)
        w, h = A4
        doc.addPageTemplates([PageTemplate(frames=[Frame(60, 100, w / 2 - 75, h - 200), Frame(w / 2 + 15, 100, w / 2 - 75, h - 200)])])
        doc.build([Paragraph(left, st), FrameBreak(), Paragraph(right, st)])
        flat = " ".join(rag.parse_document("Merkblatt.pdf", buf.getvalue()).split())
        self.assertIn(left, flat)  # the left column as one text, not interleaved with the right one
        self.assertIn(right, flat)


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
