"""Document parsing: PDF (layout mode, tables with and without blank lines between rows), DOCX, Markdown/TXT."""
from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import Any

from ..config import ApiError, MAX_UPLOAD_BYTES


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


def _layout_rows(block: list[str], starts: list[int], one_value: bool = False) -> list[str] | None:
    """Cells → columns. None when any assignment is uncertain (cell far from every column start, reaching into the next
    column, or two cells of one line in the same column): the block is then treated as running text, never as a table
    with values in the wrong column. one_value: rows split without blank lines — a second line carrying a number into a
    cell that already has one is a merged row, not a wrapped cell (uncertain → None)."""
    cols = [""] * len(starts)
    for line in block:
        used: set[int] = set()
        for pos, text in _layout_cells(line):
            k = min(range(len(starts)), key=lambda j: (abs(starts[j] - pos), j))
            if abs(starts[k] - pos) > LAYOUT_TOL or k in used or (k + 1 < len(starts) and pos + len(text) > starts[k + 1] + 1):
                return None
            if one_value and re.search(r"\d", cols[k]) and re.search(r"\d", text):
                return None
            used.add(k)
            cols[k] = _join_cell(cols[k], text)
    return [c.replace("|", "\\|").strip() for c in cols]


CONT_END = re.compile(r"(?:[-/&,+(]|\b(?:or|and|und|oder|with|mit|of|der|des|für|for))$")


def _continues(prev: dict[int, str], cells: list[tuple[int, str]], starts: list[int]) -> bool:
    """Visible evidence that a line continues the cells above it: a cell above ends open ('ChaCha20-', 'Storage /', 'und',
    an unclosed bracket) or a cell starts lower case / with a closing bracket."""
    for pos, text in cells:
        k = min(range(len(starts)), key=lambda j: (abs(starts[j] - pos), j))
        above = prev.get(k, "")
        if text[:1].islower() or text[:1] == ")" or (above and (CONT_END.search(above) or above.count("(") > above.count(")"))):
            return True
    return False


def _row_groups(block: list[str], starts: list[int], header: bool) -> list[list[str]] | None:
    """Lines of a table without blank lines between rows → one line group per row, via the first column. A line without a
    first-column cell continues the current row (wrapped cells); a line with one starts a new row when it fills every
    column and shows no continuation evidence (_continues), continues the row when it shows such evidence, and is
    uncertain otherwise (partial line, capitalised: new row with an empty cell or a wrapped first cell?) → None, i.e.
    running text. Rows must also agree on numeric columns: a row without a number in a column where every other row has
    one is a wrapped line split off by mistake → None."""
    groups: list[list[str]] = []
    prev: dict[int, str] = {}
    for line in block:
        cells = _layout_cells(line)
        if not cells:
            continue
        first = abs(cells[0][0] - starts[0]) <= LAYOUT_TOL
        if groups and (not first or _continues(prev, cells, starts)):
            groups[-1].append(line)
        elif not groups or len(cells) == len(starts):
            groups.append([line])
            prev = {}
        else:
            return None
        for pos, text in cells:
            prev[min(range(len(starts)), key=lambda j: (abs(starts[j] - pos), j))] = text
    body = [_layout_rows(g, starts) for g in groups[1 if header else 0:]]
    if len(body) >= 2 and all(r is not None for r in body):
        for k in range(len(starts)):
            numeric = [bool(re.search(r"\d", r[k])) for r in body if r is not None and r[k]]
            if len(numeric) >= 3 and sum(numeric) == len(numeric) - 1:
                return None
    return groups


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
        if len(_layout_cells(widest)) >= 3:
            rows = []
            j = i + 1
            while j < len(blocks) and len(_layout_cells(blocks[j][0])) >= 2:
                rows.append(blocks[j])
                j += 1
            starts = [p for p, _ in _layout_cells(widest)]
            header, body = None, []
            if rows and len(head) <= 3:
                header = _layout_rows(head, starts)
                # rows separated by blank lines: one block = one row (wrapped cells may fill every column); a single
                # block after the header holds all rows without blank lines and is split by the first column
                groups = _row_groups(rows[0], starts, False) if len(rows) == 1 else rows
                tight = len(rows) == 1
                body = [_layout_rows(g, starts, tight) for g in groups] if groups else []
            elif not rows and len(head) >= 3:  # whole table in one block: header row + ≥ 2 rows split by the first column
                groups = _row_groups(head, starts, True)
                if groups and len(groups) >= 3:
                    header = _layout_rows(groups[0], starts)
                    body = [_layout_rows(g, starts, True) for g in groups[1:]]
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
