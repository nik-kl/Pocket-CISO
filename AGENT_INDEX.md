# AGENT_INDEX — lookup table (read this FIRST, update on every change)

Stack: Next.js 16 (App Router, TS, Tailwind v4, lucide-react) **+ optional Python FastAPI/Ollama backend** in `backend/`. Hybrid: the UI always works; Python only upgrades the engine.
**Start (2 terminals):** `cd backend && source .venv/bin/activate && python main.py` · `npm run dev` → http://localhost:3000.
First-time Python setup: `cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`. Ollama (optional): `ollama pull qwen2.5:7b && ollama pull bge-m3`.
**Always `rm tsconfig.tsbuildinfo` before `npx tsc --noEmit`** (stale incremental info hides errors).

## Hybrid architecture
```
Browser ─► /api/v1 (route.ts gateway) ─┬─► Python FastAPI :8000  (query · stage-file · sync-inbox · approve)  ─► Ollama :11434 (LLM + embeddings)
                                       └─► src/lib/db.ts (local TS engine, ALWAYS available)
Both engines read/write the same data/pocket-ciso-db.json (+ data/vectors.json for Python) under an atomic-mkdir lock (data/.db.lock).
```
- Gateway routing: `query`, `stage_policy`, `stage_file`, `sync_inbox`, `approve` → Python first. Offline ⇒ local engine. Timeout/5xx on **mutations are NOT retried locally** (504/502) to avoid double-apply; `query` may fall back on timeout/5xx. Everything else (`authenticate`, `logout`, `create_ticket`, `create_sync_ticket`, `resolve_ticket`, `reject`, `reset_demo`, `export_report`, snapshot) is always local.
- Backend status: `GET /api/v1` returns `backend: BackendStatus` (cached 4 s); `POST {action:"health"}` forces a probe. UI polls every 8 s → sidebar `data-testid="backend-status"` (`data-mode=python|fallback`): `Python FastAPI + Ollama (Connected)` · `Python FastAPI · Rule Engine (Ollama offline)` · `Local Fallback Engine (Active)`.
- Python verifies the same HMAC bearer token (secret in DB `meta.secret`) and enforces RBAC itself. It needs the DB file to exist: the gateway calls `db.ensureDb()` before proxying (Python alone answers 503 `DB_NOT_READY` until the web app was opened once).
- Responses carry `served: "python"|"local"`.

## File map
| File | Purpose |
|---|---|
| `next.config.mjs` | `devIndicators:false`, `agentRules:false` |
| `src/app/globals.css` · `layout.tsx` | Semantic light/dark tokens (`bg-canvas/surface/raised`, `border-line`, `text-fg/muted/faint`, `brand/ok/warn/bad[-fg]`); pre-paint theme script |
| `src/app/page.tsx` | App shell: session, snapshot, views, RBAC switcher (shows the mapped **Entra ID / OIDC group** per role: tooltip, active-row subtitle, clearance pill), ElevationModal, backend status box (polls health), ThemeSwitcher |
| `src/app/api/v1/route.ts` | Hybrid gateway (see above). Multipart `stage_file` (≤10 MB; offline: `.md/.txt` only, else 415 `NEEDS_PYTHON`) |
| `src/components/StatelessChatView.tsx` | Pipeline B UI: hero, telemetry pills **with per-step ms**, Under-the-Hood (engine, latency), citation drawer (raw text, SHA-256, approved at/by, similarity), denied card, fallback ticket |
| `src/components/CisoDiffDashboard.tsx` | Pipeline A UI: queue, **StageModal** (drag&drop PDF/DOCX/MD/TXT or paste text, min role, optional "replaces", animated scanner, result summary), word-diff, conflict callouts (+`verifiedBy` badge, Sync-Ticket, "erledigt" once the ticket is resolved), override, approve/reject, **Sync Inbox button** (`RefreshCw`, action `sync_inbox`, toast via `syncMessage`) |
| `src/components/KnowledgeAndAuditView.tsx` | Chunks table (+Approved by), audit trail + chain badge, tickets (**click a row → Enterprise Webhook Payload JSON + "Als erledigt markieren (Resolve)"**, action `resolve_ticket`), **Export NIS-2 Audit Report (JSON/CSV)**, **Print / PDF Summary** (`AttestationSheet`: portal into `<body>` + print CSS, NIS-2 Governance Attestation from `export_report`), demo reset |
| `src/components/ui-primitives.tsx` | Button, Badge, Modal, SidePanel, ThemeSwitcher, Toast, ElevationModal |
| `src/lib/mock-store.ts` | `ROLE_META.group` (OIDC group), `Ticket` (`status "open"|"resolved"`, `sha256`, `slaDue`, `resolved*`), `Conflict.ticketResolved`, `SyncReport`, `buildTicketPayload(ticket)` (Jira/ServiceNow webhook body). Shared types (`LiveChunk.approvedBy`, `Conflict.verifiedBy`, `QueryResult.trace.latencyMs/engineMode/topScore`, `scores`, `BackendStatus`, `StageStats`, `AuditReport`), `callApi`, `callApiForm`, `fetchSnapshot` |
| `src/lib/db.ts` | Local TS engine: JSON DB + cross-process lock, seed, auth/HMAC, keyword query engine, **chunkDocument (table-aware: `tableStatements` / `sectionParagraphs`) / stageInTx / stageDocument / stagePolicy**, **`syncWatchedInbox`** (+`ingestInTx`), **`resolveTicket`**, `migrate()` (legacy ticket status, `meta.ingestedFiles`), merge, audit hash chain, `exportReport`, `ensureDb` |
| `backend/main.py` | FastAPI app (CORS localhost:3000, 127.0.0.1:8000). Endpoints below. `python main.py`. Lifespan starts the inbox watcher when `AUTO_SYNC=1` |
| `backend/rag_engine.py` | Ollama client, hash-ngram fallback embeddings, NumPy vector store, parser+chunker (**table-aware**, DOCX tables kept in document order), Pipeline A/B, **`sync_watched_inbox` / `start_inbox_watcher`**, NLI check, conflict detector, shared-DB access (lock, audit chain, token verify) |
| `backend/requirements.txt` | fastapi, uvicorn, pydantic, httpx, numpy, pypdf, python-docx, python-multipart |
| `backend/watched_inbox/` | Zero-touch connector folder (SharePoint/SMB stand-in). Ships `BCM_Wiederanlauf_Matrix_2026.md` (Markdown RTO/RPO table: Tier 1 = 2 Stunden → conflicts with the live 24 h rules in `BCM_Notfallplan_2025.pdf` / `Incident_Response_Runbook.pdf`) |
| `backend/seed_policies/` | `Passwort_Richtlinie_v4.md` (series of Passwort_Policy_v2; conflicts with Onboarding), `NIS2_Incident_Playbook.md` (L2; 12 h/4 h vs Runbook 24 h → conflicts), `KI_Nutzungsrichtlinie_2026.md` (answers the "ChatGPT" fallback question once approved) |

## Python endpoints (`http://127.0.0.1:8000`, Bearer token optional → L1)
| Endpoint | Role | Notes |
|---|---|---|
| `GET /health` | – | `{python, dbReady, ollama{connected,llmModel,embedModel}, engineMode, vectorCount, liveChunks}` |
| `POST /query` `{question}` | any | Pipeline B (stateless, no history param). Returns `{ok, result: QueryResult, answer, citations[], telemetry{rewrittenQuery,rbacFilter,topScore,nliVerified,latencyMs,engineMode}}` |
| `POST /stage-file` multipart `file`/`text`, `minRole`, `replaces?`, `title?`, `author?` **or** JSON `{title,targetDoc,newText,minRole}` | L2 | Pipeline A → `{ok, diff, stats}` |
| `POST /sync-inbox` | L2 | Zero-touch connector → `{ok, report: SyncReport}` (`scanned`, `staged[{file,diffId,conflicts}]`, `skipped[{file,reason}]`, `failed[{file,error}]`) |
| `POST /approve/{diff_id}` `{override?}` | L3 | 409 `CONFLICT_UNRESOLVED` unless every conflict has a Sync-Ticket or `override`; moves hunks + vectors to live, audits `APPROVED_AND_MERGED` |
Errors: `{ok:false, code, error}` + HTTP status (`ApiError`).

## Pipeline B (Python, `run_query`) — 4 timed stages
1 rewrite (LLM temp 0; **validated**: refusals / answers / lost key terms ⇒ rule-based rewrite; known English intents receive bounded German policy hints for Cloud Security, BCM RTO/RPO and risk acceptance) → 2 RBAC pre-filter (`minRole ≤ role` before ranking) → 3 hybrid retrieval: dense cosine = max(original, rewrite) plus lexical support. Single-intent questions use `LEX_MIN` (0.34); multi-intent questions use a calibrated 0.12–0.34 lexical floor so separate chunks can answer separate sub-questions while the BGE-M3 dense gate remains `MIN_SCORE` 0.55 (hash: 0.25) → 4 generation (temp 0, answers the *original* question, `[C1]` markers) with up to four grounded context chunks + lexical **NLI** (numbers must occur in cited chunks, ≥ 60 % content words supported, ≥ 2 content words, `§n` refs ignored). NLI fail ⇒ extractive grounded answer. Restricted-only match ⇒ `denied` (+`RBAC_DENIED` audit); nothing ⇒ `fallback` ("Keine eindeutige Richtlinie gefunden — Halluzinationsschutz aktiv (Konfidenz < 85%)"). Confidence = `scale_score(topScore, threshold→85 %)`, fallback capped 84 %.
## Pipeline A (Python, `stage_document`)
parse (.pdf pypdf · .docx python-docx · .md/.txt) → chunk by headings (`##`, `§n`, `1.2 Titel`; ≤ 600 chars) → embed → align to **same series** (name-token Jaccard ≥ 0.5, or explicit `replaces`) by § number then cosine ≥ `align` → red/green hunks (unchanged skipped) → conflict scan vs **other** docs the actor may see: cosine ≥ 0.68 (Ollama/bge-m3; hash 0.30) **and** topical overlap ≥ 0.25 → verdict cascade `check_conflict`: rules (same unit, different value) ± LLM JSON (temp 0). LLM-only verdicts need a shared unit + overlap ≥ 0.30 or overlap ≥ 0.70. `verifiedBy`: `rules | llm | llm+rules`. Vectors for pending hunks live under `stg:<hunkId>` in `data/vectors.json` and move to the new chunk id on approve.
## Env (Python)
`OLLAMA_BASE_URL` (http://localhost:11434) · `LLM_MODEL` (qwen2.5:7b → else first non-embedding model) · `EMBED_MODEL` (bge-m3 → else any `*embed*`) — changing the embedding model rebuilds `data/vectors.json` automatically · `MIN_SCORE` `CONFLICT_SIM` `ALIGN_SIM` `LEX_MIN` · `LLM_TIMEOUT` `REWRITE_TIMEOUT` `CONFLICT_LLM_TIMEOUT` · `AUTO_SYNC` (=1 → background thread scans the inbox every `AUTO_SYNC_SECONDS`, default 30) · `WATCHED_INBOX_DIR` (also read by the Node fallback) · `CORS_ORIGINS` · `HOST` `PORT` · `POCKET_CISO_DATA_DIR` · `MAX_UPLOAD_MB`. Next: `PYTHON_BACKEND_URL`.

## Gateway/local actions (`POST /api/v1`, Bearer optional)
`health` · `authenticate{role,password}` · `logout` · `query{question}` · `create_ticket{question,reason}` · `stage_policy{title,targetDoc,newText,minRole}` · `stage_file` (multipart) · `create_sync_ticket{diffId,conflictId}` · `resolve_ticket{ticketId,resolutionNote}` (L2+; ticket → `resolved`, linked `diff.conflicts[].ticketId` → `status:"ticket"` + `ticketResolved`, audits `TICKET_RESOLVED`) · `sync_inbox` (L2+; Python first, else `syncWatchedInbox`) · `approve{diffId,override?}` · `reject{diffId,reason}` · `export_report` (L2+, audits `AUDIT_EXPORT`, returns `AuditReport` with `policySetHash`, chain head, `reportHash = sha256(schema|generatedAt|collection|liveVersion|policySetHash|headHash|entries)`) · `reset_demo` (L3).

## DB schema (`data/pocket-ciso-db.json`, shared by both engines)
`meta{secret,liveVersion,nextTicket,nextAudit,seededAt,ingestedFiles{filename→sha256}}` · `users[]` (L1 employee@company.at; L2 admin@company.at/admin2026; L3 ciso@company.at/ciso2026; SHA-256 hashes) · `live_chunks[]{id,docName,section,minRole,content,updatedAt,sha256,approvedBy}` · `staged_diffs[]{…,hunks[],conflicts[{…,verifiedBy}]}` · `audit_logs[]` (hash chain: `sha256(prevHash|id|timestamp|actor|role|action|target|status|detail)`; **Python must keep this byte-identical**) · `tickets[{id,type,system,queue,title,description,targetDoc,ref,createdBy,createdAt,status:"open"|"resolved",sha256,slaDue,resolvedAt,resolvedBy,resolutionNote}]` (legacy `OPEN`/`CLOSED` migrated on load). `data/vectors.json`: `{engine, dim, vectors{chunkId|stg:hunkId: float[]}}`; rebuilt automatically when the embedding engine changes or the DB drifted (e.g. chunks merged by the local engine).

## Stage 4 — zero-touch ingestion, tables, closed loop
- **Watched inbox** (`backend/watched_inbox/`, `.pdf .docx .md .txt`; dotfiles/symlinks ignored): per file → SHA-256 → skip if `meta.ingestedFiles[file]` equals it or a **pending** diff with the same `docName` exists → else `stage_document(author "Auto-Connector (watched_inbox)", min_role L2, as_actor = connector@L2)` → in the **same atomic DB write** record the hash and append `AUTO_CONNECTOR_SYNC` (role `SYSTEM`; the usual `POLICY_STAGED` precedes it). `NO_CHANGES` (identical to live) just records the hash. Failed files are reported, not recorded (retried next cycle). Python and Node mirror each other; Node cannot parse PDF/DOCX (reported as failed).
- Triggers: gateway `sync_inbox` / `POST /sync-inbox` (L2+) and, with `AUTO_SYNC=1`, a daemon thread (`INBOX_LOCK` prevents double staging). The connector's conflict scan uses L2 visibility regardless of who triggered.
- **Table-aware chunking** (`chunk_document` / `chunkDocument`): a Markdown table block inside a section becomes one statement per data row — `Tabelle [§2 Titel]: System-Tier: Tier 1 (Kritisch) | RTO: 2 Stunden | RPO: 15 Minuten.` — then packed (≤ 600 chars) with the section's prose, so rows stay atomic and unit/number conflict rules see `2 Stunden` vs `24 Stunden`. DOCX tables are emitted as Markdown tables **in body order** (formerly appended at the end → wrong section). Headerless tables fall back to plain cell lists.
- **Closed loop**: `resolve_ticket` → audit `TICKET_RESOLVED`; the ticket row expands to the webhook payload (`projectKey "SEC"`, `issueType "Policy-Conflict"|"Knowledge-Gap"`, `sha256`, `conflictingDoc`, `slaDue` = +5 d sync / +2 d gap).
- **Print summary**: `export_report` (audited as `AUDIT_EXPORT`) → `AttestationSheet` (white "paper" sheet, hides the app on print via `body > *:not(#nis2-print-root)`).

## Multilingual acceptance test
- The ten-language/complexity matrix is expected to produce answers for items 1, 2, 3, 5, 6, 9 and 10, and hard fallbacks below 85% for items 4, 7 and 8.
- English intent hints are deliberately narrow: US SaaS + strictly confidential → Cloud Security/Public Cloud/On-Premise; Tier 2 + RPO → BCM table/RTO/RPO values; critical risks + acceptance → CISO/Geschäftsführung/Risk Owner. Unknown English questions do not receive a generic translation boost.

## Behaviour contracts / gotchas
- RBAC server-side in both engines; tokens `ROLE.exp.HMAC` (8 h); L1 switch = logout.
- Dashboard selects a newly staged diff **after** `refresh()` (selecting before the snapshot arrived got reset — fixed).
- "Dynamic proof": password question → v2 (10 Zeichen) → stage `Passwort_Richtlinie_v4.md` → sync-ticket → approve → 15 Zeichen citing v4. Unknown KI question becomes answerable after approving `KI_Nutzungsrichtlinie_2026.md`.
- Small local LLMs are unreliable → every LLM step has a validated rule-based fallback; never trust LLM output without the NLI / quote / JSON guards.
- Seeded diffs: `diff-passwort` (conflict `chk-onboard-3`), `diff-bcm` (conflict `chk-ir-7-1`).
- Theme: `localStorage["pocket-ciso-theme"]`, `.dark` on `<html>`. Session: `sessionStorage["pocket-ciso-session"]`.
- After `reset_demo` the inbox hashes are cleared too, so the next `sync_inbox` stages the sample file again.
- Avoid `24 Stunden` inside a staged table when you want a conflict with a 24 h rule: the numeric rule needs values on **both** sides that the other lacks.
- `data/`, `backend/.venv/`, `__pycache__/` are gitignored.
