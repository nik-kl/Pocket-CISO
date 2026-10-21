"use client";

import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, FileDiff, FileText, GitMerge, Loader2, Lock, Plus, RefreshCw, TicketPlus, UploadCloud, X } from "lucide-react";
import {
  ROLE_RANK, callApi, callApiForm, formatTime, shortHash,
  type Conflict, type DiffHunk, type Role, type Session, type Snapshot, type StageStats, type StagedDiff, type SyncReport, type Ticket,
} from "@/lib/mock-store";
import { Badge, Button, Modal, inputCls } from "./ui-primitives";

interface Props {
  session: Session;
  snap: Snapshot | null;
  refresh: () => Promise<void>;
  notify: (text: string, tone?: "ok" | "bad" | "info") => void;
}

// Toast text for a watched-inbox scan.
function syncMessage(r: SyncReport): string {
  const parts: string[] = [];
  if (r.staged.length) {
    const conflicts = r.staged.reduce((n, x) => n + x.conflicts, 0);
    parts.push(`${r.staged.length} Dokument${r.staged.length === 1 ? "" : "e"} automatisch gestaged: ${r.staged.map((x) => x.file).join(", ")} (${conflicts} Konflikt${conflicts === 1 ? "" : "e"})`);
  }
  if (r.failed.length) parts.push(`${r.failed.length} fehlgeschlagen — ${r.failed[0].file}: ${r.failed[0].error}`);
  return parts.length ? parts.join(" · ") : "Inbox synchron — keine neuen Dateiänderungen";
}

// Pipeline A UI: staging queue → semantic diff → mandatory conflict resolution → CISO sign-off (real merge via /api/v1).
export default function CisoDiffDashboard({ session, snap, refresh, notify }: Props) {
  const diffs = snap?.diffs ?? [];
  const [selectedId, setSelectedId] = useState("");
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  const [approveOpen, setApproveOpen] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [stageOpen, setStageOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  // Falls back to the first pending diff when the selection no longer exists (merged / not loaded yet).
  const selected = diffs.find((d) => d.id === selectedId) ?? diffs.find((d) => d.status === "pending") ?? diffs[0];

  if (ROLE_RANK[session.role] < ROLE_RANK.L2) {
    return (
      <div className="flex h-full items-center justify-center px-6">
        <div className="max-w-sm space-y-3 text-center">
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl border border-line bg-surface">
            <Lock size={20} className="text-faint" />
          </div>
          <h2 className="text-base font-semibold tracking-tight text-fg">Zugriff verweigert</h2>
          <p className="text-sm leading-6 text-muted">Die Policy-Freigabe erfordert Clearance L2. Wechseln Sie links die RBAC-Identität (Passwort erforderlich).</p>
        </div>
      </div>
    );
  }

  const canApprove = session.role === "L3";
  const pending = diffs.filter((d) => d.status === "pending");
  const rejected = diffs.filter((d) => d.status === "rejected");
  const openConflicts = selected?.conflicts.filter((c) => c.status === "open") ?? [];
  const override = !!(selected && overrides[selected.id]);
  const mergeBlocked = openConflicts.length > 0 && !override;

  const syncTicket = async (c: Conflict) => {
    if (!selected) return;
    setBusy(c.id);
    const r = await callApi<{ ticket: Ticket }>("create_sync_ticket", { diffId: selected.id, conflictId: c.id }, session.token);
    setBusy(null);
    if (r.ok) notify(`Sync-Ticket ${r.ticket.id} für ${c.docName} erstellt.`, "ok");
    else notify(r.error, "bad");
    await refresh();
  };

  const approve = async () => {
    if (!selected) return;
    setBusy("approve");
    const r = await callApi<{ merged: number; superseded: number; docName: string }>("approve", { diffId: selected.id, override }, session.token);
    setBusy(null);
    setApproveOpen(false);
    if (r.ok) notify(`${r.docName} gemerged: ${r.merged} Chunk(s) live, ${r.superseded} ersetzt.`, "ok");
    else notify(r.error, "bad");
    await refresh();
  };

  const syncInbox = async () => {
    setBusy("sync");
    const r = await callApi<{ report: SyncReport }>("sync_inbox", {}, session.token);
    setBusy(null);
    if (!r.ok) {
      notify(r.error, "bad");
      return;
    }
    const tone = r.report.failed.length ? "bad" : r.report.staged.length ? "ok" : "info";
    notify(syncMessage(r.report), tone);
    await refresh(); // select only after the new diff is part of the snapshot
    if (r.report.staged[0]) setSelectedId(r.report.staged[0].diffId);
  };

  const reject = async () => {
    if (!selected) return;
    setBusy("reject");
    const r = await callApi("reject", { diffId: selected.id, reason: reason.trim() }, session.token);
    setBusy(null);
    if (r.ok) {
      setRejectOpen(false);
      setReason("");
      notify(`${selected.docName} zur Überarbeitung zurückgewiesen.`, "info");
    } else notify(r.error, "bad");
    await refresh();
  };

  return (
    <div className="flex h-full overflow-hidden">
      <aside className="w-80 shrink-0 space-y-5 overflow-y-auto border-r border-line bg-surface/60 p-4">
        <div className="space-y-2">
          <div className="flex items-center justify-between px-1">
            <h2 className="text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">Staging Vector DB</h2>
            <Badge tone="warn">{pending.length} ausstehend</Badge>
          </div>
          <Button variant="outline" className="w-full !justify-start" onClick={() => setStageOpen(true)}>
            <Plus size={14} /> Stage Policy Update
          </Button>
          <Button
            variant="outline"
            className="w-full !justify-start"
            onClick={() => void syncInbox()}
            disabled={busy === "sync"}
            title="Scannt backend/watched_inbox/ auf neue/geänderte Richtlinien (SharePoint/SMB Connector)"
          >
            <RefreshCw size={14} className={busy === "sync" ? "animate-spin" : ""} /> Sync Inbox
            <span className="ml-auto font-mono text-[10px] font-normal text-faint">watched_inbox/</span>
          </Button>
          {pending.length === 0 && <div className="rounded-lg border border-dashed border-line-strong p-4 text-center text-xs text-faint">Keine ausstehenden Policy-Diffs.</div>}
          {pending.map((d) => (
            <QueueItem key={d.id} diff={d} active={d.id === selected?.id} onClick={() => setSelectedId(d.id)} />
          ))}
        </div>
        {rejected.length > 0 && (
          <div className="space-y-2">
            <h2 className="px-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">Abgelehnt</h2>
            {rejected.map((d) => (
              <QueueItem key={d.id} diff={d} active={d.id === selected?.id} onClick={() => setSelectedId(d.id)} />
            ))}
          </div>
        )}
      </aside>

      {selected ? (
        <section className="flex h-full min-w-0 flex-1 flex-col overflow-hidden">
          <div className="flex-1 overflow-y-auto">
            <div className="animate-fade-in max-w-5xl space-y-6 p-8" key={selected.id}>
              <div className="overflow-hidden rounded-xl border border-line bg-surface shadow-card">
                <div className="flex flex-wrap items-center gap-2.5 border-b border-line px-5 py-4">
                  <FileDiff size={16} className="text-brand-fg" />
                  <h2 className="font-mono text-sm font-semibold text-fg">{selected.docName}</h2>
                  <StatusBadge status={selected.status} />
                </div>
                <p className="px-5 pt-4 text-sm leading-6 text-muted">{selected.summary}</p>
                <dl className="grid gap-x-8 gap-y-3 px-5 py-4 text-xs sm:grid-cols-2">
                  <Meta label="Source file" value={selected.docName} mono />
                  <Meta label="Replaces version" value={selected.replacesDoc ?? "— (neues Dokument)"} mono />
                  <Meta label="Author" value={selected.author} />
                  <Meta label="Uploaded" value={formatTime(selected.uploadedAt)} />
                  <Meta label="SHA-256" value={shortHash(selected.sha256)} mono />
                  <Meta label="Target collection" value={selected.targetCollection} mono />
                </dl>
              </div>

              {selected.conflicts.map((c) => (
                <ConflictCallout
                  key={c.id}
                  conflict={c}
                  stagedRule={selected.hunks[0]?.newContent ?? ""}
                  stagedFile={selected.docName}
                  canAct={canApprove && selected.status === "pending"}
                  busy={busy === c.id}
                  onTicket={() => void syncTicket(c)}
                />
              ))}

              <div className="space-y-4">
                <h3 className="text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">
                  {selected.hunks.length} Chunk{selected.hunks.length > 1 ? "s" : ""} Modified
                </h3>
                {selected.hunks.map((h, i) => (
                  <DiffBlock key={h.id} hunk={h} docName={selected.docName} index={i} />
                ))}
              </div>
            </div>
          </div>

          <footer className="shrink-0 space-y-3 border-t border-line bg-surface/90 px-8 py-4 backdrop-blur">
            {selected.status === "pending" ? (
              <>
                {canApprove && openConflicts.length > 0 && (
                  <label className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-warn/30 bg-warn/5 px-3 py-2.5 text-[13px] text-fg">
                    <input
                      type="checkbox"
                      checked={override}
                      onChange={(e) => setOverrides((o) => ({ ...o, [selected.id]: e.target.checked }))}
                      className="mt-0.5 h-4 w-4 accent-[var(--brand)]"
                    />
                    <span>
                      Override: Konflikt bewusst akzeptieren und Merge erzwingen
                      <span className="block text-[11px] text-faint">Wird als APPROVED_AND_MERGED mit Override-Vermerk im Audit Log festgehalten.</span>
                    </span>
                  </label>
                )}
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="text-xs">
                    {!canApprove ? (
                      <span className="inline-flex items-center gap-1.5 text-warn-fg"><Lock size={12} /> Read-only — nur L3 (CISO) darf freigeben oder ablehnen.</span>
                    ) : openConflicts.length ? (
                      <span className="inline-flex items-center gap-1.5 text-warn-fg">
                        <AlertTriangle size={12} /> {openConflicts.length} ungelöster Konflikt — Sync-Ticket erstellen oder Override bestätigen.
                      </span>
                    ) : selected.conflicts.length ? (
                      <span className="inline-flex items-center gap-1.5 text-ok-fg"><Check size={12} /> Alle Konflikte per Sync-Ticket adressiert.</span>
                    ) : (
                      <span className="inline-flex items-center gap-1.5 text-ok-fg"><Check size={12} /> Keine Konflikte erkannt.</span>
                    )}
                  </div>
                  <div className="flex gap-2">
                    <Button variant="outline" disabled={!canApprove} onClick={() => setRejectOpen(true)}>
                      <X size={14} /> Reject / Request Changes
                    </Button>
                    <Button variant="success" disabled={!canApprove || mergeBlocked} onClick={() => setApproveOpen(true)}>
                      <GitMerge size={14} /> Approve &amp; Merge to Production DB
                    </Button>
                  </div>
                </div>
              </>
            ) : (
              <div className="flex items-center gap-2 text-sm text-bad-fg">
                <X size={15} /> Zur Überarbeitung zurückgewiesen
                <span className="font-mono text-xs text-faint">· {selected.decidedAt ? formatTime(selected.decidedAt) : ""}</span>
                {selected.decisionNote && <span className="text-xs text-muted">— {selected.decisionNote}</span>}
              </div>
            )}
          </footer>
        </section>
      ) : (
        <div className="flex flex-1 items-center justify-center text-sm text-faint">Die Staging-Queue ist leer.</div>
      )}

      <Modal
        open={approveOpen}
        title="Merge to Production DB bestätigen"
        onClose={() => setApproveOpen(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setApproveOpen(false)}>Abbrechen</Button>
            <Button variant="success" onClick={approve} disabled={busy === "approve"}>
              {busy === "approve" ? <Loader2 size={14} className="animate-spin" /> : <GitMerge size={14} />} {openConflicts.length ? "Trotz Konflikt mergen" : "Mergen"}
            </Button>
          </>
        }
      >
        <p>
          <span className="font-mono text-fg">{selected?.docName}</span> wird aus dem Staging in <span className="font-mono text-fg">{selected?.targetCollection}</span> übernommen.
          Der Chat antwortet danach auf Basis der neuen Version; ersetzte Chunks werden entfernt.
        </p>
        {openConflicts.length > 0 && (
          <p className="flex gap-2 rounded-lg border border-warn/30 bg-warn/5 p-3 text-warn-fg">
            <AlertTriangle size={16} className="mt-0.5 shrink-0" />
            Override aktiv: {openConflicts.map((c) => c.docName).join(", ")} bleibt widersprüchlich.
          </p>
        )}
      </Modal>

      <Modal
        open={rejectOpen}
        title="Ablehnen / Überarbeitung anfordern"
        onClose={() => setRejectOpen(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setRejectOpen(false)}>Abbrechen</Button>
            <Button variant="danger" disabled={reason.trim().length < 3 || busy === "reject"} onClick={reject}>
              <X size={14} /> Ablehnen
            </Button>
          </>
        }
      >
        <label className="block text-xs text-faint" htmlFor="reject-reason">Begründung (wird im Audit Log festgehalten)</label>
        <textarea id="reject-reason" value={reason} onChange={(e) => setReason(e.target.value)} rows={4} className={inputCls} placeholder="z. B. Onboarding_IT_Handbuch.pdf zuerst anpassen." />
      </Modal>

      <StageModal
        open={stageOpen}
        role={session.role}
        token={session.token}
        docs={[...new Set((snap?.chunks ?? []).map((c) => c.docName))]}
        onClose={() => setStageOpen(false)}
        onStaged={async (diff, stats) => {
          notify(`${diff.docName} gestaged: ${stats.changed} Chunk(s), ${stats.conflicts} Konflikt${stats.conflicts === 1 ? "" : "e"} · ${stats.engineMode}`, stats.conflicts ? "info" : "ok");
          await refresh(); // select only after the new diff is part of the snapshot
          setSelectedId(diff.id);
        }}
      />
    </div>
  );
}

const SCAN_STEPS = [
  "Parsing & semantisches Chunking",
  "Embeddings berechnen",
  "Diff gegen Live-Chunks (Red/Green)",
  "Cross-Document Conflict Scanner (Temp 0.0)",
];
const ACCEPT = ".pdf,.docx,.md,.txt";
const MAX_BYTES = 10 * 1024 * 1024;

function StageModal({
  open, role, token, docs, onClose, onStaged,
}: {
  open: boolean;
  role: Role;
  token: string | null;
  docs: string[];
  onClose: () => void;
  onStaged: (diff: StagedDiff, stats: StageStats) => Promise<void>;
}) {
  const [tab, setTab] = useState<"file" | "text">("file");
  const [file, setFile] = useState<File | null>(null);
  const [drag, setDrag] = useState(false);
  const [title, setTitle] = useState("");
  const [targetDoc, setTargetDoc] = useState("");
  const [newText, setNewText] = useState("");
  const [replaces, setReplaces] = useState("");
  const [minRole, setMinRole] = useState<Role>("L1");
  const [phase, setPhase] = useState<"form" | "scanning" | "done">("form");
  const [step, setStep] = useState(0);
  const [result, setResult] = useState<{ diff: StagedDiff; stats: StageStats; served?: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const picker = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setTab("file"); setFile(null); setTitle(""); setTargetDoc(""); setNewText(""); setReplaces(""); setMinRole("L1");
      setPhase("form"); setStep(0); setResult(null); setError(null);
    }
  }, [open]);

  // Animated scanner: advances while the request is in flight (the last step stays active until the response arrives).
  useEffect(() => {
    if (phase !== "scanning") return;
    const t = setInterval(() => setStep((x) => Math.min(x + 1, SCAN_STEPS.length - 1)), 900);
    return () => clearInterval(t);
  }, [phase]);

  const pick = (f: File | undefined | null) => {
    if (!f) return;
    if (!/\.(pdf|docx|md|txt)$/i.test(f.name)) return setError("Nur .pdf, .docx, .md und .txt werden unterstützt.");
    if (f.size > MAX_BYTES) return setError("Datei größer als 10 MB.");
    setError(null);
    setFile(f);
  };

  const canSubmit = tab === "file" ? !!file : title.trim().length >= 3 && targetDoc.trim().length >= 3 && newText.trim().length >= 10;

  const submit = async () => {
    setError(null);
    setPhase("scanning");
    setStep(0);
    const r =
      tab === "file"
        ? await (() => {
            const fd = new FormData();
            fd.set("file", file!);
            fd.set("minRole", minRole);
            if (replaces.trim()) fd.set("replaces", replaces.trim());
            return callApiForm<{ diff: StagedDiff; stats: StageStats; served?: string }>("stage_file", fd, token);
          })()
        : await callApi<{ diff: StagedDiff; stats: StageStats; served?: string }>("stage_policy", { title, targetDoc, newText, minRole }, token);
    if (!r.ok) {
      setPhase("form");
      setError(r.error);
      return;
    }
    setStep(SCAN_STEPS.length);
    setResult(r);
    setPhase("done");
  };

  const finish = async () => {
    if (result) await onStaged(result.diff, result.stats);
    onClose();
  };

  return (
    <Modal
      open={open}
      wide
      title="Stage Policy Update"
      onClose={phase === "scanning" ? () => undefined : onClose}
      footer={
        phase === "done" ? (
          <Button onClick={finish}><FileDiff size={14} /> Diff öffnen</Button>
        ) : (
          <>
            <Button variant="ghost" onClick={onClose} disabled={phase === "scanning"}>Abbrechen</Button>
            <Button onClick={submit} disabled={!canSubmit || phase === "scanning"}>
              {phase === "scanning" ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />} In Staging aufnehmen
            </Button>
          </>
        )
      }
    >
      {phase === "form" ? (
        <div className="space-y-3">
          <div role="tablist" aria-label="Quelle" className="inline-flex rounded-lg border border-line bg-surface p-0.5">
            {([["file", "Datei hochladen"], ["text", "Text einfügen"]] as const).map(([id, label]) => (
              <button key={id} role="tab" aria-selected={tab === id} onClick={() => { setTab(id); setError(null); }}
                className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${tab === id ? "bg-canvas text-fg shadow-sm ring-1 ring-line" : "text-faint hover:text-fg"}`}>
                {label}
              </button>
            ))}
          </div>

          {tab === "file" ? (
            <>
              <button
                type="button"
                onClick={() => picker.current?.click()}
                onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
                onDragLeave={() => setDrag(false)}
                onDrop={(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer.files[0]); }}
                aria-label="Datei auswählen oder hier ablegen"
                className={`flex w-full flex-col items-center gap-1.5 rounded-xl border-2 border-dashed px-4 py-7 text-center transition-colors ${drag ? "border-brand bg-brand/10" : "border-line-strong bg-surface hover:border-brand/60"}`}
              >
                {file ? <FileText size={22} className="text-brand-fg" /> : <UploadCloud size={22} className="text-faint" />}
                {file ? (
                  <>
                    <span className="font-mono text-[13px] text-fg">{file.name}</span>
                    <span className="text-[11px] text-faint">{(file.size / 1024).toFixed(1)} KB · klicken zum Ändern</span>
                  </>
                ) : (
                  <>
                    <span className="text-[13px] font-medium text-fg">PDF, DOCX, Markdown oder TXT hierher ziehen</span>
                    <span className="text-[11px] text-faint">oder klicken zum Auswählen · max. 10 MB</span>
                  </>
                )}
              </button>
              <input ref={picker} type="file" accept={ACCEPT} className="hidden" aria-hidden tabIndex={-1} onChange={(e) => { pick(e.target.files?.[0]); e.target.value = ""; }} />
              <Field label="Ersetzt Dokument (optional)" htmlFor="st-replaces" hint="Leer lassen: Dokumentserie wird automatisch am Dateinamen erkannt.">
                <input id="st-replaces" list="live-docs" value={replaces} onChange={(e) => setReplaces(e.target.value)} className={`${inputCls} font-mono`} placeholder="Passwort_Policy_v2.docx" />
              </Field>
            </>
          ) : (
            <>
              <Field label="Title" htmlFor="st-title">
                <input id="st-title" value={title} onChange={(e) => setTitle(e.target.value)} className={inputCls} placeholder="z. B. Mobile-Work: Blickschutzpflicht verschärfen" />
              </Field>
              <Field label="Target Document" htmlFor="st-doc" hint="Bestehendes Dokument wählen (wird als neue Version gestaged) oder neuen Namen eingeben.">
                <input id="st-doc" list="live-docs" value={targetDoc} onChange={(e) => setTargetDoc(e.target.value)} className={`${inputCls} font-mono`} placeholder="Mobile_Work_Policy_v2.pdf" />
              </Field>
              <Field label="New Rule Text" htmlFor="st-text">
                <textarea id="st-text" value={newText} onChange={(e) => setNewText(e.target.value)} rows={5} className={inputCls} placeholder="Neuer Regeltext — optional mit ## §1 Überschriften (10–20.000 Zeichen)" />
              </Field>
            </>
          )}
          <datalist id="live-docs">{docs.map((d) => <option key={d} value={d} />)}</datalist>

          <div>
            <div className="mb-1 text-xs font-medium text-faint">Min Role (RBAC-Stufe der neuen Chunks)</div>
            <div role="radiogroup" aria-label="Min Role" className="inline-flex gap-1 rounded-lg border border-line bg-surface p-0.5">
              {(["L1", "L2", "L3"] as Role[]).map((r) => {
                const disabled = ROLE_RANK[r] > ROLE_RANK[role];
                return (
                  <button key={r} type="button" role="radio" aria-checked={minRole === r} disabled={disabled} onClick={() => setMinRole(r)}
                    className={`rounded-md px-3 py-1 font-mono text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${minRole === r ? "bg-canvas text-fg shadow-sm ring-1 ring-line" : "text-faint hover:text-fg"}`}>
                    {r}
                  </button>
                );
              })}
            </div>
          </div>
          {error && <p role="alert" className="text-xs font-medium text-bad-fg">{error}</p>}
        </div>
      ) : (
        <div className="space-y-4" aria-live="polite">
          <ol className="space-y-2.5" aria-label="Scanner-Fortschritt">
            {SCAN_STEPS.map((label, i) => {
              const done = i < step || phase === "done";
              const active = i === step && phase === "scanning";
              return (
                <li key={label} className={`flex items-center gap-2.5 text-[13px] ${done ? "text-fg" : active ? "text-fg" : "text-faint"}`}>
                  <span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full border ${done ? "border-ok/40 bg-ok/10 text-ok-fg" : active ? "border-brand/40 bg-brand/10 text-brand-fg" : "border-line"}`}>
                    {done ? <Check size={11} /> : active ? <Loader2 size={11} className="animate-spin" /> : null}
                  </span>
                  {label}
                </li>
              );
            })}
          </ol>
          {phase === "done" && result && (
            <div className="space-y-2 rounded-lg border border-line bg-surface p-3.5">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone="ok"><Check size={10} /> Gestaged</Badge>
                <Badge>{result.stats.chunks} Chunks</Badge>
                <Badge tone="brand">{result.stats.changed} geändert/neu</Badge>
                <Badge>{result.stats.unchanged} unverändert</Badge>
                <Badge tone={result.stats.conflicts ? "bad" : "ok"}>{result.stats.conflicts} Konflikt{result.stats.conflicts === 1 ? "" : "e"}</Badge>
              </div>
              <p className="font-mono text-[11px] text-muted">{result.diff.docName} · SHA-256 {result.diff.sha256.slice(0, 16)}…</p>
              <p className="text-[11px] text-faint">Engine: {result.stats.engineMode} · {result.stats.latencyMs.total ?? 0} ms{result.served === "python" ? " · Python FastAPI" : " · lokale Engine"}</p>
              {result.diff.conflicts.length > 0 && (
                <ul className="space-y-1 border-t border-line pt-2 text-xs text-warn-fg">
                  {result.diff.conflicts.map((c) => (
                    <li key={c.id} className="flex items-start gap-1.5"><AlertTriangle size={12} className="mt-0.5 shrink-0" /> <span className="font-mono">{c.docName} {c.section.split(" ")[0]}</span></li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

function Field({ label, htmlFor, hint, children }: { label: string; htmlFor: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={htmlFor} className="mb-1 block text-xs font-medium text-faint">{label}</label>
      {children}
      {hint && <p className="mt-1 text-[11px] text-faint">{hint}</p>}
    </div>
  );
}

function StatusBadge({ status }: { status: StagedDiff["status"] }) {
  return status === "rejected" ? <Badge tone="bad"><X size={11} /> Abgelehnt</Badge> : <Badge tone="warn">Staging</Badge>;
}

function Meta({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline gap-3">
      <dt className="w-32 shrink-0 text-faint">{label}</dt>
      <dd className={`min-w-0 break-all text-fg ${mono ? "font-mono" : ""}`}>{value}</dd>
    </div>
  );
}

function QueueItem({ diff, active, onClick }: { diff: StagedDiff; active: boolean; onClick: () => void }) {
  const open = diff.conflicts.filter((c) => c.status === "open").length;
  return (
    <button
      onClick={onClick}
      aria-current={active}
      className={`w-full rounded-lg border p-3.5 text-left transition-colors ${active ? "border-line-strong bg-raised" : "border-line bg-canvas hover:border-line-strong hover:bg-raised/50"}`}
    >
      <div className="truncate font-mono text-[13px] font-medium text-fg">{diff.docName}</div>
      <div className="mt-1 truncate text-[11px] text-faint">{diff.author}</div>
      <div className="font-mono text-[11px] text-faint">{formatTime(diff.uploadedAt)}</div>
      <div className="mt-2.5 flex flex-wrap gap-1.5">
        <StatusBadge status={diff.status} />
        {diff.conflicts.length > 0 && diff.status === "pending" && (
          open > 0 ? <Badge tone="bad"><AlertTriangle size={10} /> {open} Conflict</Badge> : <Badge tone="ok"><Check size={10} /> Adressiert</Badge>
        )}
        <Badge>{diff.hunks.length} Chunk{diff.hunks.length > 1 ? "s" : ""} Modified</Badge>
      </div>
    </button>
  );
}

function ConflictCallout({
  conflict: c, stagedRule, stagedFile, canAct, busy, onTicket,
}: { conflict: Conflict; stagedRule: string; stagedFile: string; canAct: boolean; busy: boolean; onTicket: () => void }) {
  const addressed = c.status === "ticket";
  return (
    <div role="alert" className={`overflow-hidden rounded-lg border ${addressed ? "border-ok/30 bg-ok/5" : "border-warn/30 bg-warn/5"}`}>
      <div className="flex flex-wrap items-center gap-2 px-4 py-3">
        <AlertTriangle size={15} className={addressed ? "text-ok-fg" : "text-warn-fg"} />
        <span className="text-sm font-semibold text-fg">Cross-Document Conflict</span>
        <Badge tone={c.severity === "high" ? "bad" : "warn"}>{c.severity === "high" ? "High priority" : "Medium priority"}</Badge>
        {c.verifiedBy && <Badge tone="brand">{c.verifiedBy === "llm+rules" ? "LLM + Regeln verifiziert" : c.verifiedBy === "llm" ? "LLM-verifiziert" : "Regelbasiert"}</Badge>}
        {addressed && <Badge tone="ok"><Check size={10} /> Sync-Ticket {c.ticketId}{c.ticketResolved ? " · erledigt" : ""}</Badge>}
        <Badge className="ml-auto font-mono">similarity {c.similarity.toFixed(2)}</Badge>
      </div>
      <p className="px-4 pb-3 text-[13px] leading-6 text-muted">⚠️ {c.message}</p>
      <table className="w-full border-t border-line text-xs">
        <tbody className="divide-y divide-line">
          <tr>
            <th scope="row" className="w-44 bg-canvas/50 px-4 py-2.5 text-left align-top font-medium text-faint">Staged Rule</th>
            <td className="px-4 py-2.5 text-fg">
              <span className="block font-mono text-[11px] text-ok-fg">{stagedFile}</span>
              {stagedRule}
            </td>
          </tr>
          <tr>
            <th scope="row" className="bg-canvas/50 px-4 py-2.5 text-left align-top font-medium text-faint">Conflicting Existing Document</th>
            <td className="px-4 py-2.5 text-fg">
              <span className="block font-mono text-[11px] text-bad-fg">{c.docName}, {c.section.split(" ")[0]}</span>
              {c.quote}
            </td>
          </tr>
        </tbody>
      </table>
      {!addressed && canAct && (
        <div className="flex items-center justify-between gap-3 border-t border-line px-4 py-3">
          <span className="text-[11px] text-faint">Mandatory: Konflikt per Sync-Ticket adressieren oder unten per Override akzeptieren.</span>
          <Button variant="outline" onClick={onTicket} disabled={busy}>
            {busy ? <Loader2 size={14} className="animate-spin" /> : <TicketPlus size={14} />} Create Sync-Ticket for Conflicting Doc
          </Button>
        </div>
      )}
    </div>
  );
}

type Seg = { text: string; changed: boolean };

// Word-level LCS diff; consecutive changed words are merged into one highlighted run.
function wordDiff(a: string, b: string): { oldSegs: Seg[]; newSegs: Seg[] } {
  const x = a.split(/\s+/);
  const y = b.split(/\s+/);
  const dp = Array.from({ length: x.length + 1 }, () => new Array<number>(y.length + 1).fill(0));
  for (let i = x.length - 1; i >= 0; i--)
    for (let j = y.length - 1; j >= 0; j--) dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);

  const oldTok: Seg[] = [];
  const newTok: Seg[] = [];
  let i = 0;
  let j = 0;
  while (i < x.length && j < y.length) {
    if (x[i] === y[j]) {
      oldTok.push({ text: x[i++], changed: false });
      newTok.push({ text: y[j++], changed: false });
    } else if (dp[i + 1][j] >= dp[i][j + 1]) oldTok.push({ text: x[i++], changed: true });
    else newTok.push({ text: y[j++], changed: true });
  }
  while (i < x.length) oldTok.push({ text: x[i++], changed: true });
  while (j < y.length) newTok.push({ text: y[j++], changed: true });

  const merge = (toks: Seg[]) =>
    toks.reduce<Seg[]>((acc, t) => {
      const last = acc[acc.length - 1];
      if (last && last.changed === t.changed) last.text += ` ${t.text}`;
      else acc.push({ ...t });
      return acc;
    }, []);
  return { oldSegs: merge(oldTok), newSegs: merge(newTok) };
}

function DiffBlock({ hunk: h, docName, index }: { hunk: DiffHunk; docName: string; index: number }) {
  const hasOld = h.oldContent !== null;
  const { oldSegs, newSegs } = hasOld ? wordDiff(h.oldContent!, h.newContent) : { oldSegs: [], newSegs: [{ text: h.newContent, changed: false }] };
  // Mock line number: stable per section, resembling a real file position.
  const line = 8 + ([...h.section].reduce((s, ch) => s + ch.charCodeAt(0), 0) % 80) + index * 3;
  return (
    <div className="overflow-hidden rounded-lg border border-line font-mono text-sm shadow-card">
      <div className="flex items-center justify-between gap-4 border-b border-line bg-surface px-4 py-2 text-xs text-muted">
        <span className="truncate">{h.section}</span>
        <span className="shrink-0 text-faint">
          {h.oldDocName ?? "∅"} <span className="text-line-strong">→</span> {docName}
        </span>
      </div>
      {hasOld && <DiffRow kind="old" oldNo={line} segs={oldSegs} meta={`${h.oldDocName} · ${h.oldSection?.split(" ")[0]} · live`} />}
      <DiffRow kind="new" oldNo={undefined} newNo={line} segs={newSegs} meta={`${docName} · ${h.section.split(" ")[0]} · staged · min ${h.minRole}`} />
    </div>
  );
}

function DiffRow({ kind, oldNo, newNo, segs, meta }: { kind: "old" | "new"; oldNo?: number; newNo?: number; segs: Seg[]; meta: string }) {
  const isOld = kind === "old";
  const tone = isOld
    ? { row: "bg-bad/10 border-bad text-bad-fg", mark: "bg-bad/25", gutter: "text-bad-fg/60" }
    : { row: "bg-ok/10 border-ok text-ok-fg", mark: "bg-ok/25", gutter: "text-ok-fg/60" };
  return (
    <div className={`grid grid-cols-[3rem_3rem_1.75rem_1fr] border-l-2 ${tone.row}`}>
      <span className={`select-none px-2 py-2.5 text-right text-xs leading-6 ${tone.gutter}`}>{oldNo ?? ""}</span>
      <span className={`select-none px-2 py-2.5 text-right text-xs leading-6 ${tone.gutter}`}>{newNo ?? ""}</span>
      <span className="select-none py-2.5 text-center leading-6">{isOld ? "−" : "+"}</span>
      <div className="min-w-0 py-2.5 pr-4">
        <p className="text-[13px] leading-6 [overflow-wrap:anywhere]">
          {segs.map((s, i) => (
            <span key={i}>
              {i > 0 && " "}
              {s.changed ? <mark className={`rounded px-1 text-inherit ${tone.mark}`}>{s.text}</mark> : s.text}
            </span>
          ))}
        </p>
        <p className="mt-0.5 text-[10px] text-faint">{meta}</p>
      </div>
    </div>
  );
}
