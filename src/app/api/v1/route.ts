// Unified REST gateway. GET = state snapshot, POST = { action, ...payload } (JSON) or multipart (stage_file).
//
// Hybrid resilience: query / stage / sync_inbox / approve are first proxied to the Python FastAPI + Ollama backend
// (PYTHON_BACKEND_URL, default http://127.0.0.1:8000). If it is not running, the identical action is served by the
// local TypeScript engine in src/lib/db.ts, so the UI works out-of-the-box with or without Python/Ollama.
// Authentication, tickets (create/resolve), reject, audit export and snapshots always run locally (same JSON DB, shared via file lock).
import { NextResponse, type NextRequest } from "next/server";
import * as db from "@/lib/db";
import { FALLBACK_BACKEND, type BackendStatus, type Role } from "@/lib/mock-store";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const PY = (process.env.PYTHON_BACKEND_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const HEALTH_TIMEOUT_MS = 3000;
const QUERY_TIMEOUT_MS = 25_000;
const STAGE_TIMEOUT_MS = 90_000;
const APPROVE_TIMEOUT_MS = 15_000;
const SYNC_TIMEOUT_MS = 120_000;
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

type PyRes = { state: "offline" } | { state: "timeout" } | { state: "ok"; status: number; body: unknown };
type Json = Record<string, unknown>;

async function py(path: string, init: RequestInit, timeoutMs: number): Promise<PyRes> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(`${PY}${path}`, { ...init, signal: ctl.signal, cache: "no-store" });
    return { state: "ok", status: r.status, body: await r.json().catch(() => null) };
  } catch (e) {
    return (e as Error).name === "AbortError" ? { state: "timeout" } : { state: "offline" };
  } finally {
    clearTimeout(timer);
  }
}

/* ── backend status (cached so snapshots stay cheap) ── */

let healthCache: { at: number; status: BackendStatus } | null = null;

async function backendStatus(force = false): Promise<BackendStatus> {
  if (!force && healthCache && Date.now() - healthCache.at < 4000) return healthCache.status;
  const r = await py("/health", {}, HEALTH_TIMEOUT_MS);
  let status = FALLBACK_BACKEND;
  if (r.state === "ok" && r.status === 200 && r.body && (r.body as Json).python === true) {
    const b = r.body as { ollama?: { connected?: boolean; llmModel?: string | null; embedModel?: string | null }; vectorCount?: number; engineMode?: string };
    status = {
      mode: "python",
      ollama: !!b.ollama?.connected,
      llmModel: b.ollama?.llmModel ?? null,
      embedModel: b.ollama?.embedModel ?? null,
      vectorCount: typeof b.vectorCount === "number" ? b.vectorCount : null,
      engineMode: b.engineMode ?? "Python FastAPI",
    };
  }
  healthCache = { at: Date.now(), status };
  return status;
}

/* ── helpers ── */

const bearer = (req: NextRequest) => {
  const h = req.headers.get("authorization");
  return h?.startsWith("Bearer ") ? h.slice(7).trim() : null;
};

const NO_STORE = { "Cache-Control": "no-store" };

const reply = (out: { ok: boolean; status?: number; code?: string; error?: string }, served: "python" | "local" = "local") =>
  out.ok
    ? NextResponse.json({ ...out, status: undefined, served }, { headers: NO_STORE })
    : NextResponse.json({ ok: false, code: out.code, error: out.error, served }, { status: out.status ?? 400, headers: NO_STORE });

const configurationReply = (error: unknown): NextResponse | null => {
  if (!(error instanceof Error)) return null;
  if (!/POCKET_CISO|Auth-Secret|Nicht-Demo-Modus/.test(error.message)) return null;
  return reply({ ok: false, status: 503, code: "AUTH_CONFIG_MISSING", error: error.message });
};

/**
 * Try the Python backend. Returns a response when Python handled the request (success or authoritative error),
 * or null when the caller should use the local engine.
 * `retryLocal`: whether a timeout / 5xx may be re-served locally (safe for read-only queries only).
 */
async function viaPython(path: string, init: RequestInit, timeoutMs: number, retryLocal: boolean): Promise<NextResponse | null> {
  await db.ensureDb(); // Python reads users/secret/chunks from the shared JSON file
  const r = await py(path, init, timeoutMs);
  if (r.state === "offline") {
    healthCache = { at: Date.now(), status: FALLBACK_BACKEND };
    return null;
  }
  if (r.state === "timeout") {
    return retryLocal ? null : reply({ ok: false, status: 504, code: "PYTHON_TIMEOUT", error: "Das Python-Backend antwortet nicht rechtzeitig. Bitte Status prüfen und erneut versuchen." }, "python");
  }
  if (r.status >= 500 && retryLocal) return null;
  const body = r.body as Json | null;
  if (!body || typeof body.ok !== "boolean") return reply({ ok: false, status: 502, code: "PYTHON_BAD_RESPONSE", error: "Ungültige Antwort des Python-Backends." }, "python");
  return reply({ ...(body as { ok: boolean }), status: r.status } as { ok: boolean; status: number }, "python");
}

const jsonInit = (token: string | null, body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  body: JSON.stringify(body),
});

/* ── GET: snapshot (+ live backend status) ── */

export async function GET(req: NextRequest) {
  try {
    const [snap, backend] = await Promise.all([db.getSnapshot(bearer(req)), backendStatus()]);
    return NextResponse.json({ ...snap, backend }, { headers: NO_STORE });
  } catch (e) {
    console.error("[api/v1 GET]", e);
    return configurationReply(e) ?? NextResponse.json({ ok: false, code: "INTERNAL", error: "Interner Fehler." }, { status: 500 });
  }
}

/* ── POST: multipart stage_file ── */

async function stageFile(form: FormData, token: string | null): Promise<NextResponse> {
  const file = form.get("file");
  const text = String(form.get("text") ?? "");
  const minRole = String(form.get("minRole") ?? "L1");
  const replaces = String(form.get("replaces") ?? "").trim();
  const title = String(form.get("title") ?? "").trim();
  const author = String(form.get("author") ?? "").trim();
  const isFile = file instanceof File && file.size > 0;
  if (!isFile && !text.trim()) return reply({ ok: false, status: 400, code: "NO_INPUT", error: "Datei oder Text erforderlich." });
  if (isFile && file.size > MAX_UPLOAD_BYTES) return reply({ ok: false, status: 413, code: "TOO_LARGE", error: "Datei größer als 10 MB." });

  const fwd = new FormData();
  if (isFile) fwd.set("file", file, file.name);
  else fwd.set("text", text);
  fwd.set("docName", String(form.get("docName") ?? "Eingefuegter_Text.md"));
  for (const [k, v] of [["minRole", minRole], ["replaces", replaces], ["title", title], ["author", author]] as const) if (v) fwd.set(k, v);

  const proxied = await viaPython("/stage-file", { method: "POST", headers: token ? { Authorization: `Bearer ${token}` } : {}, body: fwd }, STAGE_TIMEOUT_MS, false);
  if (proxied) return proxied;

  // Local engine: plain-text formats only (PDF/DOCX parsing lives in the Python backend).
  let content = text;
  let docName = String(form.get("docName") ?? "Eingefuegter_Text.md");
  if (isFile) {
    docName = file.name.replace(/\\/g, "/").split("/").pop()!.replace(/[^\w.\- ]/g, "_").slice(0, 120);
    if (!/\.(md|txt)$/i.test(docName))
      return reply({ ok: false, status: 415, code: "NEEDS_PYTHON", error: "PDF/DOCX-Parsing benötigt das Python-Backend (python backend/main.py). Offline werden .md und .txt unterstützt." });
    content = await file.text();
  }
  return reply(await db.stageDocument(token, { docName, title, text: content, minRole: minRole as Role, replaces: replaces || null, author: author || undefined }));
}

/* ── POST ── */

export async function POST(req: NextRequest) {
  const token = bearer(req);
  try {
    if ((req.headers.get("content-type") ?? "").startsWith("multipart/form-data")) {
      const form = await req.formData();
      if (form.get("action") !== "stage_file") return reply({ ok: false, status: 400, code: "UNKNOWN_ACTION", error: "Unbekannte Aktion." });
      return await stageFile(form, token);
    }

    let body: Record<string, unknown>;
    try {
      body = await req.json();
    } catch {
      return reply({ ok: false, status: 400, code: "BAD_JSON", error: "Ungültiger Request-Body." });
    }
    const s = (k: string) => (typeof body[k] === "string" ? (body[k] as string) : "");

    switch (body.action) {
      case "health": return NextResponse.json({ ok: true, backend: await backendStatus(true) }, { headers: NO_STORE });
      case "authenticate": return reply(await db.authenticate(token, s("role"), s("password")));
      case "logout": return reply(await db.logout(token));
      case "create_ticket": return reply(await db.createTicket(token, s("question"), s("reason")));
      case "create_sync_ticket": return reply(await db.createSyncTicket(token, s("diffId"), s("conflictId")));
      case "resolve_ticket": return reply(await db.resolveTicket(token, s("ticketId"), s("resolutionNote")));
      case "reject": return reply(await db.rejectDiff(token, s("diffId"), s("reason")));
      case "reset_demo": return reply(await db.resetDemo(token));
      case "export_report": return reply(await db.exportReport(token));

      case "query":
        return (await viaPython("/query", jsonInit(token, { question: s("question") }), QUERY_TIMEOUT_MS, true)) ?? reply(await db.runQuery(token, s("question")));

      case "stage_policy": {
        const payload = { title: s("title"), targetDoc: s("targetDoc"), newText: s("newText"), minRole: s("minRole") };
        return (await viaPython("/stage-file", jsonInit(token, payload), STAGE_TIMEOUT_MS, false)) ?? reply(await db.stagePolicy(token, payload));
      }

      case "sync_inbox":
        return (await viaPython("/sync-inbox", jsonInit(token, {}), SYNC_TIMEOUT_MS, false)) ?? reply(await db.syncWatchedInbox(token));

      case "approve":
        return (
          (await viaPython(`/approve/${encodeURIComponent(s("diffId"))}`, jsonInit(token, { override: body.override === true }), APPROVE_TIMEOUT_MS, false)) ??
          reply(await db.approveDiff(token, s("diffId"), body.override === true))
        );

      default: return reply({ ok: false, status: 400, code: "UNKNOWN_ACTION", error: "Unbekannte Aktion." });
    }
  } catch (e) {
    console.error("[api/v1 POST]", e);
    return configurationReply(e) ?? reply({ ok: false, status: 500, code: "INTERNAL", error: "Interner Fehler." });
  }
}
