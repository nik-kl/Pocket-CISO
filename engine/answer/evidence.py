"""Evidence pack (v2): the numbered spans [S1..Sn] one structured LLM call answers from — role view only, no instruction spans."""
from __future__ import annotations

from dataclasses import dataclass

from .gate import Part
from ..search.lexical import coverage, doc_title, section_title
from ..search.normalize import TABLE_LABEL

PACK_SPANS = 8
PACK_TOKENS = 2500


@dataclass
class Entry:
    chunk: int
    lo: int  # first and last sentence of the chunk shown (anchor ± one neighbour)
    hi: int


def _anchors(idx: "LexIndex", part: Part) -> list[tuple[int, int]]:
    """Spans of one part by priority: its selected windows (answering span first), then per further ranked chunk the span
    with the highest key-term coverage."""
    out = [s for w in part.windows for s in reversed(w)]
    for i in part.ranked:
        ok = [k for k in range(len(idx.span_text[i])) if not idx.span_instr[i][k]]
        if ok:
            out.append((i, max(ok, key=lambda k: (coverage(part.qt, idx.span_terms[i][k], idx.ctx_terms[i]), -k))))
    return out


def entry_sentences(idx: "LexIndex", e: Entry) -> list[str]:
    return [TABLE_LABEL.sub("", idx.span_text[e.chunk][k]) for k in range(e.lo, e.hi + 1)]


def entry_text(idx: "LexIndex", e: Entry) -> str:
    return " ".join(entry_sentences(idx, e))


def entry_path(idx: "LexIndex", e: Entry) -> str:
    c = idx.chunks[e.chunk]
    return f"{doc_title(c['docName'])} › {section_title(c['section'])}"


def _tokens(text: str) -> int:
    return len(text) // 3  # shortcut: chars/3 ≈ Qwen tokens for German prose, switch to the model tokenizer if the cap ever binds


def evidence_pack(idx: "LexIndex", parts: list[Part], max_spans: int = PACK_SPANS, max_tokens: int = PACK_TOKENS) -> list[Entry]:
    """Anchors of all parts interleaved (deterministic: part order, then priority); each anchor comes with the previous and
    the next sentence of its chunk (a table row alone: rows are self-contained and carry their column labels); anchors that
    touch an entry of the same chunk extend it; at most `max_spans` entries and about `max_tokens` tokens."""
    lists = [_anchors(idx, p) for p in parts]
    pack: list[Entry] = []
    used = 0
    for n in range(max((len(a) for a in lists), default=0)):
        for anchors in lists:
            if n >= len(anchors):
                continue
            i, k = anchors[n]
            spans = idx.span_text[i]
            lo, hi = k, k
            if not TABLE_LABEL.match(spans[k]):
                lo = k - 1 if k > 0 and not TABLE_LABEL.match(spans[k - 1]) else k
                hi = k + 1 if k + 1 < len(spans) and not idx.span_instr[i][k + 1] and not TABLE_LABEL.match(spans[k + 1]) else k
            near = next((e for e in pack if e.chunk == i and e.lo - 1 <= hi and lo <= e.hi + 1), None)
            if near:
                before = _tokens(entry_text(idx, near))
                near.lo, near.hi = min(near.lo, lo), max(near.hi, hi)
                used += _tokens(entry_text(idx, near)) - before
                continue
            e = Entry(i, lo, hi)
            cost = _tokens(entry_path(idx, e) + entry_text(idx, e))
            if len(pack) >= max_spans or used + cost > max_tokens:
                return pack
            pack.append(e)
            used += cost
    return pack


def pack_prompt(idx: "LexIndex", pack: list[Entry]) -> str:
    return "\n\n".join(f"[S{n + 1}] {entry_path(idx, e)}\n{entry_text(idx, e)}" for n, e in enumerate(pack))
