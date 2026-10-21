// SERVER ONLY. File-backed JSON database (data/pocket-ciso-db.json), auto-seeded on first run.
// Zero native deps: fs/promises + path + crypto. Every public function is one atomic transaction
// and maps 1:1 to an /api/v1 action (→ future FastAPI endpoint).

import { promises as fs } from "fs";
import path from "path";
import crypto from "crypto";
import {
  ROLE_RANK,
  type AnswerSegment,
  type AuditReport,
  type StageStats,
  type AuditLog,
  type AuditStatus,
  type Conflict,
  type DiffHunk,
  type LiveChunk,
  type QueryResult,
  type Role,
  type Snapshot,
  type StagedDiff,
  type Ticket,
} from "./mock-store";

/* ───────────────────────── Types ───────────────────────── */

interface DbUser {
  email: string;
  role: Role;
  clearance: string;
  requiresPassword: boolean;
  passwordHash: string | null;
}

interface Meta {
  secret: string;
  liveVersion: number;
  nextTicket: number;
  nextAudit: number;
  seededAt: string;
}

export interface Db {
  meta: Meta;
  users: DbUser[];
  live_chunks: LiveChunk[];
  staged_diffs: StagedDiff[];
  audit_logs: AuditLog[];
  tickets: Ticket[];
}

type Fail = { ok: false; status: number; code: string; error: string };
export type R<T extends object> = ({ ok: true } & T) | Fail;
const fail = (status: number, code: string, error: string): Fail => ({ ok: false, status, code, error });
const isFail = (x: unknown): x is Fail => !!x && typeof x === "object" && (x as Fail).ok === false;

interface Actor {
  email: string;
  role: Role;
}

const COLLECTION = "policies_live_v1";
const GENESIS = "0".repeat(64);
const TOKEN_TTL_MS = 8 * 3600 * 1000;
const DB_PATH = path.join(process.cwd(), "data", "pocket-ciso-db.json");

export const sha256 = (s: string) => crypto.createHash("sha256").update(s, "utf8").digest("hex");
const uid = (prefix: string) => `${prefix}-${crypto.randomBytes(4).toString("hex")}`;
const nowIso = () => new Date().toISOString();
const ago = (hours: number) => new Date(Date.now() - hours * 3600 * 1000).toISOString();

/* ───────────────────────── Seed ───────────────────────── */

function chunk(id: string, docName: string, section: string, minRole: Role, content: string, hoursAgo: number): LiveChunk {
  return { id, docName, section, minRole, content, updatedAt: ago(hoursAgo), sha256: sha256(`${docName}|${section}|${content}`), approvedBy: "ciso@company.at" };
}

function seedDb(): Db {
  const live: LiveChunk[] = [
    chunk("chk-mobile-4-2", "Mobile_Work_Policy_v2.pdf", "§4.2 Öffentlicher Raum", "L1",
      "Die Verarbeitung vertraulicher Daten im öffentlichen Raum, insbesondere in öffentlichen Verkehrsmitteln (Bahn, Bus, Flugzeug), ist nur mit aktiviertem Blickschutzfilter und ausschließlich über das Unternehmens-VPN zulässig. Streng vertrauliche Daten (Klasse 3) dürfen dort nicht bearbeitet werden.", 24 * 41),
    chunk("chk-mobile-5-1", "Mobile_Work_Policy_v2.pdf", "§5.1 Öffentliche Netze", "L1",
      "Die Nutzung öffentlicher WLAN-Netze ohne aktives Unternehmens-VPN ist untersagt. Telefonate mit vertraulichem Inhalt sind in öffentlichen Bereichen zu vermeiden.", 24 * 41),
    chunk("chk-pw-v2-2", "Passwort_Policy_v2.docx", "§2 Passwortkomplexität", "L1",
      "Passwörter müssen mindestens 10 Zeichen lang sein und alle 90 Tage geändert werden.", 24 * 120),
    chunk("chk-pw-v2-4", "Passwort_Policy_v2.docx", "§4 Mehrfaktor-Authentifizierung", "L1",
      "Mehrfaktor-Authentifizierung wird für Administratorkonten empfohlen.", 24 * 120),
    chunk("chk-onboard-3", "Onboarding_IT_Handbuch.pdf", "§3 Ersteinrichtung (Seite 3)", "L1",
      "Bei der Ersteinrichtung des Benutzerkontos ist ein Passwort mit 10 Zeichen Mindestlänge zu wählen. Das Initialpasswort ist beim ersten Login zu ändern.", 24 * 75),
    chunk("chk-ir-7-3", "Incident_Response_Runbook.pdf", "§7.3 Meldepflichten NIS-2", "L2",
      "Erhebliche Sicherheitsvorfälle sind gemäß NIS-2 (Art. 23) unverzüglich, spätestens jedoch innerhalb von 24 Stunden als Frühwarnung an das BSI zu melden. Die Meldung erfolgt ausschließlich durch das CISO-Office; eine Vorfallmeldung folgt binnen 72 Stunden.", 24 * 30),
    chunk("chk-ir-7-1", "Incident_Response_Runbook.pdf", "§7.1 Wiederanlauf Tier 1", "L2",
      "Der Wiederanlauf kritischer Systeme (Tier 1) ist innerhalb von 24 Stunden nach Eintritt der Störung sicherzustellen.", 24 * 30),
    chunk("chk-bcm25-5", "BCM_Notfallplan_2025.pdf", "§5.2 Wiederanlauf & Notfallübungen", "L2",
      "Kritische Systeme (Tier 1) müssen nach einem Ausfall innerhalb von 24 Stunden wiederhergestellt sein (RTO). Notfallübungen finden jährlich statt.", 24 * 200),
    chunk("chk-risk-3", "Risikoakzeptanz_Richtlinie_v1.pdf", "§3 Freigabe von Restrisiken", "L3",
      "Restrisiken der Stufe „hoch“ dürfen ausschließlich durch den CISO gemeinsam mit der Geschäftsführung akzeptiert werden. Die Akzeptanz ist zu dokumentieren und jährlich zu überprüfen.", 24 * 90),
  ];
  const byId = (id: string) => live.find((c) => c.id === id)!;

  const pwHunks: DiffHunk[] = [
    {
      id: "h-pw-1", section: "§2 Passwortkomplexität", minRole: "L1", oldChunkId: "chk-pw-v2-2",
      oldDocName: "Passwort_Policy_v2.docx", oldSection: "§2 Passwortkomplexität", oldContent: byId("chk-pw-v2-2").content,
      newContent: "Passwörter müssen mindestens 14 Zeichen lang sein. Ein regelmäßiger Wechsel ohne Verdachtsmoment entfällt.",
    },
    {
      id: "h-pw-2", section: "§4 Mehrfaktor-Authentifizierung", minRole: "L1", oldChunkId: "chk-pw-v2-4",
      oldDocName: "Passwort_Policy_v2.docx", oldSection: "§4 Mehrfaktor-Authentifizierung", oldContent: byId("chk-pw-v2-4").content,
      newContent: "Mehrfaktor-Authentifizierung ist für alle privilegierten Konten verpflichtend.",
    },
  ];
  const bcmHunks: DiffHunk[] = [
    {
      id: "h-bcm-1", section: "§5.2 Wiederanlauf & Notfallübungen", minRole: "L2", oldChunkId: "chk-bcm25-5",
      oldDocName: "BCM_Notfallplan_2025.pdf", oldSection: "§5.2 Wiederanlauf & Notfallübungen", oldContent: byId("chk-bcm25-5").content,
      newContent: "Kritische Systeme (Tier 1) müssen nach einem Ausfall innerhalb von 8 Stunden wiederhergestellt sein (RTO). Notfallübungen finden halbjährlich statt.",
    },
  ];

  const diffs: StagedDiff[] = [
    {
      id: "diff-passwort", title: "Passwort-Policy v3 (NIS-2 / BSI ORP.4)", docName: "Passwort_Policy_v3.docx",
      replacesDoc: "Passwort_Policy_v2.docx", author: "m.keller@company.at (IT-Security)", uploadedAt: ago(18),
      summary: "Mindestlänge 10 → 14 Zeichen, periodischer Wechsel entfällt, MFA verpflichtend (NIS-2 / BSI-Grundschutz ORP.4).",
      sha256: sha256(pwHunks.map((h) => h.newContent).join("\n")), targetCollection: COLLECTION, status: "pending",
      decidedAt: null, decidedBy: null, decisionNote: null, hunks: pwHunks,
      conflicts: [{
        id: "cf-pw-1", chunkId: "chk-onboard-3", docName: "Onboarding_IT_Handbuch.pdf", section: "§3 Ersteinrichtung (Seite 3)",
        quote: byId("chk-onboard-3").content,
        message: "Semantischer Konflikt erkannt: Dieses Update widerspricht Onboarding_IT_Handbuch.pdf (Seite 3), wo weiterhin '10 Zeichen Mindestlänge' gefordert wird.",
        similarity: 0.91, severity: "high", status: "open", ticketId: null,
      }],
    },
    {
      id: "diff-bcm", title: "BCM-Notfallplan 2026", docName: "BCM_Notfallplan_2026.pdf",
      replacesDoc: "BCM_Notfallplan_2025.pdf", author: "s.brandt@company.at (BCM-Beauftragte)", uploadedAt: ago(4),
      summary: "Wiederanlaufzeit (RTO) für Tier-1-Systeme von 24 h auf 8 h verkürzt, Notfallübungen halbjährlich.",
      sha256: sha256(bcmHunks.map((h) => h.newContent).join("\n")), targetCollection: COLLECTION, status: "pending",
      decidedAt: null, decidedBy: null, decisionNote: null, hunks: bcmHunks,
      conflicts: [{
        id: "cf-bcm-1", chunkId: "chk-ir-7-1", docName: "Incident_Response_Runbook.pdf", section: "§7.1 Wiederanlauf Tier 1",
        quote: byId("chk-ir-7-1").content,
        message: "Semantischer Konflikt erkannt: Dieses Update widerspricht Incident_Response_Runbook.pdf (Seite 7), wo weiterhin ein Tier-1-Wiederanlauf von '24 Stunden' gefordert wird.",
        similarity: 0.87, severity: "medium", status: "open", ticketId: null,
      }],
    },
  ];

  const db: Db = {
    meta: { secret: crypto.randomBytes(32).toString("hex"), liveVersion: 1, nextTicket: 4712, nextAudit: 1, seededAt: nowIso() },
    users: [
      { email: "employee@company.at", role: "L1", clearance: "Employee", requiresPassword: false, passwordHash: null },
      { email: "admin@company.at", role: "L2", clearance: "IT-Admin", requiresPassword: true, passwordHash: sha256("admin2026") },
      { email: "ciso@company.at", role: "L3", clearance: "CISO", requiresPassword: true, passwordHash: sha256("ciso2026") },
    ],
    live_chunks: live,
    staged_diffs: diffs,
    audit_logs: [],
    tickets: [],
  };
  appendAudit(db, { actor: "system@pocket-ciso", role: "SYSTEM", action: "DB_INITIALIZED", target: COLLECTION, status: "INFO", detail: `${live.length} Chunks, 3 Benutzer geladen` }, ago(48));
  appendAudit(db, { actor: "m.keller@company.at", role: "L2", action: "POLICY_STAGED", target: "Passwort_Policy_v3.docx", status: "SUCCESS", detail: "2 Chunks in Staging Vector DB, 1 Konflikt erkannt" }, ago(18));
  appendAudit(db, { actor: "s.brandt@company.at", role: "L2", action: "POLICY_STAGED", target: "BCM_Notfallplan_2026.pdf", status: "SUCCESS", detail: "1 Chunk in Staging Vector DB, 1 Konflikt erkannt" }, ago(4));
  return db;
}

/* ───────────────────────── Persistence ───────────────────────── */

const g = globalThis as unknown as { __pcQueue?: Promise<unknown>; __pcFails?: Record<string, number[]> };

async function load(): Promise<Db> {
  try {
    const raw = await fs.readFile(DB_PATH, "utf8");
    const parsed = JSON.parse(raw) as Db;
    if (!parsed.meta || !Array.isArray(parsed.live_chunks)) throw new Error("Datenbank-Schema ungültig: " + DB_PATH);
    return parsed;
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
    const db = seedDb();
    await persist(db);
    return db;
  }
}

async function persist(db: Db): Promise<void> {
  await fs.mkdir(path.dirname(DB_PATH), { recursive: true });
  const tmp = `${DB_PATH}.${process.pid}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(db, null, 2), "utf8");
  await fs.rename(tmp, DB_PATH);
}

// Cross-process mutex (atomic mkdir) shared with the Python backend (backend/rag_engine.py → file_lock).
async function acquireLock(): Promise<() => Promise<void>> {
  const dir = path.dirname(DB_PATH);
  const lock = path.join(dir, ".db.lock");
  await fs.mkdir(dir, { recursive: true });
  const deadline = Date.now() + 10_000;
  for (;;) {
    try {
      await fs.mkdir(lock);
      return async () => {
        await fs.rmdir(lock).catch(() => undefined);
      };
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
      const st = await fs.stat(lock).catch(() => null);
      if (st && Date.now() - st.mtimeMs > 60_000) {
        await fs.rmdir(lock).catch(() => undefined);
        continue;
      }
      if (Date.now() > deadline) throw new Error("Datenbank ist gesperrt.");
      await new Promise((r) => setTimeout(r, 20));
    }
  }
}

// Serialised read-modify-write (in-process queue + cross-process lock); persists only when the document changed.
function transact<T>(fn: (db: Db) => T): Promise<T> {
  const run = async () => {
    const release = await acquireLock();
    try {
      const db = await load();
      const before = JSON.stringify(db, null, 2);
      const out = fn(db);
      if (JSON.stringify(db, null, 2) !== before) await persist(db);
      return out;
    } finally {
      await release();
    }
  };
  const prev = g.__pcQueue ?? Promise.resolve();
  const next = prev.then(run, run);
  g.__pcQueue = next.catch(() => undefined);
  return next;
}

// Makes sure the DB file exists (seeds it) so the Python backend can read users/secret/chunks.
export const ensureDb = (): Promise<void> => transact(() => undefined);

/* ───────────────────────── Audit trail (hash chain) ───────────────────────── */

const hashEntry = (e: AuditLog) =>
  sha256([e.prevHash, e.id, e.timestamp, e.actor, e.role, e.action, e.target, e.status, e.detail].join("|"));

function appendAudit(
  db: Db,
  e: { actor: string; role: AuditLog["role"]; action: string; target: string; status: AuditStatus; detail?: string },
  timestamp?: string
): AuditLog {
  const entry: AuditLog = {
    id: `AUD-${String(db.meta.nextAudit++).padStart(4, "0")}`,
    timestamp: timestamp ?? nowIso(),
    actor: e.actor, role: e.role, action: e.action, target: e.target, status: e.status, detail: e.detail ?? "",
    prevHash: db.audit_logs.at(-1)?.hash ?? GENESIS, hash: "",
  };
  entry.hash = hashEntry(entry);
  db.audit_logs.push(entry);
  return entry;
}

function verifyChain(logs: AuditLog[]) {
  let prev = GENESIS;
  for (const l of logs) {
    if (l.prevHash !== prev || l.hash !== hashEntry(l)) return { valid: false, entries: logs.length, brokenAt: l.id };
    prev = l.hash;
  }
  return { valid: true, entries: logs.length, brokenAt: null };
}

/* ───────────────────────── Auth (HMAC session tokens) ───────────────────────── */

const mac = (secret: string, payload: string) => crypto.createHmac("sha256", secret).update(payload).digest("hex");

function signToken(secret: string, role: Role): string {
  const payload = `${role}.${Date.now() + TOKEN_TTL_MS}`;
  return `${payload}.${mac(secret, payload)}`;
}

function verifyToken(secret: string, token: string | null): Role | null {
  if (!token) return null;
  const [role, exp, sig] = token.split(".");
  if ((role !== "L2" && role !== "L3") || !exp || !sig) return null;
  const expected = mac(secret, `${role}.${exp}`);
  if (sig.length !== expected.length || !crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expected))) return null;
  return Number(exp) > Date.now() ? role : null;
}

function actorFor(db: Db, token: string | null): Actor {
  const role = verifyToken(db.meta.secret, token) ?? "L1";
  return { email: db.users.find((u) => u.role === role)!.email, role };
}

// Returns the actor, or a Fail (and an audit entry) when the role is insufficient.
function authorize(db: Db, token: string | null, need: Role, action: string, target: string): Actor | Fail {
  const a = actorFor(db, token);
  if (ROLE_RANK[a.role] >= ROLE_RANK[need]) return a;
  appendAudit(db, { actor: a.email, role: a.role, action: "ACCESS_DENIED", target: `${action}:${target}`, status: "DENIED", detail: `Erfordert ${need}` });
  return fail(403, "FORBIDDEN", `Diese Aktion erfordert Clearance ${need}.`);
}

export function authenticate(token: string | null, role: string, password: string): Promise<R<{ token: string; session: { role: Role; email: string; clearance: string } }>> {
  return transact((db) => {
    const requester = actorFor(db, token);
    const user = db.users.find((u) => u.role === role && u.requiresPassword);
    if (!user || !user.passwordHash) return fail(400, "BAD_ROLE", "Für diese Rolle ist keine Anmeldung möglich.");

    const fails = (g.__pcFails ??= {});
    const recent = (fails[user.role] ??= []).filter((t) => Date.now() - t < 60_000);
    fails[user.role] = recent;
    if (recent.length >= 5) return fail(429, "RATE_LIMITED", "Zu viele Fehlversuche. Bitte eine Minute warten.");

    const given = Buffer.from(sha256(typeof password === "string" ? password : ""));
    const valid = crypto.timingSafeEqual(given, Buffer.from(user.passwordHash));
    if (!valid) {
      recent.push(Date.now());
      appendAudit(db, { actor: requester.email, role: requester.role, action: "AUTH_ELEVATION_FAILED", target: `→ ${user.role} (${user.email})`, status: "FAILED", detail: "Ungültiges Passwort" });
      return fail(401, "INVALID_CREDENTIALS", "Falsches Passwort.");
    }
    fails[user.role] = [];
    appendAudit(db, { actor: user.email, role: user.role, action: "AUTH_ELEVATION_SUCCESS", target: `→ ${user.role} (${user.clearance})`, status: "SUCCESS", detail: `Sitzung ${TOKEN_TTL_MS / 3600000} h gültig` });
    return { ok: true as const, token: signToken(db.meta.secret, user.role), session: { role: user.role, email: user.email, clearance: user.clearance } };
  });
}

export function logout(token: string | null): Promise<R<object>> {
  return transact((db) => {
    const a = actorFor(db, token);
    if (a.role !== "L1") appendAudit(db, { actor: a.email, role: a.role, action: "AUTH_LOGOUT", target: "→ L1", status: "SUCCESS", detail: "Sitzung beendet" });
    return { ok: true as const };
  });
}

/* ───────────────────────── Snapshot ───────────────────────── */

export function getSnapshot(token: string | null): Promise<Omit<Snapshot, "backend">> {
  return transact((db) => {
    const a = actorFor(db, token);
    const user = db.users.find((u) => u.role === a.role)!;
    const priv = ROLE_RANK[a.role] >= ROLE_RANK.L2;
    return {
      session: { role: a.role, email: a.email, clearance: user.clearance },
      collection: COLLECTION,
      liveVersion: db.meta.liveVersion,
      pendingCount: db.staged_diffs.filter((d) => d.status === "pending").length,
      totalChunks: db.live_chunks.length,
      chunks: db.live_chunks.filter((c) => ROLE_RANK[c.minRole] <= ROLE_RANK[a.role]),
      diffs: priv ? db.staged_diffs : null,
      audit: priv ? db.audit_logs : null,
      tickets: priv ? db.tickets : null,
      integrity: priv ? verifyChain(db.audit_logs) : null,
    };
  });
}

/* ───────────────────────── Pipeline B: stateless retrieval ───────────────────────── */

const norm = (s: string) =>
  s.toLowerCase().replace(/ä/g, "a").replace(/ö/g, "o").replace(/ü/g, "u").replace(/ß/g, "ss");
const tokenize = (s: string) => norm(s).split(/[^a-z0-9]+/).filter(Boolean);

const STOP = new Set(
  ("wie was wer wo wann wieso warum welche welcher welches bis muss musst mussen darf durfen kann konnen ich mein meine meinen mir mich wir uns das der die den dem des " +
    "ein eine einen einem einer und oder im in am an auf aus bei mit nach von vom zu zur zum fur uber auch nicht sein ist sind wird werden wurde hat haben es sie er ob als wenn " +
    "dass noch nur so machen gibt gilt soll sollen sollte dann dies diese").split(" ")
);

const SYN: Record<string, string[]> = {
  zug: ["bahn", "offentlich", "verkehrsmittel", "personenverkehr"],
  bahn: ["zug", "offentlich", "verkehrsmittel"],
  unterwegs: ["offentlich", "bahn", "mobile"],
  homeoffice: ["mobile", "remote"],
  kennwort: ["passwort"],
  vorfall: ["sicherheitsvorfall", "incident"],
  incident: ["sicherheitsvorfall", "vorfall"],
  gemeldet: ["melden", "meldung", "meldepflicht"],
  melden: ["meldung", "meldepflicht"],
  meldung: ["melden", "meldepflicht"],
  meldepflicht: ["melden", "meldung"],
};

const REWRITES: Record<string, string> = {
  [tokenize("Darf ich das auch im Zug machen?").join(" ")]:
    "Ist das Arbeiten mit vertraulichen Daten im öffentlichen Personenverkehr (Zug) laut Mobile-Work-Policy erlaubt?",
  [tokenize("Wie lang muss mein Passwort sein?").join(" ")]:
    "Welche Mindestlänge und welche Wechselintervalle gelten laut aktueller Passwort-Policy für Benutzerpasswörter?",
  [tokenize("Bis wann muss ein Sicherheitsvorfall nach NIS-2 gemeldet werden?").join(" ")]:
    "Welche Meldefristen gelten für erhebliche Sicherheitsvorfälle gegenüber dem BSI gemäß NIS-2 und Incident-Response-Runbook?",
  [tokenize("Darf ich private KI-Tools wie ChatGPT für Kundenangebote nutzen?").join(" ")]:
    "Ist die Nutzung nicht freigegebener generativer KI-Dienste zur Verarbeitung von Kundendaten laut Unternehmensrichtlinie erlaubt?",
};

const tokMatch = (a: string, b: string) => a === b || (a.length >= 7 && b.length >= 7 && a.slice(0, 7) === b.slice(0, 7));

function queryGroups(q: string): string[][] {
  const seen = new Set<string>();
  const groups: string[][] = [];
  for (const t of tokenize(q)) {
    if (t.length < 3 || STOP.has(t) || /^\d+$/.test(t) || seen.has(t)) continue;
    seen.add(t);
    groups.push([t, ...(SYN[t] ?? []).map(norm)]);
  }
  return groups;
}

function coverage(groups: string[][], c: LiveChunk): number {
  if (!groups.length) return 0;
  const toks = tokenize(`${c.docName} ${c.section} ${c.content}`);
  return groups.filter((grp) => grp.some((t) => toks.some((x) => tokMatch(t, x)))).length / groups.length;
}

const confidenceOf = (cov: number, seed: string) => {
  if (cov < 0.6) return Math.round((0.3 + 0.8 * cov) * 100) / 100;
  const jitter = (parseInt(sha256(seed).slice(0, 2), 16) % 5) / 100;
  return Math.round((0.82 + 0.16 * cov - jitter) * 100) / 100;
};

const NO_ANSWER = "Keine eindeutige Richtlinie gefunden — Halluzinationsschutz aktiv (Konfidenz < 85%).";
const LOCAL_ENGINE = "Local Fallback Engine (keyword)";
const ms = (t0: number) => Math.round((performance.now() - t0) * 10) / 10;

export function runQuery(token: string | null, question: string): Promise<R<{ result: QueryResult }>> {
  return transact((db) => {
    const tStart = performance.now();
    const q = typeof question === "string" ? question.trim() : "";
    if (q.length < 3 || q.length > 500) return fail(400, "BAD_QUESTION", "Die Frage muss zwischen 3 und 500 Zeichen lang sein.");
    const a = actorFor(db, token);

    // 1 — isolated rewrite (no history exists in this function by design)
    const groups = queryGroups(q);
    const rewrite =
      REWRITES[tokenize(q).join(" ")] ??
      `Welche freigegebene Richtlinie regelt: „${q}“?${groups.length ? ` [Suchbegriffe: ${groups.map((x) => x[0]).join(", ")}]` : ""}`;
    const tRewrite = ms(tStart);

    // 2 — RBAC pre-filter before ranking
    const t2 = performance.now();
    const scored = db.live_chunks.map((c) => ({ c, cov: coverage(groups, c) }));
    const accessible = scored.filter((s) => ROLE_RANK[s.c.minRole] <= ROLE_RANK[a.role]).sort((x, y) => y.cov - x.cov);
    const restricted = scored.filter((s) => ROLE_RANK[s.c.minRole] > ROLE_RANK[a.role]).sort((x, y) => y.cov - x.cov);
    const tRbac = ms(t2);

    // 3 — retrieval + threshold gate
    const t3 = performance.now();
    const best = accessible[0];
    const tRetrieval = ms(t3);
    const base = {
      original: q, rewrite, rbacFilter: `min_role <= ${a.role}`, searched: accessible.length, total: db.live_chunks.length, temperature: 0,
      engineMode: LOCAL_ENGINE, rewriteBy: "rules", topScore: Math.round((best?.cov ?? 0) * 100) / 100,
    };
    const lat = (generation: number, nli: number) => ({ rewrite: tRewrite, rbac: tRbac, retrieval: tRetrieval, generation, nli, total: ms(tStart) });

    if (best && best.cov >= 0.6) {
      // 4 — extractive, grounded-by-construction answer
      const t4 = performance.now();
      const top = accessible.filter((s) => s.cov >= 0.6 && s.cov >= best.cov - 0.15).slice(0, 2);
      const segments: AnswerSegment[] = [];
      const citations: Record<string, LiveChunk> = {};
      const scores: Record<string, number> = {};
      top.forEach((s, i) => {
        segments.push({ type: "text", text: `${i > 0 ? " " : ""}${s.c.content} ` }, { type: "cite", chunkId: s.c.id });
        citations[s.c.id] = s.c;
        scores[s.c.id] = Math.round(s.cov * 100) / 100;
      });
      const tGen = ms(t4);
      return { ok: true as const, result: { kind: "answer" as const, requiredRole: null, segments, citations, scores, trace: { ...base, nliVerified: true, confidence: confidenceOf(best.cov, q + best.c.id), answerBy: "rules", latencyMs: lat(tGen, 0) } } };
    }

    if (restricted[0] && restricted[0].cov >= 0.6) {
      const need = restricted[0].c.minRole;
      appendAudit(db, { actor: a.email, role: a.role, action: "RBAC_DENIED", target: restricted[0].c.docName, status: "DENIED", detail: `Retrieval erfordert ${need}` });
      return {
        ok: true as const,
        result: {
          kind: "denied" as const, requiredRole: need,
          segments: [{ type: "text" as const, text: `Zu dieser Frage existieren freigegebene Dokumente, die Ihre aktuelle Clearance (${a.role}) nicht einsehen darf.` }],
          citations: {}, scores: {}, trace: { ...base, nliVerified: false, confidence: 0, latencyMs: lat(0, 0) },
        },
      };
    }

    return {
      ok: true as const,
      result: {
        kind: "fallback" as const, requiredRole: null,
        segments: [{ type: "text" as const, text: NO_ANSWER }],
        citations: {}, scores: {}, trace: { ...base, nliVerified: false, confidence: Math.min(0.84, confidenceOf(best?.cov ?? 0, q)), latencyMs: lat(0, 0) },
      },
    };
  });
}

/* ───────────────────────── Tickets ───────────────────────── */

function newTicket(db: Db, t: Omit<Ticket, "id" | "createdAt" | "status" | "system" | "queue">): Ticket {
  const ticket: Ticket = { ...t, id: `CISO-${db.meta.nextTicket++}`, system: "Jira", queue: "CISO-Office", createdAt: nowIso(), status: "OPEN" };
  db.tickets.push(ticket);
  return ticket;
}

export function createTicket(token: string | null, question: string, reason: string): Promise<R<{ ticket: Ticket }>> {
  return transact((db) => {
    const q = typeof question === "string" ? question.trim().slice(0, 500) : "";
    if (q.length < 3) return fail(400, "BAD_QUESTION", "Frage fehlt.");
    const a = actorFor(db, token);
    const ticket = newTicket(db, {
      type: "KNOWLEDGE_GAP", title: `Richtlinien-Anfrage: ${q.slice(0, 80)}`, targetDoc: null, ref: null, createdBy: a.email,
      description: `Keine eindeutige Richtlinie gefunden (${reason === "RBAC" ? "RBAC-Sperre" : "Konfidenz < 85%"}). Ursprüngliche Frage: „${q}“`,
    });
    appendAudit(db, { actor: a.email, role: a.role, action: "TICKET_CREATED", target: ticket.id, status: "SUCCESS", detail: "Knowledge-Gap an CISO-Office" });
    return { ok: true as const, ticket };
  });
}

export function createSyncTicket(token: string | null, diffId: string, conflictId: string): Promise<R<{ ticket: Ticket }>> {
  return transact((db) => {
    const a = authorize(db, token, "L3", "create_sync_ticket", diffId);
    if (isFail(a)) return a;
    const diff = db.staged_diffs.find((d) => d.id === diffId && d.status === "pending");
    const conflict = diff?.conflicts.find((c) => c.id === conflictId);
    if (!diff || !conflict) return fail(404, "NOT_FOUND", "Diff oder Konflikt nicht gefunden.");
    if (conflict.status === "ticket") return fail(409, "ALREADY_ADDRESSED", "Für diesen Konflikt existiert bereits ein Sync-Ticket.");
    const ticket = newTicket(db, {
      type: "POLICY_SYNC", title: `Dokument anpassen: ${conflict.docName}`, targetDoc: conflict.docName, ref: diff.id, createdBy: a.email,
      description: `${diff.docName} (Staging) widerspricht ${conflict.docName} ${conflict.section}. Bitte das Dokument an die neue Regelung anpassen: „${conflict.quote.slice(0, 160)}“`,
    });
    conflict.status = "ticket";
    conflict.ticketId = ticket.id;
    appendAudit(db, { actor: a.email, role: a.role, action: "SYNC_TICKET_CREATED", target: conflict.docName, status: "SUCCESS", detail: `${ticket.id} für Konflikt mit ${diff.docName}` });
    return { ok: true as const, ticket };
  });
}

/* ───────────────────────── Pipeline A: staging, merge, reject ───────────────────────── */

const docFamily = (name: string) => name.replace(/_v\d+(\.[A-Za-z0-9]+)$/, "$1").replace(/_\d{4}(\.[A-Za-z0-9]+)$/, "$1").toLowerCase();

function bumpVersion(name: string): string {
  const m = name.match(/^(.*_v)(\d+)(\.[A-Za-z0-9]+)$/);
  if (m) return `${m[1]}${Number(m[2]) + 1}${m[3]}`;
  const ext = name.match(/(\.[A-Za-z0-9]+)$/)?.[1] ?? "";
  return `${ext ? name.slice(0, -ext.length) : name}_v2${ext}`;
}

const contentTokens = (s: string) => new Set(tokenize(s).filter((t) => t.length >= 3 && !STOP.has(t) && !/^\d+$/.test(t)).map((t) => (t.length >= 7 ? t.slice(0, 7) : t)));
const numbersOf = (s: string) => new Set(s.match(/\d+/g) ?? []);

// Simulated semantic contradiction check: same topic (token overlap) but different numeric values. Only chunks the actor may see.
function detectConflicts(db: Db, text: string, ignoreChunkIds: string[], ignoreDocs: Set<string>, ignoreFamily: string, maxRank: number): Conflict[] {
  const mine = contentTokens(text);
  const myNums = numbersOf(text);
  const out: Conflict[] = [];
  if (!myNums.size) return out;
  for (const c of db.live_chunks) {
    if (ROLE_RANK[c.minRole] > maxRank || ignoreChunkIds.includes(c.id) || ignoreDocs.has(c.docName) || docFamily(c.docName) === ignoreFamily) continue;
    const theirs = contentTokens(c.content);
    const overlap = [...mine].filter((t) => theirs.has(t)).length / Math.max(1, Math.min(mine.size, theirs.size));
    const theirNums = numbersOf(c.content);
    const differs = theirNums.size > 0 && ([...myNums].some((n) => !theirNums.has(n)) && [...theirNums].some((n) => !myNums.has(n)));
    if (overlap >= 0.34 && differs) {
      const vals = [...theirNums].filter((n) => !myNums.has(n)).join(", ");
      out.push({
        id: uid("cf"), chunkId: c.id, docName: c.docName, section: c.section, quote: c.content,
        message: `Semantischer Konflikt erkannt: Dieses Update widerspricht ${c.docName} (${c.section.split(" ")[0]}), wo weiterhin abweichende Werte (${vals}) gefordert werden.`,
        similarity: Math.min(0.99, Math.round((0.55 + 0.4 * overlap) * 100) / 100), severity: overlap >= 0.6 ? "high" : "medium", status: "open", ticketId: null, verifiedBy: "rules",
      });
    }
  }
  return out;
}

/* ── document chunker (mirrors backend/rag_engine.py → chunk_document) ── */

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

interface DocChunk {
  section: string;
  number: string;
  content: string;
}

const MAX_CHUNK = 600;
const HEAD_MD = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/;
const HEAD_PARA = /^\s*§\s*(\d+(?:\.\d+)*)\.?\s+(.+?)\s*$/;
const HEAD_NUM = /^\s*(\d+(?:\.\d+)*)\.?\s+([A-ZÄÖÜ][^.!?;:]{2,80})\s*$/;
const NUM_TITLE = /^§?\s*(\d+(?:\.\d+)*)\.?\s*[:–-]?\s*(.*)$/;
const BULLET = /^\s*(?:[-*•]|\d+[.)])\s+/;

const splitSentences = (t: string) => t.split(/(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9„"(])/).map((x) => x.trim()).filter(Boolean);

function packChunks(paragraphs: string[]): string[] {
  const pieces: string[] = [];
  for (const p of paragraphs) {
    if (p.length <= MAX_CHUNK) {
      pieces.push(p);
      continue;
    }
    let cur = "";
    for (let sent of splitSentences(p).length ? splitSentences(p) : [p]) {
      while (sent.length > MAX_CHUNK) {
        const cut = sent.lastIndexOf(" ", MAX_CHUNK) > 0 ? sent.lastIndexOf(" ", MAX_CHUNK) : MAX_CHUNK;
        pieces.push(sent.slice(0, cut).trim());
        sent = sent.slice(cut).trim();
      }
      if (cur && cur.length + 1 + sent.length > MAX_CHUNK) {
        pieces.push(cur);
        cur = sent;
      } else cur = `${cur} ${sent}`.trim();
    }
    if (cur) pieces.push(cur);
  }
  const chunks: string[] = [];
  let cur = "";
  for (const p of pieces) {
    if (cur && cur.length + 1 + p.length > MAX_CHUNK) {
      chunks.push(cur);
      cur = p;
    } else cur = `${cur} ${p}`.trim();
  }
  if (cur) chunks.push(cur);
  return chunks;
}

function bodyParagraphs(lines: string[]): string[] {
  const paras: string[] = [];
  let buf: string[] = [];
  const flush = () => {
    if (buf.length) paras.push(buf.join(" "));
    buf = [];
  };
  for (const ln of lines) {
    if (!ln.trim()) {
      flush();
      continue;
    }
    let item = ln.replace(BULLET, "").trim();
    if (BULLET.test(ln) && item && !".!?:;".includes(item.at(-1)!)) item += ".";
    buf.push(item);
  }
  flush();
  return paras.map((x) => x.replace(/\s+/g, " ").trim()).filter(Boolean);
}

export function chunkDocument(raw: string): { title: string | null; chunks: DocChunk[] } {
  const text = raw.replace(/\r\n?/g, "\n").replace(/\0/g, "");
  let title: string | null = null;
  const sections: { number: string; title: string; lines: string[] }[] = [];
  let cur: (typeof sections)[number] | null = null;
  const pre: string[] = [];
  const start = (rawTitle: string, number?: string) => {
    let t = rawTitle;
    let n = number;
    const m = NUM_TITLE.exec(t);
    if (n === undefined && m) {
      n = m[1];
      t = m[2];
    }
    cur = { number: n ?? String(sections.length + 1), title: t.replace(/^[\s#*:]+|[\s#*:]+$/g, "") || "Abschnitt", lines: [] };
    sections.push(cur);
  };
  for (const line of text.split("\n")) {
    const md = HEAD_MD.exec(line);
    if (md) {
      if (md[1].length === 1 && title === null && !sections.length) {
        title = md[2].trim();
        continue;
      }
      start(md[2].trim());
      continue;
    }
    const pm = HEAD_PARA.exec(line);
    if (pm) {
      start(pm[2], pm[1]);
      continue;
    }
    const nm = HEAD_NUM.exec(line);
    if (nm) {
      start(nm[2], nm[1]);
      continue;
    }
    (cur ? (cur as (typeof sections)[number]).lines : pre).push(line);
  }
  if (pre.reduce((n, x) => n + x.trim().length, 0) >= (sections.length ? 40 : 15)) sections.unshift({ number: "0", title: "Vorbemerkung", lines: pre });

  const chunks: DocChunk[] = [];
  for (const sec of sections) {
    const parts = packChunks(bodyParagraphs(sec.lines)).filter((p) => p.length >= 15);
    parts.forEach((content, i) => chunks.push({ section: `§${sec.number} ${sec.title}${i ? ` (Teil ${i + 1})` : ""}`, number: sec.number, content }));
  }
  return { title, chunks };
}

/* ── Pipeline A core: chunk → align to series → diff → conflicts ── */

interface StageInput {
  docName: string;
  title: string;
  text: string;
  minRole: Role;
  author?: string;
  replaces?: string | null;
}

export function stageDocument(token: string | null, p: StageInput): Promise<R<{ diff: StagedDiff; stats: StageStats }>> {
  return transact((db) => {
    const t0 = performance.now();
    const a = authorize(db, token, "L2", "stage_file", String(p.docName));
    if (isFail(a)) return a;
    if (!(p.minRole in ROLE_RANK)) return fail(400, "BAD_ROLE", "Ungültige Min-Role.");
    if (ROLE_RANK[p.minRole] > ROLE_RANK[a.role]) return fail(403, "FORBIDDEN", `Min-Role ${p.minRole} übersteigt Ihre Clearance (${a.role}).`);
    if (!/^[\w.\- ]{3,120}$/.test(p.docName)) return fail(400, "BAD_TARGET", "Dokumentname: 3–120 Zeichen (Buchstaben, Ziffern, . _ -).");
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

    const merged = new Map<string, Conflict>();
    for (const h of hunks)
      for (const c of detectConflicts(db, h.newContent, series.map((x) => x.id), seriesDocs, docFamily(p.docName), ROLE_RANK[a.role]))
        if (!merged.has(c.chunkId) || merged.get(c.chunkId)!.similarity < c.similarity) merged.set(c.chunkId, c);
    const conflicts = [...merged.values()].sort((x, y) => y.similarity - x.similarity).slice(0, 6);

    const replacesDoc = match.size ? [...match.values()].map((j) => series[j].docName).sort((x, y) => [...match.values()].filter((v) => series[v].docName === y).length - [...match.values()].filter((v) => series[v].docName === x).length)[0] : null;
    const diff: StagedDiff = {
      id: uid("diff"), title: (p.title || docTitle || p.docName).slice(0, 120), docName: p.docName, replacesDoc,
      author: (p.author || `${a.email} (${a.role})`).slice(0, 80), uploadedAt: nowIso(),
      summary: `${hunks.length} geänderte/neue Chunk(s), ${unchanged} unverändert, ${conflicts.length} Konflikt(e) · Engine: ${LOCAL_ENGINE}`,
      sha256: sha256(hunks.map((h) => h.newContent).join("\n")), targetCollection: COLLECTION, status: "pending",
      decidedAt: null, decidedBy: null, decisionNote: null, hunks, conflicts,
    };
    db.staged_diffs.push(diff);
    appendAudit(db, { actor: a.email, role: a.role, action: "POLICY_STAGED", target: p.docName, status: "SUCCESS", detail: `${hunks.length} Chunk(s), ${conflicts.length} Konflikt(e), SHA-256 ${diff.sha256.slice(0, 12)}…, Engine ${LOCAL_ENGINE}` });
    return {
      ok: true as const, diff,
      stats: { chunks: picked.length, changed: hunks.length, unchanged, conflicts: conflicts.length, engineMode: LOCAL_ENGINE, latencyMs: { total: Math.round(performance.now() - t0) } },
    };
  });
}

// Pasted-text variant: an existing target document ⇒ next version replaces it, otherwise a new document.
export async function stagePolicy(token: string | null, p: { title: string; targetDoc: string; newText: string; minRole: string }): Promise<R<{ diff: StagedDiff; stats: StageStats }>> {
  const target = String(p.targetDoc ?? "").trim();
  const title = String(p.title ?? "").trim();
  const text = String(p.newText ?? "").trim();
  if (title.length < 3 || title.length > 120) return fail(400, "BAD_TITLE", "Titel: 3–120 Zeichen.");
  if (!/^[\w.\- ]{3,120}$/.test(target)) return fail(400, "BAD_TARGET", "Zieldokument: 3–120 Zeichen (Buchstaben, Ziffern, . _ -).");
  if (text.length < 10 || text.length > 2000) return fail(400, "BAD_TEXT", "Regeltext: 10–2000 Zeichen.");
  const existing = await transact((db) => db.live_chunks.find((c) => c.docName.toLowerCase() === target.toLowerCase())?.docName ?? null);
  return stageDocument(token, {
    docName: existing ? bumpVersion(existing) : /\.[A-Za-z0-9]{1,5}$/.test(target) ? target : `${target}.md`,
    title, text, minRole: p.minRole as Role, replaces: existing,
  });
}

/* ── audit export (NIS-2 / ISO 27001 evidence) ── */

export function exportReport(token: string | null): Promise<R<{ report: AuditReport }>> {
  return transact((db) => {
    const a = authorize(db, token, "L2", "export_report", COLLECTION);
    if (isFail(a)) return a;
    appendAudit(db, { actor: a.email, role: a.role, action: "AUDIT_EXPORT", target: "NIS-2 Audit Report", status: "SUCCESS", detail: `${db.audit_logs.length} Einträge, ${db.live_chunks.length} aktive Policy-Chunks` });
    const chain = verifyChain(db.audit_logs);
    const activePolicies = [...db.live_chunks]
      .sort((x, y) => x.docName.localeCompare(y.docName) || x.section.localeCompare(y.section))
      .map((c) => ({ id: c.id, docName: c.docName, section: c.section, minRole: c.minRole, sha256: c.sha256, approvedAt: c.updatedAt, approvedBy: c.approvedBy ?? "unbekannt" }));
    const policySetHash = sha256(activePolicies.map((x) => x.sha256).sort().join("\n"));
    const headHash = db.audit_logs.at(-1)?.hash ?? GENESIS;
    const schema = "pocket-ciso/nis2-audit-report/v1";
    const generatedAt = nowIso();
    const reportHash = sha256([schema, generatedAt, COLLECTION, db.meta.liveVersion, policySetHash, headHash, db.audit_logs.length].join("|"));
    return {
      ok: true as const,
      report: {
        schema, generatedAt, generatedBy: { email: a.email, role: a.role }, collection: COLLECTION, liveVersion: db.meta.liveVersion,
        chain: { valid: chain.valid, entries: chain.entries, headHash, algorithm: "sha256(prevHash|id|timestamp|actor|role|action|target|status|detail)" },
        policySetHash, activePolicies, auditLogs: db.audit_logs, tickets: db.tickets, reportHash,
      },
    };
  });
}

export function approveDiff(token: string | null, diffId: string, override: boolean): Promise<R<{ merged: number; superseded: number; docName: string }>> {
  return transact((db) => {
    const a = authorize(db, token, "L3", "approve", diffId);
    if (isFail(a)) return a;
    const idx = db.staged_diffs.findIndex((d) => d.id === diffId && d.status === "pending");
    if (idx < 0) return fail(404, "NOT_FOUND", "Pending-Diff nicht gefunden.");
    const diff = db.staged_diffs[idx];
    const open = diff.conflicts.filter((c) => c.status === "open");
    if (open.length && override !== true) {
      appendAudit(db, { actor: a.email, role: a.role, action: "MERGE_BLOCKED", target: diff.docName, status: "DENIED", detail: `${open.length} ungelöste(r) Konflikt(e)` });
      return fail(409, "CONFLICT_UNRESOLVED", "Ungelöste Konflikte: Sync-Ticket erstellen oder Override bestätigen.");
    }

    let superseded = 0;
    const ts = nowIso();
    for (const h of diff.hunks) {
      if (h.oldChunkId) {
        const before = db.live_chunks.length;
        db.live_chunks = db.live_chunks.filter((c) => c.id !== h.oldChunkId);
        superseded += before - db.live_chunks.length;
      }
      db.live_chunks.push({ id: uid("chk"), docName: diff.docName, section: h.section, minRole: h.minRole, content: h.newContent, updatedAt: ts, sha256: sha256(`${diff.docName}|${h.section}|${h.newContent}`), approvedBy: a.email });
    }
    db.staged_diffs.splice(idx, 1);
    db.meta.liveVersion += 1;
    const resolution = open.length ? `Override (${open.length} Konflikt akzeptiert)` : diff.conflicts.length ? `Konflikte per Sync-Ticket adressiert (${diff.conflicts.map((c) => c.ticketId).join(", ")})` : "keine Konflikte";
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

// Demo helper (L3): wipes the DB back to the seed so the "v2 → v3" proof can be replayed. Resets the audit chain.
export function resetDemo(token: string | null): Promise<R<object>> {
  return transact((db) => {
    const a = authorize(db, token, "L3", "reset_demo", COLLECTION);
    if (isFail(a)) return a;
    const fresh = seedDb();
    fresh.meta.secret = db.meta.secret;
    appendAudit(fresh, { actor: a.email, role: a.role, action: "DEMO_RESET", target: COLLECTION, status: "INFO", detail: "Datenbank auf Seed-Zustand zurückgesetzt" });
    Object.assign(db, fresh);
    return { ok: true as const };
  });
}
