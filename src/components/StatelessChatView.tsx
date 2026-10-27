"use client";

import { useEffect, useState } from "react";
import {
  AlertTriangle, ArrowUpRight, Check, ChevronDown, ChevronRight, CornerDownLeft, FileText, Loader2, Lock,
  RotateCcw, ShieldCheck, ShieldX, Ticket as TicketIcon, X,
} from "lucide-react";
import {
  QUICK_ACTIONS, ROLE_RANK, callApi, citationLabel, formatTime,
  type QueryResult, type Role, type Session, type Snapshot, type Ticket,
} from "@/lib/mock-store";
import { Badge, Button, SidePanel } from "./ui-primitives";

interface Props {
  session: Session;
  snap: Snapshot | null;
  notify: (text: string, tone?: "ok" | "bad" | "info") => void;
  onElevate: (role: Role) => void;
}

// Pipeline B UI: every submit is an isolated query — only the question (never history) is sent to /api/v1.
export default function StatelessChatView({ session, snap, notify, onElevate }: Props) {
  const [input, setInput] = useState("");
  const [submitted, setSubmitted] = useState<{ text: string; nonce: number } | null>(null);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [telemetryOpen, setTelemetryOpen] = useState(false);
  const [openChunkId, setOpenChunkId] = useState<string | null>(null);
  const [ticket, setTicket] = useState<{ nonce: number; data: Ticket } | null>(null);
  const [ticketBusy, setTicketBusy] = useState(false);

  // Re-runs when the question, the identity or the live DB version changes (e.g. after a CISO merge).
  useEffect(() => {
    if (!submitted) return;
    let off = false;
    setLoading(true);
    callApi<{ result: QueryResult }>("query", { question: submitted.text }, session.token).then((r) => {
      if (off) return;
      setLoading(false);
      if (r.ok) setResult(r.result);
      else notify(r.error, "bad");
    });
    return () => {
      off = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [submitted, session.token, snap?.liveVersion]);

  const ask = (text: string) => {
    if (text.trim().length < 3) return;
    setInput("");
    setSubmitted((prev) => ({ text: text.trim(), nonce: (prev?.nonce ?? 0) + 1 }));
    setOpenChunkId(null);
  };

  const reset = () => {
    setSubmitted(null);
    setResult(null);
    setOpenChunkId(null);
  };

  const createTicket = async () => {
    if (!submitted || !result) return;
    setTicketBusy(true);
    const r = await callApi<{ ticket: Ticket }>("create_ticket", { question: submitted.text, reason: result.kind === "denied" ? "RBAC" : "LOW_CONFIDENCE" }, session.token);
    setTicketBusy(false);
    if (r.ok) {
      setTicket({ nonce: submitted.nonce, data: r.ticket });
      notify(`Ticket ${r.ticket.id} im CISO-Office angelegt.`, "ok");
    } else notify(r.error, "bad");
  };

  const chunk = openChunkId && result ? result.citations[openChunkId] : null;
  const score = chunk && result ? result.scores?.[chunk.id] : undefined;
  const local = (result?.trace.engineMode ?? "").startsWith("Local");
  const activeTicket = ticket && submitted && ticket.nonce === submitted.nonce ? ticket.data : null;

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-4xl px-6 py-8">
          {!submitted ? (
            <Hero role={session.role} onPick={ask} />
          ) : !result ? (
            <div className="flex items-center justify-center gap-2 py-24 text-sm text-faint">
              <Loader2 size={15} className="animate-spin" /> Pipeline B läuft …
            </div>
          ) : (
            <ResultView
              result={result}
              question={submitted.text}
              nonce={submitted.nonce}
              role={session.role}
              loading={loading}
              telemetryOpen={telemetryOpen}
              onToggleTelemetry={() => setTelemetryOpen((o) => !o)}
              onCite={setOpenChunkId}
              onReset={reset}
              ticket={activeTicket}
              ticketBusy={ticketBusy}
              onCreateTicket={createTicket}
              onElevate={onElevate}
            />
          )}
        </div>
      </div>

      <div className="shrink-0 px-6 pb-4 pt-2">
        <div className="mx-auto w-full max-w-4xl">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              ask(input);
            }}
            className="flex items-center gap-2 rounded-xl border border-line-strong bg-canvas p-2 shadow-card focus-within:ring-1 focus-within:ring-brand"
          >
            <span className="ml-1 inline-flex shrink-0 items-center gap-1.5 rounded-md border border-ok/25 bg-ok/10 px-2 py-1 text-[11px] font-medium text-ok-fg">
              <Lock size={11} /> Stateless Session
            </span>
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Compliance-Frage stellen … z. B. „Darf ich das auch im Zug machen?“"
              aria-label="Compliance-Frage"
              maxLength={500}
              className="min-w-0 flex-1 bg-transparent px-2 py-1.5 text-sm text-fg placeholder:text-faint focus:outline-none"
            />
            <Button type="submit" disabled={input.trim().length < 3} className="!py-1.5">
              Fragen <CornerDownLeft size={13} />
            </Button>
          </form>
          <p className="mt-2 text-center text-[11px] text-faint">
            Stateless Mode Active (Zero Context Contamination) · Antworten basieren ausschließlich auf freigegebenen Dokumenten.
          </p>
        </div>
      </div>

      <SidePanel open={!!chunk} title="Source Chunk Inspector" onClose={() => setOpenChunkId(null)}>
        {chunk && (
          <>
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <FileText size={15} className="text-brand-fg" />
                <span className="break-all font-mono text-[13px] text-fg">{chunk.docName}</span>
              </div>
              <div className="flex flex-wrap gap-1.5">
                <Badge>{chunk.section}</Badge>
                <Badge tone={chunk.minRole === "L1" ? "ok" : "warn"}>Min. Role {chunk.minRole}</Badge>
                <Badge tone="ok">Live Vector DB</Badge>
              </div>
            </div>
            <div className="overflow-hidden rounded-lg border border-line bg-surface">
              <div className="border-b border-line px-3 py-1.5 font-mono text-[11px] text-faint">chunk_id: {chunk.id}</div>
              <blockquote className="whitespace-pre-line border-l-2 border-brand px-4 py-3 text-[13px] leading-6 text-fg">{readableChunk(chunk.content)}</blockquote>
            </div>
            <dl className="divide-y divide-line overflow-hidden rounded-lg border border-line text-xs">
              <Prov label="Similarity score" value={score !== undefined ? score.toFixed(3) : "—"} hint={local ? "Term-Coverage (lokale Engine)" : "Cosine (Embedding)"} />
              <Prov label="Freigegeben am" value={formatTime(chunk.updatedAt)} />
              <Prov label="Freigegeben von" value={chunk.approvedBy ?? "unbekannt"} hint="CISO-Approver" />
              <Prov label="SHA-256" value={chunk.sha256} mono />
            </dl>
            <p className="text-xs text-faint">Unveränderter Textauszug aus dem freigegebenen Dokument.</p>
          </>
        )}
      </SidePanel>
    </div>
  );
}

function Hero({ role, onPick }: { role: Role; onPick: (q: string) => void }) {
  return (
    <div className="animate-fade-in pt-8">
      <div className="mb-8 space-y-3">
        <Badge tone="brand">
          <ShieldCheck size={11} /> Security Compliance Command Center
        </Badge>
        <h1 className="text-3xl font-semibold tracking-tight text-fg">Was sagt unsere Richtlinie dazu?</h1>
        <p className="max-w-xl text-sm leading-6 text-muted">
          Jede Frage wird isoliert gegen die Live-Datenbank freigegebener Dokumente geprüft — mit RBAC-Filter, lexikalischer Grounding-Prüfung und Quellenangabe pro Antwort.
        </p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        {QUICK_ACTIONS.map((q) => {
          const locked = ROLE_RANK[role] < ROLE_RANK[q.minRole];
          return (
            <button
              key={q.input}
              onClick={() => onPick(q.input)}
              className="group flex min-h-32 flex-col justify-between rounded-xl border border-line bg-surface p-4 text-left shadow-card transition-all hover:border-line-strong hover:bg-raised/50"
            >
              <div className="flex items-center justify-between gap-2">
                <Badge tone={q.fallback ? "warn" : "neutral"}>{q.domain}</Badge>
                <ArrowUpRight size={16} className="text-faint transition-colors group-hover:text-brand-fg" />
              </div>
              <div className="mt-4 space-y-2">
                <p className="text-sm font-medium leading-6 text-fg">{q.input}</p>
                <div className="text-[11px] text-faint">
                  {q.fallback ? (
                    <span>Simulate Unknown Policy Query</span>
                  ) : locked ? (
                    <span className="inline-flex items-center gap-1 text-warn-fg">
                      <Lock size={10} /> Erfordert {q.minRole} — zeigt RBAC-Sperre
                    </span>
                  ) : (
                    <span className="font-mono">min. {q.minRole}</span>
                  )}
                </div>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

interface ResultProps {
  result: QueryResult;
  question: string;
  nonce: number;
  role: Role;
  loading: boolean;
  telemetryOpen: boolean;
  onToggleTelemetry: () => void;
  onCite: (id: string) => void;
  onReset: () => void;
  ticket: Ticket | null;
  ticketBusy: boolean;
  onCreateTicket: () => void;
  onElevate: (r: Role) => void;
}

function ResultView(p: ResultProps) {
  const { result, role } = p;
  const tr = result.trace;
  const lat = tr.latencyMs;
  const fmt = (n?: number) => (n === undefined ? "" : ` · ${n < 10 ? n.toFixed(1) : Math.round(n)} ms`);
  const grounded = result.kind === "answer";
  const denied = result.kind === "denied";
  const search = result.kind === "search";
  const sources = Object.values(result.citations);

  return (
    <div className={`animate-fade-in space-y-4 transition-opacity ${p.loading ? "opacity-60" : ""}`}>
      <div className="rounded-xl border border-line bg-surface p-4">
        <div className="mb-2 flex items-center justify-between">
          <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-faint">Isolated Query · #{String(p.nonce).padStart(3, "0")}</span>
          <div className="flex items-center gap-2">
            <Badge tone={(result.trace.engineMode ?? "").startsWith("Local") ? "neutral" : "brand"} className="max-w-[16rem] truncate" >{result.trace.engineMode ?? "Engine"}</Badge>
            <Badge><Lock size={10} /> No context carried</Badge>
            <button onClick={p.onReset} className="inline-flex items-center gap-1 text-[11px] text-faint hover:text-fg">
              <RotateCcw size={11} /> Neue Anfrage
            </button>
          </div>
        </div>
        <p className="text-base font-medium text-fg">{p.question}</p>
      </div>

      <div className="rounded-xl border border-line bg-surface">
        <div className="flex flex-wrap items-center gap-1.5 px-3 py-2.5">
          <span className="mr-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">Pipeline B Telemetry</span>
          <Step ok label={`1. Query Rewritten${fmt(lat?.rewrite)}`} />
          <Arrow />
          <Step ok={!denied} label={`2. RBAC Filter (${role})${fmt(lat?.rbac)}`} />
          <Arrow />
          <Step ok={result.kind === "answer"} label={`3. Retrieval-Score ${(tr.retrievalScore ?? tr.confidence).toFixed(2)}${fmt(lat?.retrieval)}`} />
          <Arrow />
          <Step ok={tr.nliVerified || tr.verification === "extractive"} label={`4. ${grounding(result.kind, tr)}${fmt(lat && (lat.generation ?? 0) + (lat.nli ?? 0) || undefined)}`} />
          <button onClick={p.onToggleTelemetry} aria-expanded={p.telemetryOpen} className="ml-auto inline-flex items-center gap-1 text-[11px] text-faint hover:text-fg">
            Under the Hood {p.telemetryOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          </button>
        </div>
        {p.telemetryOpen && (
          <dl className="grid gap-px overflow-hidden rounded-b-xl border-t border-line bg-line text-xs sm:grid-cols-2">
            <Cell label="1 · Original User Input" value={tr.original} />
            <Cell label={`2 · Query Rewrite (${tr.rewriteBy ?? "rules"})`} value={tr.rewrite} />
            <Cell label="3 · RBAC Filter Applied" value={`${tr.rbacFilter}  →  ${tr.searched} von ${tr.total} Chunks durchsuchbar`} mono />
            <Cell label="4 · Grounding Check Status" value={`${result.kind === "answer" ? (tr.verification === "extractive" ? "Verification: extractive (verbatim chunk text, no NLI)" : `NLI Verified: ${tr.nliVerified ? "TRUE" : "FALSE"}`) : "nicht verifiziert (keine Antwort)"} | Temp: ${tr.temperature.toFixed(1)} | retrieval: ${(tr.retrievalScore ?? tr.topScore ?? 0).toFixed(3)}${tr.lexicalSupport !== undefined ? ` | lexical: ${tr.lexicalSupport.toFixed(2)}` : ""}`} mono />
            <Cell label="Engine" value={`${tr.engineMode ?? "—"}${tr.answerBy && tr.answerBy !== "none" ? ` · Antwort: ${tr.answerBy}` : ""}`} mono />
            <Cell label="Latenz (ms)" value={lat ? (["rewrite", "rbac", "retrieval", "generation", "nli"] as const).filter((k) => lat[k] !== undefined).map((k) => `${k} ${lat[k]}`).concat(`Σ ${lat.total}`).join(" · ") : "—"} mono />
          </dl>
        )}
      </div>

      {denied ? (
        <article className="rounded-xl border border-bad/30 bg-bad/5 p-6 shadow-card">
          <div className="flex items-start gap-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-bad/30 bg-bad/10">
              <ShieldX size={18} className="text-bad-fg" />
            </div>
            <div className="space-y-2">
              <h2 className="text-sm font-semibold text-fg">RBAC Clearance Denied (Requires {result.requiredRole})</h2>
              <p className="text-[15px] leading-7 text-muted">{result.segments[0]?.type === "text" ? result.segments[0].text : ""}</p>
              <div className="flex flex-wrap items-center gap-2 pt-1">
                {result.requiredRole && <Button variant="outline" onClick={() => p.onElevate(result.requiredRole!)}><Lock size={13} /> Zu {result.requiredRole} wechseln</Button>}
                <TicketControl {...p} label="Zugriff per Ticket anfragen" />
              </div>
              <p className="text-[11px] text-faint">Der Zugriffsversuch wurde im Audit Log protokolliert (RBAC_DENIED).</p>
            </div>
          </div>
        </article>
      ) : (
        <article className={`rounded-xl border bg-surface p-6 shadow-card ${grounded ? "border-line" : "border-warn/30"}`}>
          <div className="mb-4 flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">{grounded ? "Verified Compliance Answer" : search ? "Fundstellen" : "Compliance Answer"}</span>
            {grounded ? (
              <span className="flex items-center gap-1.5">
                {/^(Local|Rule-based)/.test(tr.engineMode ?? "") && <Badge tone="warn"><AlertTriangle size={11} /> Keyword-Modus · geringe Präzision</Badge>}
                <Badge tone="ok"><Check size={11} /> Grounded · Retrieval {(tr.retrievalScore ?? tr.confidence).toFixed(2)}</Badge>
              </span>
            ) : search ? (
              <Badge><FileText size={11} /> Suchtreffer · nicht verifiziert</Badge>
            ) : (
              <Badge tone="warn"><AlertTriangle size={11} /> Keine belastbare Quelle</Badge>
            )}
          </div>
          <p className="text-[15px] leading-7 text-fg">
            {result.segments.map((s, i) =>
              s.type === "text" ? (
                <span key={i}>{s.text}</span>
              ) : s.type === "note" ? (
                <span key={i} className="block pt-2 text-sm italic text-muted">{s.text}</span>
              ) : (
                <button
                  key={i}
                  onClick={() => p.onCite(s.chunkId)}
                  title="Quelltext anzeigen"
                  className="mx-0.5 inline-flex items-center rounded-md border border-brand/25 bg-brand/10 px-1.5 align-baseline font-mono text-[11px] leading-5 text-brand-fg transition-colors hover:border-brand/50 hover:bg-brand/20"
                >
                  [{citationLabel(result.citations[s.chunkId])}]
                </button>
              )
            )}
          </p>
          {sources.length > 0 && (
            <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-line pt-4">
              <span className="text-[11px] text-faint">Quellen</span>
              {sources.map((c) => (
                <button key={c.id} onClick={() => p.onCite(c.id)} className="inline-flex items-center gap-1.5 rounded-md border border-line bg-canvas px-2 py-1 font-mono text-[11px] text-muted hover:border-line-strong hover:text-fg">
                  <FileText size={11} /> {c.docName} · {c.section.split(" ")[0]}
                </button>
              ))}
            </div>
          )}
          {!grounded && (
            <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-line pt-4">
              <TicketControl {...p} label="Jira/ServiceNow Ticket an CISO-Office erstellen" />
            </div>
          )}
        </article>
      )}
    </div>
  );
}

function TicketControl({ ticket, ticketBusy, onCreateTicket, label }: ResultProps & { label: string }) {
  if (ticket)
    return (
      <div className="flex items-center gap-2 text-sm text-ok-fg">
        <Check size={15} />
        {ticket.system}-Ticket <span className="font-mono">{ticket.id}</span> an {ticket.queue} erstellt.
      </div>
    );
  return (
    <Button variant="outline" onClick={onCreateTicket} disabled={ticketBusy}>
      {ticketBusy ? <Loader2 size={14} className="animate-spin" /> : <TicketIcon size={14} />} {label}
    </Button>
  );
}

/** Step 4 label: a search hit or fallback was never verified as an answer, so it shows no NLI verdict. */
function grounding(kind: string, tr: { verification?: string; nliVerified: boolean }): string {
  if (kind !== "answer") return "nicht verifiziert";
  return tr.verification === "extractive" ? "Wörtlicher Auszug (kein NLI)" : `NLI Grounded: ${tr.nliVerified ? "PASS" : "FAIL"}`;
}

/** Chunk text for people: the chunker's "Tabelle [§…]:" row label is dropped, every row becomes a "Spalte: Wert" list. */
function readableChunk(content: string): string {
  return content
    .split(/\s*Tabelle \[[^\]]*\]:\s*/)
    .map((part, i) => (i === 0 ? part : part.split(" | ").join("\n")))
    .filter((part) => part.trim())
    .join("\n\n");
}

function Step({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-md border px-2 py-0.5 font-mono text-[11px] ${ok ? "border-ok/25 bg-ok/10 text-ok-fg" : "border-warn/30 bg-warn/10 text-warn-fg"}`}>
      {ok ? <Check size={10} /> : <X size={10} />}
      {label}
    </span>
  );
}

function Prov({ label, value, hint, mono }: { label: string; value: string; hint?: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline gap-3 bg-surface px-3 py-2">
      <dt className="w-28 shrink-0 text-faint">{label}</dt>
      <dd className={`min-w-0 break-all text-fg ${mono ? "font-mono text-[11px]" : ""}`}>
        {value}
        {hint && <span className="ml-1.5 text-[10px] text-faint">{hint}</span>}
      </dd>
    </div>
  );
}

function Arrow() {
  return <ChevronRight size={12} className="text-line-strong" />;
}

function Cell({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="bg-canvas px-4 py-3">
      <dt className="mb-1 text-[10px] uppercase tracking-wider text-faint">{label}</dt>
      <dd className={`leading-5 text-muted ${mono ? "font-mono" : ""}`}>{value}</dd>
    </div>
  );
}
