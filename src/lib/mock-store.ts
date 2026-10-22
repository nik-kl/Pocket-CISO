// Shared TypeScript contract (client + server) and the browser-side API client.
// Mirrors the future FastAPI response models. NEVER import server-only modules here.

/* ───────────────────────── Roles ───────────────────────── */

export type Role = "L1" | "L2" | "L3";

export const ROLE_RANK: Record<Role, number> = { L1: 1, L2: 2, L3: 3 };
export const hasRole = (have: Role, need: Role) => ROLE_RANK[have] >= ROLE_RANK[need];

// `group` = Enterprise Directory group (Entra ID / OIDC) that maps to the RBAC level in a production SSO setup.
export const ROLE_META: Record<Role, { id: string; sub: string; label: string; needsPassword: boolean; group: string }> = {
  L1: { id: "L1: Employee", sub: "Marketing", label: "Employee", needsPassword: false, group: "oidc:SG-All-Staff" },
  L2: { id: "L2: IT-Admin", sub: "Infrastructure", label: "IT-Admin", needsPassword: true, group: "oidc:SG-IT-Infrastructure" },
  L3: { id: "L3: CISO", sub: "Admin & Approval", label: "CISO", needsPassword: true, group: "oidc:SG-CISO-Governance" },
};

export interface Session {
  role: Role;
  email: string;
  clearance: string;
  token: string | null;
}

export const L1_SESSION: Session = { role: "L1", email: "employee@company.at", clearance: "Employee", token: null };

/* ───────────────────────── Database entities ───────────────────────── */

export interface LiveChunk {
  id: string;
  docName: string;
  section: string;
  minRole: Role;
  content: string;
  updatedAt: string;
  sha256: string;
  approvedBy?: string;
  // Exact facts extracted by the Python engine at approval time (table cells, value sentences); absent for Node-side approvals.
  facts?: ChunkFact[];
  factsVersion?: number;
}

export interface ChunkFact {
  span: number; // sentence index inside the chunk content
  subject: string; // first table cell, empty for value sentences
  attribute: string; // column header, empty for value sentences
  dim: string; // "time" (minutes) | unit name | "num"
  value: number;
  surface: string; // literal text, e.g. "2 Stunden"
}

export interface Conflict {
  id: string;
  chunkId: string;
  docName: string;
  section: string;
  quote: string;
  message: string;
  similarity: number;
  severity: "high" | "medium";
  status: "open" | "ticket";
  ticketId: string | null;
  ticketResolved?: boolean;
  verifiedBy?: "llm" | "rules" | "llm+rules";
}

export interface DiffHunk {
  id: string;
  section: string;
  minRole: Role;
  oldChunkId: string | null;
  oldDocName: string | null;
  oldSection: string | null;
  oldContent: string | null;
  newContent: string;
}

export interface StagedDiff {
  id: string;
  title: string;
  docName: string;
  replacesDoc: string | null;
  author: string;
  uploadedAt: string;
  summary: string;
  sha256: string;
  targetCollection: string;
  status: "pending" | "rejected";
  decidedAt: string | null;
  decidedBy: string | null;
  decisionNote: string | null;
  hunks: DiffHunk[];
  removedChunkIds?: string[];
  conflicts: Conflict[];
}

export type AuditStatus = "SUCCESS" | "FAILED" | "DENIED" | "INFO";

export interface AuditLog {
  id: string;
  timestamp: string;
  actor: string;
  role: Role | "SYSTEM";
  action: string;
  target: string;
  status: AuditStatus;
  detail: string;
  prevHash: string;
  hash: string;
  v?: 2; // hash format: absent = legacy "|"-joined, 2 = JSON array (unambiguous)
}

export interface Ticket {
  id: string;
  type: "KNOWLEDGE_GAP" | "POLICY_SYNC";
  system: "Jira" | "ServiceNow";
  queue: string;
  title: string;
  description: string;
  targetDoc: string | null;
  ref: string | null;
  createdBy: string;
  createdAt: string;
  status: "open" | "resolved";
  sha256?: string;
  slaDue?: string;
  resolvedAt?: string | null;
  resolvedBy?: string | null;
  resolutionNote?: string | null;
}

/* ───────────────────────── API payloads ───────────────────────── */

export interface Snapshot {
  session: { role: Role; email: string; clearance: string };
  collection: string;
  liveVersion: number;
  pendingCount: number;
  totalChunks: number;
  chunks: LiveChunk[];
  diffs: StagedDiff[] | null;
  audit: AuditLog[] | null;
  auditTotal?: number | null;
  tickets: Ticket[] | null;
  integrity: { valid: boolean; entries: number; brokenAt: string | null } | null;
  backend: BackendStatus;
}

// "note" = statement about the answer itself (e.g. an unanswered part), never a claim from the documents.
export type AnswerSegment = { type: "text"; text: string } | { type: "note"; text: string } | { type: "cite"; chunkId: string; spanId?: string };

export interface LatencyMs {
  rewrite: number;
  rbac: number;
  retrieval: number;
  generation?: number;
  nli?: number;
  total: number;
}

export interface QueryResult {
  kind: "answer" | "search" | "fallback" | "denied";
  requiredRole: Role | null;
  segments: AnswerSegment[];
  hits?: { chunkId: string; docName: string; section: string; content: string }[]; // kind "search": no answer, RBAC-filtered spans
  citations: Record<string, LiveChunk>;
  scores?: Record<string, number>;
  trace: {
    original: string;
    rewrite: string;
    rbacFilter: string;
    searched: number;
    total: number;
    nliVerified: boolean;
    temperature: number;
    confidence: number; // = retrievalScore (kept for compatibility; not a probability)
    topScore?: number;
    retrievalScore?: number; // raw cosine of the best chunk (Python) or query-term coverage (local engine)
    lexicalSupport?: number; // share of question terms found in the best chunk
    verification?: "lexical-nli" | "extractive" | "none"; // what actually checked the answer
    latencyMs?: LatencyMs;
    engineMode?: string;
    rewriteBy?: string;
    answerBy?: string;
  };
}

export interface BackendStatus {
  mode: "python" | "fallback";
  ollama: boolean;
  llmModel: string | null;
  embedModel: string | null;
  vectorCount: number | null;
  engineMode: string;
}

export const FALLBACK_BACKEND: BackendStatus = { mode: "fallback", ollama: false, llmModel: null, embedModel: null, vectorCount: null, engineMode: "Local Fallback Engine (keyword)" };

export interface StageStats {
  chunks: number;
  changed: number;
  unchanged: number;
  conflicts: number;
  engineMode: string;
  latencyMs: Record<string, number>;
}

// Result of a watched-inbox scan (Zero-Touch connector, Pipeline A).
export interface SyncReport {
  inbox: string;
  scanned: number;
  staged: { file: string; diffId: string; conflicts: number }[];
  skipped: { file: string; reason: string }[];
  failed: { file: string; error: string }[];
  engineMode: string;
}

export interface AuditReport {
  schema: string;
  generatedAt: string;
  generatedBy: { email: string; role: Role };
  collection: string;
  liveVersion: number;
  chain: { valid: boolean; entries: number; headHash: string; algorithm: string };
  policySetHash: string;
  activePolicies: { id: string; docName: string; section: string; minRole: Role; sha256: string; approvedAt: string; approvedBy: string; contentHash: string }[];
  auditLogs: AuditLog[];
  tickets: Ticket[];
  reportHash: string;
}

export type ApiOk<T> = { ok: true } & T;
export interface ApiErr {
  ok: false;
  code: string;
  error: string;
}
export type ApiResult<T> = ApiOk<T> | ApiErr;

/* ───────────────────────── Client helpers ───────────────────────── */

export const citationLabel = (c: Pick<LiveChunk, "docName" | "section">) => `${c.docName} ${c.section.split(" ")[0]}`;

export const formatTime = (iso: string) =>
  new Date(iso).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });

// Enterprise webhook body that the Jira / ServiceNow connector would send for a ticket (shown in the Knowledge & Audit view).
export function buildTicketPayload(t: Ticket): Record<string, unknown> {
  const sla = t.slaDue ?? new Date(new Date(t.createdAt).getTime() + (t.type === "POLICY_SYNC" ? 5 : 2) * 86_400_000).toISOString();
  return {
    endpoint: t.system === "Jira" ? "POST /rest/api/3/issue" : "POST /api/now/table/incident",
    system: t.system,
    projectKey: "SEC",
    issueType: t.type === "POLICY_SYNC" ? "Policy-Conflict" : "Knowledge-Gap",
    summary: t.title,
    description: t.description,
    queue: t.queue,
    reporter: t.createdBy,
    sha256: t.sha256 ?? null,
    conflictingDoc: t.targetDoc,
    stagingRef: t.ref,
    priority: t.type === "POLICY_SYNC" ? "High" : "Medium",
    slaDue: sla,
    status: t.status,
  };
}

export const shortHash = (h: string) => `${h.slice(0, 8)}…${h.slice(-6)}`;

export const QUICK_ACTIONS: { input: string; domain: string; minRole: Role; fallback?: boolean }[] = [
  { input: "Darf ich das auch im Zug machen?", domain: "Mobile Work", minRole: "L1" },
  { input: "Wie lang muss mein Passwort sein?", domain: "Passwort-Policy", minRole: "L1" },
  { input: "Bis wann muss ein Sicherheitsvorfall nach NIS-2 gemeldet werden?", domain: "NIS-2 Meldepflicht", minRole: "L2" },
  {
    input: "Darf ich private KI-Tools wie ChatGPT für Kundenangebote nutzen?",
    domain: "Out-of-Scope · Fallback",
    minRole: "L1",
    fallback: true,
  },
];

const authHeader = (token: string | null): Record<string, string> => (token ? { Authorization: `Bearer ${token}` } : {});

export async function callApi<T extends object>(
  action: string,
  payload: Record<string, unknown> = {},
  token: string | null = null
): Promise<ApiResult<T>> {
  try {
    const res = await fetch("/api/v1", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeader(token) },
      body: JSON.stringify({ action, ...payload }),
      cache: "no-store",
    });
    return (await res.json()) as ApiResult<T>;
  } catch {
    return { ok: false, code: "NETWORK", error: "Backend nicht erreichbar." };
  }
}

// Multipart variant for file uploads (Pipeline A). The browser sets the boundary, so no Content-Type header here.
export async function callApiForm<T extends object>(action: string, fields: FormData, token: string | null = null): Promise<ApiResult<T>> {
  try {
    fields.set("action", action);
    const res = await fetch("/api/v1", { method: "POST", headers: authHeader(token), body: fields, cache: "no-store" });
    return (await res.json()) as ApiResult<T>;
  } catch {
    return { ok: false, code: "NETWORK", error: "Backend nicht erreichbar." };
  }
}

export async function fetchSnapshot(token: string | null): Promise<Snapshot | null> {
  try {
    const res = await fetch("/api/v1", { headers: authHeader(token), cache: "no-store" });
    return res.ok ? ((await res.json()) as Snapshot) : null;
  } catch {
    return null;
  }
}
