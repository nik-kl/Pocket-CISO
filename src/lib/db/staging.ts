// SERVER ONLY. Pipeline A (local, md/txt): document series, conflicts, staging, approval/merge and rejection.
import { ROLE_RANK, type StageStats, type Conflict, type DiffHunk, type Role, type StagedDiff } from "../mock-store";
import { appendAudit } from "./audit";
import { authorize } from "./auth";
import { chunkDocument } from "./chunk";
import { LOCAL_ENGINE, STOP, norm, tokenize } from "./search";
import { transact } from "./store";
import { Actor, COLLECTION, Db, R, fail, isFail, nowIso, sha256, uid } from "./types";

/* ───────────────────────── Pipeline A: staging, merge, reject ───────────────────────── */

const docFamily = (name: string) => name.replace(/_v\d+(\.[A-Za-z0-9]+)$/, "$1").replace(/_\d{4}(\.[A-Za-z0-9]+)$/, "$1").toLowerCase();

function bumpVersion(name: string): string {
  const m = name.match(/^(.*_v)(\d+)(\.[A-Za-z0-9]+)$/);
  if (m) return `${m[1]}${Number(m[2]) + 1}${m[3]}`;
  const ext = name.match(/(\.[A-Za-z0-9]+)$/)?.[1] ?? "";
  return `${ext ? name.slice(0, -ext.length) : name}_v2${ext}`;
}

const contentTokens = (s: string) => new Set(tokenize(s).filter((t) => t.length >= 3 && !STOP.has(t) && !/^\d+$/.test(t)).map((t) => (t.length >= 7 ? t.slice(0, 7) : t)));
const numbersOf = (s: string) => new Set((s.match(/\d+(?:[.,]\d+)?/g) ?? []).map((n) => n.replace(",", ".")));

// Mirrors UNIT_RE / quantities() / _rule_conflict() in engine/search/facts.py + engine/staging.py: same unit + different value on the same topic.
const UNIT_RE = /(\d+(?:[.,]\d+)?)\s*(stunden|stunde|std|tagen|tage|tag|wochen|woche|monaten|monate|monat|jahren|jahre|jahr|minuten|minute|zeichen|sekunden|prozent|%|versuche|versuchen|mal)\b/gi;
const UNIT_LABEL: Record<string, string> = { stun: "Stunden", tage: "Tage", woch: "Wochen", mona: "Monate", jahr: "Jahre", minu: "Minuten", zeic: "Zeichen", seku: "Sekunden", proz: "%", vers: "Versuche", mal: "Mal" };

function quantities(text: string): Map<string, Set<string>> {
  const out = new Map<string, Set<string>>();
  for (const m of text.matchAll(UNIT_RE)) {
    const u = norm(m[2]);
    const key = u.startsWith("std") ? "stun" : u === "%" || u === "prozent" ? "proz" : u.slice(0, 4);
    out.set(key, (out.get(key) ?? new Set()).add(m[1].replace(",", ".")));
  }
  return out;
}

const minus = (x: Set<string>, y: Set<string>) => [...x].filter((v) => !y.has(v));

function ruleConflict(mine: string, theirs: string, overlap: number): { severity: "high" | "medium"; explanation: string } | null {
  const qa = quantities(mine);
  const qb = quantities(theirs);
  const diffs = [...qa.keys()].filter((u) => qb.has(u)).map((u) => [u, minus(qa.get(u)!, qb.get(u)!), minus(qb.get(u)!, qa.get(u)!)] as const).filter(([, x, y]) => x.length && y.length);
  if (overlap >= 0.25 && diffs.length) {
    const text = diffs.map(([u, x, y]) => `neu ${x.sort().join(", ")} ${UNIT_LABEL[u] ?? u} ↔ bestehend ${y.sort().join(", ")} ${UNIT_LABEL[u] ?? u}`).join("; ");
    return { severity: overlap >= 0.5 ? "high" : "medium", explanation: `Abweichende Werte: ${text}.` };
  }
  if (overlap >= 0.34 && !qa.size && !qb.size) { // no units on either side: generic number mismatch
    const na = numbersOf(mine);
    const nb = numbersOf(theirs);
    const x = minus(na, nb);
    const y = minus(nb, na);
    if (x.length && y.length) return { severity: "medium", explanation: `Abweichende Werte: neu ${x.sort().join(", ")} ↔ bestehend ${y.sort().join(", ")}.` };
  }
  return null;
}

// Rule-based contradiction check against live chunks the actor may see (no vectors in this engine; "similarity" is the raw topical token overlap).
function detectConflicts(db: Db, text: string, ignoreChunkIds: string[], ignoreDocs: Set<string>, ignoreFamily: string, maxRank: number): Conflict[] {
  const mine = contentTokens(text);
  const out: Conflict[] = [];
  if (!numbersOf(text).size) return out;
  for (const c of db.live_chunks) {
    if (ROLE_RANK[c.minRole] > maxRank || ignoreChunkIds.includes(c.id) || ignoreDocs.has(c.docName) || docFamily(c.docName) === ignoreFamily) continue;
    const theirs = contentTokens(c.content);
    const overlap = [...mine].filter((t) => theirs.has(t)).length / Math.max(1, Math.min(mine.size, theirs.size));
    const verdict = ruleConflict(text, c.content, overlap);
    if (!verdict) continue;
    out.push({
      id: uid("cf"), chunkId: c.id, docName: c.docName, section: c.section, quote: c.content,
      message: `Semantischer Konflikt erkannt: Dieses Update widerspricht ${c.docName} (${c.section.split(" ")[0]}). ${verdict.explanation}`,
      similarity: Math.min(0.99, Math.round(overlap * 100) / 100), severity: verdict.severity, status: "open", ticketId: null, verifiedBy: "rules",
    });
  }
  return out;
}

const GENERIC_NAME_WORDS = new Set(["policy", "policies", "richtlinie", "richtlinien", "leitlinie", "guideline", "plan", "handbuch", "playbook", "runbook", "konzept", "standard", "dokument"]);

function seriesTokens(name: string): Set<string> {
  const stem = name.replace(/\.[A-Za-z0-9]{1,5}$/, "").toLowerCase();
  return new Set(stem.split(/[^a-z0-9äöüß]+/).filter((t) => t && !/^v?\d+(\.\d+)*$/.test(t) && !["final", "draft", "entwurf"].includes(t) && !GENERIC_NAME_WORDS.has(t)));
}

function sameSeries(a: string, b: string): boolean {
  const sa = seriesTokens(a);
  const sb = seriesTokens(b);
  if (!sa.size || !sb.size) return false;
  return [...sa].filter((t) => sb.has(t)).length / new Set([...sa, ...sb]).size >= 0.5;
}

/* ── Pipeline A core: chunk → align to series → diff → conflicts ── */

// Unicode-aware (umlauts!) like Python's \w; names are NFC-normalised first because macOS hands out NFD file names.
// Same character rule as safe_doc_name in engine/ingest/parse.py: letters, digits, space . _ - & , ( ) — everything else → "_".
const DOC_NAME_RE = /^[\p{L}\p{N}_.\-&,() ]{3,120}$/u;
export const safeDocName = (name: string) => name.normalize("NFC").replace(/[^\p{L}\p{N}_.\-&,() ]/gu, "_").slice(0, 120);

interface StageInput {
  docName: string;
  title: string;
  text: string;
  minRole: Role;
  author?: string;
  trustedAuthor?: boolean; // only the server-side connector may set the displayed author
  partial?: boolean; // pasted rule text updates single clauses: nothing is removed from the replaced document
  replaces?: string | null;
}

// The displayed author comes from the verified session; a client-supplied name is only shown as a claim.
function authorLabel(a: Actor, p: StageInput): string {
  const base = `${a.email} (${a.role})`;
  if (!p.author) return base.slice(0, 120);
  return (p.trustedAuthor ? p.author : `${base} · angegeben: ${p.author}`).slice(0, 120);
}

// Core of Pipeline A inside an open transaction (shared by manual staging and the watched-inbox connector).
export function stageInTx(db: Db, a: Actor, p: StageInput): R<{ diff: StagedDiff; stats: StageStats }> {
  const t0 = performance.now();
  if (!(p.minRole in ROLE_RANK)) return fail(400, "BAD_ROLE", "Ungültige Min-Role.");
  if (ROLE_RANK[p.minRole] > ROLE_RANK[a.role]) return fail(403, "FORBIDDEN", `Min-Role ${p.minRole} übersteigt Ihre Clearance (${a.role}).`);
  if (!DOC_NAME_RE.test(p.docName)) return fail(400, "BAD_TARGET", "Dokumentname: 3–120 Zeichen (Buchstaben, Ziffern, Leerzeichen, . _ - & , ( )).");
  if (p.text.length > 200_000) return fail(413, "TOO_LARGE", "Text zu groß.");

  const { title: docTitle, chunks } = chunkDocument(p.text);
  if (!chunks.length) return fail(422, "EMPTY_DOCUMENT", "Im Dokument wurde kein auswertbarer Text gefunden.");
  const picked = chunks.slice(0, 200);

  const visible = db.live_chunks.filter((c) => ROLE_RANK[c.minRole] <= ROLE_RANK[a.role]);
  const seriesDocs = new Set(
    p.replaces ? visible.filter((c) => c.docName.toLowerCase() === p.replaces!.toLowerCase()).map((c) => c.docName) : visible.filter((c) => sameSeries(c.docName, p.docName)).map((c) => c.docName)
  );
  if (p.replaces && !seriesDocs.size) return fail(404, "REPLACES_NOT_FOUND", `Zieldokument „${p.replaces}“ ist nicht in der Live-DB.`);
  const series = visible.filter((c) => seriesDocs.has(c.docName));

  // align each new chunk to at most one old chunk (same § number first, then token overlap)
  const pairs: [number, number, number][] = [];
  picked.forEach((ch, i) =>
    series.forEach((old, j) => {
      const num = /^§(\d+(?:\.\d+)*)/.exec(old.section)?.[1];
      if (num !== undefined && num === ch.number) return pairs.push([2, i, j]);
      const mine = contentTokens(ch.content);
      const theirs = contentTokens(old.content);
      const ov = [...mine].filter((t) => theirs.has(t)).length / Math.max(1, Math.min(mine.size, theirs.size));
      if (ov >= 0.5) pairs.push([ov, i, j]);
    })
  );
  pairs.sort((x, y) => y[0] - x[0]);
  const match = new Map<number, number>();
  const usedOld = new Set<number>();
  for (const [, i, j] of pairs) if (!match.has(i) && !usedOld.has(j)) (match.set(i, j), usedOld.add(j));

  const hunks: DiffHunk[] = [];
  let unchanged = 0;
  picked.forEach((ch, i) => {
    const old = match.has(i) ? series[match.get(i)!] : undefined;
    if (old && old.content.trim() === ch.content.trim()) return void unchanged++;
    const section = ch.section.startsWith("§0 Vorbemerkung") ? (old?.section ?? `§1 ${(p.title || docTitle || p.docName).slice(0, 40)}`) : ch.section;
    hunks.push({
      id: uid("h"), section, minRole: p.minRole, oldChunkId: old?.id ?? null, oldDocName: old?.docName ?? null,
      oldSection: old?.section ?? null, oldContent: old?.content ?? null, newContent: ch.content,
    });
  });
  if (!hunks.length) return fail(409, "NO_CHANGES", "Keine Änderungen gegenüber der Live-Version erkannt.");
  const removedChunkIds = p.partial ? [] : series.filter((_, j) => !usedOld.has(j)).map((c) => c.id);

  const merged = new Map<string, Conflict>();
  for (const h of hunks)
    for (const c of detectConflicts(db, h.newContent, series.map((x) => x.id), seriesDocs, docFamily(p.docName), ROLE_RANK[a.role]))
      if (!merged.has(c.chunkId) || merged.get(c.chunkId)!.similarity < c.similarity) merged.set(c.chunkId, c);
  const conflicts = [...merged.values()].sort((x, y) => y.similarity - x.similarity).slice(0, 6);

  const replacesDoc = match.size ? [...match.values()].map((j) => series[j].docName).sort((x, y) => [...match.values()].filter((v) => series[v].docName === y).length - [...match.values()].filter((v) => series[v].docName === x).length)[0] : null;
  const diff: StagedDiff = {
    id: uid("diff"), title: (p.title || docTitle || p.docName).slice(0, 120), docName: p.docName, replacesDoc,
    author: authorLabel(a, p), uploadedAt: nowIso(),
    summary: `${hunks.length} geänderte/neue Chunk(s), ${unchanged} unverändert, ${conflicts.length} Konflikt(e) · Engine: ${LOCAL_ENGINE}`,
    sha256: sha256(hunks.map((h) => h.newContent).join("\n")), targetCollection: COLLECTION, status: "pending",
    decidedAt: null, decidedBy: null,     decisionNote: null, hunks, removedChunkIds, conflicts,
  };
  const duplicate = db.staged_diffs.find((d) => d.status === "pending" && d.docName === p.docName && d.sha256 === diff.sha256);
  if (duplicate) // idempotent re-submit (e.g. after a gateway timeout)
    return { ok: true as const, diff: duplicate, stats: { chunks: picked.length, changed: duplicate.hunks.length, unchanged, conflicts: duplicate.conflicts.length, engineMode: LOCAL_ENGINE, latencyMs: { total: Math.round(performance.now() - t0) } } };
  db.staged_diffs.push(diff);
  appendAudit(db, { actor: a.email, role: a.role, action: "POLICY_STAGED", target: p.docName, status: "SUCCESS", detail: `${hunks.length} Chunk(s), ${conflicts.length} Konflikt(e), SHA-256 ${diff.sha256.slice(0, 12)}…, Engine ${LOCAL_ENGINE}` });
  return {
    ok: true as const, diff,
    stats: { chunks: picked.length, changed: hunks.length, unchanged, conflicts: conflicts.length, engineMode: LOCAL_ENGINE, latencyMs: { total: Math.round(performance.now() - t0) } },
  };
}

export function stageDocument(token: string | null, p: StageInput): Promise<R<{ diff: StagedDiff; stats: StageStats }>> {
  return transact((db) => {
    const a = authorize(db, token, "L2", "stage_file", String(p.docName));
    return isFail(a) ? a : stageInTx(db, a, p);
  });
}

// Pasted-text variant: an existing target document ⇒ next version replaces it, otherwise a new document.
export async function stagePolicy(token: string | null, p: { title: string; targetDoc: string; newText: string; minRole: string }): Promise<R<{ diff: StagedDiff; stats: StageStats }>> {
  const target = String(p.targetDoc ?? "").trim();
  const title = String(p.title ?? "").trim();
  const text = String(p.newText ?? "").trim();
  if (title.length < 3 || title.length > 120) return fail(400, "BAD_TITLE", "Titel: 3–120 Zeichen.");
  if (!DOC_NAME_RE.test(target)) return fail(400, "BAD_TARGET", "Zieldokument: 3–120 Zeichen (Buchstaben, Ziffern, Leerzeichen, . _ - & , ( )).");
  if (text.length < 10 || text.length > 2000) return fail(400, "BAD_TEXT", "Regeltext: 10–2000 Zeichen.");
  const existing = await transact((db) => db.live_chunks.find((c) => c.docName.toLowerCase() === target.toLowerCase())?.docName ?? null);
  return stageDocument(token, {
    docName: existing ? bumpVersion(existing) : /\.[A-Za-z0-9]{1,5}$/.test(target) ? target : `${target}.md`,
    title, text, minRole: p.minRole as Role, replaces: existing, partial: true,
  });
}

export function approveDiff(token: string | null, diffId: string, override: boolean, overrideReason = ""): Promise<R<{ merged: number; superseded: number; docName: string }>> {
  return transact((db) => {
    const a = authorize(db, token, "L3", "approve", diffId);
    if (isFail(a)) return a;
    const idx = db.staged_diffs.findIndex((d) => d.id === diffId && d.status === "pending");
    if (idx < 0) return fail(404, "NOT_FOUND", "Pending-Diff nicht gefunden.");
    const diff = db.staged_diffs[idx];
    const stale = [
      ...diff.hunks.filter((h) => h.oldChunkId).map((h) => ({ id: h.oldChunkId!, content: h.oldContent })),
      ...(diff.removedChunkIds ?? []).map((id) => ({ id, content: null })),
    ].some(({ id, content }) => {
      const current = db.live_chunks.find((c) => c.id === id);
      return !current || (content !== null && current.content !== content);
    });
    if (stale) {
      appendAudit(db, { actor: a.email, role: a.role, action: "MERGE_BLOCKED", target: diff.docName, status: "DENIED", detail: "Staging-Basis ist nicht mehr aktuell" });
      return fail(409, "STALE_DIFF", "Die Live-Version hat sich seit dem Staging geändert. Bitte erneut diffen.");
    }
    const open = diff.conflicts.filter((c) => {
      if (c.status === "open") return true;
      const ticket = c.ticketId ? db.tickets.find((t) => t.id === c.ticketId) : null;
      return !ticket || ticket.status !== "resolved";
    });
    if (open.length && override !== true) {
      appendAudit(db, { actor: a.email, role: a.role, action: "MERGE_BLOCKED", target: diff.docName, status: "DENIED", detail: `${open.length} ungelöste(r) Konflikt(e): ${open.map((c) => c.id).join(", ")}` });
      return fail(409, "CONFLICT_UNRESOLVED", "Ungelöste Konflikte: Sync-Ticket erstellen und erledigen oder Override mit Begründung bestätigen.");
    }
    const overrideNote = String(overrideReason ?? "").trim().slice(0, 500);
    if (open.length && overrideNote.length < 10) return fail(400, "OVERRIDE_REASON_REQUIRED", "Für einen Override ist eine Begründung (mind. 10 Zeichen) erforderlich.");

    let superseded = 0;
    const ts = nowIso();
    const removed = new Set(diff.removedChunkIds ?? []);
    for (const h of diff.hunks) {
      if (h.oldChunkId) {
        const before = db.live_chunks.length;
        db.live_chunks = db.live_chunks.filter((c) => c.id !== h.oldChunkId);
        superseded += before - db.live_chunks.length;
      }
      db.live_chunks.push({ id: uid("chk"), docName: diff.docName, section: h.section, minRole: h.minRole, content: h.newContent, updatedAt: ts, sha256: sha256(`${diff.docName}|${h.section}|${h.newContent}`), approvedBy: a.email });
    }
    if (removed.size) {
      const before = db.live_chunks.length;
      db.live_chunks = db.live_chunks.filter((c) => !removed.has(c.id));
      superseded += before - db.live_chunks.length;
    }
    if (diff.replacesDoc) {
      for (const c of db.live_chunks) {
        if (c.docName === diff.replacesDoc) {
          c.docName = diff.docName;
          c.sha256 = sha256(`${c.docName}|${c.section}|${c.content}`);
        }
      }
    }
    db.staged_diffs.splice(idx, 1);
    db.meta.liveVersion += 1;
    const resolution = open.length ? `Override (${open.length} Konflikt akzeptiert: ${open.map((c) => c.id).join(", ")}; Begründung: ${overrideNote})` : diff.conflicts.length ? `Konflikte per Sync-Ticket adressiert (${diff.conflicts.map((c) => c.ticketId).join(", ")})` : "keine Konflikte";
    appendAudit(db, { actor: a.email, role: a.role, action: "APPROVED_AND_MERGED", target: diff.docName, status: "SUCCESS", detail: `${diff.hunks.length} Chunk(s) live, ${superseded} ersetzt; ${resolution}` });
    return { ok: true as const, merged: diff.hunks.length, superseded, docName: diff.docName };
  });
}

export function rejectDiff(token: string | null, diffId: string, reason: string): Promise<R<object>> {
  return transact((db) => {
    const a = authorize(db, token, "L3", "reject", diffId);
    if (isFail(a)) return a;
    const diff = db.staged_diffs.find((d) => d.id === diffId && d.status === "pending");
    if (!diff) return fail(404, "NOT_FOUND", "Pending-Diff nicht gefunden.");
    const note = String(reason ?? "").trim().slice(0, 500);
    if (note.length < 3) return fail(400, "BAD_REASON", "Begründung erforderlich.");
    diff.status = "rejected";
    diff.decidedAt = nowIso();
    diff.decidedBy = a.email;
    diff.decisionNote = note;
    appendAudit(db, { actor: a.email, role: a.role, action: "REJECTED", target: diff.docName, status: "SUCCESS", detail: note });
    return { ok: true as const };
  });
}
