"""Pipeline A: document series, staging (diff, alignment, conflicts) and approval/merge into the live collection."""
from __future__ import annotations

import json
import numpy as np
import re
import time
import uuid
from collections import Counter
from typing import Any

from .config import ApiError, COLLECTION, CONFLICT_LLM_TIMEOUT, CONNECTOR_ACTOR, ROLE_RANK, now_iso, params_for, sha256
from .ingest.chunk import chunk_document
from .llm import EMBEDDER, OLLAMA, engine_label
from .search.facts import FACTS_VERSION, UNIT_LABEL, extract_facts, quantities, topical_overlap
from .search.lexical import doc_title, refresh_lexical
from .search.normalize import numbers_of
from .store import VECTORS, append_audit, authorize, ensure_vectors, file_lock, locked_db, read_db, write_db


GENERIC = {"policy", "policies", "richtlinie", "richtlinien", "leitlinie", "guideline", "plan", "handbuch", "playbook", "runbook", "konzept", "standard", "dokument"}


def series_tokens(name: str) -> set[str]:
    stem_ = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name).lower()
    toks = [t for t in re.split(r"[^a-z0-9äöüß]+", stem_) if t]
    return {t for t in toks if not re.fullmatch(r"v?\d+(?:\.\d+)*", t) and t not in {"final", "draft", "entwurf"} and t not in GENERIC}


def same_series(a: str, b: str) -> bool:
    sa, sb = series_tokens(a), series_tokens(b)
    return bool(sa and sb) and len(sa & sb) / len(sa | sb) >= 0.5


def bump_version(name: str) -> str:
    m = re.match(r"^(.*_v)(\d+)(\.[A-Za-z0-9]+)$", name)
    if m:
        return f"{m.group(1)}{int(m.group(2)) + 1}{m.group(3)}"
    ext = (re.search(r"(\.[A-Za-z0-9]+)$", name) or [None, ""])[1]
    return f"{name[: -len(ext)] if ext else name}_v2{ext}"


def _json_from(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _rule_conflict(a: str, b: str) -> dict[str, Any]:
    """Deterministic evidence: same topic + same unit with different values (e.g. 12 Stunden ↔ 24 Stunden)."""
    overlap = topical_overlap(a, b)
    qa, qb = quantities(a), quantities(b)
    diffs = [(u, qa[u] - qb[u], qb[u] - qa[u]) for u in qa.keys() & qb.keys() if qa[u] - qb[u] and qb[u] - qa[u]]
    if overlap >= 0.25 and diffs:
        text = "; ".join(f"neu {', '.join(sorted(x))} {UNIT_LABEL.get(u, u)} ↔ bestehend {', '.join(sorted(y))} {UNIT_LABEL.get(u, u)}" for u, x, y in diffs)
        return {"has_conflict": True, "severity": "High" if overlap >= 0.5 else "Medium", "explanation": f"Abweichende Werte: {text}.", "conflicting_quote": b}
    if overlap >= 0.34 and not qa and not qb:  # no units on either side: generic number mismatch
        na, nb = numbers_of(a), numbers_of(b)
        if na - nb and nb - na:
            return {"has_conflict": True, "severity": "Medium", "explanation": f"Abweichende Werte: neu {', '.join(sorted(na - nb))} ↔ bestehend {', '.join(sorted(nb - na))}.", "conflicting_quote": b}
    return {"has_conflict": False, "severity": "Medium", "explanation": "", "conflicting_quote": ""}


CONFLICT_SYSTEM = "Du bist ein strenger Compliance-Prüfer für Unternehmensrichtlinien. Antworte ausschließlich mit einem einzigen JSON-Objekt, ohne weiteren Text."


def check_conflict(a: dict[str, str], b: dict[str, str]) -> tuple[dict[str, Any], str]:
    """Verification cascade → (verdict, verifier). Rules give precise numeric evidence; the LLM (temp 0.0, strict JSON)
    judges non-numeric contradictions and is only trusted when the two rules share real topical vocabulary."""
    rules = _rule_conflict(a["text"], b["text"])
    llm: dict[str, Any] | None = None
    if OLLAMA.llm_model():
        prompt = (
            "Vergleiche Regel A (neu, gestaged) und Regel B (bestehend, aus einer anderen Richtlinie). "
            "Widersprechen sie sich bei demselben Sachverhalt (z. B. unterschiedliche Zahlen, Fristen, Berechtigungen)? "
            "Unterschiedliche Themen sind KEIN Widerspruch.\n"
            f"Regel A ({a['doc']}, {a['section']}): {a['text']}\n"
            f"Regel B ({b['doc']}, {b['section']}): {b['text']}\n"
            'Antworte als JSON: {"has_conflict": true|false, "severity": "High"|"Medium", '
            '"explanation": "kurze Begründung auf Deutsch", "conflicting_quote": "wörtliches Zitat aus Regel B"}'
        )
        out = _json_from(OLLAMA.chat(CONFLICT_SYSTEM, prompt, json_mode=True, timeout=CONFLICT_LLM_TIMEOUT, max_tokens=220))
        if isinstance(out, dict) and isinstance(out.get("has_conflict"), bool):
            quote = str(out.get("conflicting_quote") or "").strip()
            if len(quote) < 15 or quote not in b["text"]:  # guard against hallucinated quotes
                quote = b["text"]
            llm = {"has_conflict": out["has_conflict"], "severity": "High" if str(out.get("severity", "")).lower() == "high" else "Medium",
                   "explanation": str(out.get("explanation") or "").strip()[:400], "conflicting_quote": quote}
    if rules["has_conflict"]:
        return rules, "llm+rules" if llm and llm["has_conflict"] else "rules"
    if llm and llm["has_conflict"]:
        ov = topical_overlap(a["text"], b["text"])
        shared_unit = bool(quantities(a["text"]).keys() & quantities(b["text"]).keys())
        if (shared_unit and ov >= 0.30) or ov >= 0.70:  # LLM-only verdicts need numeric context or very strong topical overlap
            return llm, "llm"
    return {"has_conflict": False, "severity": "Medium", "explanation": "", "conflicting_quote": ""}, "llm" if llm else "rules"


def _record_connector_ingest(db: dict[str, Any], c: dict[str, str], doc_name: str, diff: dict[str, Any]) -> None:
    db["meta"].setdefault("ingestedFiles", {})[c["file"]] = c["sha256"]
    append_audit(db, CONNECTOR_ACTOR["email"], "SYSTEM", "AUTO_CONNECTOR_SYNC", doc_name, "SUCCESS",
                 f"{c['file']} → {diff['id']} (SHA-256 {c['sha256'][:12]}…, {len(diff['hunks'])} Chunk(s), {len(diff['conflicts'])} Konflikt(e)) · Trigger: {c['trigger']}")


def stage_author_label(actor: dict[str, str], claimed: str | None, trusted: bool) -> str:
    """The author shown to the CISO comes from the verified session; a client-supplied name is only shown as a claim."""
    base = f"{actor['email']} ({actor['role']})"
    if not claimed:
        return base[:120]
    return (claimed if trusted else f"{base} · angegeben: {claimed}")[:120]


def stage_document(
    token: str | None,
    *,
    doc_name: str,
    text: str,
    title: str | None = None,
    min_role: str = "L1",
    author: str | None = None,
    replaces: str | None = None,
    as_actor: dict[str, str] | None = None,
    connector: dict[str, str] | None = None,
    partial: bool = False,
) -> dict[str, Any]:
    """`as_actor` (watched-inbox connector) skips token authorization; `connector` {file, sha256, trigger} makes the
    final write also remember the file hash and append AUTO_CONNECTOR_SYNC — all in one atomic DB update."""
    t_all = time.perf_counter()
    lat: dict[str, int] = {}

    actor = as_actor
    if actor is None:
        with locked_db() as db:  # cheap authorization (+ persisted ACCESS_DENIED audit)
            actor = authorize(db, token, "L2", "stage_file", doc_name)
    if min_role not in ROLE_RANK:
        raise ApiError(400, "BAD_ROLE", "Ungültige Min-Role.")
    if ROLE_RANK[min_role] > ROLE_RANK[actor["role"]]:
        raise ApiError(403, "FORBIDDEN", f"Min-Role {min_role} übersteigt Ihre Clearance ({actor['role']}).")

    t = time.perf_counter()
    doc_title, chunks = chunk_document(text)
    if not chunks:
        raise ApiError(422, "EMPTY_DOCUMENT", "Im Dokument wurde kein auswertbarer Text gefunden.")
    chunks = chunks[:200]
    lat["chunking"] = int((time.perf_counter() - t) * 1000)

    db = read_db()
    live = db["live_chunks"]
    visible = [c for c in live if ROLE_RANK[c["minRole"]] <= ROLE_RANK[actor["role"]]]

    if replaces:
        series_docs = {c["docName"] for c in visible if c["docName"].lower() == replaces.lower()}
        if not series_docs:
            raise ApiError(404, "REPLACES_NOT_FOUND", f"Zieldokument „{replaces}“ ist nicht in der Live-DB.")
    else:
        series_docs = {c["docName"] for c in visible if same_series(c["docName"], doc_name)}
    series = [c for c in visible if c["docName"] in series_docs]

    t = time.perf_counter()
    eid, vecs = ensure_vectors(db)
    new_vecs = EMBEDDER.embed([c["content"] for c in chunks], "document", eid)
    if new_vecs is None:
        eid, vecs = ensure_vectors(db, force_hash=True)
        new_vecs = EMBEDDER.embed([c["content"] for c in chunks], "document", eid)
    if new_vecs is None:
        raise ApiError(503, "EMBED_FAILED", "Embeddings konnten nicht berechnet werden.")
    lat["embedding"] = int((time.perf_counter() - t) * 1000)
    prm = params_for(eid)

    # Red/Green diff: align each new chunk to at most one old chunk of the same document series.
    t = time.perf_counter()
    pairs: list[tuple[float, int, int]] = []
    for i, ch in enumerate(chunks):
        for j, old in enumerate(series):
            old_num = (re.match(r"§(\d+(?:\.\d+)*)", old["section"]) or [None, None])[1]
            if old_num is not None and old_num == ch["number"]:
                pairs.append((2.0, i, j))
            elif old["id"] in vecs:
                sim = float(np.dot(new_vecs[i], vecs[old["id"]]))
                if sim >= prm["align"]:
                    pairs.append((sim, i, j))
    pairs.sort(reverse=True)
    match: dict[int, int] = {}
    used_old: set[int] = set()
    for _, i, j in pairs:
        if i not in match and j not in used_old:
            match[i] = j
            used_old.add(j)

    hunks: list[dict[str, Any]] = []
    hunk_vecs: dict[str, np.ndarray] = {}
    unchanged = 0
    for i, ch in enumerate(chunks):
        old = series[match[i]] if i in match else None
        if old and old["content"].strip() == ch["content"].strip():
            unchanged += 1
            continue
        hid = f"h-{uuid.uuid4().hex[:8]}"
        section = ch["section"]
        if section.startswith("§0 Vorbemerkung"):  # headingless text inherits the replaced chunk's section (or the title)
            section = old["section"] if old else f"§1 {(title or doc_title or doc_name)[:40]}"
        hunks.append({
            "id": hid, "section": section, "minRole": min_role,
            "oldChunkId": old["id"] if old else None, "oldDocName": old["docName"] if old else None,
            "oldSection": old["section"] if old else None, "oldContent": old["content"] if old else None,
            "newContent": ch["content"],
        })
        hunk_vecs[hid] = new_vecs[i]
    lat["diff"] = int((time.perf_counter() - t) * 1000)
    if not hunks:
        raise ApiError(409, "NO_CHANGES", "Keine Änderungen gegenüber der Live-Version erkannt.")
    # pasted rule text (partial) updates single clauses; only complete documents may remove the clauses they no longer contain
    removed_chunk_ids = [] if partial else [c["id"] for j, c in enumerate(series) if j not in used_old]

    # Cross-document conflict scanner: vector pre-selection (cosine) → LLM verdict (temp 0.0, JSON) → rule fallback.
    t = time.perf_counter()
    others = [c for c in visible if c["docName"] not in series_docs and c["id"] in vecs]
    conflicts: dict[str, dict[str, Any]] = {}
    llm_budget, deadline = 8, time.monotonic() + 55
    if others:
        mat = np.stack([vecs[c["id"]] for c in others])
        for h in hunks:
            sims = mat @ hunk_vecs[h["id"]]
            for idx in np.argsort(-sims)[:3]:
                sim, cand = float(sims[idx]), others[int(idx)]
                if sim < prm["conflict_sim"] or time.monotonic() > deadline or topical_overlap(h["newContent"], cand["content"]) < 0.25:
                    continue
                a = {"doc": doc_name, "section": h["section"], "text": h["newContent"]}
                b = {"doc": cand["docName"], "section": cand["section"], "text": cand["content"]}
                if llm_budget > 0:
                    llm_budget -= 1
                    verdict, by = check_conflict(a, b)
                else:
                    verdict, by = _rule_conflict(a["text"], b["text"]), "rules"
                if not verdict["has_conflict"]:
                    continue
                shown = round(sim, 2)  # raw cosine similarity (no rescaling)
                prev = conflicts.get(cand["id"])
                if prev and prev["similarity"] >= shown:
                    continue
                conflicts[cand["id"]] = {
                    "id": f"cf-{uuid.uuid4().hex[:8]}", "chunkId": cand["id"], "docName": cand["docName"], "section": cand["section"],
                    "quote": verdict["conflicting_quote"] or cand["content"],
                    "message": f"Semantischer Konflikt erkannt: Dieses Update widerspricht {cand['docName']} ({cand['section'].split(' ')[0]}). {verdict['explanation']}".strip(),
                    "similarity": shown, "severity": verdict["severity"].lower(), "status": "open", "ticketId": None, "verifiedBy": by,
                }
    conflict_list = sorted(conflicts.values(), key=lambda c: -c["similarity"])[:6]
    lat["conflictScan"] = int((time.perf_counter() - t) * 1000)

    lat["total"] = int((time.perf_counter() - t_all) * 1000)
    label = engine_label(eid)
    replaces_doc = None
    if series_docs and match:
        replaces_doc = Counter(series[j]["docName"] for j in match.values()).most_common(1)[0][0]
    diff = {
        "id": f"diff-{uuid.uuid4().hex[:8]}",
        "title": (title or doc_title or doc_name)[:120],
        "docName": doc_name,
        "replacesDoc": replaces_doc,
        "author": stage_author_label(actor, author, as_actor is not None),
        "uploadedAt": now_iso(),
        "summary": f"{len(hunks)} geänderte/neue Chunk(s), {unchanged} unverändert, {len(conflict_list)} Konflikt(e) · Engine: {label}",
        "sha256": sha256("\n".join(h["newContent"] for h in hunks)),
        "targetCollection": COLLECTION,
        "status": "pending", "decidedAt": None, "decidedBy": None, "decisionNote": None,
        "hunks": hunks, "removedChunkIds": removed_chunk_ids, "conflicts": conflict_list,
    }
    with file_lock():
        db = read_db()
        duplicate = next((d for d in db["staged_diffs"] if d["status"] == "pending" and d["docName"] == doc_name and d["sha256"] == diff["sha256"]), None)
        if duplicate:  # idempotent re-submit (e.g. after a gateway timeout)
            return {"ok": True, "diff": duplicate, "duplicate": True, "stats": {
                "chunks": len(chunks), "changed": len(duplicate["hunks"]), "unchanged": unchanged, "conflicts": len(duplicate["conflicts"]),
                "engineMode": label, "latencyMs": lat}}
        live_now = {c["id"]: c["content"] for c in db["live_chunks"]}
        if any(h["oldChunkId"] and live_now.get(h["oldChunkId"]) != h["oldContent"] for h in hunks):
            raise ApiError(409, "STALE_BASE", "Die Live-Version hat sich während der Analyse geändert. Bitte erneut einreichen.")
        db["staged_diffs"].append(diff)
        append_audit(db, actor["email"], actor["role"], "POLICY_STAGED", doc_name, "SUCCESS",
                     f"{len(hunks)} Chunk(s), {len(conflict_list)} Konflikt(e), SHA-256 {diff['sha256'][:12]}…, Engine {label}")
        if connector:
            _record_connector_ingest(db, connector, doc_name, diff)
        with VECTORS.lock:
            VECTORS.reload()
            if VECTORS.engine == eid:
                for hid, v in hunk_vecs.items():
                    VECTORS.vecs[f"stg:{hid}"] = v
                VECTORS.save()
        write_db(db)
    return {"ok": True, "diff": diff, "stats": {
        "chunks": len(chunks), "changed": len(hunks), "unchanged": unchanged, "conflicts": len(conflict_list),
        "engineMode": label, "latencyMs": lat,
    }}


def stage_text(token: str | None, *, title: str, target_doc: str, new_text: str, min_role: str = "L1", author: str | None = None) -> dict[str, Any]:
    """Pasted-text variant (same semantics as the Node fallback): existing target ⇒ next version replaces it, else new document."""
    target = target_doc.strip()
    if not 3 <= len(title.strip()) <= 120:  # same limits as stagePolicy in src/lib/db/staging.ts
        raise ApiError(400, "BAD_TITLE", "Titel: 3–120 Zeichen.")
    if not re.fullmatch(r"[\w.\-&,() ]{3,120}", target):
        raise ApiError(400, "BAD_TARGET", "Zieldokument: 3–120 Zeichen (Buchstaben, Ziffern, Leerzeichen, . _ - & , ( )).")
    if not 10 <= len(new_text.strip()) <= 2000:
        raise ApiError(400, "BAD_TEXT", "Regeltext: 10–2000 Zeichen.")
    existing = next((c for c in read_db()["live_chunks"] if c["docName"].lower() == target.lower()), None)
    if existing:
        return stage_document(token, doc_name=bump_version(existing["docName"]), text=new_text, title=title, min_role=min_role, author=author, replaces=existing["docName"], partial=True)
    name = target if re.search(r"\.[A-Za-z0-9]{1,5}$", target) else f"{target}.md"
    return stage_document(token, doc_name=name, text=new_text, title=title, min_role=min_role, author=author, partial=True)


def approve_diff(token: str | None, diff_id: str, override: bool = False, override_reason: str = "") -> dict[str, Any]:
    with locked_db() as db:
        actor = authorize(db, token, "L3", "approve", diff_id)
        idx = next((i for i, d in enumerate(db["staged_diffs"]) if d["id"] == diff_id and d["status"] == "pending"), -1)
        if idx < 0:
            raise ApiError(404, "NOT_FOUND", "Pending-Diff nicht gefunden.")
        diff = db["staged_diffs"][idx]
        stale = False
        for h in diff["hunks"]:
            if not h.get("oldChunkId"):
                continue
            current = next((c for c in db["live_chunks"] if c["id"] == h["oldChunkId"]), None)
            if current is None or current["content"] != h.get("oldContent"):
                stale = True
                break
        if not stale:
            stale = any(not any(c["id"] == old_id for c in db["live_chunks"]) for old_id in diff.get("removedChunkIds", []))
        if stale:
            append_audit(db, actor["email"], actor["role"], "MERGE_BLOCKED", diff["docName"], "DENIED", "Staging-Basis ist nicht mehr aktuell")
            raise ApiError(409, "STALE_DIFF", "Die Live-Version hat sich seit dem Staging geändert. Bitte erneut diffen.")
        open_conflicts = []
        for conflict in diff["conflicts"]:
            if conflict["status"] == "open":
                open_conflicts.append(conflict)
                continue
            ticket = next((t for t in db["tickets"] if t["id"] == conflict.get("ticketId")), None)
            if ticket is None or ticket["status"] != "resolved":
                open_conflicts.append(conflict)
        if open_conflicts and not override:
            ids = ", ".join(c["id"] for c in open_conflicts)
            append_audit(db, actor["email"], actor["role"], "MERGE_BLOCKED", diff["docName"], "DENIED", f"{len(open_conflicts)} ungelöste(r) Konflikt(e): {ids}")
            raise ApiError(409, "CONFLICT_UNRESOLVED", "Ungelöste Konflikte: Sync-Ticket erstellen und erledigen oder Override mit Begründung bestätigen.")
        override_note = (override_reason or "").strip()[:500]
        if open_conflicts and len(override_note) < 10:
            raise ApiError(400, "OVERRIDE_REASON_REQUIRED", "Für einen Override ist eine Begründung (mind. 10 Zeichen) erforderlich.")

        superseded, moved = 0, 0
        ts = now_iso()
        with VECTORS.lock:
            VECTORS.reload()
            removed_ids = set(diff.get("removedChunkIds", []))
            for h in diff["hunks"]:
                if h["oldChunkId"]:
                    before = len(db["live_chunks"])
                    db["live_chunks"] = [c for c in db["live_chunks"] if c["id"] != h["oldChunkId"]]
                    superseded += before - len(db["live_chunks"])
                    VECTORS.vecs.pop(h["oldChunkId"], None)
                new_id = f"chk-{uuid.uuid4().hex[:8]}"
                db["live_chunks"].append({
                    "id": new_id, "docName": diff["docName"], "section": h["section"], "minRole": h["minRole"], "content": h["newContent"],
                    "updatedAt": ts, "sha256": sha256(f"{diff['docName']}|{h['section']}|{h['newContent']}"), "approvedBy": actor["email"],
                })
                vec = VECTORS.vecs.pop(f"stg:{h['id']}", None)
                if vec is not None:
                    VECTORS.vecs[new_id] = vec
                    moved += 1
            if removed_ids:
                before = len(db["live_chunks"])
                db["live_chunks"] = [c for c in db["live_chunks"] if c["id"] not in removed_ids]
                superseded += before - len(db["live_chunks"])
                for old_id in removed_ids:
                    VECTORS.vecs.pop(old_id, None)
            if diff.get("replacesDoc"):
                for chunk in db["live_chunks"]:
                    if chunk["docName"] == diff["replacesDoc"]:
                        chunk["docName"] = diff["docName"]
                        chunk["sha256"] = sha256(f"{chunk['docName']}|{chunk['section']}|{chunk['content']}")
            for chunk in db["live_chunks"]:  # exact facts (table cells, value sentences) live in the chunk record
                if chunk.get("factsVersion") != FACTS_VERSION:
                    chunk["facts"], chunk["factsVersion"] = extract_facts(chunk["content"]), FACTS_VERSION
            refresh_lexical(db["live_chunks"])  # BM25 terms + spans are precomputed here, never per query
            VECTORS.save()
        db["staged_diffs"].pop(idx)
        db["meta"]["liveVersion"] += 1
        if open_conflicts:
            resolution = f"Override ({len(open_conflicts)} Konflikt akzeptiert: {', '.join(c['id'] for c in open_conflicts)}; Begründung: {override_note})"
        elif diff["conflicts"]:
            resolution = "Konflikte per Sync-Ticket adressiert (" + ", ".join(str(c["ticketId"]) for c in diff["conflicts"]) + ")"
        else:
            resolution = "keine Konflikte"
        append_audit(db, actor["email"], actor["role"], "APPROVED_AND_MERGED", diff["docName"], "SUCCESS",
                     f"{len(diff['hunks'])} Chunk(s) live, {superseded} ersetzt, {moved} Vektor(en) übernommen; {resolution}")
    return {"ok": True, "merged": len(diff["hunks"]), "superseded": superseded, "docName": diff["docName"]}
