// SERVER ONLY. Hash-chained audit log (v2 JSON hashing, legacy verification) with the HMAC head anchor.
import crypto from "crypto";
import { type AuditLog, type AuditStatus } from "../mock-store";
import { authSecret } from "./credentials";
import { Db, GENESIS, nowIso, sha256 } from "./types";

/* ───────────────────────── Audit trail (hash chain) ───────────────────────── */

// v2 hashes a JSON array (field boundaries are unambiguous even if a note contains "|"); entries without `v` keep the legacy "|" join.
const hashEntry = (e: AuditLog) => {
  const fields = [e.prevHash, e.id, e.timestamp, e.actor, e.role, e.action, e.target, e.status, e.detail];
  return sha256(e.v === 2 ? JSON.stringify(fields) : fields.join("|"));
};

export const anchorOf = (count: number, head: string) => crypto.createHmac("sha256", authSecret()).update(`audit-anchor|${count}|${head}`).digest("hex");

export function appendAudit(
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

export function verifyChain(logs: AuditLog[]) {
  let prev = GENESIS;
  for (const l of logs) {
    if (l.prevHash !== prev || l.hash !== hashEntry(l)) return { valid: false, entries: logs.length, brokenAt: l.id };
    prev = l.hash;
  }
  return { valid: true, entries: logs.length, brokenAt: null as string | null };
}

// Chain links + head anchor (a deleted tail or a rewritten chain without the secret fails the anchor).
export function verifyAudit(db: Db) {
  const chain = verifyChain(db.audit_logs);
  if (!chain.valid) return chain;
  const head = db.audit_logs.at(-1)?.hash ?? GENESIS;
  if (db.meta.auditAnchor !== anchorOf(db.audit_logs.length, head)) return { valid: false, entries: chain.entries, brokenAt: "ANCHOR" as string | null };
  return chain;
}
