// SERVER ONLY. Public API of the local engine: every function is one atomic transaction and maps 1:1 to an /api/v1
// action (src/app/api/v1/route.ts). Zero native deps: fs/promises + path + crypto.

export { sha256 } from "./types";
export type { Db, R } from "./types";
export { ensureDb } from "./store";
export { authenticate, checkRole, logout } from "./auth";
export { runQuery } from "./search";
export { resolveTicket, createTicket, createSyncTicket } from "./tickets";
export { safeDocName, stageDocument, stagePolicy, approveDiff, rejectDiff } from "./staging";
export { chunkDocument } from "./chunk";
export { syncWatchedInbox } from "./inbox";
export { getSnapshot, exportReport, resetDemo } from "./report";
