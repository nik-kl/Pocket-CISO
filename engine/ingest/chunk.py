"""Semantic chunking: headings (## / §N / 1.2 Title) → atomic chunks ≤ 600 chars; table rows become statements."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..config import MAX_CHUNK_CHARS
from .parse import PAGE_FOOTER
from ..search.normalize import split_sentences


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
