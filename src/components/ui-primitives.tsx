"use client";

import { useEffect, useRef, useState, type ButtonHTMLAttributes, type FormEvent, type ReactNode } from "react";
import { CheckCircle2, ChevronDown, ChevronRight, Loader2, Lock, Monitor, Moon, ShieldAlert, Sun, X, XCircle } from "lucide-react";
import { ROLE_META, type Role } from "@/lib/mock-store";

/* Shared UI primitives — keep ALL small elements in this single file. Colors come from semantic tokens (globals.css). */

export const inputCls =
  "w-full rounded-lg border border-line-strong bg-canvas px-3 py-2 text-sm text-fg placeholder:text-faint focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand";

type ButtonVariant = "primary" | "success" | "danger" | "ghost" | "outline";

const buttonStyles: Record<ButtonVariant, string> = {
  primary: "bg-brand text-white shadow-sm hover:bg-brand/90",
  success: "bg-ok text-white shadow-sm hover:bg-ok/90",
  danger: "bg-bad text-white hover:bg-bad/90",
  outline: "border border-line-strong bg-canvas text-fg hover:bg-raised",
  ghost: "text-muted hover:bg-raised hover:text-fg",
};

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return (
    <button
      {...props}
      className={`inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg px-3.5 py-2 text-[13px] font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/60 disabled:cursor-not-allowed disabled:opacity-40 ${buttonStyles[variant]} ${className}`}
    />
  );
}

export type BadgeTone = "neutral" | "brand" | "ok" | "warn" | "bad";

const badgeStyles: Record<BadgeTone, string> = {
  neutral: "border-line bg-raised text-muted",
  brand: "border-brand/25 bg-brand/10 text-brand-fg",
  ok: "border-ok/25 bg-ok/10 text-ok-fg",
  warn: "border-warn/30 bg-warn/10 text-warn-fg",
  bad: "border-bad/30 bg-bad/10 text-bad-fg",
};

export function Badge({ tone = "neutral", children, className = "" }: { tone?: BadgeTone; children: ReactNode; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1 whitespace-nowrap rounded-md border px-1.5 py-0.5 text-[11px] font-medium leading-4 ${badgeStyles[tone]} ${className}`}>
      {children}
    </span>
  );
}

export const roleTone = (r: Role | "SYSTEM"): BadgeTone => (r === "L3" ? "brand" : r === "L2" ? "warn" : "neutral");

export function Modal({
  open, title, onClose, children, footer, wide,
}: { open: boolean; title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-sm" onClick={onClose} role="dialog" aria-modal="true" aria-label={title}>
      <div
        className={`animate-fade-in max-h-[90vh] w-full overflow-y-auto rounded-xl border border-line bg-canvas shadow-pop ${wide ? "max-w-xl" : "max-w-md"}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-line px-5 py-3.5">
          <h2 className="text-sm font-semibold tracking-tight text-fg">{title}</h2>
          <button onClick={onClose} aria-label="Schließen" className="rounded p-1 text-faint hover:bg-raised hover:text-fg">
            <X size={15} />
          </button>
        </div>
        <div className="space-y-3 px-5 py-4 text-sm leading-6 text-muted">{children}</div>
        {footer && <div className="flex justify-end gap-2 border-t border-line px-5 py-3.5">{footer}</div>}
      </div>
    </div>
  );
}

export function SidePanel({ open, title, onClose, children }: { open: boolean; title: string; onClose: () => void; children: ReactNode }) {
  if (!open) return null;
  return (
    <>
      <div className="fixed inset-0 z-30 bg-black/30" onClick={onClose} aria-hidden />
      <aside className="animate-slide-in fixed inset-y-0 right-0 z-40 flex w-full max-w-md flex-col border-l border-line bg-canvas shadow-pop" aria-label={title}>
        <div className="flex h-14 shrink-0 items-center justify-between border-b border-line px-5">
          <h2 className="text-sm font-semibold tracking-tight text-fg">{title}</h2>
          <button onClick={onClose} aria-label="Schließen" className="rounded p-1 text-faint hover:bg-raised hover:text-fg">
            <X size={15} />
          </button>
        </div>
        <div className="flex-1 space-y-5 overflow-y-auto p-5 text-sm text-muted">{children}</div>
      </aside>
    </>
  );
}

/* ───────── Theme engine: Light / Dark / System ───────── */

type ThemeChoice = "light" | "dark" | "system";
const THEME_KEY = "pocket-ciso-theme";

const applyTheme = (c: ThemeChoice) => {
  const dark = c === "dark" || (c === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
};

export function ThemeSwitcher() {
  const [choice, setChoice] = useState<ThemeChoice | null>(null);

  useEffect(() => {
    const s = localStorage.getItem(THEME_KEY);
    setChoice(s === "light" || s === "dark" ? s : "system");
  }, []);

  useEffect(() => {
    if (!choice) return;
    applyTheme(choice);
    if (choice !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const h = () => applyTheme("system");
    mq.addEventListener("change", h);
    return () => mq.removeEventListener("change", h);
  }, [choice]);

  const opts: { v: ThemeChoice; label: string; Icon: typeof Sun }[] = [
    { v: "light", label: "Light", Icon: Sun },
    { v: "dark", label: "Dark", Icon: Moon },
    { v: "system", label: "System", Icon: Monitor },
  ];
  return (
    <div role="radiogroup" aria-label="Theme" className="inline-flex rounded-lg border border-line bg-surface p-0.5">
      {opts.map(({ v, label, Icon }) => (
        <button
          key={v}
          role="radio"
          aria-checked={choice === v}
          aria-label={label}
          title={label}
          onClick={() => {
            localStorage.setItem(THEME_KEY, v);
            setChoice(v);
          }}
          className={`rounded-md p-1.5 transition-colors ${choice === v ? "bg-canvas text-fg shadow-sm ring-1 ring-line" : "text-faint hover:text-fg"}`}
        >
          <Icon size={14} />
        </button>
      ))}
    </div>
  );
}

/* ───────── Toast ───────── */

export interface ToastMsg {
  id: number;
  tone: "ok" | "bad" | "info";
  text: string;
}

export function Toast({ toast, onDone }: { toast: ToastMsg | null; onDone: () => void }) {
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(onDone, 4500);
    return () => clearTimeout(t);
  }, [toast, onDone]);
  if (!toast) return null;
  const Icon = toast.tone === "ok" ? CheckCircle2 : toast.tone === "bad" ? XCircle : ShieldAlert;
  const color = toast.tone === "ok" ? "text-ok-fg" : toast.tone === "bad" ? "text-bad-fg" : "text-brand-fg";
  return (
    <div role="status" className="animate-fade-in fixed bottom-5 right-5 z-[60] flex max-w-sm items-start gap-2.5 rounded-xl border border-line bg-canvas px-4 py-3 text-[13px] text-fg shadow-pop">
      <Icon size={16} className={`mt-0.5 shrink-0 ${color}`} />
      <span className="leading-5">{toast.text}</span>
    </div>
  );
}

/* ───────── RBAC elevation (password prompt) ───────── */

const DEMO_HINT: Record<string, string> = { L2: "admin2026", L3: "ciso2026" };

export function ElevationModal({
  role, onClose, onSubmit,
}: { role: Role | null; onClose: () => void; onSubmit: (password: string) => Promise<string | null> }) {
  const [pw, setPw] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (role) {
      setPw("");
      setError(null);
      setHint(false);
      setTimeout(() => input.current?.focus(), 30);
    }
  }, [role]);

  if (!role) return null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const err = await onSubmit(pw);
    setBusy(false);
    if (err) {
      setError(err);
      setPw("");
      input.current?.focus();
    }
  };

  return (
    <Modal
      open
      title="RBAC Elevation"
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" type="button" onClick={onClose}>Abbrechen</Button>
          <Button type="submit" form="elevation-form" disabled={!pw || busy}>
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Lock size={14} />} Entsperren
          </Button>
        </>
      }
    >
      <form id="elevation-form" onSubmit={submit} className="space-y-3">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-line bg-surface">
            <Lock size={16} className="text-brand-fg" />
          </div>
          <div>
            <p className="text-sm font-medium text-fg">Zugriff auf {ROLE_META[role].id} entsperren</p>
            <p className="text-xs text-faint">Passwort erforderlich. Der Versuch wird im Audit Log protokolliert.</p>
          </div>
        </div>
        <input
          ref={input}
          type="password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          placeholder="Rollen-Passwort"
          aria-label="Rollen-Passwort"
          autoComplete="off"
          className={`${inputCls} ${error ? "!border-bad focus:!ring-bad" : ""}`}
        />
        {error && (
          <p role="alert" className="flex items-center gap-1.5 text-xs font-medium text-bad-fg">
            <XCircle size={13} /> {error}
          </p>
        )}
        <div>
          <button type="button" onClick={() => setHint((h) => !h)} aria-expanded={hint} className="flex items-center gap-1 text-xs text-faint hover:text-fg">
            {hint ? <ChevronDown size={12} /> : <ChevronRight size={12} />} Demo Credentials Hint
          </button>
          {hint && (
            <p className="mt-1.5 rounded-md border border-line bg-surface px-3 py-2 font-mono text-xs text-muted">
              admin2026 / ciso2026 <span className="text-faint">(hier: {DEMO_HINT[role]})</span>
            </p>
          )}
        </div>
      </form>
    </Modal>
  );
}
