"""Displayed spans and search hits."""
from __future__ import annotations

from typing import Any

from .gate import ANAPHOR, Part
from ..search.lexical import coverage
from ..search.normalize import TABLE_LABEL


def display_span(text: str) -> str:
    """Span as shown to the user: the generated 'Tabelle [§…]:' prefix is a chunker label, not document text, and a table
    row reads as a 'Spalte: Wert; Spalte: Wert' list."""
    return TABLE_LABEL.sub("", text).replace(" | ", "; ") if TABLE_LABEL.match(text) else text


def related_spans(idx: "LexIndex", part: Part, w: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Spans of a window shown to the user: the answering (last) span, a preceding span only when it contributes a key term
    that the answering span and its section/document title lack, and the antecedent of an anaphoric next span — no
    neighbouring sentence without relation to the question."""
    li, lk = w[-1]
    known = idx.span_terms[li][lk] | idx.ctx_terms[li]
    keep = []
    for n, (i, k) in enumerate(w):
        nxt = w[n + 1] if n + 1 < len(w) else None
        adds = any(any(a <= idx.span_terms[i][k] for a in kt.alts) and not any(a <= known for a in kt.alts) for kt in part.qt.keys)
        if nxt is None or adds or ANAPHOR.match(idx.span_text[nxt[0]][nxt[1]]):
            keep.append((i, k))
    return keep


CONTRADICTION_LABEL = "Mögliche Widersprüche – nicht als Antwort bestätigt"


def search_hits(idx: "LexIndex", parts: list[Part], per_part: int = 3) -> list[dict[str, Any]]:
    """Unconfirmed parts above the lexical floor or the dense gate: their best windows (then the next fused chunks), RBAC-filtered by construction
    (only chunks of the role view are ever ranked)."""
    hits: list[dict[str, Any]] = []
    for p in parts:
        if not ((p.lexical_ok and not p.capped_only) or p.dense_ok):
            continue
        cand = [(w[-1][0], " ".join(display_span(idx.span_text[i][k]) for i, k in related_spans(idx, p, w))) for w in p.windows]
        for i in p.ranked:  # further chunks: only their sentences that share a key term (all sentences when none does);
            # instruction-like spans (prompt injection) are never shown, as in answers
            ok = [k for k in range(len(idx.span_text[i])) if not idx.span_instr[i][k]]
            best = max((coverage(p.qt, idx.span_terms[i][k]) for k in ok), default=0.0)
            rel = [display_span(idx.span_text[i][k]) for k in ok if best > 0 and coverage(p.qt, idx.span_terms[i][k]) >= 0.5 * best]
            text = " ".join(rel) or " ".join(display_span(idx.span_text[i][k]) for k in ok)
            if text:
                cand.append((i, text))
        n = 0
        for i, text in cand:
            if n >= per_part or any(h["chunkId"] == idx.chunks[i]["id"] for h in hits):
                continue
            c = idx.chunks[i]
            hits.append({"chunkId": c["id"], "docName": c["docName"], "section": c["section"], "content": text})
            n += 1
    return hits
