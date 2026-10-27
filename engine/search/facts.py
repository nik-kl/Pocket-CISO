"""Exact facts and unit-aware values (durations in minutes, frequencies, identifiers) stored at approval time."""
from __future__ import annotations

import re
from typing import Any

from .normalize import (
    FREQ_WORDS, R_STOP, TABLE_LABEL, UNIT_CANON, _LEX, _num, content_stems, fold,
    norm, split_sentences,
)


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


FACTS_VERSION = 1
QTY_RE = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)*)\s*(%|[A-Za-zÄÖÜäöüß]+\b)?")
DURATION_MIN = {"s": 1 / 60, "min": 1.0, "h": 60.0, "d": 1440.0, "w": 10080.0, "mo": 43200.0, "y": 518400.0}  # month = 30 d, year = 12 months


def _to_float(v: str) -> float | None:
    try:
        return float(_num(v))
    except ValueError:
        return None


def _identifier_before(text: str, pos: int) -> bool:
    """'Tier 1', 'Stufe 3', 'TLS 1.3', 'AES-256': a capitalised word or acronym (not a function word) directly before the number."""
    m = re.search(r"([A-Za-zÄÖÜäöüß]+)(-|\s)$", text[max(0, pos - 40) : pos])
    if not m:
        return False
    word = m.group(1)
    return m.group(2) == "-" or (word[:1].isupper() and fold(word) not in R_STOP)


def values_in(text: str) -> list[tuple[str, float, str]]:
    """[(dimension, value in base unit, surface)] — durations in minutes ('time'), other units by name, bare numbers as 'num'.
    Frequency words count as durations (jährlich = 12 Monate). Dates, version numbers and section numbers are skipped."""
    out: list[tuple[str, float, str]] = []
    for m in QTY_RE.finditer(text):
        raw, word = m.group(1), m.group(2) or ""
        if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{2,4}", raw) or text[max(0, m.start() - 2) : m.start()].strip() in ("§", "v", "V"):
            continue
        v = _to_float(raw)
        if v is None:
            continue
        unit = "pct" if word == "%" else UNIT_CANON.get(fold(word)) if word else None
        if unit in DURATION_MIN:
            out.append(("time", v * DURATION_MIN[unit], f"{raw} {word}"))
        elif unit:
            out.append((unit, v, f"{raw} {word}"))
        elif _identifier_before(text, m.start()):
            continue  # identifier number: "Tier 1", "TLS 1.3", "AES-256", "ISO 27001"
        else:
            out.append(("num", v, raw))
    for w in _LEX.findall(fold(text)):
        if w in FREQ_WORDS:
            q = FREQ_WORDS[w]
            n, u = re.match(r"(\d+)(\w+)", q).groups()
            out.append(("time", float(n) * DURATION_MIN[u], w))
    return out


def extract_facts(content: str) -> list[dict[str, Any]]:
    """Structured facts (span, subject, attribute, value, unit, surface): every quantity cell of a table row (subject = first
    cell, attribute = column header) and every clear value sentence (≤ 2 distinct values; subject = the sentence itself)."""
    facts: list[dict[str, Any]] = []
    for k, span in enumerate(split_sentences(content) or [content]):
        if span.startswith("Tabelle ["):
            body = TABLE_LABEL.sub("", span).rstrip(".")
            cells = [c.strip() for c in re.split(r"\s+\|\s+", body)]
            pairs = [c.split(": ", 1) if ": " in c else ["", c] for c in cells]
            subject = pairs[0][1] if pairs else ""
            for attr, val in pairs[1:]:
                for dim, base, surface in values_in(val):
                    facts.append({"span": k, "subject": subject, "attribute": attr, "dim": dim, "value": round(base, 6), "surface": surface.strip()})
            continue
        vals = values_in(span)
        if vals and len({(d, v) for d, v, _ in vals}) <= 2:
            for dim, base, surface in vals:
                facts.append({"span": k, "subject": "", "attribute": "", "dim": dim, "value": round(base, 6), "surface": surface.strip()})
    return facts


def chunk_facts(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    """Facts stored in the DB chunk record at approval time (Python); recomputed when missing (e.g. a Node-side approval)."""
    if chunk.get("factsVersion") == FACTS_VERSION and isinstance(chunk.get("facts"), list):
        return chunk["facts"]
    return extract_facts(chunk["content"])
