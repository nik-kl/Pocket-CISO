// SERVER ONLY. Snapshot for the UI, audit export (NIS-2 / ISO 27001 evidence) and the demo reset.
import { ROLE_RANK, type AuditReport, type Snapshot } from "../mock-store";
import { appendAudit, verifyAudit } from "./audit";
import { actorFor, authorize } from "./auth";
import { seedDb, transact } from "./store";
import { AUDIT_SNAPSHOT_LIMIT, COLLECTION, DEMO_MODE, GENESIS, R, fail, isFail, nowIso, sha256 } from "./types";

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
