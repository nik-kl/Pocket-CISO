// SERVER ONLY. CISO-office tickets: fallback tickets, sync tickets for conflicts, resolution.
import { type Ticket } from "../mock-store";
import { appendAudit } from "./audit";
import { actorFor, authorize } from "./auth";
import { transact } from "./store";
import { Db, R, fail, isFail, nowIso, sha256 } from "./types";

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
