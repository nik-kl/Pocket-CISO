// SERVER ONLY. Stateless HMAC session tokens, RBAC checks, sampled denial audit, login with backoff.
import crypto from "crypto";
import { ROLE_RANK, type AuditLog, type Role } from "../mock-store";
import { appendAudit } from "./audit";
import { authSecret, credential } from "./credentials";
import { transact } from "./store";
import { Actor, Db, Fail, R, TOKEN_TTL_MS, fail, g } from "./types";

/* ───────────────────────── Auth (HMAC session tokens) ───────────────────────── */

const mac = (secret: string, payload: string) => crypto.createHmac("sha256", secret).update(payload).digest("hex");

function signToken(secret: string, role: Role): string {
  const payload = `${role}.${Date.now() + TOKEN_TTL_MS}`;
  return `${payload}.${mac(secret, payload)}`;
}

function verifyToken(secret: string, token: string | null): Role | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [role, exp, sig] = parts;
  if ((role !== "L2" && role !== "L3") || !exp || !sig) return null;
  const given = Buffer.from(sig, "utf8");
  const expected = Buffer.from(mac(secret, `${role}.${exp}`), "utf8");
  if (given.length !== expected.length || !crypto.timingSafeEqual(given, expected)) return null;
  return Number(exp) > Date.now() ? role : null;
}

export function actorFor(db: Db, token: string | null): Actor {
  const role = verifyToken(authSecret(), token) ?? "L1";
  return { email: db.users.find((u) => u.role === role)!.email, role };
}

// Denials are sampled: at most one audit entry per (actor, action, target-prefix) per minute; the next entry reports how many were dropped.
// This keeps anonymous callers from growing / rewriting the audit file at will while every distinct denial class stays visible.
const DENIAL_WINDOW_MS = 60_000;
export function appendDenial(db: Db, e: { actor: string; role: AuditLog["role"]; action: string; target: string; detail: string }): void {
  const state = (g.__pcDenials ??= {});
  const key = `${e.actor}|${e.action}|${e.target.split(":")[0]}`;
  const now = Date.now();
  const prev = state[key];
  if (prev && now - prev.at < DENIAL_WINDOW_MS) {
    prev.n += 1;
    return;
  }
  state[key] = { at: now, n: 0 };
  const dropped = prev?.n ? ` (+${prev.n} gleichartige Ablehnungen unterdrückt)` : "";
  appendAudit(db, { ...e, status: "DENIED", detail: `${e.detail}${dropped}` });
}

// Returns the actor, or a Fail (and an audit entry) when the role is insufficient.
export function authorize(db: Db, token: string | null, need: Role, action: string, target: string): Actor | Fail {
  const a = actorFor(db, token);
  if (ROLE_RANK[a.role] >= ROLE_RANK[need]) return a;
  appendDenial(db, { actor: a.email, role: a.role, action: "ACCESS_DENIED", target: `${action}:${target}`, detail: `Erfordert ${need}` });
  return fail(403, "FORBIDDEN", `Diese Aktion erfordert Clearance ${need}.`);
}

const LOGIN_WINDOW_MS = 15 * 60_000;

// Per-(client, role) limit with exponential backoff, plus a higher per-account ceiling so an attacker cannot trivially lock out L3.
function loginRetryAfterMs(client: string, role: string): number {
  const now = Date.now();
  const fails = (g.__pcFails ??= {});
  const check = (key: string, limit: number) => {
    const recent = (fails[key] ??= []).filter((t) => now - t < LOGIN_WINDOW_MS);
    fails[key] = recent;
    if (recent.length < limit) return 0;
    const backoff = Math.min(LOGIN_WINDOW_MS, 30_000 * 2 ** (recent.length - limit));
    return Math.max(0, recent[recent.length - 1] + backoff - now);
  };
  return Math.max(check(`ip:${client}|${role}`, client === "direct" ? 30 : 5), check(`acct:${role}`, 30)); // "direct" = no trusted proxy: one shared bucket, so only the account ceiling applies
}

export function authenticate(token: string | null, role: string, password: string, client = "local"): Promise<R<{ token: string; session: { role: Role; email: string; clearance: string } }>> {
  return transact((db) => {
    const requester = actorFor(db, token);
    const user = db.users.find((u) => u.role === role && u.requiresPassword);
    if (!user || (user.role !== "L2" && user.role !== "L3")) return fail(400, "BAD_ROLE", "Für diese Rolle ist keine Anmeldung möglich.");

    const wait = loginRetryAfterMs(client, user.role);
    if (wait > 0) return fail(429, "RATE_LIMITED", `Zu viele Fehlversuche. Bitte ${Math.ceil(wait / 1000)} s warten.`);

    const cred = credential(user.role);
    const given = crypto.scryptSync(typeof password === "string" ? password.slice(0, 256) : "", cred.salt, 64);
    if (!crypto.timingSafeEqual(given, cred.hash)) {
      const fails = g.__pcFails!;
      for (const key of [`ip:${client}|${user.role}`, `acct:${user.role}`]) (fails[key] ??= []).push(Date.now());
      appendAudit(db, { actor: requester.email, role: requester.role, action: "AUTH_ELEVATION_FAILED", target: `→ ${user.role} (${user.email})`, status: "FAILED", detail: "Ungültiges Passwort" });
      return fail(401, "INVALID_CREDENTIALS", "Falsches Passwort.");
    }
    g.__pcFails![`ip:${client}|${user.role}`] = [];
    appendAudit(db, { actor: user.email, role: user.role, action: "AUTH_ELEVATION_SUCCESS", target: `→ ${user.role} (${user.clearance})`, status: "SUCCESS", detail: `Sitzung ${TOKEN_TTL_MS / 3600000} h gültig` });
    return { ok: true as const, token: signToken(authSecret(), user.role), session: { role: user.role, email: user.email, clearance: user.clearance } };
  });
}

export function logout(token: string | null): Promise<R<object>> {
  return transact((db) => {
    const a = actorFor(db, token);
    if (a.role !== "L1") appendAudit(db, { actor: a.email, role: a.role, action: "AUTH_LOGOUT", target: "→ L1", status: "SUCCESS", detail: "Sitzung beendet" });
    return { ok: true as const };
  });
}

export function checkRole(token: string | null, need: Role): Promise<R<{ authorized: true }>> {
  return transact((db) => {
    const a = actorFor(db, token);
    return ROLE_RANK[a.role] >= ROLE_RANK[need]
      ? { ok: true as const, authorized: true as const }
      : fail(403, "FORBIDDEN", `Diese Aktion erfordert Clearance ${need}.`);
  });
}
