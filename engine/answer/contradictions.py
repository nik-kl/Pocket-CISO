"""Deterministic contradiction rule between live documents (same quantity, disjoint values)."""
from __future__ import annotations

import re
from collections import defaultdict

from .gate import doc_refs, span_windows, window_ok, window_terms
from ..search.facts import DURATION_MIN, topical_overlap, values_in
from ..search.lexical import RoleView, coverage
from ..search.normalize import FREQ_WORDS, analyze, fold


def _cue_dims(cues: set[str]) -> set[str]:
    return {"time" if c in DURATION_MIN or c == "mo" else c for c in cues}


def window_values(idx: "LexIndex", part: "Part", w: list[tuple[int, int]]) -> dict[str, set[float]]:
    """Values a window states for the asked quantity: for a table row the cells whose column header matches the question
    (all quantity cells if none matches), for sentences every value; restricted to the question's unit dimension if it has one."""
    dims = _cue_dims(part.qt.unit_cues)
    out: dict[str, set[float]] = defaultdict(set)
    for i, k in w:
        row = [f for f in idx.facts_by_span.get((i, k), []) if f["attribute"]]
        keys = set().union(*(a for kt in part.qt.keys for a in kt.alts))
        hit_attrs = {f["attribute"] for f in idx.facts_by_span.get((i, k), [])} if row else set()
        hit_attrs = {a for a in hit_attrs if set(analyze(a)) & keys}
        if row and hit_attrs:  # the asked column ("RPO") decides which cell is the answer
            vals = [(f["dim"], f["value"]) for f in row if f["attribute"] in hit_attrs]
        elif not part.value:
            vals = []  # no asked column and no value question: numbers in the window are not the answer
        elif row:
            # no asked column: a row with several values of one dimension ('RTO 12 Stunden | RPO 1 Stunde') does not say
            # which one answers the question → none of them is used
            by_dim: dict[str, set[float]] = defaultdict(set)
            for f in row:
                by_dim[f["dim"]].add(f["value"])
            vals = [(d, v) for d, vs in by_dim.items() if len(vs) == 1 for v in vs]
        else:
            vals = [(d, v) for d, v, _ in values_in(idx.span_text[i][k])]
        text = fold(idx.span_text[i][k])
        by_col = {round(f["value"], 6) for f in row if INTERVAL_COL.search(fold(f["attribute"]))}  # 'Prüfintervall: 6 Monate'
        for d, v in vals:
            if not dims or d in dims:
                out["freq" if d == "time" and round(v, 6) in by_col else _freq_or(d, v, text)].add(round(v, 6))
    return out


INTERVAL_COL = re.compile(r"intervall|interval|zyklus|turnus|frequen|rhythmus|haufigkeit")
FREQ_CUE = re.compile(r"\b(?:alle|every|each|je|pro|per)\s+(\d+(?:[.,]\d+)?)\b")


def _freq_or(dim: str, value: float, text: str) -> str:
    """Contradictions compare like with like: an interval ('halbjährlich', 'alle 6 Monate') is 'freq', a duration
    ('Wiederanlaufzeit von 24 Stunden') stays 'time' — both are minutes in values_in."""
    if dim != "time":
        return dim
    freq = {round(DURATION_MIN[re.match(r"\d+(\w+)", q).group(1)] * float(re.match(r"\d+", q).group(0)), 6)
            for w, q in FREQ_WORDS.items() if re.search(rf"\b{w}\b", text)}
    freq |= {round(base, 6) for _, base, sf in values_in(text) for m in FREQ_CUE.finditer(text) if sf.startswith(m.group(1))}
    return "freq" if round(value, 6) in freq else "time"


CONTRA_OVERLAP = 0.25  # same threshold as the staging conflict rule (_rule_conflict); 0.15 measured: 3 false flags, no gain


def find_contradiction(idx: "LexIndex", view: RoleView, part: "Part", ranked: list[int]) -> list[tuple[list[tuple[int, int]], dict[str, set[float]]]]:
    """Unit-aware contradiction between live documents, built on the staging conflict rule (_rule_conflict): another
    document's window answers the same question (key-term coverage ≥ max(0.4, best − 0.45), all identifiers such as 'tier1'
    present), talks about the same thing as the selected window (topical overlap ≥ 0.25) and states values of the same
    dimension that are disjoint from the selected ones. Returns [] or one window per document with its values."""
    if not part.windows:
        return []
    best = part.windows[0]
    best_text = " ".join(idx.span_text[i][k] for i, k in best)
    best_vals = window_values(idx, part, best)
    if not best_vals:
        return []
    idents = [kt.alts[0] for kt in part.qt.keys if kt.kind == "ident"]
    refs = doc_refs(part, window_terms(idx, best), idx.ctx_terms[best[-1][0]])
    floor = max(0.4, part.coverage - 0.45)
    best_doc = idx.chunks[best[-1][0]]["docName"]
    per_doc: dict[str, tuple[float, list[tuple[int, int]], dict[str, set[float]]]] = {}
    for w in span_windows(idx, view, ranked):
        doc = idx.chunks[w[-1][0]]["docName"]
        if doc == best_doc or not window_ok(idx, part, w):
            continue
        terms = window_terms(idx, w)
        cov = coverage(part.qt, terms, idx.ctx_terms[w[-1][0]])
        if cov < floor or not all(a <= terms for a in idents):
            continue
        if any(not any(a <= terms | idx.ctx_terms[w[-1][0]] for a in kt.alts) for kt in refs):
            continue  # the question names the selected document: another document cannot contradict the answer
        if topical_overlap(best_text, " ".join(idx.span_text[i][k] for i, k in w)) < CONTRA_OVERLAP:
            continue
        vals = window_values(idx, part, w)
        if not any(d in best_vals and best_vals[d] and not (vals[d] & best_vals[d]) for d in vals):
            continue
        prev = per_doc.get(doc)
        if prev is None or cov > prev[0] + 1e-9:
            per_doc[doc] = (cov, w, vals)
    if not per_doc:
        return []
    others = sorted(per_doc.values(), key=lambda x: (-x[0], x[1][-1]))[:4]
    return [(best, best_vals)] + [(w, v) for _, w, v in others]
