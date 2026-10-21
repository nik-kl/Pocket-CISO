"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Database, GitPullRequest, Lock, LockOpen, Scale, ShieldCheck, Terminal } from "lucide-react";
import StatelessChatView from "@/components/StatelessChatView";
import CisoDiffDashboard from "@/components/CisoDiffDashboard";
import KnowledgeAndAuditView from "@/components/KnowledgeAndAuditView";
import { Badge, ElevationModal, ThemeSwitcher, Toast, roleTone, type ToastMsg } from "@/components/ui-primitives";
import {
  FALLBACK_BACKEND, L1_SESSION, ROLE_META, callApi, fetchSnapshot,
  type BackendStatus, type Role, type Session, type Snapshot,
} from "@/lib/mock-store";

type View = "chat" | "ciso" | "kb";
const SESSION_KEY = "pocket-ciso-session";
const ROLES: Role[] = ["L1", "L2", "L3"];

const CRUMBS: Record<View, [string, string]> = {
  chat: ["Workspace", "Ask Pocket CISO"],
  ciso: ["Governance", "Policy Diff Queue"],
  kb: ["Governance", "Knowledge DB & Audit Log"],
};

// App shell: owns the RBAC session, the server snapshot (single source of truth) and the active workspace.
export default function Page() {
  const [session, setSession] = useState<Session>(L1_SESSION);
  const [view, setView] = useState<View>("chat");
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [elevate, setElevate] = useState<Role | null>(null);
  const [toast, setToast] = useState<ToastMsg | null>(null);
  const [backend, setBackend] = useState<BackendStatus>(FALLBACK_BACKEND);
  const sessionRef = useRef(session);
  sessionRef.current = session;

  const notify = useCallback((text: string, tone: ToastMsg["tone"] = "info") => setToast({ id: Date.now(), tone, text }), []);

  const applySession = useCallback((s: Session) => {
    setSession(s);
    if (s.token) sessionStorage.setItem(SESSION_KEY, JSON.stringify(s));
    else sessionStorage.removeItem(SESSION_KEY);
  }, []);

  const refresh = useCallback(async () => {
    const token = sessionRef.current.token;
    const s = await fetchSnapshot(token);
    if (!s) return notify("Backend nicht erreichbar.", "bad");
    if (token && s.session.role !== sessionRef.current.role) {
      applySession(L1_SESSION);
      notify("Sitzung abgelaufen — zurück auf L1.", "bad");
    }
    setSnap(s);
    setBackend(s.backend ?? FALLBACK_BACKEND);
  }, [applySession, notify]);

  // Live backend indicator: Python/Ollama can be started or stopped at any time.
  useEffect(() => {
    const t = setInterval(async () => {
      const r = await callApi<{ backend: BackendStatus }>("health");
      if (r.ok) setBackend(r.backend);
    }, 8000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(SESSION_KEY);
      if (raw) {
        const s = JSON.parse(raw) as Session;
        if (s.token && ROLES.includes(s.role)) {
          setSession(s);
          sessionRef.current = s;
        }
      }
    } catch {}
    void refresh();
  }, [refresh]);

  const logout = useCallback(async () => {
    const token = sessionRef.current.token;
    if (token) await callApi("logout", {}, token);
    applySession(L1_SESSION);
    sessionRef.current = L1_SESSION;
    await refresh();
    notify("Auf L1: Employee zurückgesetzt.", "info");
  }, [applySession, notify, refresh]);

  const pickRole = (r: Role) => {
    if (r === session.role) return;
    if (r === "L1") void logout();
    else setElevate(r);
  };

  const submitElevation = async (password: string): Promise<string | null> => {
    const res = await callApi<{ token: string; session: Omit<Session, "token"> }>("authenticate", { role: elevate, password }, session.token);
    if (!res.ok) {
      void refresh();
      return res.error;
    }
    const next: Session = { ...res.session, token: res.token };
    applySession(next);
    sessionRef.current = next;
    setElevate(null);
    await refresh();
    notify(`${ROLE_META[next.role].id} entsperrt.`, "ok");
    return null;
  };

  const pending = snap?.pendingCount ?? 0;
  const navItem = (active: boolean) =>
    `flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] font-medium transition-colors ${
      active ? "bg-raised text-fg ring-1 ring-inset ring-line-strong/70" : "text-muted hover:bg-raised/60 hover:text-fg"
    }`;
  const icon = (active: boolean) => (active ? "text-brand-fg" : "text-faint");

  const py = backend.mode === "python";
  const backendLabel = py
    ? backend.ollama
      ? "Python FastAPI + Ollama (Connected)"
      : "Python FastAPI · Rule Engine (Ollama offline)"
    : "Local Fallback Engine (Active)";
  const backendTone = py && backend.ollama ? "ok" : py ? "warn" : "neutral";
  const status = [
    { k: "Vector DB", v: py ? `NumPy Store (${backend.vectorCount ?? 0} Vektoren)` : "Keyword Index (Local)" },
    { k: "Inference", v: py && backend.ollama && backend.llmModel ? `${backend.llmModel} (Temp 0.0)` : "Rule-based (Temp 0.0)" },
    { k: "Mode", v: "Stateless Zero-Retention" },
    { k: "Collection", v: `${snap?.collection ?? "policies_live_v1"} (${snap?.totalChunks ?? "…"} Chunks)` },
  ];

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-canvas text-fg antialiased selection:bg-brand/30">
      <aside className="flex w-64 shrink-0 flex-col justify-between gap-4 overflow-y-auto border-r border-line bg-surface p-4">
        <div className="space-y-6">
          <div className="flex items-center gap-2.5 px-1">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-b from-indigo-400 to-indigo-600 shadow-lg shadow-indigo-500/20">
              <ShieldCheck size={17} className="text-white" />
            </div>
            <div className="min-w-0">
              <div className="text-sm font-semibold tracking-tight text-fg">Pocket CISO</div>
              <div className="font-mono text-[10px] text-faint">v0.4 On-Prem</div>
            </div>
          </div>

          <nav aria-label="Workspaces" className="space-y-1">
            <div className="px-2.5 pb-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">Workspaces</div>
            <button className={navItem(view === "chat")} onClick={() => setView("chat")} aria-current={view === "chat" ? "page" : undefined}>
              <Terminal size={15} className={icon(view === "chat")} />
              <span className="flex-1">Ask Pocket CISO</span>
              <span className="font-mono text-[10px] text-faint">B</span>
            </button>
            <button className={navItem(view === "ciso")} onClick={() => setView("ciso")} aria-current={view === "ciso" ? "page" : undefined}>
              <GitPullRequest size={15} className={icon(view === "ciso")} />
              <span className="flex-1">Policy Diff &amp; Sign-Off</span>
              {pending > 0 ? (
                <span className="flex h-5 min-w-5 items-center justify-center rounded-full bg-warn/15 px-1.5 text-[11px] font-semibold text-warn-fg ring-1 ring-inset ring-warn/30" title={`${pending} Pending Policy Diff${pending > 1 ? "s" : ""}`}>
                  {pending}
                  <span className="sr-only"> Pending Policy Diff{pending > 1 ? "s" : ""}</span>
                </span>
              ) : (
                <span className="font-mono text-[10px] text-faint">A</span>
              )}
            </button>
            <button className={navItem(view === "kb")} onClick={() => setView("kb")} aria-current={view === "kb" ? "page" : undefined}>
              <Database size={15} className={icon(view === "kb")} />
              <span className="flex-1">Knowledge DB &amp; Audit Log</span>
            </button>
          </nav>

          <section className="rounded-lg border border-line bg-canvas p-3" aria-label="System status">
            <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">System Status</div>
            <div
              data-testid="backend-status"
              data-mode={backend.mode}
              title={backend.engineMode}
              className={`mb-2.5 flex items-start gap-2 rounded-md border px-2 py-1.5 text-[11px] leading-4 ${
                backendTone === "ok" ? "border-ok/30 bg-ok/10 text-ok-fg" : backendTone === "warn" ? "border-warn/30 bg-warn/10 text-warn-fg" : "border-line bg-surface text-muted"
              }`}
            >
              <span className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${backendTone === "ok" ? "bg-ok shadow-[0_0_6px_var(--ok)]" : backendTone === "warn" ? "bg-warn" : "bg-faint"}`} />
              <span>
                <span className="opacity-70">Backend: </span>
                <span className="font-medium">{backendLabel}</span>
              </span>
            </div>
            <ul className="space-y-1.5">
              {status.map((r) => (
                <li key={r.k} className="flex items-start gap-2 text-[11px] leading-4">
                  <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-ok shadow-[0_0_6px_var(--ok)]" />
                  <span>
                    <span className="text-faint">{r.k}: </span>
                    <span className="font-mono text-muted">{r.v}</span>
                  </span>
                </li>
              ))}
            </ul>
          </section>
        </div>

        <section className="space-y-1.5" aria-label="Active RBAC identity">
          <div className="px-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-faint">Active RBAC Identity</div>
          <div role="radiogroup" aria-label="Rolle wählen" className="space-y-1 rounded-xl border border-line bg-canvas p-1.5">
            {ROLES.map((r) => {
              const active = r === session.role;
              return (
                <div key={r} className="relative">
                  <button
                    role="radio"
                    aria-checked={active}
                    aria-label={`${ROLE_META[r].id} (${ROLE_META[r].sub})`}
                    title={`${r} → ${ROLE_META[r].group} (Entra ID / OIDC Gruppe)`}
                    onClick={() => pickRole(r)}
                    className={`flex w-full items-center gap-2.5 rounded-lg border px-2.5 py-2 text-left transition-colors ${
                      active ? "border-line-strong bg-raised text-fg" : "border-transparent text-muted hover:bg-raised/60 hover:text-fg"
                    }`}
                  >
                    <span className={`h-2 w-2 shrink-0 rounded-full ${active ? "bg-ok shadow-[0_0_8px_var(--ok)]" : "bg-line-strong"}`} />
                    <span className="min-w-0 flex-1">
                      <span className="block text-[13px] font-medium leading-4">{ROLE_META[r].id}</span>
                      <span className="block text-[11px] leading-4 text-faint">{ROLE_META[r].sub}</span>
                      {active && <span className="block truncate font-mono text-[10px] leading-4 text-brand-fg">{ROLE_META[r].group}</span>}
                    </span>
                    {!active && ROLE_META[r].needsPassword && <Lock size={12} className="text-faint" />}
                    {active && r !== "L1" && <span className="w-5" aria-hidden />}
                  </button>
                  {active && r !== "L1" && (
                    <button
                      onClick={() => void logout()}
                      aria-label="Sperren und zu L1 wechseln"
                      title="Lock / Logout to L1"
                      className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md p-1.5 text-faint hover:bg-canvas hover:text-bad-fg"
                    >
                      <LockOpen size={13} />
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      </aside>

      <div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b border-line bg-canvas/80 px-6 backdrop-blur">
          <div className="flex items-center gap-2 text-[13px]">
            <span className="text-faint">{CRUMBS[view][0]}</span>
            <span className="text-line-strong">/</span>
            <span className="font-medium text-fg">{CRUMBS[view][1]}</span>
          </div>
          <div className="flex items-center gap-2">
            <span
              className="hidden items-center gap-1.5 rounded-full border border-line bg-surface px-2.5 py-1 text-[11px] text-muted xl:inline-flex"
              title="Pocket CISO Assistenzsystem — Keine rechtsverbindliche Freigabe. Antworten basieren ausschließlich auf freigegebenen Dokumenten."
            >
              <Scale size={12} className="text-faint" />
              Assistenzsystem — Keine rechtsverbindliche Freigabe
            </span>
            <ThemeSwitcher />
            <span title={`${session.role} → ${ROLE_META[session.role].group} (Entra ID / OIDC Gruppe)`}>
              <Badge tone={roleTone(session.role)} className="!px-2 !py-1">
                <span className="font-mono">{session.role}</span> Clearance · {session.clearance}
                <span className="hidden border-l border-current/20 pl-1.5 font-mono text-[10px] opacity-80 2xl:inline">{ROLE_META[session.role].group}</span>
              </Badge>
            </span>
          </div>
        </header>

        <main className="min-h-0 flex-1 overflow-hidden">
          {view === "chat" && <StatelessChatView session={session} snap={snap} notify={notify} onElevate={setElevate} />}
          {view === "ciso" && <CisoDiffDashboard session={session} snap={snap} refresh={refresh} notify={notify} />}
          {view === "kb" && <KnowledgeAndAuditView session={session} snap={snap} refresh={refresh} notify={notify} />}
        </main>
      </div>

      <ElevationModal role={elevate} onClose={() => setElevate(null)} onSubmit={submitElevation} />
      <Toast toast={toast} onDone={() => setToast(null)} />
    </div>
  );
}
