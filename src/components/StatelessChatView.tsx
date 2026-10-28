"use client";

import { useEffect, useState, type ReactNode } from "react";
import {
  AlertTriangle, ArrowUpRight, Check, ChevronDown, ChevronRight, Clock, CornerDownLeft, FileText, Loader2, Lock,
  RotateCcw, ShieldCheck, ShieldX, Ticket as TicketIcon, X,
} from "lucide-react";
import {
  QUICK_ACTIONS, ROLE_RANK, callApi, citationLabel, formatTime,
  type FastHit, type FastResult, type QueryResult, type Role, type Session, type Snapshot, type Ticket,
} from "@/lib/mock-store";
import { Badge, Button, SidePanel } from "./ui-primitives";

interface Props {
  session: Session;
  snap: Snapshot | null;
  notify: (text: string, tone?: "ok" | "bad" | "info") => void;
  onElevate: (role: Role) => void;
}

// Pipeline B UI: every submit is an isolated query — only the question (never history) is sent to /api/v1.
// Two stages: query_fast shows verbatim source sentences with the key sentence highlighted (no LLM), then query confirms an
// answer or not; after CHECK_TIMEOUT_MS without stage 2 the status reads "Prüfung nicht abgeschlossen".
const CHECK_TIMEOUT_MS = 8000;

export default function StatelessChatView({ session, snap, notify, onElevate }: Props) {
  const [input, setInput] = useState("");
  const [submitted, setSubmitted] = useState<{ text: string; nonce: number } | null>(null);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [fast, setFast] = useState<FastResult | null | undefined>(undefined); // undefined = stage 1 pending, null = none
  const [timedOut, setTimedOut] = useState(false);
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
    setFast(undefined);
    setResult(null);
    setTimedOut(false);
    const timer = setTimeout(() => !off && setTimedOut(true), CHECK_TIMEOUT_MS);
    (async () => {
      const f = await callApi<{ fast: FastResult | null }>("query_fast", { question: submitted.text }, session.token);
      if (off) return;
      setFast(f.ok ? f.fast : null);
      const r = await callApi<{ result: QueryResult }>("query", { question: submitted.text }, session.token);
      if (off) return;
      clearTimeout(timer);
      setLoading(false);
      if (r.ok) setResult(r.result);
      else notify(r.error, "bad");
    })();
    return () => {
      off = true;
      clearTimeout(timer);
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
    setFast(undefined);
    setOpenChunkId(null);
  };

  const createTicket = async () => {
    if (!submitted) return;
    setTicketBusy(true);
    const r = await callApi<{ ticket: Ticket }>("create_ticket", { question: submitted.text, reason: result?.kind === "denied" ? "RBAC" : "LOW_CONFIDENCE" }, session.token);
    setTicketBusy(false);
    if (r.ok) {
      setTicket({ nonce: submitted.nonce, data: r.ticket });
      notify(`Ticket ${r.ticket.id} im CISO-Office angelegt.`, "ok");
    } else notify(r.error, "bad");
  };

  const citations = { ...(fast?.citations ?? {}), ...(result?.citations ?? {}) };
  const chunk = openChunkId ? citations[openChunkId] ?? null : null;
  const score = chunk ? result?.scores?.[chunk.id] ?? fast?.hits.find((h) => h.chunkId === chunk.id)?.dense ?? undefined : undefined;
  const local = (result?.trace.engineMode ?? "").startsWith("Local");
  const activeTicket = ticket && submitted && ticket.nonce === submitted.nonce ? ticket.data : null;

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-4xl px-6 py-8">
          {!submitted ? (
            <Hero role={session.role} onPick={ask} />
          ) : !result && fast === undefined ? (
            <div className="flex items-center justify-center gap-2 py-24 text-sm text-faint">
              <Loader2 size={15} className="animate-spin" /> Fundstellen werden gesucht …
            </div>
          ) : (
            <ResultView
              key={submitted.nonce}
              result={result}
              fast={fast ?? null}
              timedOut={timedOut}
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
  result: QueryResult | null;
  fast: FastResult | null;
  timedOut: boolean;
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

type Status = "checking" | "confirmed" | "unconfirmed" | "incomplete";

const STATUS: Record<Status, { label: string; tone: "neutral" | "ok" | "warn"; icon: ReactNode }> = {
  checking: { label: "wird geprüft", tone: "neutral", icon: <Loader2 size={11} className="animate-spin" /> },
  confirmed: { label: "bestätigte Antwort", tone: "ok", icon: <Check size={11} /> },
  unconfirmed: { label: "nicht als Antwort bestätigt", tone: "warn", icon: <AlertTriangle size={11} /> },
  incomplete: { label: "Prüfung nicht abgeschlossen", tone: "warn", icon: <Clock size={11} /> },
};

function ResultView(p: ResultProps) {
  const { result, fast, role } = p;
  const details = ROLE_RANK[role] >= ROLE_RANK.L2; // L1 sees the simple card only
  const status: Status = result ? (result.kind === "answer" ? "confirmed" : "unconfirmed") : p.timedOut ? "incomplete" : "checking";
  const st = STATUS[status];
  const citations = { ...(fast?.citations ?? {}), ...(result?.citations ?? {}) };
  // stage-1 hits stay on screen; without stage 1 (local engine) the stage-2 search hits are shown the same way, without a key sentence
  const hits: FastHit[] = fast?.hits.length
    ? fast.hits
    : (result?.hits ?? []).map((h) => ({ ...h, sentences: [h.content], key: -1, keySentence: "", source: "ranked" as const, coverage: 0, dense: null }));
  const answer = result && (result.kind === "answer" || result.trace.contradiction);
  const [sourcesOpen, setSourcesOpen] = useState(false); // after a confirmed answer the source sentences fold away
  const showHits = !answer || sourcesOpen;

  if (result?.kind === "denied")
    return (
      <div className="animate-fade-in space-y-4">
        <QuestionHeader {...p} />
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
      </div>
    );

  return (
    <div className="animate-fade-in space-y-4">
      <QuestionHeader {...p} />
      <article data-testid="answer-card" data-status={status} className={`rounded-xl border bg-surface p-6 shadow-card ${status === "confirmed" ? "border-ok/30" : "border-line"}`}>
        <div className="mb-4 flex items-center justify-between gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">{status === "confirmed" ? "Antwort" : "Fundstellen"}</span>
          <Badge tone={st.tone}>{st.icon} {st.label}</Badge>
        </div>

        {answer && (
          <p data-testid="confirmed-answer" className="mb-4 text-[15px] leading-7 text-fg">
            {result.segments.map((s, i) =>
              s.type === "text" ? (
                <span key={i}>{s.text}</span>
              ) : s.type === "note" ? (
                <span key={i} className="block pt-2 text-sm italic text-muted">{s.text}</span>
              ) : (
                <button key={i} onClick={() => p.onCite(s.chunkId)} title="Quelltext anzeigen"
                  className="mx-0.5 inline-flex items-center rounded-md border border-brand/25 bg-brand/10 px-1.5 align-baseline font-mono text-[11px] leading-5 text-brand-fg transition-colors hover:border-brand/50 hover:bg-brand/20">
                  [{citationLabel(citations[s.chunkId])}]
                </button>
              )
            )}
          </p>
        )}

        {answer && hits.length > 0 && (
          <button onClick={() => setSourcesOpen((o) => !o)} aria-expanded={sourcesOpen} className="mb-2 inline-flex w-full items-center gap-1 border-t border-line pt-4 text-[11px] text-faint hover:text-fg">
            {sourcesOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />} Originalstellen ({hits.length})
          </button>
        )}
        {hits.length > 0 ? (
          showHits && <ol className="space-y-3">
            {hits.map((h) => (
              <li key={h.chunkId} data-testid="hit" className="rounded-lg border border-line bg-canvas px-4 py-3">
                <button onClick={() => p.onCite(h.chunkId)} className="mb-1.5 inline-flex items-center gap-1.5 font-mono text-[11px] text-muted hover:text-fg">
                  <FileText size={11} /> {h.docName} · {h.section}
                </button>
                <p className="text-[14px] leading-6 text-muted">
                  {h.sentences.map((t, i) =>
                    i === h.key ? (
                      <mark key={i} data-testid="key-sentence" className="rounded bg-brand/15 px-0.5 font-medium text-fg">{t}</mark>
                    ) : (
                      <span key={i}>{t}</span>
                    )
                  ).flatMap((el, i) => (i ? [" ", el] : [el]))}
                </p>
              </li>
            ))}
          </ol>
        ) : (
          !answer && (
            <p className="text-[15px] leading-7 text-muted">
              {status === "checking" ? "Noch keine wörtliche Fundstelle – die Prüfung läuft." : result?.segments[0]?.type === "text" ? result.segments[0].text : "Keine Fundstelle im freigegebenen Bestand."}
            </p>
          )
        )}

        {hits.length > 0 && status !== "confirmed" && (
          <p className="mt-3 text-[11px] text-faint">Wörtliche Auszüge aus freigegebenen Dokumenten · {status === "checking" ? "die Antwort wird noch geprüft" : "keine bestätigte Antwort"}.</p>
        )}
        {(status === "unconfirmed" || status === "incomplete") && (
          <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-line pt-4">
            <TicketControl {...p} label="Jira/ServiceNow Ticket an CISO-Office erstellen" />
          </div>
        )}
      </article>

      {details && <Details {...p} />}
    </div>
  );
}

function QuestionHeader(p: ResultProps) {
  const engine = p.result?.trace.engineMode ?? p.fast?.trace.engineMode;
  return (
    <div className="rounded-xl border border-line bg-surface p-4">
      <div className="mb-2 flex items-center justify-between">
        <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-faint">Isolated Query · #{String(p.nonce).padStart(3, "0")}</span>
        <div className="flex items-center gap-2">
          {ROLE_RANK[p.role] >= ROLE_RANK.L2 && engine && <Badge tone={engine.startsWith("Local") ? "neutral" : "brand"} className="max-w-[16rem] truncate">{engine}</Badge>}
          <Badge><Lock size={10} /> No context carried</Badge>
          <button onClick={p.onReset} className="inline-flex items-center gap-1 text-[11px] text-faint hover:text-fg">
            <RotateCcw size={11} /> Neue Anfrage
          </button>
        </div>
      </div>
      <p className="text-base font-medium text-fg">{p.question}</p>
    </div>
  );
}

/** L2/L3: telemetry of both stages — scores, judge verdict per part, span verifier per sentence. Collapsed by default. */
function Details(p: ResultProps) {
  const { result, fast, role } = p;
  const tr = result?.trace;
  const lat = tr?.latencyMs;
  const fmt = (n?: number) => (n === undefined ? "" : ` · ${n < 10 ? n.toFixed(1) : Math.round(n)} ms`);
  const verdict = (v: unknown) => (v === true ? "JA" : v === false ? "NEIN" : "—");
  return (
    <div data-testid="details" className="rounded-xl border border-line bg-surface">
      <button onClick={p.onToggleTelemetry} aria-expanded={p.telemetryOpen} className="flex w-full items-center gap-1.5 px-3 py-2.5 text-left">
        <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">Details · Scores, Judge, Verifier</span>
        {fast && <span className="font-mono text-[11px] text-faint">· Stufe 1 {fast.trace.latencyMs.total} ms</span>}
        {lat && <span className="font-mono text-[11px] text-faint">· Stufe 2 {lat.total} ms</span>}
        <span className="ml-auto text-faint">{p.telemetryOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</span>
      </button>
      {p.telemetryOpen && (
        <div className="space-y-3 border-t border-line px-3 py-3">
          {tr && (
            <div className="flex flex-wrap items-center gap-1.5">
              <Step ok label={`1. Teilfragen${fmt(lat?.rewrite)}`} />
              <Arrow />
              <Step ok label={`2. RBAC Filter (${role})${fmt(lat?.rbac)}`} />
              <Arrow />
              <Step ok={result.kind === "answer"} label={`3. Retrieval-Score ${(tr.retrievalScore ?? tr.confidence).toFixed(2)}${fmt(lat?.retrieval)}`} />
              <Arrow />
              <Step ok={tr.nliVerified || tr.verification === "extractive"} label={`4. ${grounding(result.kind, tr)}${fmt(lat && (lat.generation ?? 0) + (lat.nli ?? 0) || undefined)}`} />
            </div>
          )}
          <dl className="grid gap-px overflow-hidden rounded-lg border border-line bg-line text-xs sm:grid-cols-2">
            {fast && (
              <Cell label="Stufe 1 · Fundstellen (ohne LLM)" mono value={fast.hits.map((h, i) => `#${i + 1} ${h.docName} ${h.section.split(" ")[0]} · ${h.source} · cov ${h.coverage.toFixed(2)}${h.dense !== null ? ` · dense ${h.dense.toFixed(3)}` : ""} · Kernsatz ${h.key + 1}/${h.sentences.length}`).join("\n") || "keine"} />
            )}
            {fast && <Cell label="Stufe 1 · Fragetyp je Teilfrage" mono value={fast.trace.parts.map((x) => `${x.kind}${x.arg ? ` (${x.arg})` : ""} · cov ${x.coverage.toFixed(2)}${x.lexicalOk ? " · Boden ok" : ""}${x.densePath ? " · dense" : ""}${x.contradiction ? " · Widerspruch" : ""} — ${x.text}`).join("\n") || "—"} />}
            {tr && <Cell label="Judge je Teilfrage" mono value={(tr.parts ?? []).map((x) => `${verdict(x.features?.judge)} · cov ${x.coverage.toFixed(2)}${x.reason ? ` · ${x.reason}` : ""} — ${x.text}`).join("\n") || "—"} />}
            {tr && <Cell label="Verifier je Satz" mono value={(tr.sentences ?? []).map((x) => `${x.verified ? "OK" : "FAIL"} · ${x.source}${x.problems.length ? ` · ${x.problems.join(", ")}` : ""} — ${x.text}`).join("\n") || "keine Antwortsätze"} />}
            {tr && <Cell label="RBAC Filter" mono value={`${tr.rbacFilter}  →  ${tr.searched} von ${tr.total} Chunks durchsuchbar`} />}
            {tr && <Cell label="Engine" mono value={`${tr.engineMode ?? "—"}${tr.answerBy && tr.answerBy !== "none" ? ` · Antwort: ${tr.answerBy}` : ""}${tr.retrievalCached ? " · Retrieval aus Stufe 1" : ""}`} />}
            {lat && <Cell label="Latenz Stufe 2 (ms)" mono value={(["rewrite", "rbac", "retrieval", "generation", "nli"] as const).filter((k) => lat[k] !== undefined).map((k) => `${k} ${lat[k]}`).concat(`Σ ${lat.total}`).join(" · ")} />}
          </dl>
        </div>
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
      <dd className={`whitespace-pre-line leading-5 text-muted ${mono ? "font-mono" : ""}`}>{value}</dd>
    </div>
  );
}
