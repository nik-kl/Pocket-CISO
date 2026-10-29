"""Pipeline B: stateless, RBAC-filtered query → answer / labelled search hits / fallback, with trace."""
from __future__ import annotations

import copy
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .compose import CONTRADICTION_LABEL, display_span, search_hits
from .contradictions import find_contradiction
from .evidence import entry_sentences, evidence_pack, pack_prompt
from .gate import (
    DOC_REF, GATE_COVERAGE, GATE_MISSING, NO_ANSWER, Part, SEARCH_LABEL, UNCONFIRMED_LABEL, VALUE_Q, WHO_Q,
    dense_window, fact_chunks, fix_function_words, question_parts, select_windows, window_terms,
)
from .keysentence import key_sentence, question_kind
from .structured import ask, checked
from ..config import ApiError, ROLE_RANK
from ..llm import OLLAMA, engine_label
from ..search.dense import dense_scores, diversify, fuse
from ..search.facts import values_in
from ..search.lexical import coverage, doc_title, key_terms, lexical_index, section_title
from ..search.normalize import GENERIC_WORDS, fold
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
    for i in sorted(bm, key=lambda i: (-bm[i], i))[:3]:
        if coverage(qt, frozenset(idx.doc_terms[i]), idx.ctx_terms[i]) >= GATE_COVERAGE:
            return idx.chunks[i]
    return None


@dataclass
class Retrieval:
    """Deterministic part of Pipeline B (no LLM): question parts, candidates, windows, lexical floor, dense path,
    contradiction rule. Stage 1 (run_fast) shows it as search hits; stage 2 (run_query) judges it."""
    texts: list[str]
    context: list[str]
    injection: bool
    parts: list[Part]
    dense_on: bool
    best_dense: float
    candidate_docs: list[str]
    ms: int
    cached: bool = False


# retrieval of stage 1 reused by stage 2 of the same question: key = (corpus signature, role, question, embedding model),
# so a merge, another role or another embedding model never hits a stale entry; LRU, deep-copied on every read
_RET_CACHE: "OrderedDict[tuple[str, str, str, str], Retrieval]" = OrderedDict()
_RET_LOCK = threading.Lock()
RET_CACHE_SIZE = 64


def reset_retrieval_cache() -> None:
    with _RET_LOCK:
        _RET_CACHE.clear()


def _check_question(question: str) -> str:
    q = (question or "").strip()
    if not 3 <= len(q) <= 500:
        raise ApiError(400, "BAD_QUESTION", "Die Frage muss zwischen 3 und 500 Zeichen lang sein.")
    return q


def retrieve(db: dict[str, Any], idx: "LexIndex", view: "RoleView", role: str, q: str) -> Retrieval:
    key = (idx.sig, role, q, OLLAMA.embed_model() or "")
    with _RET_LOCK:
        hit = _RET_CACHE.get(key)
        if hit is not None:
            _RET_CACHE.move_to_end(key)
            return copy.deepcopy(hit)
    r = _retrieve(db, idx, view, q)
    if key[3] and not r.dense_on and r.texts and view.n:
        return r  # embedding call failed: not cached, the next request retries the dense path
    with _RET_LOCK:
        _RET_CACHE[key] = copy.deepcopy(r)
        _RET_CACHE[key].cached = True
        while len(_RET_CACHE) > RET_CACHE_SIZE:
            _RET_CACHE.popitem(last=False)
    return r


def _retrieve(db: dict[str, Any], idx: "LexIndex", view: "RoleView", q: str) -> Retrieval:
    t = time.perf_counter()
    texts, context, injection = question_parts(q)
    ctx_weights: dict[str, float] = {}
    for c in context:
        for x, w in key_terms(c, view).weights.items():
            ctx_weights[x] = max(ctx_weights.get(x, 0.0), 0.3 * w)
    dense_all = dense_scores(db, view, texts) if texts and view.n else None
    parts: list[Part] = []
    best_dense = 0.0
    candidate_docs: list[str] = []
    for ptext in texts:
        qt = key_terms(ptext, view)
        f = fix_function_words(ptext, view)
        part = Part(ptext, qt, bool(VALUE_Q.search(f) or qt.unit_cues), bool(WHO_Q.search(f)), [], flag_text=f)
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
        part.dense_of = {i: round(dense[i], 4) for i in ranked if i in dense} if dense else {}
        part.lexical_ok = bool(part.windows) and part.coverage >= GATE_COVERAGE and part.features["missingMax"] <= GATE_MISSING
        # the floor reached only because unknown words were capped: the judge decides, but a NEIN yields no search hits
        # ("Wie ist das Wetter in Wien?" must not list policy sentences)
        raw_total = sum(k.raw_weight or k.weight for k in qt.keys)
        part.capped_only = part.lexical_ok and raw_total > total_w and (total_w - sum(unmatched)) < GATE_COVERAGE * raw_total
        if part.lexical_ok:
            # deterministic contradiction rule (step 4, same with and without LLM): never an answer, shown as search hits
            part.contradiction = find_contradiction(idx, view, part, ranked) or None
        # dense path only below the lexical floor: a second judge call after a NEIN on the lexical window gave the 7B judge a
        # second chance to say JA and produced 2 false answers on the dev set (I-1, R-10) → never retried
        if dense and not part.lexical_ok:
            dw = dense_window(idx, part, dense)
            if dw:
                part.dense_ok, part.windows = True, [dw[0]]
                part.features["densePath"] = round(dw[1], 4)
        parts.append(part)
    return Retrieval(texts, context, injection, parts, dense_all is not None, best_dense, candidate_docs, int((time.perf_counter() - t) * 1000))


def run_query(token: str | None, question: str) -> dict[str, Any]:
    """Pipeline B: retrieval → contradiction rule → evidence pack → ONE structured LLM call → deterministic guarantees per part
    (structured.checked) → answer with disclosed open parts / labelled search hits / fallback."""
    t_all = time.perf_counter()
    q = _check_question(question)
    lat: dict[str, int] = {}
    db = cached_db()
    actor = actor_for(db, token)
    role = actor["role"]
    t = time.perf_counter()
    idx = lexical_index(db)
    view = idx.view(role)  # RBAC pre-filter before any scoring
    lat["rbac"] = int((time.perf_counter() - t) * 1000)
    t = time.perf_counter()
    ret = retrieve(db, idx, view, role, q)
    parts = ret.parts
    lat["retrieval"] = int((time.perf_counter() - t) * 1000)
    use_llm = bool(OLLAMA.llm_model())
    label = engine_label()
    best_cov = max((p.coverage for p in parts), default=0.0)
    score = round(ret.best_dense if ret.dense_on else best_cov, 3)
    trace: dict[str, Any] = {
        "original": q, "rewrite": " | ".join(ret.texts) if ret.texts else q, "rbacFilter": f"min_role <= {role}", "searched": view.n, "total": len(idx.chunks),
        "nliVerified": False, "temperature": 0.0, "confidence": 0.0, "topScore": score, "retrievalScore": score, "lexicalSupport": round(best_cov, 2),
        "latencyMs": lat, "engineMode": label, "rewriteBy": "rules (Teilfragen)", "answerBy": "none", "injectionRemoved": ret.injection,
        "candidateDocs": list(dict.fromkeys(ret.candidate_docs)), "retrievalCached": ret.cached, "verification": "none",
        "parts": [{"text": p.text, "answered": False, "coverage": round(p.coverage, 3), "value": p.value, "who": p.who, "reason": p.reason,
                   "features": p.features} for p in parts],
        "denseTop": round(ret.best_dense, 3) if ret.dense_on else None, "contradiction": False, "partial": False,
    }
    by_id = {c["id"]: c for c in idx.chunks}

    def done(kind: str, segments: list[dict[str, str]], cited: list[str], spans: dict[str, Any] | None = None, hits: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        lat["total"] = int((time.perf_counter() - t_all) * 1000)
        plain = " ".join(s["text"].strip() for s in segments if s["type"] in ("text", "note"))
        citations = {cid: by_id[cid] for cid in dict.fromkeys(cited)}
        telemetry = {"rewrittenQuery": trace["rewrite"], "rbacFilter": trace["rbacFilter"], "topScore": trace["topScore"], "nliVerified": trace["nliVerified"],
                     "latencyMs": lat, "engineMode": label, "rbacDenied": False, "requiredRole": None}
        result = {"kind": kind, "requiredRole": None, "segments": segments, "citations": citations,
                  "scores": {cid: round(best_cov, 3) for cid in citations} if kind == "answer" else {}, "spans": spans or {}, "trace": trace}
        if hits is not None:
            result["hits"] = hits
        return {"ok": True, "result": result, "answer": plain, "citations": list(citations.values()), "telemetry": telemetry}

    conflicts = [p for p in parts if p.contradiction]
    if conflicts:  # possible contradictions are shown as labelled search hits with every value and source, never as an answer
        trace.update({"answerBy": "search", "contradiction": True,
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
        return done("search", segments, [h["chunkId"] for h in hits], hits=hits)

    t = time.perf_counter()
    pack = evidence_pack(idx, parts) if use_llm and any(p.lexical_ok or p.dense_ok for p in parts) else []
    model = ask(" ".join(ret.texts), ret.context, pack_prompt(idx, pack)) if pack else None
    asked = " ".join(ret.texts + ret.context)  # the cleaned question: instruction sentences are never a sub-question
    # a document the question names ("laut BIA", "according to the playbook"; generic words aside) is the only source
    named = {w for m in DOC_REF.finditer(fold(asked)) for w in m.group(1).split("-")} - GENERIC_WORDS
    in_named = {n for n, e in enumerate(pack) if named & set(re.findall(r"[a-z0-9]+", fold(doc_title(idx.chunks[e.chunk]["docName"]))))}
    results = [(mp, *checked(mp, [entry_sentences(idx, e) for e in pack], asked, view.vocab, in_named or None)) for mp in model or []]
    lat["generation"] = int((time.perf_counter() - t) * 1000)
    trace["pack"] = [{"span": f"S{n + 1}", "chunkId": idx.chunks[e.chunk]["id"], "docName": idx.chunks[e.chunk]["docName"]} for n, e in enumerate(pack)]
    trace["answerParts"] = [{"question": str(mp.get("question") or ""), "status": mp.get("status"), "type": mp.get("type"), "checked": st, "reason": why,
                             "modelAnswer": str(mp.get("answer") or "")}
                            for mp, st, _, _, why in results]
    if pack and model is None:
        trace["answerBy"] = "llm: keine gültige Antwort"
    answered = [r for r in results if r[1] == "answered"]
    if not answered:
        hidden = _restricted_match(db, role, q)
        if hidden:
            with locked_db() as dbw:
                append_denial(dbw, actor["email"], role, "RBAC_DENIED", hidden["docName"], f"Retrieval erfordert {hidden['minRole']}")
        trace["candidateDocs"] = trace["candidateDocs"] if view.n else []
        hits = search_hits(idx, parts)
        if not hits:
            return done("fallback", [{"type": "text", "text": NO_ANSWER}], [])
        trace["answerBy"] = "search"
        segments = [{"type": "text", "text": (UNCONFIRMED_LABEL if use_llm else SEARCH_LABEL) + ":"}]
        for h in hits:
            segments += [{"type": "text", "text": f" {h['docName']} {h['section'].split(' ')[0]}: {h['content']} "}, {"type": "cite", "chunkId": h["chunkId"]}]
        return done("search", segments, [h["chunkId"] for h in hits], hits=hits)

    segments, spans_out = [], {}
    for mp, _, quotes, answer, _ in answered:
        if answer and not any(answer.rstrip(".").lower() in t.lower() for _, t in quotes):  # the quote alone says it
            segments.append({"type": "text", "text": (" " if segments else "") + answer + " "})
        for n, text in quotes:  # the source sentences, verbatim (table rows as "Spalte: Wert; …")
            cid = idx.chunks[pack[n].chunk]["id"]
            sid = f"{cid}:q{len(spans_out)}"
            spans_out[sid] = {"chunkId": cid, "text": text.replace(" | ", "; ")}
            segments += [{"type": "text", "text": spans_out[sid]["text"] + " "}, {"type": "cite", "chunkId": cid, "spanId": sid}]
    for mp, status, _, _, _ in results:
        if status == "not_found":
            segments.append({"type": "note", "text": f"Zu „{mp.get('question') or q}“ wurde keine Aussage als Antwort bestätigt."})
    trace.update({"nliVerified": True, "verification": "quote+verifier", "answerBy": "llm-structured", "confidence": round(best_cov, 2),
                  "partial": any(r[1] == "not_found" for r in results)})
    return done("answer", segments, [s["chunkId"] for s in segments if s["type"] == "cite"], spans_out)


# ── stage 1: search hits with a key sentence, no LLM ──

FAST_HITS = 3


def _hit(idx: "LexIndex", part: Part, spans: list[tuple[int, int]], kind: str, arg: str | None, source: str) -> dict[str, Any]:
    i = spans[-1][0]
    texts = [idx.span_text[x][k] for x, k in spans]
    cov = [coverage(part.qt, idx.span_terms[x][k]) for x, k in spans]
    key = key_sentence(kind, arg, texts, cov)
    sentences = [display_span(t) for t in texts]
    c = idx.chunks[i]
    return {"chunkId": c["id"], "docName": c["docName"], "section": c["section"], "sentences": sentences,
            "spanIds": [idx.span_ids[x][k] for x, k in spans], "key": key, "keySentence": sentences[key], "content": " ".join(sentences),
            "source": source, "coverage": round(coverage(part.qt, frozenset().union(*(idx.span_terms[x][k] for x, k in spans)), idx.ctx_terms[i]), 3),
            "dense": part.dense_of.get(i)}


def _chunk_spans(idx: "LexIndex", part: Part, i: int, kind: str, arg: str | None) -> list[tuple[int, int]]:
    """Shown sentences of a further ranked chunk: the key sentence (chosen over all its sentences) and the sentences sharing
    most key terms (coverage ≥ half the best), ≤ 3 in document order; instruction-like spans are never shown."""
    ok = [k for k in range(len(idx.span_text[i])) if not idx.span_instr[i][k]]
    if not ok:
        return []
    cov = [coverage(part.qt, idx.span_terms[i][k]) for k in ok]
    key = ok[key_sentence(kind, arg, [idx.span_text[i][k] for k in ok], cov)]
    best = max(cov)
    rel = sorted((k for k, cv in zip(ok, cov) if best > 0 and cv >= 0.5 * best), key=lambda k: (-cov[ok.index(k)], k))
    keep = sorted(list(dict.fromkeys([key] + rel))[:3])
    return [(i, k) for k in keep]


def fast_hits(idx: "LexIndex", view: "RoleView", parts: list[Part], limit: int = FAST_HITS) -> list[dict[str, Any]]:
    """Stage-1 hits: per part its selected windows, then further ranked chunks
    (same floor as search_hits: lexical floor without capped-only matches, or the dense gate); parts interleaved, ≤ `limit`
    chunks. RBAC-filtered by construction (windows and ranks come from the role view only)."""
    per_part: list[list[dict[str, Any]]] = []
    for p in parts:
        kind, arg = question_kind(p.text, p.value, p.who, p.flag_text)
        cands: list[dict[str, Any]] = []
        if (p.lexical_ok and not p.capped_only) or p.dense_ok:
            cands += [_hit(idx, p, w, kind, arg, "window") for w in p.windows]
            for i in p.ranked:
                spans = _chunk_spans(idx, p, i, kind, arg)
                if spans:
                    cands.append(_hit(idx, p, spans, kind, arg, "ranked"))
                if len(cands) >= limit + len(p.windows) + 1:
                    break
        per_part.append(cands)
    hits: list[dict[str, Any]] = []
    for n in range(max((len(c) for c in per_part), default=0)):
        for cands in per_part:
            if n < len(cands) and len(hits) < limit and all(h["chunkId"] != cands[n]["chunkId"] for h in hits):
                hits.append(cands[n])
    return hits


def run_fast(token: str | None, question: str) -> dict[str, Any]:
    """Stage 1 (≤ 1.5 s): retrieval + RBAC + injection filter + key sentence, no LLM; verbatim source sentences only. Never
    writes audit entries — a restricted match is logged once, by stage 2 (run_query)."""
    t_all = time.perf_counter()
    q = _check_question(question)
    db = cached_db()
    role = actor_for(db, token)["role"]
    t = time.perf_counter()
    idx = lexical_index(db)
    view = idx.view(role)
    lat = {"rbac": int((time.perf_counter() - t) * 1000)}
    ret = retrieve(db, idx, view, role, q)
    lat["retrieval"] = ret.ms if not ret.cached else 0
    hits = fast_hits(idx, view, ret.parts)
    lat["total"] = int((time.perf_counter() - t_all) * 1000)
    kinds = [question_kind(p.text, p.value, p.who, p.flag_text) for p in ret.parts]
    best_cov = max((p.coverage for p in ret.parts), default=0.0)
    by_id = {c["id"]: c for c in idx.chunks}
    return {"ok": True, "fast": {
        "hits": hits, "citations": {h["chunkId"]: by_id[h["chunkId"]] for h in hits},
        "trace": {"original": q, "rewrite": " | ".join(ret.texts) if ret.texts else q, "rbacFilter": f"min_role <= {role}", "searched": view.n,
                  "total": len(idx.chunks), "injectionRemoved": ret.injection, "engineMode": engine_label(), "latencyMs": lat,
                  "retrievalScore": round(ret.best_dense if ret.dense_on else best_cov, 3), "lexicalSupport": round(best_cov, 2),
                  "parts": [{"text": p.text, "kind": k, "arg": a, "coverage": round(p.coverage, 3), "lexicalOk": p.lexical_ok, "densePath": p.dense_ok,
                             "contradiction": bool(p.contradiction), "features": p.features} for p, (k, a) in zip(ret.parts, kinds)]},
    }}
