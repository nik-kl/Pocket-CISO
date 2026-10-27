"""Pipeline B: stateless, RBAC-filtered query → answer / labelled search hits / fallback, with trace."""
from __future__ import annotations

import re
import time
from typing import Any

from .compose import CONTRADICTION_LABEL, compose_sentences, display_span, related_spans, search_hits
from .contradictions import find_contradiction
from .gate import (
    GATE_COVERAGE, GATE_MISSING, NORM_Q, NO_ANSWER, Part, SEARCH_LABEL, UNCONFIRMED_LABEL, VALUE_Q,
    WHO_Q, dense_window, fact_chunks, question_parts, select_windows, window_ok, window_terms,
)
from .judge import judge_answerable
from .verify import has_value, value_dim
from ..config import ApiError, ROLE_RANK
from ..llm import OLLAMA, engine_label
from ..search.dense import dense_scores, diversify, fuse
from ..search.facts import values_in
from ..search.lexical import coverage, key_terms, lexical_index
from ..search.normalize import fold
from ..store import actor_for, append_denial, cached_db, locked_db


def _restricted_match(db: dict[str, Any], role: str, question: str) -> dict[str, Any] | None:
    """Audit only (RBAC_DENIED): would a chunk above the caller's clearance have answered? Never influences the response."""
    if role == "L3":
        return None
    idx = lexical_index(db)
    full = idx.view("L3")
    restricted = {i for i in full.allowed if ROLE_RANK[idx.chunks[i]["minRole"]] > ROLE_RANK[role]}
    if not restricted:
        return None
    qt = key_terms(question, full)
    bm = {i: s for i, s in full.bm25(qt.weights).items() if i in restricted}
    for i in sorted(bm, key=lambda i: (-bm[i], idx.chunks[i]["id"]))[:3]:
        if coverage(qt, frozenset(idx.doc_terms[i]), idx.ctx_terms[i]) >= GATE_COVERAGE:
            return idx.chunks[i]
    return None


def run_query(token: str | None, question: str) -> dict[str, Any]:
    t_all = time.perf_counter()
    q = (question or "").strip()
    if not 3 <= len(q) <= 500:
        raise ApiError(400, "BAD_QUESTION", "Die Frage muss zwischen 3 und 500 Zeichen lang sein.")
    lat: dict[str, int] = {}
    db = cached_db()
    actor = actor_for(db, token)
    role = actor["role"]

    t = time.perf_counter()
    idx = lexical_index(db)
    view = idx.view(role)  # RBAC pre-filter before any scoring
    texts, context, injection = question_parts(q)
    lat["rbac"] = int((time.perf_counter() - t) * 1000)

    t = time.perf_counter()
    ctx_weights: dict[str, float] = {}
    for c in context:
        for x, w in key_terms(c, view).weights.items():
            ctx_weights[x] = max(ctx_weights.get(x, 0.0), 0.3 * w)
    dense_all = dense_scores(db, view, texts) if texts and view.n else None
    use_llm = bool(OLLAMA.llm_model())
    ctx_note = f" (Kontext: {' '.join(context)})" if context else ""  # the judge needs the scenario to resolve "sie", "das"
    parts: list[Part] = []
    best_dense = 0.0
    candidate_docs: list[str] = []
    for ptext in texts:
        qt = key_terms(ptext, view)
        f = fold(ptext)
        part = Part(ptext, qt, bool(VALUE_Q.search(f) or qt.unit_cues), bool(WHO_Q.search(f)), [], bool(NORM_Q.search(f)))
        if not qt.keys or not view.n:
            part.reason = "keine Suchbegriffe" if view.n else "keine Fundstelle"
            parts.append(part)
            continue
        weights = dict(ctx_weights)
        for x, w in qt.weights.items():
            weights[x] = max(weights.get(x, 0.0), w)
        bm = view.bm25(weights)
        dense = dense_scores(db, view, [ptext]) if dense_all is not None else None
        ranked = diversify(idx, fuse(view, bm, dense))
        if part.value:  # exact facts first: chunks with a matching fact join the candidates even outside the fused top-k
            ranked += [i for i in fact_chunks(idx, view, part) if i not in ranked]
        candidate_docs += [idx.chunks[i]["docName"] for i in ranked]
        select_windows(idx, view, part, ranked)
        dense_top = max((dense.get(i, 0.0) for w in part.windows for i, _ in w), default=0.0) if dense else None
        best_dense = max(best_dense, dense_top or 0.0)
        got = frozenset().union(*(window_terms(idx, w) for w in part.windows)) if part.windows else frozenset()
        ctx_got = frozenset().union(*(idx.ctx_terms[w[-1][0]] for w in part.windows)) if part.windows else frozenset()
        total_w = sum(k.weight for k in qt.keys) or 1.0
        unmatched = [k.weight for k in qt.keys if not any(a <= got or a <= ctx_got for a in k.alts)]
        win_chunks = {i for w in part.windows for i, _ in w}
        part.features = {
            "coverage": round(part.coverage, 4), "dense": round(dense_top, 4) if dense_top is not None else None,
            "bm25": round(max((bm.get(i, 0.0) for i in win_chunks), default=0.0), 3),
            "missingMax": round(max(unmatched, default=0.0) / total_w, 4),
        }
        part.ranked = ranked
        part.lexical_ok = bool(part.windows) and part.coverage >= GATE_COVERAGE and part.features["missingMax"] <= GATE_MISSING
        # the floor reached only because unknown words were capped: the judge decides, but a NEIN yields no search hits
        # ("Wie ist das Wetter in Wien?" must not list policy sentences)
        raw_total = sum(k.raw_weight or k.weight for k in qt.keys)
        part.capped_only = part.lexical_ok and raw_total > total_w and (total_w - sum(unmatched)) < GATE_COVERAGE * raw_total
        if part.lexical_ok:
            # a contradiction is reported only when the judge confirms every listed window on its own (no new false flags)
            # deterministic contradiction rule (step 4, same with and without LLM): never an answer, shown as search hits
            part.contradiction = find_contradiction(idx, view, part, ranked) or None
            if use_llm and not part.contradiction:
                part.features["judge"] = judge_answerable(ptext + ctx_note, [idx.span_text[i][k] for w in part.windows for i, k in w][:6])
        # dense path only below the lexical floor: a second judge call after a NEIN on the lexical window gave the 7B judge a
        # second chance to say JA and produced 2 false answers on the dev set (I-1, R-10) → never retried
        if dense and not part.lexical_ok:
            dw = dense_window(idx, view, part, dense)
            if dw:
                part.dense_ok, part.windows = True, [dw[0]]
                part.features["densePath"] = round(dw[1], 4)
                if use_llm:
                    part.features["judge"] = judge_answerable(ptext + ctx_note, [idx.span_text[i][k] for i, k in dw[0]])
        part.answered = (part.lexical_ok or part.dense_ok) and part.features.get("judge") is True and not part.contradiction
        if not part.answered and part.windows:
            part.reason = ("Abdeckung zu gering" if not (part.lexical_ok or part.dense_ok) else "Antwortbarkeitsprüfung: nein" if use_llm else "kein LLM: nur Fundstellen")
        parts.append(part)
    # shared evidence pool: before a part is reported as unsupported, the judge checks it against the windows already selected
    # for the answered parts (e.g. "… und wer nimmt die Anmeldung entgegen?" answered by the cited registration sentence),
    # so the answer never cites a sentence and calls the same point unsupported
    if use_llm:
        pool = [w for p in parts if p.answered for w in p.windows]
        for p in parts:
            if p.answered or p.contradiction or not pool:
                continue
            ws = [w for w in pool if not p.value or window_ok(idx, p, w)]  # role names are left to the judge here
            spans = [idx.span_text[i][k] for w in ws for i, k in w][:6]
            p.pooled = bool(spans)
            if spans and judge_answerable(f"{p.text} (Gesamtfrage: {q})", spans) is True:
                p.windows, p.answered, p.reason = ws, True, ""
                p.coverage = max(p.coverage, coverage(p.qt, frozenset().union(*(window_terms(idx, w) for w in ws))))
                p.features = {**(p.features or {}), "pool": True}
    # value questions: the shown spans must carry a value of the asked dimension (duration, count, deadline, frequency). The
    # value sentence of a confirmed window can be dropped by related_spans (its key term already in the section title) →
    # it is added back; a confirmed window without such a value is no answer to a value question → search hit / note.
    for p in parts:
        if not p.answered:
            continue
        p.shown = [related_spans(idx, p, w) for w in p.windows]
        if not p.value:
            continue
        dim = value_dim(p.text)
        if any(has_value(dim, idx.span_text[i][k]) for ws in p.shown for i, k in ws):
            continue
        add = next(((n, x) for n, w in enumerate(p.windows) for x in reversed(w) if has_value(dim, idx.span_text[x[0]][x[1]])), None)
        if add:
            n, x = add
            p.shown[n] = [y for y in p.windows[n] if y in p.shown[n] or y == x]
            p.features = {**(p.features or {}), "valueSpan": "added"}
        else:
            p.answered, p.reason = False, f"kein Wert der gefragten Art ({dim})"
            p.features = {**(p.features or {}), "valueSpan": "missing"}
    lat["retrieval"] = int((time.perf_counter() - t) * 1000)

    label = engine_label()
    trace: dict[str, Any] = {
        "original": q, "rewrite": " | ".join(texts) if texts else q, "rbacFilter": f"min_role <= {role}", "searched": view.n, "total": len(idx.chunks),
        "nliVerified": False, "temperature": 0.0, "confidence": 0.0, "topScore": 0.0, "latencyMs": lat, "engineMode": label,
        "rewriteBy": "rules (Teilfragen)", "answerBy": "none", "injectionRemoved": injection, "candidateDocs": list(dict.fromkeys(candidate_docs)),
        "parts": [{"text": p.text, "answered": p.answered, "coverage": round(p.coverage, 3), "value": p.value, "who": p.who, "reason": p.reason,
                   "features": p.features} for p in parts],
        "denseTop": round(best_dense, 3) if dense_all is not None else None, "contradiction": False, "partial": False,
    }
    # retrieval score for every kind (answer, search, fallback): best dense cosine of the selected windows, else lexical coverage
    best_cov = max((p.coverage for p in parts), default=0.0)
    trace["retrievalScore"] = trace["topScore"] = round(best_dense if dense_all is not None else best_cov, 3)
    trace["lexicalSupport"] = round(best_cov, 2)

    def done(kind: str, segments: list[dict[str, str]], citations: dict[str, Any], scores: dict[str, float], spans: dict[str, Any]) -> dict[str, Any]:
        lat["total"] = int((time.perf_counter() - t_all) * 1000)
        plain = " ".join(s["text"].strip() for s in segments if s["type"] in ("text", "note"))
        telemetry = {"rewrittenQuery": trace["rewrite"], "rbacFilter": trace["rbacFilter"], "topScore": trace["topScore"], "nliVerified": trace["nliVerified"],
                     "latencyMs": lat, "engineMode": label, "rbacDenied": False, "requiredRole": None}
        return {"ok": True, "result": {"kind": kind, "requiredRole": None, "segments": segments, "citations": citations, "scores": scores, "spans": spans, "trace": trace},
                "answer": plain, "citations": list(citations.values()), "telemetry": telemetry}

    answered = [p for p in parts if p.answered]
    conflicts = [p for p in parts if p.contradiction]
    if conflicts:  # possible contradictions are shown as labelled search hits with every value and source, never as an answer
        trace.update({"verification": "none", "answerBy": "search", "contradiction": True,
                      "contradictionValues": [{"docName": idx.chunks[w[-1][0]]["docName"], "section": idx.chunks[w[-1][0]]["section"],
                                               "values": {d: sorted(v) for d, v in vals.items()}} for p in conflicts for w, vals in p.contradiction]})
        segments: list[dict[str, str]] = [{"type": "text", "text": CONTRADICTION_LABEL + ":"}]
        hits = []
        for p in conflicts:
            for w, vals in p.contradiction:
                c = idx.chunks[w[-1][0]]
                text = " ".join(display_span(idx.span_text[i][k]) for i, k in w)
                words = {fold(x): x for x in re.findall(r"[\wÄÖÜäöüß]+", text)}  # 'halbjahrlich' → 'halbjährlich'
                shown = list(dict.fromkeys(words.get(sf.strip(), sf.strip()) for d, v, sf in values_in(text)
                                           if round(v, 6) in vals.get(d, set()) or round(v, 6) in vals.get("freq", set())))
                value = f" (Wert: {', '.join(shown)})" if shown else ""
                segments += [{"type": "text", "text": f" {c['docName']} {c['section'].split(' ')[0]}: {text}{value} "}, {"type": "cite", "chunkId": c["id"]}]
                hits.append({"chunkId": c["id"], "docName": c["docName"], "section": c["section"], "content": text, "values": shown})
        out = done("search", segments, {h["chunkId"]: idx.chunks[[c["id"] for c in idx.chunks].index(h["chunkId"])] for h in hits}, {}, {})
        out["result"]["hits"] = hits
        return out
    if not answered:
        hidden = _restricted_match(db, role, q)
        if hidden:
            with locked_db() as dbw:
                append_denial(dbw, actor["email"], role, "RBAC_DENIED", hidden["docName"], f"Retrieval erfordert {hidden['minRole']}")
        trace.update({"verification": "none", "candidateDocs": trace["candidateDocs"] if view.n else []})
        hits = search_hits(idx, parts)
        if not hits:
            return done("fallback", [{"type": "text", "text": NO_ANSWER}], {}, {}, {})
        trace["answerBy"] = "search"
        segments: list[dict[str, str]] = [{"type": "text", "text": (UNCONFIRMED_LABEL if use_llm else SEARCH_LABEL) + ":"}]
        for h in hits:
            segments += [{"type": "text", "text": f" {h['docName']} {h['section'].split(' ')[0]}: {h['content']} "}, {"type": "cite", "chunkId": h["chunkId"]}]
        by_id = {c["id"]: c for c in idx.chunks}
        out = done("search", segments, {h["chunkId"]: by_id[h["chunkId"]] for h in hits}, {}, {})
        out["result"]["hits"] = hits
        return out

    t = time.perf_counter()
    ordered: list[tuple[str, str]] = []
    for p in answered:
        for ws in p.shown:
            for i, k in ws:
                sid = idx.span_ids[i][k]
                if sid not in dict(ordered):
                    ordered.append((sid, idx.span_text[i][k]))
    sentences, answer_by = compose_sentences(q, ordered, view.vocab, use_llm)
    said = " ".join(s.text for s in sentences)
    if any(p.value and not has_value(value_dim(p.text), said) for p in answered):  # phrasing dropped the value → verbatim spans
        sentences, answer_by = compose_sentences(q, ordered, view.vocab, False)[0], "verbatim (Wert fehlte in der Formulierung)"
    lat["generation"] = int((time.perf_counter() - t) * 1000)

    span_chunk = {idx.span_ids[i][k]: i for i in range(len(idx.chunks)) for k in range(len(idx.span_ids[i]))}
    segments: list[dict[str, str]] = []
    for s in sentences:
        segments.append({"type": "text", "text": (" " if segments else "") + s.text + " "})
        for sid in s.spans:
            segments.append({"type": "cite", "chunkId": idx.chunks[span_chunk[sid]]["id"], "spanId": sid})
    unanswered = [p for p in parts if not p.answered]
    for p in unanswered:  # after a pool check the cited spans may touch the point: no claim that nothing exists
        segments.append({"type": "note", "text": f"Zu „{p.text}“ wurde keine Aussage als Antwort bestätigt; bitte die zitierten Stellen prüfen."
                         if p.pooled or (p.features or {}).get("valueSpan") else f"Zu „{p.text}“ enthält der freigegebene Bestand keine belegbare Aussage."})
    cited_chunks = list(dict.fromkeys(s["chunkId"] for s in segments if s["type"] == "cite"))
    by_id = {c["id"]: c for c in idx.chunks}
    spans_out = {sid: {"chunkId": idx.chunks[span_chunk[sid]]["id"], "text": text} for sid, text in ordered}
    cov = min(p.coverage for p in answered)
    trace.update({
        "nliVerified": all(s.verified for s in sentences), "verification": "span-verifier", "answerBy": answer_by, "partial": bool(unanswered),
        "confidence": round(cov, 2), "retrievalScore": round(best_dense if dense_all is not None else cov, 3), "topScore": round(best_dense if dense_all is not None else cov, 3),
        "lexicalSupport": round(cov, 2),
        "sentences": [{"text": s.text, "spans": s.spans, "source": s.source, "verified": s.verified, "problems": s.reasons or []} for s in sentences],
    })
    scores = {cid: round(max((p.coverage for p in answered), default=0.0), 3) for cid in cited_chunks}
    return done("answer", segments, {cid: by_id[cid] for cid in cited_chunks}, scores, spans_out)
