// SERVER ONLY. File-backed JSON database (data/pocket-ciso-db.json), auto-seeded on first run.
// Zero native deps: fs/promises + path + crypto. Every public function is one atomic transaction
// and maps 1:1 to an /api/v1 action (→ future FastAPI endpoint).

import { promises as fs, readFileSync } from "fs";
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
  type SyncReport,
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
  liveVersion: number;
  nextTicket: number;
  nextAudit: number;
  seededAt: string;
  ingestedFiles: Record<string, string>; // watched_inbox filename → SHA-256 of the last ingested content
  auditAnchor?: string; // HMAC(secret, count|headHash): makes truncating the audit tail detectable without the secret
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
const AUDIT_SNAPSHOT_LIMIT = 200; // full log is available via export_report; the chain is always verified over all entries
const TOKEN_TTL_MS = 8 * 3600 * 1000;
const DB_PATH = path.join(process.env.POCKET_CISO_DATA_DIR ?? path.join(process.cwd(), "data"), "pocket-ciso-db.json");
const DATA_DIR = path.dirname(DB_PATH);
const AUTH_SECRET_PATH = path.join(DATA_DIR, ".auth-secret");
const IS_PRODUCTION = process.env.NODE_ENV === "production" || process.env.POCKET_CISO_ENV === "production";
const DEMO_MODE = !IS_PRODUCTION && (process.env.POCKET_CISO_DEMO === "1" || process.env.POCKET_CISO_DEMO_MODE === "1");
let authSecretCache: string | null = null;

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
    meta: { liveVersion: 1, nextTicket: 4712, nextAudit: 1, seededAt: nowIso(), ingestedFiles: {} },
    users: [
      { email: "employee@company.at", role: "L1", clearance: "Employee", requiresPassword: false, passwordHash: null },
      { email: "admin@company.at", role: "L2", clearance: "IT-Admin", requiresPassword: true, passwordHash: configuredPasswordHash("L2") },
      { email: "ciso@company.at", role: "L3", clearance: "CISO", requiresPassword: true, passwordHash: configuredPasswordHash("L3") },
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

function emptyDb(): Db {
  const db: Db = {
    meta: { liveVersion: 1, nextTicket: 4712, nextAudit: 1, seededAt: nowIso(), ingestedFiles: {} },
    users: [
      { email: "employee@company.at", role: "L1", clearance: "Employee", requiresPassword: false, passwordHash: null },
      { email: "admin@company.at", role: "L2", clearance: "IT-Admin", requiresPassword: true, passwordHash: configuredPasswordHash("L2") },
      { email: "ciso@company.at", role: "L3", clearance: "CISO", requiresPassword: true, passwordHash: configuredPasswordHash("L3") },
    ],
    live_chunks: [],
    staged_diffs: [],
    audit_logs: [],
    tickets: [],
  };
  appendAudit(db, { actor: "system@pocket-ciso", role: "SYSTEM", action: "DB_INITIALIZED", target: COLLECTION, status: "INFO", detail: "Leere Produktionsdatenbank initialisiert" });
  return db;
}

/* ───────────────────────── Persistence ───────────────────────── */

const g = globalThis as unknown as {
  __pcQueue?: Promise<unknown>;
  __pcFails?: Record<string, number[]>;
  __pcDenials?: Record<string, { at: number; n: number }>;
  __pcCred?: Partial<Record<"L2" | "L3", { salt: Buffer; hash: Buffer }>>;
};

// Older DB files: ticket status was "OPEN"/"CLOSED", meta.ingestedFiles did not exist.
function migrate(db: Db): Db {
  delete (db.meta as Meta & { secret?: string }).secret;
  db.meta.ingestedFiles ??= {};
  if (!db.meta.auditAnchor && verifyChain(db.audit_logs).valid) db.meta.auditAnchor = anchorOf(db.audit_logs.length, db.audit_logs.at(-1)?.hash ?? GENESIS); // legacy DB: trust on first use
  for (const t of db.tickets) {
    const legacy = t.status as string;
    if (legacy === "OPEN") t.status = "open";
    if (legacy === "CLOSED") t.status = "resolved";
  }
  return db;
}

const MIN_PASSWORD_LENGTH = 14;
const ENV_MANAGED = "env-managed"; // the DB never stores a password hash; credentials come from the environment

function configuredPassword(role: "L2" | "L3"): string {
  const envName = role === "L2" ? "POCKET_CISO_L2_PASSWORD" : "POCKET_CISO_L3_PASSWORD";
  const configured = process.env[envName]?.trim();
  if (configured) {
    if (!DEMO_MODE && configured.length < MIN_PASSWORD_LENGTH) throw new Error(`${envName} muss mindestens ${MIN_PASSWORD_LENGTH} Zeichen lang sein.`);
    return configured;
  }
  if (!DEMO_MODE) throw new Error(`${envName} muss im Nicht-Demo-Modus gesetzt sein.`);
  return role === "L2" ? "admin2026" : "ciso2026";
}

// Per-process random salt; the plaintext only lives in the environment, never in data/.
function credential(role: "L2" | "L3") {
  const cache = (g.__pcCred ??= {});
  if (!cache[role]) {
    const salt = crypto.randomBytes(16);
    cache[role] = { salt, hash: crypto.scryptSync(configuredPassword(role), salt, 64) };
  }
  return cache[role]!;
}

const configuredPasswordHash = (role: "L2" | "L3"): string => (configuredPassword(role), ENV_MANAGED);

async function ensureAuthSecret(): Promise<string> {
  if (authSecretCache) return authSecretCache;
  const configured = process.env.POCKET_CISO_SECRET?.trim();
  if (configured) {
    if (configured.length < 32) throw new Error("POCKET_CISO_SECRET muss mindestens 32 Zeichen lang sein.");
    authSecretCache = configured;
    return configured;
  }
  if (!DEMO_MODE) {
    throw new Error("POCKET_CISO_SECRET muss außerhalb des Demo-Modus gesetzt sein.");
  }
  try {
    const existing = (await fs.readFile(AUTH_SECRET_PATH, "utf8")).trim();
    if (existing.length >= 32) {
      authSecretCache = existing;
      return existing;
    }
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
  }
  const generated = crypto.randomBytes(32).toString("hex");
  await fs.mkdir(DATA_DIR, { recursive: true });
  await fs.writeFile(AUTH_SECRET_PATH, `${generated}\n`, { encoding: "utf8", mode: 0o600 });
  authSecretCache = generated;
  return generated;
}

function authSecret(): string {
  if (authSecretCache) return authSecretCache;
  const configured = process.env.POCKET_CISO_SECRET?.trim();
  if (configured && configured.length >= 32) return configured;
  throw new Error("Auth-Secret ist noch nicht initialisiert.");
}

function applyCredentialPolicy(db: Db): void {
  for (const role of ["L2", "L3"] as const) {
    configuredPassword(role); // throws on missing / too short non-demo passwords
    db.users.find((u) => u.role === role)!.passwordHash = ENV_MANAGED;
  }
}

async function load(): Promise<Db> {
  await ensureAuthSecret();
  try {
    const raw = await fs.readFile(DB_PATH, "utf8");
    const parsed = JSON.parse(raw) as Db;
    if (!parsed.meta || !Array.isArray(parsed.live_chunks)) throw new Error("Datenbank-Schema ungültig: " + DB_PATH);
    const beforeMigration = JSON.stringify(parsed);
    const db = migrate(parsed);
    applyCredentialPolicy(db);
    if (beforeMigration !== JSON.stringify(db)) await persist(db);
    return db;
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
    const db = DEMO_MODE ? seedDb() : emptyDb();
    await persist(db);
    return db;
  }
}

async function persist(db: Db): Promise<void> {
  await fs.mkdir(path.dirname(DB_PATH), { recursive: true, mode: 0o700 });
  const tmp = `${DB_PATH}.${process.pid}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(db, null, 2), { encoding: "utf8", mode: 0o600 }); // audit data: owner-only
  await fs.rename(tmp, DB_PATH);
}

// Cross-process mutex (atomic mkdir) shared with the Python backend (backend/rag_engine.py → file_lock).
// The lock directory holds an `owner` file ("pid:token"): release only removes a lock we own, and a stale lock is stolen by an
// atomic rename (one winner) after verifying that the renamed directory still carries the owner we judged stale.
const LOCK_STALE_MS = 60_000;

const pidAlive = (pid: number) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return (e as NodeJS.ErrnoException).code === "EPERM";
  }
};

async function ownerOf(lock: string): Promise<string | null> {
  return fs.readFile(path.join(lock, "owner"), "utf8").then((x) => x.trim(), () => null);
}

async function stealStaleLock(lock: string): Promise<void> {
  const st = await fs.stat(lock).catch(() => null);
  if (!st || Date.now() - st.mtimeMs <= LOCK_STALE_MS) return;
  const seen = await ownerOf(lock);
  const pid = Number(seen?.split(":")[0]);
  if (seen && Number.isInteger(pid) && pidAlive(pid) && Date.now() - st.mtimeMs <= 10 * LOCK_STALE_MS) return; // slow but alive
  const parked = `${lock}.stale-${crypto.randomBytes(4).toString("hex")}`;
  try {
    await fs.rename(lock, parked);
  } catch {
    return; // somebody else won the steal
  }
  if ((await ownerOf(parked)) === seen) await fs.rm(parked, { recursive: true, force: true });
  else await fs.rename(parked, lock).catch(() => fs.rm(parked, { recursive: true, force: true })); // we moved a fresh lock: put it back
}

async function acquireLock(): Promise<() => Promise<void>> {
  const dir = path.dirname(DB_PATH);
  const lock = path.join(dir, ".db.lock");
  await fs.mkdir(dir, { recursive: true });
  const deadline = Date.now() + 10_000;
  const token = `${process.pid}:${crypto.randomBytes(6).toString("hex")}`;
  for (;;) {
    try {
      await fs.mkdir(lock);
      await fs.writeFile(path.join(lock, "owner"), token).catch(() => undefined);
      return async () => {
        if ((await ownerOf(lock)) === token) await fs.rm(lock, { recursive: true, force: true });
      };
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
      await stealStaleLock(lock);
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

// v2 hashes a JSON array (field boundaries are unambiguous even if a note contains "|"); entries without `v` keep the legacy "|" join.
const hashEntry = (e: AuditLog) => {
  const fields = [e.prevHash, e.id, e.timestamp, e.actor, e.role, e.action, e.target, e.status, e.detail];
  return sha256(e.v === 2 ? JSON.stringify(fields) : fields.join("|"));
};

const anchorOf = (count: number, head: string) => crypto.createHmac("sha256", authSecret()).update(`audit-anchor|${count}|${head}`).digest("hex");

function appendAudit(
  db: Db,
  e: { actor: string; role: AuditLog["role"]; action: string; target: string; status: AuditStatus; detail?: string },
  timestamp?: string
): AuditLog {
  const entry: AuditLog = {
    id: `AUD-${String(db.meta.nextAudit++).padStart(4, "0")}`,
    timestamp: timestamp ?? nowIso(),
    actor: e.actor, role: e.role, action: e.action, target: e.target, status: e.status, detail: e.detail ?? "",
    prevHash: db.audit_logs.at(-1)?.hash ?? GENESIS, hash: "", v: 2,
  };
  entry.hash = hashEntry(entry);
  db.audit_logs.push(entry);
  db.meta.auditAnchor = anchorOf(db.audit_logs.length, entry.hash);
  return entry;
}

function verifyChain(logs: AuditLog[]) {
  let prev = GENESIS;
  for (const l of logs) {
    if (l.prevHash !== prev || l.hash !== hashEntry(l)) return { valid: false, entries: logs.length, brokenAt: l.id };
    prev = l.hash;
  }
  return { valid: true, entries: logs.length, brokenAt: null as string | null };
}

// Chain links + head anchor (a deleted tail or a rewritten chain without the secret fails the anchor).
function verifyAudit(db: Db) {
  const chain = verifyChain(db.audit_logs);
  if (!chain.valid) return chain;
  const head = db.audit_logs.at(-1)?.hash ?? GENESIS;
  if (db.meta.auditAnchor !== anchorOf(db.audit_logs.length, head)) return { valid: false, entries: chain.entries, brokenAt: "ANCHOR" as string | null };
  return chain;
}

/* ───────────────────────── Auth (HMAC session tokens) ───────────────────────── */

const mac = (secret: string, payload: string) => crypto.createHmac("sha256", secret).update(payload).digest("hex");

function signToken(secret: string, role: Role): string {
  const payload = `${role}.${Date.now() + TOKEN_TTL_MS}`;
  return `${payload}.${mac(secret, payload)}`;
}

function verifyToken(secret: string, token: string | null): Role | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [role, exp, sig] = parts;
  if ((role !== "L2" && role !== "L3") || !exp || !sig) return null;
  const given = Buffer.from(sig, "utf8");
  const expected = Buffer.from(mac(secret, `${role}.${exp}`), "utf8");
  if (given.length !== expected.length || !crypto.timingSafeEqual(given, expected)) return null;
  return Number(exp) > Date.now() ? role : null;
}

function actorFor(db: Db, token: string | null): Actor {
  const role = verifyToken(authSecret(), token) ?? "L1";
  return { email: db.users.find((u) => u.role === role)!.email, role };
}

// Denials are sampled: at most one audit entry per (actor, action, target-prefix) per minute; the next entry reports how many were dropped.
// This keeps anonymous callers from growing / rewriting the audit file at will while every distinct denial class stays visible.
const DENIAL_WINDOW_MS = 60_000;
function appendDenial(db: Db, e: { actor: string; role: AuditLog["role"]; action: string; target: string; detail: string }): void {
  const state = (g.__pcDenials ??= {});
  const key = `${e.actor}|${e.action}|${e.target.split(":")[0]}`;
  const now = Date.now();
  const prev = state[key];
  if (prev && now - prev.at < DENIAL_WINDOW_MS) {
    prev.n += 1;
    return;
  }
  state[key] = { at: now, n: 0 };
  const dropped = prev?.n ? ` (+${prev.n} gleichartige Ablehnungen unterdrückt)` : "";
  appendAudit(db, { ...e, status: "DENIED", detail: `${e.detail}${dropped}` });
}

// Returns the actor, or a Fail (and an audit entry) when the role is insufficient.
function authorize(db: Db, token: string | null, need: Role, action: string, target: string): Actor | Fail {
  const a = actorFor(db, token);
  if (ROLE_RANK[a.role] >= ROLE_RANK[need]) return a;
  appendDenial(db, { actor: a.email, role: a.role, action: "ACCESS_DENIED", target: `${action}:${target}`, detail: `Erfordert ${need}` });
  return fail(403, "FORBIDDEN", `Diese Aktion erfordert Clearance ${need}.`);
}

const LOGIN_WINDOW_MS = 15 * 60_000;

// Per-(client, role) limit with exponential backoff, plus a higher per-account ceiling so an attacker cannot trivially lock out L3.
function loginRetryAfterMs(client: string, role: string): number {
  const now = Date.now();
  const fails = (g.__pcFails ??= {});
  const check = (key: string, limit: number) => {
    const recent = (fails[key] ??= []).filter((t) => now - t < LOGIN_WINDOW_MS);
    fails[key] = recent;
    if (recent.length < limit) return 0;
    const backoff = Math.min(LOGIN_WINDOW_MS, 30_000 * 2 ** (recent.length - limit));
    return Math.max(0, recent[recent.length - 1] + backoff - now);
  };
  return Math.max(check(`ip:${client}|${role}`, client === "direct" ? 30 : 5), check(`acct:${role}`, 30)); // "direct" = no trusted proxy: one shared bucket, so only the account ceiling applies
}

export function authenticate(token: string | null, role: string, password: string, client = "local"): Promise<R<{ token: string; session: { role: Role; email: string; clearance: string } }>> {
  return transact((db) => {
    const requester = actorFor(db, token);
    const user = db.users.find((u) => u.role === role && u.requiresPassword);
    if (!user || (user.role !== "L2" && user.role !== "L3")) return fail(400, "BAD_ROLE", "Für diese Rolle ist keine Anmeldung möglich.");

    const wait = loginRetryAfterMs(client, user.role);
    if (wait > 0) return fail(429, "RATE_LIMITED", `Zu viele Fehlversuche. Bitte ${Math.ceil(wait / 1000)} s warten.`);

    const cred = credential(user.role);
    const given = crypto.scryptSync(typeof password === "string" ? password.slice(0, 256) : "", cred.salt, 64);
    if (!crypto.timingSafeEqual(given, cred.hash)) {
      const fails = g.__pcFails!;
      for (const key of [`ip:${client}|${user.role}`, `acct:${user.role}`]) (fails[key] ??= []).push(Date.now());
      appendAudit(db, { actor: requester.email, role: requester.role, action: "AUTH_ELEVATION_FAILED", target: `→ ${user.role} (${user.email})`, status: "FAILED", detail: "Ungültiges Passwort" });
      return fail(401, "INVALID_CREDENTIALS", "Falsches Passwort.");
    }
    g.__pcFails![`ip:${client}|${user.role}`] = [];
    appendAudit(db, { actor: user.email, role: user.role, action: "AUTH_ELEVATION_SUCCESS", target: `→ ${user.role} (${user.clearance})`, status: "SUCCESS", detail: `Sitzung ${TOKEN_TTL_MS / 3600000} h gültig` });
    return { ok: true as const, token: signToken(authSecret(), user.role), session: { role: user.role, email: user.email, clearance: user.clearance } };
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
    const visibleDiffs = priv
      ? db.staged_diffs
        .map((d) => {
          const hunks = d.hunks.filter((h) => ROLE_RANK[h.minRole] <= ROLE_RANK[a.role]);
          const conflicts = d.conflicts.filter((c) => {
            const target = db.live_chunks.find((chunk) => chunk.id === c.chunkId);
            return !target || ROLE_RANK[target.minRole] <= ROLE_RANK[a.role];
          });
          return { ...d, hunks, conflicts };
        })
        .filter((d) => d.hunks.length > 0)
      : null;
    return {
      session: { role: a.role, email: a.email, clearance: user.clearance },
      collection: COLLECTION,
      liveVersion: db.meta.liveVersion,
      pendingCount: db.staged_diffs.filter((d) => d.status === "pending").length,
      totalChunks: db.live_chunks.length,
      chunks: db.live_chunks.filter((c) => ROLE_RANK[c.minRole] <= ROLE_RANK[a.role]),
      diffs: visibleDiffs,
      audit: priv ? db.audit_logs.slice(-AUDIT_SNAPSHOT_LIMIT) : null,
      auditTotal: priv ? db.audit_logs.length : null,
      tickets: priv ? db.tickets : null,
      integrity: priv ? verifyAudit(db) : null,
    };
  });
}

export function checkRole(token: string | null, need: Role): Promise<R<{ authorized: true }>> {
  return transact((db) => {
    const a = actorFor(db, token);
    return ROLE_RANK[a.role] >= ROLE_RANK[need]
      ? { ok: true as const, authorized: true as const }
      : fail(403, "FORBIDDEN", `Diese Aktion erfordert Clearance ${need}.`);
  });
}

/* ───────────────────────── Pipeline B: stateless retrieval ───────────────────────── */

const norm = (s: string) =>
  s.toLowerCase().replace(/ä/g, "a").replace(/ö/g, "o").replace(/ü/g, "u").replace(/ß/g, "ss");
const tokenize = (s: string) => norm(s).split(/[^a-z0-9]+/).filter(Boolean);

const STOP = new Set(
  ("wie was wer wo wann wieso warum welche welcher welches bis muss musst mussen darf durfen kann konnen ich mein meine meinen mir mich wir uns das der die den dem des " +
    "ein eine einen einem einer und oder im in am an auf aus bei mit nach von vom zu zur zum fur uber auch nicht sein ist sind wird werden wurde hat haben es sie er ob als wenn " +
    "dass noch nur so machen gibt gilt soll sollen sollte dann dies diese eines einem einer neue neuen neues dafür dafuer laut " +
    "a an and are as at be been being but by can does for from have how i if in is it may my of on or our the their them these they this to us was we what who with").split(" ")
);

// General domain glossary shared with the Python engine (backend/glossary.json): German/English pairs and abbreviations.
// Each single-word member expands to the other single-word members of its group (no question-specific hints).
let glossaryCache: Map<string, string[]> | null = null;
function glossary(): Map<string, string[]> {
  if (glossaryCache) return glossaryCache;
  const map = new Map<string, string[]>();
  try {
    const raw = JSON.parse(readFileSync(path.join(process.cwd(), "backend", "glossary.json"), "utf8")) as { groups?: string[][] };
    for (const group of raw.groups ?? []) {
      const words = group.map((m) => tokenize(m)).filter((t) => t.length === 1).map((t) => t[0]);
      for (const w of words) map.set(w, [...new Set([...(map.get(w) ?? []), ...words.filter((x) => x !== w)])]);
    }
  } catch {
    // no glossary → plain keyword matching
  }
  glossaryCache = map;
  return map;
}

const tokMatch = (a: string, b: string) => a === b || (a.length >= 7 && b.length >= 7 && a.slice(0, 7) === b.slice(0, 7));

function queryGroups(q: string): string[][] {
  const seen = new Set<string>();
  const groups: string[][] = [];
  for (const t of tokenize(q)) {
    const key = t.length >= 7 ? t.slice(0, 7) : t;
    if (t.length < 3 || STOP.has(t) || /^\d+$/.test(t) || seen.has(key)) continue;
    seen.add(key);
    groups.push([t, ...(glossary().get(t) ?? [])]);
  }
  return groups;
}

function coverage(groups: string[][], c: LiveChunk): number {
  if (!groups.length) return 0;
  const toks = tokenize(`${c.docName} ${c.section} ${c.content}`);
  return groups.filter((grp) => grp.some((t) => toks.some((x) => tokMatch(t, x)))).length / groups.length;
}

const NO_ANSWER = "Keine belegbare Antwort im freigegebenen Bestand."; // same text as backend/rag_engine.py
const SEARCH_LABEL = "Fundstellen, keine KI-Antwort";
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
    const rewrite = `Welche freigegebene Richtlinie regelt: „${q}“?${groups.length ? ` [Suchbegriffe: ${groups.map((x) => x[0]).join(", ")}]` : ""}`;
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
    const coverageThreshold = groups.length >= 8 ? Math.max(0.12, Math.min(0.6, 2 / groups.length)) : 0.6;
    const tRetrieval = ms(t3);
    const base = {
      original: q, rewrite, rbacFilter: `min_role <= ${a.role}`, searched: accessible.length, total: db.live_chunks.length, temperature: 0,
      engineMode: LOCAL_ENGINE, rewriteBy: "rules", topScore: Math.round((best?.cov ?? 0) * 100) / 100,
    };
    const lat = (generation: number, nli: number) => ({ rewrite: tRewrite, rbac: tRbac, retrieval: tRetrieval, generation, nli, total: ms(tStart) });

    if (best && best.cov >= coverageThreshold) {
      // 4 — search hits only: this engine never composes an answer (the Python engine does, with a verifier)
      const t4 = performance.now();
      const top = accessible.filter((s) => s.cov >= coverageThreshold && s.cov >= best.cov - 0.15).slice(0, 3);
      const segments: AnswerSegment[] = [{ type: "text", text: `${SEARCH_LABEL}:` }];
      const citations: Record<string, LiveChunk> = {};
      const scores: Record<string, number> = {};
      top.forEach((s) => {
        segments.push({ type: "text", text: ` ${s.c.docName} ${s.c.section.split(" ")[0]}: ${s.c.content} ` }, { type: "cite", chunkId: s.c.id });
        citations[s.c.id] = s.c;
        scores[s.c.id] = Math.round(s.cov * 100) / 100;
      });
      const hits = top.map((s) => ({ chunkId: s.c.id, docName: s.c.docName, section: s.c.section, content: s.c.content }));
      const tGen = ms(t4);
      return { ok: true as const, result: { kind: "search" as const, requiredRole: null, segments, citations, scores, hits, trace: { ...base, nliVerified: false, verification: "none" as const, confidence: Math.round(best.cov * 100) / 100, retrievalScore: Math.round(best.cov * 100) / 100, lexicalSupport: Math.round(best.cov * 100) / 100, answerBy: "search", latencyMs: lat(tGen, 0) } } };
    }

    if (restricted[0] && restricted[0].cov >= coverageThreshold) {
      const need = restricted[0].c.minRole;
      appendDenial(db, { actor: a.email, role: a.role, action: "RBAC_DENIED", target: restricted[0].c.docName, detail: `Retrieval erfordert ${need}` });
      return {
        ok: true as const,
        result: {
          kind: "fallback" as const, requiredRole: null, // indistinguishable from "no match" (no existence oracle)
          segments: [{ type: "text" as const, text: NO_ANSWER }],
          citations: {}, scores: {}, trace: { ...base, topScore: 0, nliVerified: false, confidence: 0, latencyMs: lat(0, 0) },
        },
      };
    }

    return {
      ok: true as const,
      result: {
        kind: "fallback" as const, requiredRole: null,
        segments: [{ type: "text" as const, text: NO_ANSWER }],
        citations: {}, scores: {}, trace: { ...base, nliVerified: false, verification: "none" as const, confidence: Math.round((best?.cov ?? 0) * 100) / 100, retrievalScore: Math.round((best?.cov ?? 0) * 100) / 100, latencyMs: lat(0, 0) },
      },
    };
  });
}

/* ───────────────────────── Tickets ───────────────────────── */

const SLA_DAYS: Record<Ticket["type"], number> = { POLICY_SYNC: 5, KNOWLEDGE_GAP: 2 };

function newTicket(db: Db, t: Omit<Ticket, "id" | "createdAt" | "status" | "system" | "queue" | "sha256" | "slaDue">): Ticket {
  const id = `CISO-${db.meta.nextTicket++}`;
  const createdAt = nowIso();
  const ticket: Ticket = {
    ...t, id, system: "Jira", queue: "CISO-Office", createdAt, status: "open",
    sha256: sha256([id, t.type, t.title, t.description, t.targetDoc ?? "", createdAt].join("|")),
    slaDue: new Date(Date.now() + SLA_DAYS[t.type] * 86_400_000).toISOString(),
  };
  db.tickets.push(ticket);
  return ticket;
}

// Closed loop: an L2/L3 user confirms the remediation → ticket resolved, linked staging conflicts marked as addressed.
export function resolveTicket(token: string | null, ticketId: string, resolutionNote: string): Promise<R<{ ticket: Ticket; conflictsUpdated: number }>> {
  return transact((db) => {
    const a = authorize(db, token, "L2", "resolve_ticket", ticketId);
    if (isFail(a)) return a;
    const ticket = db.tickets.find((t) => t.id === ticketId);
    if (!ticket) return fail(404, "NOT_FOUND", "Ticket nicht gefunden.");
    if (ticket.status === "resolved") return fail(409, "ALREADY_RESOLVED", "Das Ticket ist bereits erledigt.");
    const note = String(resolutionNote ?? "").trim().slice(0, 500);
    if (note.length < 3) return fail(400, "BAD_NOTE", "Lösungsnotiz erforderlich (mind. 3 Zeichen).");

    ticket.status = "resolved";
    ticket.resolvedAt = nowIso();
    ticket.resolvedBy = a.email;
    ticket.resolutionNote = note;
    const linked = db.staged_diffs.flatMap((d) => d.conflicts).filter((c) => c.ticketId === ticket.id);
    for (const c of linked) {
      c.status = "ticket";
      c.ticketResolved = true;
    }
    appendAudit(db, { actor: a.email, role: a.role, action: "TICKET_RESOLVED", target: ticket.id, status: "SUCCESS", detail: `${note}${linked.length ? ` · ${linked.length} Konflikt(e) als erledigt markiert` : ""}` });
    return { ok: true as const, ticket, conflictsUpdated: linked.length };
  });
}

const MAX_OPEN_L1_TICKETS = 100; // L1 has no login by design, so open tickets from it are capped instead

export function createTicket(token: string | null, question: string, reason: string): Promise<R<{ ticket: Ticket }>> {
  return transact((db) => {
    const q = typeof question === "string" ? question.trim().slice(0, 500) : "";
    if (q.length < 3) return fail(400, "BAD_QUESTION", "Frage fehlt.");
    const a = actorFor(db, token);
    const duplicate = db.tickets.find((t) => t.type === "KNOWLEDGE_GAP" && t.status === "open" && t.createdBy === a.email && t.description.endsWith(`„${q}“`));
    if (duplicate) return { ok: true as const, ticket: duplicate };
    if (a.role === "L1" && db.tickets.filter((t) => t.status === "open" && t.createdBy === a.email).length >= MAX_OPEN_L1_TICKETS)
      return fail(429, "TICKET_LIMIT", "Zu viele offene Anfragen. Bitte das CISO-Office kontaktieren.");
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
const numbersOf = (s: string) => new Set((s.match(/\d+(?:[.,]\d+)?/g) ?? []).map((n) => n.replace(",", ".")));

// Mirrors UNIT_RE / quantities() / _rule_conflict() in backend/rag_engine.py: same unit + different value on the same topic.
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
// "2 Stunden", "250.000 EUR", "36 Stunden": KPI values in tables/boxes, not numbered headings (mirrors is_quantity_heading in rag_engine.py).
const QUANTITY_TITLE = /^(stunden?|minuten?|tage[n]?|werktage[n]?|wochen?|monate[n]?|jahre[n]?|sekunden?|prozent|zeichen|versuche[n]?|mal|eur|euro|mb|gb|tb)\b/i;
const isQuantityHeading = (number: string, title: string) => /^\d{1,3}(\.\d{3})+$/.test(number) || QUANTITY_TITLE.test(title.trim());
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

/* ── table awareness: every Markdown table row becomes a self-contained statement ("Spalte: Wert | …") ── */

const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

const splitCells = (row: string) =>
  row.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.replace(/\\\|/g, "|").replace(/\*\*|`/g, "").trim());

function tableStatements(rows: string[], label: string): string[] {
  const hasHeader = rows.length >= 2 && TABLE_SEP.test(rows[1]);
  const header = hasHeader ? splitCells(rows[0]) : [];
  const body = hasHeader ? rows.slice(2) : rows;
  const out: string[] = [];
  for (const row of body) {
    if (TABLE_SEP.test(row)) continue;
    const pairs: string[] = [];
    splitCells(row).forEach((cell, i) => {
      if (!cell) return;
      pairs.push(header[i] ? `${header[i]}: ${cell}` : cell);
    });
    if (pairs.length) out.push(`Tabelle [${label}]: ${pairs.join(" | ").replace(/[.;]+$/, "")}.`);
  }
  return out;
}

// Body lines of one section → paragraphs; table blocks are converted row by row, in document order.
function sectionParagraphs(lines: string[], label: string): string[] {
  const paras: string[] = [];
  let text: string[] = [];
  let table: string[] = [];
  const flushText = () => {
    paras.push(...bodyParagraphs(text));
    text = [];
  };
  const flushTable = () => {
    paras.push(...tableStatements(table, label));
    table = [];
  };
  for (const ln of lines) {
    if (TABLE_ROW.test(ln)) {
      flushText();
      table.push(ln);
      continue;
    }
    flushTable();
    text.push(ln);
  }
  flushText();
  flushTable();
  return paras;
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
    if (nm && !isQuantityHeading(nm[1], nm[2])) {
      start(nm[2], nm[1]);
      continue;
    }
    (cur ? (cur as (typeof sections)[number]).lines : pre).push(line);
  }
  if (pre.reduce((n, x) => n + x.trim().length, 0) >= (sections.length ? 40 : 15)) sections.unshift({ number: "0", title: "Vorbemerkung", lines: pre });

  const chunks: DocChunk[] = [];
  for (const sec of sections) {
    const parts = packChunks(sectionParagraphs(sec.lines, `§${sec.number} ${sec.title}`)).filter((p) => p.length >= 15);
    parts.forEach((content, i) => chunks.push({ section: `§${sec.number} ${sec.title}${i ? ` (Teil ${i + 1})` : ""}`, number: sec.number, content }));
  }
  return { title, chunks };
}

/* ── Pipeline A core: chunk → align to series → diff → conflicts ── */

// Unicode-aware (umlauts!) like Python's \w; names are NFC-normalised first because macOS hands out NFD file names.
// Same character rule as safe_doc_name in backend/rag_engine.py: letters, digits, space . _ - & , ( ) — everything else → "_".
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
function stageInTx(db: Db, a: Actor, p: StageInput): R<{ diff: StagedDiff; stats: StageStats }> {
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

/* ── Zero-touch connector: backend/watched_inbox/ → staging queue (mirrors rag_engine.py → sync_watched_inbox) ── */

const INBOX_DIR = process.env.WATCHED_INBOX_DIR ?? path.join(process.cwd(), "backend", "watched_inbox");
const INBOX_EXT = /\.(pdf|docx|md|txt)$/i;
const TEXT_EXT = /\.(md|txt)$/i;
const MAX_INBOX_BYTES = 10 * 1024 * 1024;
const CONNECTOR: Actor = { email: "auto-connector@watched_inbox", role: "L2" };
const CONNECTOR_AUTHOR = "Auto-Connector (watched_inbox)";

type IngestOutcome =
  | { kind: "staged"; file: string; diffId: string; conflicts: number }
  | { kind: "skipped"; file: string; reason: string }
  | { kind: "failed"; file: string; error: string };

async function listInbox(): Promise<string[]> {
  const entries = await fs.readdir(INBOX_DIR, { withFileTypes: true }).catch(() => []);
  return entries.filter((e) => e.isFile() && !e.name.startsWith(".") && INBOX_EXT.test(e.name)).map((e) => e.name).sort();
}

// One transaction per file: dedupe by hash / pending diff → stage → remember hash → audit.
function ingestInTx(db: Db, file: string, digest: string, text: string, trigger: string): IngestOutcome {
  const docName = safeDocName(file);
  if (db.meta.ingestedFiles[file] === digest) return { kind: "skipped", file, reason: "unverändert seit dem letzten Sync" };
  if (db.staged_diffs.some((d) => d.status === "pending" && d.docName === docName)) return { kind: "skipped", file, reason: "bereits in der Staging-Queue" };

  const out = stageInTx(db, CONNECTOR, { docName, title: "", text, minRole: "L2", author: CONNECTOR_AUTHOR, trustedAuthor: true });
  if (isFail(out) && out.code === "NO_CHANGES") {
    db.meta.ingestedFiles[file] = digest;
    return { kind: "skipped", file, reason: "identisch mit der Live-Version" };
  }
  if (isFail(out)) return { kind: "failed", file, error: out.error };

  db.meta.ingestedFiles[file] = digest;
  appendAudit(db, {
    actor: CONNECTOR.email, role: "SYSTEM", action: "AUTO_CONNECTOR_SYNC", target: docName, status: "SUCCESS",
    detail: `${file} → ${out.diff.id} (SHA-256 ${digest.slice(0, 12)}…, ${out.diff.hunks.length} Chunk(s), ${out.diff.conflicts.length} Konflikt(e)) · Trigger: ${trigger}`,
  });
  return { kind: "staged", file, diffId: out.diff.id, conflicts: out.diff.conflicts.length };
}

async function ingestInboxFile(file: string, trigger: string): Promise<IngestOutcome> {
  if (!TEXT_EXT.test(file)) return { kind: "failed", file, error: "PDF/DOCX-Parsing benötigt das Python-Backend." };
  const full = path.join(INBOX_DIR, file);
  if ((await fs.stat(full)).size > MAX_INBOX_BYTES) return { kind: "failed", file, error: "Datei größer als 10 MB." };
  const data = await fs.readFile(full);
  const digest = crypto.createHash("sha256").update(data).digest("hex");
  const text = data.toString("utf8").replace(/^\uFEFF/, "");
  return transact((db) => ingestInTx(db, file, digest, text, trigger));
}

export async function syncWatchedInbox(token: string | null): Promise<R<{ report: SyncReport }>> {
  const actor = await transact((db) => authorize(db, token, "L2", "sync_inbox", "watched_inbox"));
  if (isFail(actor)) return actor;

  const files = await listInbox();
  const report: SyncReport = { inbox: "backend/watched_inbox", scanned: files.length, staged: [], skipped: [], failed: [], engineMode: LOCAL_ENGINE };
  for (const file of files) {
    const out = await ingestInboxFile(file, `${actor.email} (${actor.role})`).catch((e): IngestOutcome => {
      console.error("[watched_inbox]", file, e);
      return { kind: "failed", file, error: "Datei konnte nicht verarbeitet werden." };
    });
    if (out.kind === "staged") report.staged.push({ file, diffId: out.diffId, conflicts: out.conflicts });
    if (out.kind === "skipped") report.skipped.push({ file, reason: out.reason });
    if (out.kind === "failed") report.failed.push({ file, error: out.error });
  }
  return { ok: true as const, report };
}

/* ── audit export (NIS-2 / ISO 27001 evidence) ── */

export function exportReport(token: string | null): Promise<R<{ report: AuditReport }>> {
  return transact((db) => {
    const a = authorize(db, token, "L2", "export_report", COLLECTION);
    if (isFail(a)) return a;
    appendAudit(db, { actor: a.email, role: a.role, action: "AUDIT_EXPORT", target: "NIS-2 Audit Report", status: "SUCCESS", detail: `${db.audit_logs.length} Einträge, ${db.live_chunks.length} aktive Policy-Chunks` });
    const chain = verifyAudit(db);
    const activePolicies = [...db.live_chunks]
      .sort((x, y) => x.docName.localeCompare(y.docName) || x.section.localeCompare(y.section))
      .map((c) => ({
        id: c.id, docName: c.docName, section: c.section, minRole: c.minRole,
        sha256: c.sha256, contentHash: sha256(`${c.docName}|${c.section}|${c.content}`),
        approvedAt: c.updatedAt, approvedBy: c.approvedBy ?? "unbekannt",
      }));
    const canonical = (value: unknown) => JSON.stringify(value);
    const policySetHash = sha256(canonical(activePolicies));
    const headHash = db.audit_logs.at(-1)?.hash ?? GENESIS;
    const schema = "pocket-ciso/nis2-audit-report/v1";
    const generatedAt = nowIso();
    const reportHash = sha256(canonical({
      schema, generatedAt, collection: COLLECTION, liveVersion: db.meta.liveVersion,
      chain, policySetHash, activePolicies, auditLogs: db.audit_logs, tickets: db.tickets,
    }));
    return {
      ok: true as const,
      report: {
        schema, generatedAt, generatedBy: { email: a.email, role: a.role }, collection: COLLECTION, liveVersion: db.meta.liveVersion,
        chain: { valid: chain.valid, entries: chain.entries, headHash, algorithm: "v2: sha256(JSON[prevHash,id,timestamp,actor,role,action,target,status,detail]) (Einträge ohne v: Legacy-\"|\"-Format) + HMAC-Anker über Anzahl|Head-Hash" },
        policySetHash, activePolicies, auditLogs: db.audit_logs, tickets: db.tickets, reportHash,
      },
    };
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

// Demo helper (L3): wipes the DB back to the seed so the "v2 → v3" proof can be replayed. Resets the audit chain.
export function resetDemo(token: string | null): Promise<R<object>> {
  return transact((db) => {
    if (!DEMO_MODE) {
      return fail(403, "DISABLED_IN_PRODUCTION", "Demo-Reset ist außerhalb eines explizit aktivierten Demo-Modus deaktiviert.");
    }
    const a = authorize(db, token, "L3", "reset_demo", COLLECTION);
    if (isFail(a)) return a;
    const fresh = seedDb();
    appendAudit(fresh, { actor: a.email, role: a.role, action: "DEMO_RESET", target: COLLECTION, status: "INFO", detail: "Datenbank auf Seed-Zustand zurückgesetzt" });
    Object.assign(db, fresh);
    return { ok: true as const };
  });
}
