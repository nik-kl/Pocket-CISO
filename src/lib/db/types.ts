// SERVER ONLY. DB schema types, result helpers, paths and mode flags shared by the local engine (src/lib/db/*).
import path from "path";
import crypto from "crypto";
import { type AuditLog, type LiveChunk, type Role, type StagedDiff, type Ticket } from "../mock-store";

/* ───────────────────────── Types ───────────────────────── */

interface DbUser {
  email: string;
  role: Role;
  clearance: string;
  requiresPassword: boolean;
  passwordHash: string | null;
}

export interface Meta {
  liveVersion: number;
  nextTicket: number;
  nextAudit: number;
  seededAt: string;
  ingestedFiles: Record<string, string>; // watched_inbox filename → SHA-256 of the last ingested content
  auditAnchor?: string; // HMAC(secret, count|headHash): makes truncating the audit tail detectable without the secret
}

export interface Db {
  meta: Meta;
  users: DbUser[];
  live_chunks: LiveChunk[];
  staged_diffs: StagedDiff[];
  audit_logs: AuditLog[];
  tickets: Ticket[];
}

export type Fail = { ok: false; status: number; code: string; error: string };
export type R<T extends object> = ({ ok: true } & T) | Fail;
export const fail = (status: number, code: string, error: string): Fail => ({ ok: false, status, code, error });
export const isFail = (x: unknown): x is Fail => !!x && typeof x === "object" && (x as Fail).ok === false;

export interface Actor {
  email: string;
  role: Role;
}

export const COLLECTION = "policies_live_v1";
export const GENESIS = "0".repeat(64);
export const AUDIT_SNAPSHOT_LIMIT = 200; // full log is available via export_report; the chain is always verified over all entries
export const TOKEN_TTL_MS = 8 * 3600 * 1000;
export const DB_PATH = path.join(process.env.POCKET_CISO_DATA_DIR ?? path.join(process.cwd(), "data"), "pocket-ciso-db.json");
export const DATA_DIR = path.dirname(DB_PATH);
export const AUTH_SECRET_PATH = path.join(DATA_DIR, ".auth-secret");
const IS_PRODUCTION = process.env.NODE_ENV === "production" || process.env.POCKET_CISO_ENV === "production";
export const DEMO_MODE = !IS_PRODUCTION && (process.env.POCKET_CISO_DEMO === "1" || process.env.POCKET_CISO_DEMO_MODE === "1");

export const sha256 = (s: string) => crypto.createHash("sha256").update(s, "utf8").digest("hex");
export const uid = (prefix: string) => `${prefix}-${crypto.randomBytes(4).toString("hex")}`;
export const nowIso = () => new Date().toISOString();
export const ago = (hours: number) => new Date(Date.now() - hours * 3600 * 1000).toISOString();

export const g = globalThis as unknown as {
  __pcQueue?: Promise<unknown>;
  __pcFails?: Record<string, number[]>;
  __pcDenials?: Record<string, { at: number; n: number }>;
  __pcCred?: Partial<Record<"L2" | "L3", { salt: Buffer; hash: Buffer }>>;
};
