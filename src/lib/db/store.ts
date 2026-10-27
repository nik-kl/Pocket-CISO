// SERVER ONLY. File-backed JSON DB (data/pocket-ciso-db.json): demo seed / empty production DB, migration, cross-process lock, transactions.
import { promises as fs } from "fs";
import path from "path";
import crypto from "crypto";
import { type DiffHunk, type LiveChunk, type Role, type StagedDiff } from "../mock-store";
import { anchorOf, appendAudit, verifyChain } from "./audit";
import { applyCredentialPolicy, configuredPasswordHash, ensureAuthSecret } from "./credentials";
import { COLLECTION, DB_PATH, DEMO_MODE, Db, GENESIS, Meta, ago, g, nowIso, sha256 } from "./types";

/* ───────────────────────── Seed ───────────────────────── */

export function chunk(id: string, docName: string, section: string, minRole: Role, content: string, hoursAgo: number): LiveChunk {
  return { id, docName, section, minRole, content, updatedAt: ago(hoursAgo), sha256: sha256(`${docName}|${section}|${content}`), approvedBy: "ciso@company.at" };
}

export function seedDb(): Db {
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

// Cross-process mutex (atomic mkdir) shared with the Python backend (engine/store.py → file_lock).
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
export function transact<T>(fn: (db: Db) => T): Promise<T> {
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
