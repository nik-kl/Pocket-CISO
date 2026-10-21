"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { CheckCircle2, ChevronDown, ChevronRight, Database, FileDown, Loader2, Lock, Printer, RotateCcw, Search, ShieldCheck, ShieldAlert, Ticket as TicketIcon, Webhook, X } from "lucide-react";
import { ROLE_RANK, buildTicketPayload, callApi, formatTime, shortHash, type AuditLog, type AuditReport, type Session, type Snapshot, type Ticket } from "@/lib/mock-store";
import { Badge, Button, Modal, inputCls, roleTone, type BadgeTone } from "./ui-primitives";

interface Props {
  session: Session;
  snap: Snapshot | null;
  refresh: () => Promise<void>;
  notify: (text: string, tone?: "ok" | "bad" | "info") => void;
}

const STATUS_TONE: Record<AuditLog["status"], BadgeTone> = { SUCCESS: "ok", FAILED: "bad", DENIED: "warn", INFO: "neutral" };

// Neutralise spreadsheet formula injection: cells starting with = + - @ are prefixed with an apostrophe.
const csvCell = (v: unknown) => {
  let t = String(v ?? "");
  if (/^[=+\-@\t\r]/.test(t)) t = `'${t}`;
  return `"${t.replace(/"/g, '""')}"`;
};
const csvRow = (cells: unknown[]) => cells.map(csvCell).join(",");

function reportToCsv(r: AuditReport): string {
  return [
    csvRow(["# POCKET CISO — NIS-2 AUDIT REPORT"]),
    csvRow(["schema", r.schema]), csvRow(["generatedAt", r.generatedAt]), csvRow(["generatedBy", `${r.generatedBy.email} (${r.generatedBy.role})`]),
    csvRow(["collection", r.collection]), csvRow(["liveVersion", r.liveVersion]), csvRow(["chainValid", r.chain.valid]), csvRow(["chainEntries", r.chain.entries]),
    csvRow(["chainHeadHash", r.chain.headHash]), csvRow(["chainAlgorithm", r.chain.algorithm]), csvRow(["policySetHash", r.policySetHash]), csvRow(["reportHash", r.reportHash]),
    "",
    csvRow(["# ACTIVE POLICIES"]),
    csvRow(["id", "docName", "section", "minRole", "sha256", "approvedAt", "approvedBy"]),
    ...r.activePolicies.map((p) => csvRow([p.id, p.docName, p.section, p.minRole, p.sha256, p.approvedAt, p.approvedBy])),
    "",
    csvRow(["# AUDIT LOG"]),
    csvRow(["id", "timestamp", "actor", "role", "action", "target", "status", "detail", "prevHash", "hash"]),
    ...r.auditLogs.map((a) => csvRow([a.id, a.timestamp, a.actor, a.role, a.action, a.target, a.status, a.detail, a.prevHash, a.hash])),
  ].join("\r\n");
}

function download(filename: string, content: string, mime: string) {
  const url = URL.createObjectURL(new Blob([content], { type: `${mime};charset=utf-8` }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const DEFAULT_RESOLUTION = "Dokument angepasst, Konflikt behoben.";

const th = "sticky top-0 z-10 border-b border-line bg-surface px-4 py-2.5 text-left text-[10px] font-semibold uppercase tracking-[0.1em] text-faint";
const td = "border-b border-line px-4 py-2.5 align-top";

// View 3: inspect the Live Vector DB (RBAC-filtered), the hash-chained audit trail and all tickets.
export default function KnowledgeAndAuditView({ session, snap, refresh, notify }: Props) {
  const [tab, setTab] = useState<"chunks" | "audit">("chunks");
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [resetOpen, setResetOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [exporting, setExporting] = useState<"json" | "csv" | "print" | null>(null);
  const [openTicket, setOpenTicket] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [resolving, setResolving] = useState<string | null>(null);
  const [attestation, setAttestation] = useState<AuditReport | null>(null);

  const privileged = ROLE_RANK[session.role] >= ROLE_RANK.L2;
  const chunks = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = [...(snap?.chunks ?? [])].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
    return q ? all.filter((c) => [c.docName, c.section, c.content, c.sha256, c.minRole].some((f) => f.toLowerCase().includes(q))) : all;
  }, [snap?.chunks, query]);
  const audit = useMemo(() => [...(snap?.audit ?? [])].reverse(), [snap?.audit]);
  const tickets = useMemo(() => [...(snap?.tickets ?? [])].reverse(), [snap?.tickets]);

  const reset = async () => {
    setBusy(true);
    const r = await callApi("reset_demo", {}, session.token);
    setBusy(false);
    setResetOpen(false);
    if (r.ok) notify("Demo-Datenbank zurückgesetzt.", "ok");
    else notify(r.error, "bad");
    await refresh();
  };

  const resolveTicket = async (t: Ticket) => {
    setResolving(t.id);
    const r = await callApi<{ ticket: Ticket; conflictsUpdated: number }>("resolve_ticket", { ticketId: t.id, resolutionNote: notes[t.id] ?? DEFAULT_RESOLUTION }, session.token);
    setResolving(null);
    if (r.ok) notify(`${t.id} erledigt${r.conflictsUpdated ? ` — ${r.conflictsUpdated} Konflikt(e) als adressiert markiert` : ""}.`, "ok");
    else notify(r.error, "bad");
    await refresh();
  };

  const exportReport = async (fmt: "json" | "csv" | "print") => {
    setExporting(fmt);
    const r = await callApi<{ report: AuditReport }>("export_report", {}, session.token);
    setExporting(null);
    if (!r.ok) return notify(r.error, "bad");
    if (fmt === "print") {
      setAttestation(r.report);
      await refresh();
      return;
    }
    const stamp = r.report.generatedAt.replace(/[:.]/g, "-");
    if (fmt === "json") download(`pocket-ciso-nis2-audit-${stamp}.json`, JSON.stringify(r.report, null, 2), "application/json");
    else download(`pocket-ciso-nis2-audit-${stamp}.csv`, `\uFEFF${reportToCsv(r.report)}`, "text/csv");
    notify(`NIS-2 Audit Report exportiert (SHA-256 ${r.report.reportHash.slice(0, 12)}…).`, "ok");
    await refresh();
  };

  const tabBtn = (id: "chunks" | "audit", label: string, count?: number) => (
    <button
      role="tab"
      aria-selected={tab === id}
      onClick={() => setTab(id)}
      className={`-mb-px flex items-center gap-2 border-b-2 px-1 pb-3 text-[13px] font-medium transition-colors ${tab === id ? "border-brand text-fg" : "border-transparent text-faint hover:text-fg"}`}
    >
      {label}
      {count !== undefined && <Badge>{count}</Badge>}
    </button>
  );

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="shrink-0 space-y-4 px-8 pt-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-lg font-semibold tracking-tight text-fg">
              <Database size={18} className="text-brand-fg" /> Knowledge DB &amp; Audit Log
            </h1>
            <p className="mt-0.5 text-xs text-faint">
              Collection <span className="font-mono text-muted">{snap?.collection ?? "policies_live_v1"}</span> · Live-Version v{snap?.liveVersion ?? "…"} · Sichtbar: {snap?.chunks.length ?? 0} von {snap?.totalChunks ?? 0} Chunks (RBAC {session.role})
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {privileged && (
              <div className="inline-flex items-center overflow-hidden rounded-lg border border-line-strong bg-canvas text-[13px] shadow-sm">
                <span className="flex items-center gap-1.5 px-3 py-2 font-medium text-fg">
                  <FileDown size={14} className="text-brand-fg" /> Export NIS-2 Audit Report
                </span>
                {(["json", "csv"] as const).map((fmt) => (
                  <button
                    key={fmt}
                    onClick={() => void exportReport(fmt)}
                    disabled={exporting !== null}
                    aria-label={`Export NIS-2 Audit Report (${fmt.toUpperCase()})`}
                    className="flex items-center gap-1 border-l border-line px-3 py-2 font-mono text-xs font-semibold text-muted transition-colors hover:bg-raised hover:text-fg disabled:opacity-50"
                  >
                    {exporting === fmt && <Loader2 size={12} className="animate-spin" />} {fmt.toUpperCase()}
                  </button>
                ))}
                <button
                  onClick={() => void exportReport("print")}
                  disabled={exporting !== null}
                  aria-label="NIS-2 Governance Attestation drucken oder als PDF speichern"
                  className="flex items-center gap-1.5 border-l border-line px-3 py-2 text-xs font-semibold text-muted transition-colors hover:bg-raised hover:text-fg disabled:opacity-50"
                >
                  {exporting === "print" ? <Loader2 size={12} className="animate-spin" /> : <Printer size={12} />} Print / PDF Summary
                </button>
              </div>
            )}
            {session.role === "L3" && (
              <Button variant="outline" onClick={() => setResetOpen(true)}>
                <RotateCcw size={13} /> Demo-Daten zurücksetzen
              </Button>
            )}
          </div>
        </div>
        <div role="tablist" className="flex gap-6 border-b border-line">
          {tabBtn("chunks", "Live Vector Chunks", snap?.chunks.length)}
          {tabBtn("audit", "Immutable Audit Trail & Tickets", privileged ? snap?.audit?.length : undefined)}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-8 py-6">
        {tab === "chunks" && (
          <div className="animate-fade-in space-y-4">
            <div className="relative max-w-md">
              <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint" />
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Dokument, Abschnitt, Inhalt oder Hash suchen …" aria-label="Chunks durchsuchen" className={`${inputCls} pl-9`} />
            </div>
            <div className="overflow-hidden rounded-xl border border-line shadow-card">
              <table className="w-full border-separate border-spacing-0 text-xs">
                <thead>
                  <tr>
                    <th className={th}>Document</th>
                    <th className={th}>Section</th>
                    <th className={th}>Min Role</th>
                    <th className={th}>SHA-256</th>
                    <th className={th}>Last approved</th>
                    <th className={th}>Approved by</th>
                  </tr>
                </thead>
                <tbody>
                  {chunks.length === 0 && (
                    <tr><td colSpan={6} className="px-4 py-10 text-center text-faint">Keine Chunks gefunden.</td></tr>
                  )}
                  {chunks.map((c) => (
                    <FragmentRow key={c.id} open={expanded === c.id} onToggle={() => setExpanded(expanded === c.id ? null : c.id)} content={c.content} id={c.id}>
                      <td className={`${td} font-mono text-fg`}>{c.docName}</td>
                      <td className={`${td} text-muted`}>{c.section}</td>
                      <td className={td}><Badge tone={roleTone(c.minRole)}>{c.minRole}</Badge></td>
                      <td className={`${td} font-mono text-faint`} title={c.sha256}>{shortHash(c.sha256)}</td>
                      <td className={`${td} text-muted`}>{formatTime(c.updatedAt)}</td>
                      <td className={`${td} text-muted`}>{c.approvedBy ?? "—"}</td>
                    </FragmentRow>
                  ))}
                </tbody>
              </table>
            </div>
            {session.role === "L1" && (
              <p className="flex items-center gap-1.5 text-[11px] text-faint">
                <Lock size={11} /> Chunks mit höherer Min-Role sind für L1 ausgeblendet ({(snap?.totalChunks ?? 0) - (snap?.chunks.length ?? 0)} verborgen).
              </p>
            )}
          </div>
        )}

        {tab === "audit" &&
          (!privileged ? (
            <div className="mx-auto max-w-sm space-y-3 py-20 text-center">
              <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl border border-line bg-surface"><Lock size={20} className="text-faint" /></div>
              <h2 className="text-base font-semibold text-fg">Audit Trail erfordert L2</h2>
              <p className="text-sm leading-6 text-muted">Audit Log und Tickets sind für Auditoren und Administratoren freigegeben. Wechseln Sie die RBAC-Identität (Passwort erforderlich).</p>
            </div>
          ) : (
            <div className="animate-fade-in space-y-8">
              <section className="space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h2 className="text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">Audit Trail (neueste zuerst)</h2>
                  {snap?.integrity && (
                    snap.integrity.valid ? (
                      <Badge tone="ok"><ShieldCheck size={11} /> Hash-Kette verifiziert · {snap.integrity.entries} Einträge</Badge>
                    ) : (
                      <Badge tone="bad"><ShieldAlert size={11} /> Manipulation erkannt bei {snap.integrity.brokenAt}</Badge>
                    )
                  )}
                </div>
                <div className="overflow-hidden rounded-xl border border-line shadow-card">
                  <table className="w-full border-separate border-spacing-0 text-xs">
                    <thead>
                      <tr>
                        <th className={th}>Zeitpunkt</th><th className={th}>Actor</th><th className={th}>Action</th><th className={th}>Target</th><th className={th}>Status</th><th className={th}>Hash</th>
                      </tr>
                    </thead>
                    <tbody>
                      {audit.map((a) => (
                        <tr key={a.id} className="hover:bg-raised/40">
                          <td className={`${td} whitespace-nowrap text-muted`}>{formatTime(a.timestamp)}</td>
                          <td className={td}>
                            <div className="text-fg">{a.actor}</div>
                            <Badge tone={roleTone(a.role)} className="mt-1">{a.role}</Badge>
                          </td>
                          <td className={td}>
                            <div className="font-mono font-medium text-fg">{a.action}</div>
                            {a.detail && <div className="mt-0.5 max-w-xs text-faint">{a.detail}</div>}
                          </td>
                          <td className={`${td} text-muted`}>{a.target}</td>
                          <td className={td}><Badge tone={STATUS_TONE[a.status]}>{a.status}</Badge></td>
                          <td className={`${td} font-mono text-faint`} title={`${a.id}\nprev: ${a.prevHash}\n${a.hash}`}>{shortHash(a.hash)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="space-y-3">
                <h2 className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">
                  <TicketIcon size={12} /> Tickets ({tickets.length})
                </h2>
                <div className="overflow-hidden rounded-xl border border-line shadow-card">
                  <table className="w-full border-separate border-spacing-0 text-xs">
                    <thead>
                      <tr><th className={th}>Ticket</th><th className={th}>Typ</th><th className={th}>Titel</th><th className={th}>Erstellt von</th><th className={th}>Zeitpunkt</th><th className={th}>Status</th></tr>
                    </thead>
                    <tbody>
                      {tickets.length === 0 && <tr><td colSpan={6} className="px-4 py-8 text-center text-faint">Noch keine Tickets.</td></tr>}
                      {tickets.map((t) => (
                        <Fragment key={t.id}>
                          <tr
                            onClick={() => setOpenTicket(openTicket === t.id ? null : t.id)}
                            aria-expanded={openTicket === t.id}
                            className="cursor-pointer hover:bg-raised/40"
                          >
                            <td className={`${td} font-mono font-medium text-fg`}>
                              <span className="flex items-center gap-1.5">
                                {openTicket === t.id ? <ChevronDown size={12} className="text-faint" /> : <ChevronRight size={12} className="text-faint" />}
                                {t.id}
                              </span>
                              <div className="pl-[18px] font-normal text-faint">{t.system} · {t.queue}</div>
                            </td>
                            <td className={td}><Badge tone={t.type === "POLICY_SYNC" ? "brand" : "warn"}>{t.type}</Badge></td>
                            <td className={td}>
                              <div className="text-fg">{t.title}</div>
                              <div className="mt-0.5 max-w-md text-faint">{t.description}</div>
                            </td>
                            <td className={`${td} text-muted`}>{t.createdBy}</td>
                            <td className={`${td} whitespace-nowrap text-muted`}>{formatTime(t.createdAt)}</td>
                            <td className={td}><Badge tone={t.status === "resolved" ? "ok" : "warn"}>{t.status === "resolved" ? "RESOLVED" : "OPEN"}</Badge></td>
                          </tr>
                          {openTicket === t.id && (
                            <tr>
                              <td colSpan={6} className="border-b border-line bg-surface px-4 py-4">
                                <TicketDetail
                                  ticket={t}
                                  note={notes[t.id] ?? DEFAULT_RESOLUTION}
                                  onNote={(v) => setNotes((n) => ({ ...n, [t.id]: v }))}
                                  busy={resolving === t.id}
                                  onResolve={() => void resolveTicket(t)}
                                />
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            </div>
          ))}
      </div>

      {attestation && <AttestationSheet report={attestation} onClose={() => setAttestation(null)} />}

      <Modal
        open={resetOpen}
        title="Demo-Daten zurücksetzen"
        onClose={() => setResetOpen(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setResetOpen(false)}>Abbrechen</Button>
            <Button variant="danger" onClick={reset} disabled={busy}>Zurücksetzen</Button>
          </>
        }
      >
        <p>Setzt Live-Chunks, Staging-Queue und Tickets auf den Seed-Zustand zurück und startet eine neue Audit-Hash-Kette. Nur für Demo-Zwecke — in Produktion ist der Audit Trail unveränderlich.</p>
      </Modal>
    </div>
  );
}

// Table row that expands to show the full chunk text.
function FragmentRow({ children, open, onToggle, content, id }: { children: React.ReactNode; open: boolean; onToggle: () => void; content: string; id: string }) {
  return (
    <>
      <tr onClick={onToggle} className="cursor-pointer hover:bg-raised/40" aria-expanded={open}>
        {children}
      </tr>
      {open && (
        <tr>
          <td colSpan={6} className="border-b border-line bg-surface px-4 py-3">
            <div className="mb-1 font-mono text-[10px] text-faint">chunk_id: {id}</div>
            <p className="max-w-3xl text-[13px] leading-6 text-fg">{content}</p>
          </td>
        </tr>
      )}
    </>
  );
}

// Expanded ticket: enterprise webhook payload (Jira / ServiceNow REST) + closed-loop resolve action.
function TicketDetail({ ticket: t, note, onNote, busy, onResolve }: { ticket: Ticket; note: string; onNote: (v: string) => void; busy: boolean; onResolve: () => void }) {
  const resolved = t.status === "resolved";
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
      <div className="min-w-0 space-y-2">
        <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">
          <Webhook size={12} /> Enterprise Webhook Payload
        </div>
        <pre className="max-h-72 overflow-auto rounded-lg border border-line bg-canvas p-3 font-mono text-[11px] leading-5 text-fg">{JSON.stringify(buildTicketPayload(t), null, 2)}</pre>
      </div>
      <div className="space-y-2">
        <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">Closed-Loop Remediation</div>
        {resolved ? (
          <div className="space-y-1 rounded-lg border border-ok/30 bg-ok/5 p-3 text-xs">
            <div className="flex items-center gap-1.5 font-medium text-ok-fg"><CheckCircle2 size={13} /> Als erledigt markiert</div>
            <div className="text-muted">{t.resolutionNote}</div>
            <div className="font-mono text-[11px] text-faint">{t.resolvedBy} · {t.resolvedAt ? formatTime(t.resolvedAt) : ""}</div>
          </div>
        ) : (
          <div className="space-y-2">
            <label htmlFor={`note-${t.id}`} className="block text-xs text-faint">Lösungsnotiz (wird als TICKET_RESOLVED im Audit Log festgehalten)</label>
            <textarea id={`note-${t.id}`} value={note} onChange={(e) => onNote(e.target.value)} rows={3} className={inputCls} />
            <Button variant="success" className="w-full" disabled={busy || note.trim().length < 3} onClick={onResolve}>
              {busy ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />} Als erledigt markieren (Resolve)
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

const PRINT_CSS = `
@page { size: A4; margin: 14mm; }
@media print {
  body > *:not(#nis2-print-root) { display: none !important; }
  html, body { background: #fff !important; height: auto !important; overflow: visible !important; }
  #nis2-print-root { position: static !important; overflow: visible !important; background: #fff !important; padding: 0 !important; }
  .nis2-keep { break-inside: avoid; }
}`;

const sheetTh = "border-b border-zinc-300 bg-zinc-100 px-2.5 py-1.5 text-left text-[10px] font-semibold uppercase tracking-wide text-zinc-600";
const sheetTd = "border-b border-zinc-200 px-2.5 py-1.5 align-top";

// Printable "NIS-2 Governance Attestation". Rendered into <body> (portal) so window.print() outputs only this sheet.
function AttestationSheet({ report, onClose }: { report: AuditReport; onClose: () => void }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);

  const approvals = report.auditLogs.filter((a) => a.action === "APPROVED_AND_MERGED").slice(-5).reverse();
  const valid = report.chain.valid;

  return createPortal(
    <div id="nis2-print-root" role="dialog" aria-modal="true" aria-label="NIS-2 Governance Attestation" className="fixed inset-0 z-[60] overflow-y-auto bg-black/60 p-4 backdrop-blur-sm">
      <style>{PRINT_CSS}</style>
      <div className="mx-auto max-w-4xl overflow-hidden rounded-xl bg-white text-zinc-900 shadow-pop print:max-w-none print:rounded-none print:shadow-none">
        <div className="flex items-center justify-between gap-3 border-b border-zinc-200 bg-zinc-50 px-6 py-3 print:hidden">
          <span className="text-sm font-semibold">Executive Audit Summary — Vorschau</span>
          <div className="flex items-center gap-2">
            <Button onClick={() => window.print()}><Printer size={14} /> Drucken / Als PDF speichern</Button>
            <button onClick={onClose} aria-label="Schließen" className="rounded p-1.5 text-zinc-500 hover:bg-zinc-200 hover:text-zinc-900"><X size={16} /></button>
          </div>
        </div>

        <div className="space-y-6 px-8 py-8 text-[13px] leading-6">
          <header className="flex items-start justify-between gap-6 border-b-2 border-zinc-900 pb-4">
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-zinc-500">Pocket CISO · On-Premise Compliance Middleware</div>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight">NIS-2 Governance Attestation</h1>
              <p className="text-zinc-600">Nachweis über Richtlinienstand und Integrität des Audit Trails (NIS-2 Art. 21 · ISO/IEC 27001 A.5)</p>
            </div>
            <dl className="shrink-0 space-y-0.5 text-right text-[11px] text-zinc-600">
              <div>Erstellt: <span className="font-mono text-zinc-900">{formatTime(report.generatedAt)}</span></div>
              <div>Von: <span className="font-mono text-zinc-900">{report.generatedBy.email} ({report.generatedBy.role})</span></div>
              <div>Collection: <span className="font-mono text-zinc-900">{report.collection} · v{report.liveVersion}</span></div>
            </dl>
          </header>

          <section className="nis2-keep grid gap-3 sm:grid-cols-3">
            <div className={`rounded-lg border p-3 ${valid ? "border-emerald-300 bg-emerald-50" : "border-rose-300 bg-rose-50"}`}>
              <div className="text-[10px] font-semibold uppercase tracking-wide text-zinc-600">Integrität Audit-Kette</div>
              <div className={`mt-1 flex items-center gap-1.5 text-sm font-semibold ${valid ? "text-emerald-800" : "text-rose-800"}`}>
                {valid ? <ShieldCheck size={15} /> : <ShieldAlert size={15} />} {valid ? "VERIFIZIERT" : "MANIPULATION ERKANNT"}
              </div>
              <div className="mt-0.5 text-[11px] text-zinc-600">{report.chain.entries} Einträge neu berechnet</div>
            </div>
            <div className="rounded-lg border border-zinc-200 p-3">
              <div className="text-[10px] font-semibold uppercase tracking-wide text-zinc-600">Aktive Richtlinien-Chunks</div>
              <div className="mt-1 text-sm font-semibold">{report.activePolicies.length}</div>
              <div className="mt-0.5 text-[11px] text-zinc-600">{new Set(report.activePolicies.map((p) => p.docName)).size} Dokumente</div>
            </div>
            <div className="rounded-lg border border-zinc-200 p-3">
              <div className="text-[10px] font-semibold uppercase tracking-wide text-zinc-600">Offene Tickets</div>
              <div className="mt-1 text-sm font-semibold">{report.tickets.filter((t) => t.status !== "resolved").length} von {report.tickets.length}</div>
              <div className="mt-0.5 text-[11px] text-zinc-600">Policy-Sync &amp; Knowledge-Gap</div>
            </div>
          </section>

          <section className="nis2-keep space-y-1.5">
            <h2 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-zinc-500">Kryptografische Kennzahlen</h2>
            <dl className="grid gap-x-4 gap-y-1 font-mono text-[10px] sm:grid-cols-[150px_1fr]">
              <dt className="text-zinc-500">policySetHash</dt><dd className="break-all">{report.policySetHash}</dd>
              <dt className="text-zinc-500">chain.headHash</dt><dd className="break-all">{report.chain.headHash}</dd>
              <dt className="text-zinc-500">reportHash</dt><dd className="break-all">{report.reportHash}</dd>
            </dl>
            <p className="text-[11px] text-zinc-500">policySetHash = SHA-256 über die sortierten Chunk-Hashes der aktiven Policy-Collection. Kette: {report.chain.algorithm}</p>
          </section>

          <section className="space-y-1.5">
            <h2 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-zinc-500">Aktive Richtlinien (SHA-256)</h2>
            <table className="w-full border-separate border-spacing-0 overflow-hidden rounded-md border border-zinc-300 text-[11px]">
              <thead><tr><th className={sheetTh}>Dokument</th><th className={sheetTh}>Abschnitt</th><th className={sheetTh}>Min.</th><th className={sheetTh}>SHA-256</th></tr></thead>
              <tbody>
                {report.activePolicies.map((p) => (
                  <tr key={p.id} className="nis2-keep">
                    <td className={`${sheetTd} font-mono`}>{p.docName}</td>
                    <td className={sheetTd}>{p.section}</td>
                    <td className={`${sheetTd} font-mono`}>{p.minRole}</td>
                    <td className={`${sheetTd} break-all font-mono text-[9px] text-zinc-600`}>{p.sha256}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="nis2-keep space-y-1.5">
            <h2 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-zinc-500">Letzte CISO-Freigaben</h2>
            <table className="w-full border-separate border-spacing-0 overflow-hidden rounded-md border border-zinc-300 text-[11px]">
              <thead><tr><th className={sheetTh}>Zeitpunkt</th><th className={sheetTh}>Dokument</th><th className={sheetTh}>Freigegeben durch</th><th className={sheetTh}>Konfliktbehandlung</th><th className={sheetTh}>Audit-Hash</th></tr></thead>
              <tbody>
                {approvals.length === 0 && <tr><td colSpan={5} className="px-2.5 py-4 text-center text-zinc-500">Noch keine Freigaben im Audit Trail.</td></tr>}
                {approvals.map((a) => (
                  <tr key={a.id}>
                    <td className={`${sheetTd} whitespace-nowrap`}>{formatTime(a.timestamp)}</td>
                    <td className={`${sheetTd} font-mono`}>{a.target}</td>
                    <td className={sheetTd}>{a.actor} <span className="font-mono">({a.role})</span></td>
                    <td className={`${sheetTd} text-zinc-600`}>{a.detail}</td>
                    <td className={`${sheetTd} font-mono text-[9px] text-zinc-600`} title={a.hash}>{shortHash(a.hash)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <footer className="nis2-keep grid gap-6 border-t border-zinc-300 pt-5 sm:grid-cols-2">
            {["Unterschrift CISO", "Datum / Ort"].map((label) => (
              <div key={label}>
                <div className="h-10 border-b border-zinc-400" />
                <div className="mt-1 text-[11px] text-zinc-500">{label}</div>
              </div>
            ))}
            <p className="text-[10px] leading-4 text-zinc-500 sm:col-span-2">
              Pocket CISO Assistenzsystem — Keine rechtsverbindliche Freigabe. Dieses Dokument wurde automatisiert aus der lokalen Policy-Datenbank erzeugt; die Hash-Werte können anhand des exportierten JSON-Audit-Reports unabhängig nachgerechnet werden.
            </p>
          </footer>
        </div>
      </div>
    </div>,
    document.body
  );
}
