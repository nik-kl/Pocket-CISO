"""Answer composition: displayed spans, LLM phrasing checked sentence by sentence, search hits."""
from __future__ import annotations

import re
from typing import Any

from .gate import ANAPHOR, Part
from .verify import Sentence, verify_sentence
from ..config import ANSWER_PHRASING, LLM_TIMEOUT
from ..llm import OLLAMA
from ..search.lexical import coverage
from ..search.normalize import TABLE_LABEL, Vocab, analyze, split_sentences


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


# ── answer sentences + verifier ──

PHRASE_SYSTEM = (
    "Du formulierst eine Antwort ausschließlich aus nummerierten, freigegebenen Textstellen. Regeln: höchstens drei Sätze; "
    "jeder Satz endet mit den Nummern der verwendeten Textstellen, z. B. [S1] oder [S1][S2]; Zahlen mit Einheiten, Verneinungen "
    "(nicht, kein, verboten), Modalverben (muss, darf, soll, kann) und Rollen oder Stellen (z. B. CISO, Geschäftsführung, BSI) "
    "werden wörtlich übernommen; nichts hinzufügen, was nicht in den Textstellen steht; Sprache der Textstellen verwenden."
)

CONTRADICTION_LABEL = "Mögliche Widersprüche – nicht als Antwort bestätigt"


def parse_phrased(out: str, span_ids: list[str]) -> list[Sentence]:
    sents: list[Sentence] = []
    for raw in split_sentences(out.replace("\n", " ")):
        refs = [int(n) for n in re.findall(r"S(\d+)", " ".join(re.findall(r"\[([^\]]*)\]", raw)))]
        text = re.sub(r"\s*\[[^\]]*\]", "", raw).strip()
        if not text:
            if sents and refs:  # citation block after the sentence end
                sents[-1].spans += [span_ids[r - 1] for r in refs if 0 < r <= len(span_ids) and span_ids[r - 1] not in sents[-1].spans]
            continue
        sents.append(Sentence(text, list(dict.fromkeys(span_ids[r - 1] for r in refs if 0 < r <= len(span_ids))), "llm"))
    return sents


def compose_sentences(q: str, spans: list[tuple[str, str]], vocab: Vocab, use_llm: bool) -> tuple[list[Sentence], str]:
    """spans = [(span_id, text)] in answer order. Verbatim by default; the LLM may only combine/rephrase them (≤ 3 sentences),
    and every LLM sentence that fails the verifier is replaced by the verbatim spans it cited."""
    verbatim = [Sentence(display_span(t), [sid], "verbatim") for sid, t in spans]
    # table rows stay verbatim: phrased rows dropped the asked column (the sanction of an IAM row) yet passed the verifier
    prose = [(sid, t) for sid, t in spans if not TABLE_LABEL.match(t)]
    if not use_llm or not prose:
        return verbatim, "verbatim"
    if not ANSWER_PHRASING:  # measurement variant: the answer is the verbatim evidence spans
        return verbatim, "verbatim (Formulierung aus)"
    text_of = dict(spans)
    prompt = "Frage: " + q + "\nTextstellen:\n" + "\n".join(f"[S{k + 1}] {t}" for k, (_, t) in enumerate(prose)) + "\nAntwort:"
    out = OLLAMA.chat(PHRASE_SYSTEM, prompt, timeout=LLM_TIMEOUT, max_tokens=220)
    if not out:
        return verbatim, "verbatim (LLM nicht verfügbar)"
    ids = [sid for sid, _ in prose]
    result: list[Sentence] = []
    for s in parse_phrased(out, ids)[:3]:
        if any(" ".join(r.text.split()) == " ".join(s.text.split()) for r in result):
            continue  # the model repeated a sentence (one copy per question part): show it once
        used = [sid for sid in s.spans if set(analyze(s.text, vocab)) & set(analyze(text_of[sid], vocab))]
        problems = verify_sentence(s.text, [text_of[sid] for sid in used], vocab)
        if s.text.rstrip().endswith("?"):
            problems = problems + ["question echoed"]  # the model repeated the question instead of answering it
        if problems:
            result += [Sentence(display_span(text_of[sid]), [sid], "verbatim", False, problems) for sid in (used or []) if not any(sid in r.spans for r in result)]
            continue
        result.append(Sentence(s.text, used, "llm", True, []))
    for sid, t in spans:  # every selected span must be represented; nothing the verifier could not check is shown
        if not any(sid in r.spans for r in result):
            result.append(Sentence(display_span(t), [sid], "verbatim"))
    return result, "llm+verifier"


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
