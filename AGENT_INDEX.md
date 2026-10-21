# AGENT_INDEX — lookup table (read this FIRST, update on every change)

Stack: Next.js 16 (App Router, TS, Tailwind v4, lucide-react) **+ optional Python FastAPI/Ollama backend** in `backend/`. Hybrid: the UI always works; Python only upgrades the engine. Repo layout is flat on purpose (no new files/modules without need).

## Start (exactly 2 terminals)
Demo mode (seed data, demo logins `admin2026` / `ciso2026`, Reset Demo enabled):
1. `cd backend && export POCKET_CISO_DEMO=1 && source .venv/bin/activate && python main.py`
2. `POCKET_CISO_DEMO=1 npm run dev` → http://localhost:3000

Production mode (`next build` first; **both** processes need identical values):
```bash
export POCKET_CISO_ENV=production POCKET_CISO_SECRET='<32+ random chars>' POCKET_CISO_L2_PASSWORD='<14+ chars>' POCKET_CISO_L3_PASSWORD='<14+ chars>'
cd backend && source .venv/bin/activate && python main.py                  # terminal 1
npm run build && npm run start                                              # terminal 2 (NODE_ENV=production)
```
First-time setup: `npm install` · `cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt` · optional `ollama pull qwen2.5:7b && ollama pull bge-m3`.
**Always `rm -f tsconfig.tsbuildinfo` before `npx tsc --noEmit`.** Static checks: `backend/.venv/bin/python -m py_compile backend/main.py backend/rag_engine.py`.

## Environment variables
| Variable | Used by | Meaning |
|---|---|---|
| `POCKET_CISO_ENV=production` (or `NODE_ENV=production`) | Node, Python | Production mode: demo flags are ignored, no seed data, passwords/secret mandatory, `reset_demo` → 403 |
| `POCKET_CISO_DEMO=1` (alias `POCKET_CISO_DEMO_MODE=1`) | Node, Python | Demo mode (only when not production): seed data, demo passwords, `reset_demo`, secret auto-generated in `data/.auth-secret` (0600) |
| `POCKET_CISO_SECRET` | Node, Python | HMAC secret for session tokens **and** the audit head anchor; ≥ 32 chars; required outside demo; never stored in the DB |
| `POCKET_CISO_L2_PASSWORD`, `POCKET_CISO_L3_PASSWORD` | Node | Role passwords; ≥ 14 chars outside demo; hashed with scrypt + per-process salt in memory only |
| `POCKET_CISO_DATA_DIR` | Node, Python | Data dir (default `./data`): `pocket-ciso-db.json`, `vectors.json`, `.db.lock/` |
| `WATCHED_INBOX_DIR` | Node, Python | Connector folder (default `backend/watched_inbox`) |
| `PYTHON_BACKEND_URL` | Node | default `http://127.0.0.1:8000` |
| `TRUST_PROXY=1` | Node | Trust `X-Forwarded-For` for per-client rate limits. **Only behind a reverse proxy that overwrites the header** (otherwise clients can forge it); without it all callers share one `direct` bucket whose limits are ×10 and whose login limit is the account ceiling (30 per 15 min) |
| `POCKET_CISO_RATE_LIMIT_MULT` | Node | Multiplier for the gateway rate limits (QA/load tests only) |
| `POCKET_CISO_DISABLE_HINTS=1` | Node, Python | Evaluation switch: disables the hard-coded `queryHint` / `REWRITES` |
| `OLLAMA_BASE_URL`, `LLM_MODEL` (qwen2.5:7b), `EMBED_MODEL` (bge-m3) | Python | Changing the embedding model rebuilds `data/vectors.json` |
| `MIN_SCORE` (0.55), `CONFLICT_SIM` (0.68), `ALIGN_SIM` (0.60), `LEX_MIN` (0.34) | Python | Retrieval gate / conflict / alignment thresholds (hash-embedding values differ) |
| `LLM_TIMEOUT`, `REWRITE_TIMEOUT`, `EMBED_TIMEOUT`, `EMBED_TOTAL_BUDGET` (100 s), `CONFLICT_LLM_TIMEOUT` | Python | Timeouts; staging ≤ 100 s embeddings + ≤ 55 s conflicts < gateway 180 s |
| `AUTO_SYNC=1`, `AUTO_SYNC_SECONDS` (30) | Python | Background inbox scan |
| `MAX_UPLOAD_MB` (10), `CORS_ORIGINS`, `HOST`, `PORT` | Python | |

## Hybrid architecture
```
Browser ─► /api/v1 (route.ts gateway) ─┬─► Python FastAPI :8000  (query · stage-file · sync-inbox · approve) ─► Ollama :11434
                                       └─► src/lib/db.ts (local TS engine, ALWAYS available)
Both engines read/write the same data/pocket-ciso-db.json (+ data/vectors.json for Python) under a cross-process mkdir lock (data/.db.lock/, owner token, safe steal).
```
- Routing: `stage_policy`, `stage_file`, `sync_inbox`, `approve` → Python first, else local. `query` → Python **only while Ollama is up**; Python without Ollama (hash-embedding retrieval) measured worse than the local keyword engine, so queries are then answered locally (the UI marks `Local`/`Rule-based` answers with "Keyword-Modus · geringe Präzision"). Timeout/5xx on **mutations are not replayed locally** (504/502); re-submitting a pending identical diff is idempotent. Everything else (`authenticate`, `logout`, `create_ticket`, `create_sync_ticket`, `resolve_ticket`, `reject`, `reset_demo`, `export_report`, snapshot) is always local.
- Gateway protections (`route.ts`): per-client/per-action sliding-window rate limits (query 20/min, authenticate 10/min, create_ticket 5/10 min, stage 20/min, sync 10/min, snapshot 180/min, other 60/min → 429 + `Retry-After`); multipart needs `Content-Length` (411) ≤ 10.25 MB (413) and an L2 token **before** `formData()` is read; JSON bodies are read with a hard 256 KB cap (413, also without/with a wrong `Content-Length`); non-object JSON bodies → 400; config errors → generic 503 (env names only in the server log); held DB lock → 503 `DB_LOCKED`.
- Python re-checks size/auth before parsing (`main.py /stage-file`: 411/413, `check_role` L2, then `request.form()`); DOCX is size-checked as ZIP (64 MB uncompressed) before parsing.
- Backend status: `GET /api/v1` → `backend` (cached 4 s; `POST health` ≥ 1 s). Sidebar `data-testid="backend-status"` (`data-mode=python|fallback`): `Python FastAPI + Ollama (Connected)` · `Python FastAPI · Rule Engine (Ollama offline)` · `Local Fallback Engine (Active)`.
- Responses carry `served: "python"|"local"`.

## File map
| File | Purpose |
|---|---|
| `README.md` | Pitch, setup, startup commands, feature overview |
| `ARCHITECTURE_OVERVIEW.md` | Human architecture + security model + limitations/RAG proposals |
| `next.config.mjs` | Security headers (CSP in production, XFO, nosniff, Referrer-Policy, Permissions-Policy, COOP, HSTS in production), `agentRules:false` (valid in Next 16.4) |
| `src/app/page.tsx`, `layout.tsx`, `globals.css` | Shell, RBAC switcher (OIDC group labels), status box, theme tokens |
| `src/app/api/v1/route.ts` | Hybrid gateway (above) |
| `src/components/StatelessChatView.tsx` | Pipeline B UI: telemetry shows **Retrieval-Score** and the real verification mode (`NLI` vs `Wörtlicher Auszug`), citation drawer, fallback ticket |
| `src/components/CisoDiffDashboard.tsx` | Pipeline A UI: queue, StageModal, word diff, conflicts, Sync-Tickets, **override needs a reason (≥ 10 chars)**, a conflict only counts as handled once its ticket is resolved, Sync Inbox |
| `src/components/KnowledgeAndAuditView.tsx` | Chunks, hash-chained audit log (last 200 shown, total in tab), tickets + webhook inspector + resolve, JSON/CSV export (incl. tickets, `hashVersion`, recomputed content hashes), printable attestation |
| `src/components/ui-primitives.tsx` | Button, Badge, Modal, SidePanel, ThemeSwitcher, Toast, ElevationModal |
| `src/lib/mock-store.ts` | Shared types (`Snapshot.auditTotal`, `AuditLog.v`, `QueryResult.trace.retrievalScore/lexicalSupport/verification`, `StagedDiff.removedChunkIds`, `AuditReport.activePolicies[].contentHash`), `callApi*` |
| `src/lib/db.ts` | Local TS engine: DB + lock, auth (scrypt, HMAC), demo seed / empty prod init, keyword retrieval, chunker, staging, conflicts (unit-aware, mirrors Python), approval, tickets, audit chain v2 + anchor, export, inbox sync |
| `backend/main.py` | FastAPI HTTP layer (size/auth pre-checks, 422 for malformed JSON) |
| `backend/rag_engine.py` | Ollama client, hash-ngram fallback embeddings, vector store (+ stash), parser/chunker, Pipelines A/B, inbox connector, NLI check, conflict detector, DB lock/audit/token code |
| `backend/requirements.txt` | fastapi, uvicorn, pydantic, httpx, numpy, pypdf, python-docx, python-multipart |
| `backend/watched_inbox/` | Connector folder (user documents + `BCM_Wiederanlauf_Matrix_2026.md`); connector stages as `minRole L2` |
| `backend/seed_policies/` | Upload samples (`Passwort_Richtlinie_v4.md`, `NIS2_Incident_Playbook.md`, `KI_Nutzungsrichtlinie_2026.md`) |

## Python endpoints (`http://127.0.0.1:8000`; `Authorization: Bearer <token>` optional → L1)
| Endpoint | Role | Notes |
|---|---|---|
| `GET /health` | – | status, models, vector count |
| `POST /query {question}` | any | Pipeline B, stateless. Returns `result` (kind `answer`\|`fallback`; restricted-only matches are `fallback` too), `trace{retrievalScore, lexicalSupport, verification, confidence (= retrievalScore)}` |
| `POST /stage-file` (multipart or JSON) | L2 | 411/413/403 before parsing; idempotent for identical pending diffs |
| `POST /sync-inbox` | L2 | connector → `SyncReport` |
| `POST /approve/{id} {override?, overrideReason?}` | L3 | see approval semantics |
Errors: `{ok:false, code, error}`.

## Gateway actions (`POST /api/v1`)
`health` · `authenticate{role,password}` · `logout` · `query{question}` · `create_ticket{question,reason}` (L1 allowed by design: idempotent per question, ≤ 100 open per actor) · `stage_policy{title,targetDoc,newText,minRole}` (L2; text 10–2000 chars) · `stage_file` (multipart, L2) · `create_sync_ticket{diffId,conflictId}` (L3) · `resolve_ticket{ticketId,resolutionNote}` (L2+) · `sync_inbox` (L2+) · `approve{diffId,override?,overrideReason?}` (L3) · `reject{diffId,reason}` (L3) · `export_report` (L2+; audited) · `reset_demo` (L3, demo mode only, else 403 `DISABLED_IN_PRODUCTION`).

## DB schema (`data/pocket-ciso-db.json`, owner-only 0600)
`meta{liveVersion,nextTicket,nextAudit,seededAt,ingestedFiles{file→sha256},auditAnchor}` · `users[]` (`passwordHash` is just the marker `"env-managed"`: credentials live in the environment) · `live_chunks[]{id,docName,section,minRole,content,updatedAt,sha256,approvedBy}` · `staged_diffs[]{…,hunks[],removedChunkIds[],conflicts[]}` · `tickets[]` · `audit_logs[]`.

**Audit hash format.** Entries with `v: 2` hash `sha256(JSON.stringify([prevHash,id,timestamp,actor,role,action,target,status,detail]))`; entries without `v` (legacy) hash the `|`-joined fields. Verification handles both, so old chains stay valid. `meta.auditAnchor = HMAC(secret, "audit-anchor|<count>|<headHash>")` is updated by Node and Python on every append; deleting the tail or rewriting the chain without the secret fails verification (`brokenAt: "ANCHOR"`). Legacy DBs get a trust-on-first-use anchor. **This is tamper-evident only against someone without the secret (demo mode stores the secret next to the data) — it is not append-only storage or a legal-grade seal.** Denials (`ACCESS_DENIED`, `RBAC_DENIED`) are sampled: ≤ 1 entry per actor/action/minute with a "+N unterdrückt" counter.

`export_report` → `AuditReport`: active policies (`sha256` stored, `contentHash` recomputed from content), `policySetHash` = SHA-256 over the canonical JSON of the policy list, `reportHash` = SHA-256 over the canonical JSON of the whole report body (policies, audit log, tickets, chain status).

## Approval semantics (`approve_diff` / `approveDiff`, mirrored)
1. **Stale-base check** (409 `STALE_DIFF`, audited `MERGE_BLOCKED`): every `oldChunkId` of a hunk and every `removedChunkIds` entry must still be live with unchanged content.
2. **Conflicts** (409 `CONFLICT_UNRESOLVED`): each conflict needs a Sync-Ticket in state `resolved`; otherwise `override:true` **plus** `overrideReason` ≥ 10 chars (400 `OVERRIDE_REASON_REQUIRED`). The audit entry lists the accepted conflict ids and the reason.
3. **Merge:** hunks replace their `oldChunkId`; **old chunks without a counterpart in the new version (`removedChunkIds`) are removed**; the remaining chunks of the replaced series are renamed to the new document name (hash refreshed). Python moves the staged vectors to the new chunk ids.
4. Staging records `removedChunkIds` (series chunks not matched by the new version) **for complete document uploads only**; pasted rule text (`stage_policy`) is a partial update and never removes other clauses. The diff view lists every clause that will be removed (red block, `data-testid="removed-clauses"`). Conflicts are computed at staging only — they are **not** re-run at approval (known limitation).

## Pipelines
**B (query):** rewrite (LLM, validated, else rules) → RBAC pre-filter (`minRole ≤ role`) → hybrid retrieval gate (dense cosine ≥ `MIN_SCORE` **and** lexical support ≥ `LEX_MIN`/multi-intent floor) → grounded answer (≤ 4 chunks) + lexical NLI (numbers must occur in cited chunks, ≥ 60 % content words); NLI failure ⇒ extractive answer; a citation the model did not use is never added. Restricted-only or no match ⇒ the same `fallback`. Local engine: keyword coverage; answers are verbatim chunk text (`verification: "extractive"`, `nliVerified:false`).
**A (stage):** parse → table-aware chunking (KPI lines such as `2 Stunden` are content, not headings) → embeddings → series alignment → hunks + removed chunks → conflict scan (rules: same unit/different value on shared topic; Python additionally cosine ≥ `CONFLICT_SIM` and optional LLM verdict, both with `verifiedBy`) → pending diff (author = verified session; a client-supplied name is shown as "angegeben: …").

## Acceptance matrices / eval
`backend/watched_inbox` PDFs + the two 10-question matrices from the product owner. Hint-dependent questions (USB loss in train, emergency USB, US SaaS, Tier-2 RPO, critical-risk acceptance) rely on `queryHint` — disable with `POCKET_CISO_DISABLE_HINTS=1` to measure it (QA run: −4 of 47 answerable questions, i.e. −8.5 pp). See `ARCHITECTURE_OVERVIEW.md` → "Known limitations and RAG proposals".

## Behaviour contracts / gotchas
- Wiping the data dir while Python runs: demo mode re-reads `.auth-secret` when its mtime changes; production secrets come from the environment (restart both processes after changing it).
- Never use `lsof -ti :8000` to find the Python PID (it also matches Node's client socket) — use `lsof -ti tcp:8000 -sTCP:LISTEN`.
- Node and Python must be started with the same mode variables (`POCKET_CISO_ENV` / `POCKET_CISO_DEMO`) and the same `POCKET_CISO_SECRET`; `next start` forces `NODE_ENV=production` for Node only.
- Theme: `localStorage["pocket-ciso-theme"]`; session: `sessionStorage["pocket-ciso-session"]`.
- Dashboard selects a newly staged diff after `refresh()`.
- `data/`, `backend/.venv/`, `__pycache__/` are gitignored.
