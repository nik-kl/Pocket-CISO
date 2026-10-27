// SERVER ONLY. Zero-touch connector: watched inbox folder (default data/inbox) → staging queue.
import { promises as fs } from "fs";
import path from "path";
import crypto from "crypto";
import { type SyncReport } from "../mock-store";
import { appendAudit } from "./audit";
import { authorize } from "./auth";
import { LOCAL_ENGINE } from "./search";
import { safeDocName, stageInTx } from "./staging";
import { transact } from "./store";
import { Actor, Db, R, isFail, DATA_DIR } from "./types";

/* ── Zero-touch connector: data/inbox/ → staging queue (mirrors engine/inbox.py → sync_watched_inbox) ── */

const INBOX_DIR = process.env.WATCHED_INBOX_DIR ?? path.join(DATA_DIR, "inbox"); // gitignored with data/
const INBOX_EXT = /\.(pdf|docx|md|txt)$/i;
const TEXT_EXT = /\.(md|txt)$/i;
const MAX_INBOX_BYTES = 10 * 1024 * 1024;
const CONNECTOR: Actor = { email: "auto-connector@watched_inbox", role: "L2" };
const CONNECTOR_AUTHOR = "Auto-Connector (watched_inbox)";

type IngestOutcome =
  | { kind: "staged"; file: string; diffId: string; conflicts: number }
  | { kind: "skipped"; file: string; reason: string }
  | { kind: "failed"; file: string; error: string };

async function listInbox(): Promise<string[]> {
  const entries = await fs.readdir(INBOX_DIR, { withFileTypes: true }).catch(() => []);
  return entries.filter((e) => e.isFile() && !e.name.startsWith(".") && INBOX_EXT.test(e.name)).map((e) => e.name).sort();
}

// One transaction per file: dedupe by hash / pending diff → stage → remember hash → audit.
function ingestInTx(db: Db, file: string, digest: string, text: string, trigger: string): IngestOutcome {
  const docName = safeDocName(file);
  if (db.meta.ingestedFiles[file] === digest) return { kind: "skipped", file, reason: "unverändert seit dem letzten Sync" };
  if (db.staged_diffs.some((d) => d.status === "pending" && d.docName === docName)) return { kind: "skipped", file, reason: "bereits in der Staging-Queue" };

  const out = stageInTx(db, CONNECTOR, { docName, title: "", text, minRole: "L2", author: CONNECTOR_AUTHOR, trustedAuthor: true });
  if (isFail(out) && out.code === "NO_CHANGES") {
    db.meta.ingestedFiles[file] = digest;
    return { kind: "skipped", file, reason: "identisch mit der Live-Version" };
  }
  if (isFail(out)) return { kind: "failed", file, error: out.error };

  db.meta.ingestedFiles[file] = digest;
  appendAudit(db, {
    actor: CONNECTOR.email, role: "SYSTEM", action: "AUTO_CONNECTOR_SYNC", target: docName, status: "SUCCESS",
    detail: `${file} → ${out.diff.id} (SHA-256 ${digest.slice(0, 12)}…, ${out.diff.hunks.length} Chunk(s), ${out.diff.conflicts.length} Konflikt(e)) · Trigger: ${trigger}`,
  });
  return { kind: "staged", file, diffId: out.diff.id, conflicts: out.diff.conflicts.length };
}

async function ingestInboxFile(file: string, trigger: string): Promise<IngestOutcome> {
  if (!TEXT_EXT.test(file)) return { kind: "failed", file, error: "PDF/DOCX-Parsing benötigt das Python-Backend." };
  const full = path.join(INBOX_DIR, file);
  if ((await fs.stat(full)).size > MAX_INBOX_BYTES) return { kind: "failed", file, error: "Datei größer als 10 MB." };
  const data = await fs.readFile(full);
  const digest = crypto.createHash("sha256").update(data).digest("hex");
  const text = data.toString("utf8").replace(/^\uFEFF/, "");
  return transact((db) => ingestInTx(db, file, digest, text, trigger));
}

export async function syncWatchedInbox(token: string | null): Promise<R<{ report: SyncReport }>> {
  const actor = await transact((db) => authorize(db, token, "L2", "sync_inbox", "watched_inbox"));
  if (isFail(actor)) return actor;

  const files = await listInbox();
  const report: SyncReport = { inbox: "data/inbox", scanned: files.length, staged: [], skipped: [], failed: [], engineMode: LOCAL_ENGINE };
  for (const file of files) {
    const out = await ingestInboxFile(file, `${actor.email} (${actor.role})`).catch((e): IngestOutcome => {
      console.error("[watched_inbox]", file, e);
      return { kind: "failed", file, error: "Datei konnte nicht verarbeitet werden." };
    });
    if (out.kind === "staged") report.staged.push({ file, diffId: out.diffId, conflicts: out.conflicts });
    if (out.kind === "skipped") report.skipped.push({ file, reason: out.reason });
    if (out.kind === "failed") report.failed.push({ file, error: out.error });
  }
  return { ok: true as const, report };
}
