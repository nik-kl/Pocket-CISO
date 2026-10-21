# AGENT_INDEX — lookup table (read this FIRST, update on every change)

Stack: Next.js 16 (App Router, TS, Tailwind v4, lucide-react) **+ optional Python FastAPI/Ollama backend** in `backend/`. Hybrid: the UI always works; Python only upgrades the engine.
**Start (2 terminals):** `cd backend && source .venv/bin/activate && python main.py` · `npm run dev` → http://localhost:3000.
First-time Python setup: `cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`. Ollama (optional): `ollama pull llama3.2:3b && ollama pull nomic-embed-text`.
**Always `rm tsconfig.tsbuildinfo` before `npx tsc --noEmit`** (stale incremental info hides errors).

## Hybrid architecture
```
Browser ─► /api/v1 (route.ts gateway) ─┬─► Python FastAPI :8000  (query · stage-file · approve)  ─► Ollama :11434 (LLM + embeddings)
                                       └─► src/lib/db.ts (local TS engine, ALWAYS available)
Both engines read/write the same data/pocket-ciso-db.json (+ data/vectors.json for Python) under an atomic-mkdir lock (data/.db.lock).
```
- Gateway routing: `query`, `stage_policy`, `stage_file`, `approve` → Python first. Offline ⇒ local engine. Timeout/5xx on **mutations are NOT retried locally** (504/502) to avoid double-apply; `query` may fall back on timeout/5xx. Everything else (`authenticate`, `logout`, `create_ticket`, `create_sync_ticket`, `reject`, `reset_demo`, `export_report`, snapshot) is always local.
- Backend status: `GET /api/v1` returns `backend: BackendStatus` (cached 4 s); `POST {action:"health"}` forces a probe. UI polls every 8 s → sidebar `data-testid="backend-status"` (`data-mode=python|fallback`): `Python FastAPI + Ollama (Connected)` · `Python FastAPI · Rule Engine (Ollama offline)` · `Local Fallback Engine (Active)`.
- Python verifies the same HMAC bearer token (secret in DB `meta.secret`) and enforces RBAC itself. It needs the DB file to exist: the gateway calls `db.ensureDb()` before proxying (Python alone answers 503 `DB_NOT_READY` until the web app was opened once).
- Responses carry `served: "python"|"local"`.

## File map
| File | Purpose |
|---|---|
| `next.config.mjs` | `devIndicators:false`, `agentRules:false` |
| `src/app/globals.css` · `layout.tsx` | Semantic light/dark tokens (`bg-canvas/surface/raised`, `border-line`, `text-fg/muted/faint`, `brand/ok/warn/bad[-fg]`); pre-paint theme script |
| `src/app/page.tsx` | App shell: session, snapshot, views, RBAC switcher, ElevationModal, backend status box (polls health), ThemeSwitcher |
| `src/app/api/v1/route.ts` | Hybrid gateway (see above). Multipart `stage_file` (≤10 MB; offline: `.md/.txt` only, else 415 `NEEDS_PYTHON`) |
| `src/components/StatelessChatView.tsx` | Pipeline B UI: hero, telemetry pills **with per-step ms**, Under-the-Hood (engine, latency), citation drawer (raw text, SHA-256, approved at/by, similarity), denied card, fallback ticket |
| `src/components/CisoDiffDashboard.tsx` | Pipeline A UI: queue, **StageModal** (drag&drop PDF/DOCX/MD/TXT or paste text, min role, optional "replaces", animated scanner, result summary), word-diff, conflict callouts (+`verifiedBy` badge, Sync-Ticket), override, approve/reject |
| `src/components/KnowledgeAndAuditView.tsx` | Chunks table (+Approved by), audit trail + chain badge, tickets, **Export NIS-2 Audit Report (JSON/CSV)**, demo reset |
| `src/components/ui-primitives.tsx` | Button, Badge, Modal, SidePanel, ThemeSwitcher, Toast, ElevationModal |
| `src/lib/mock-store.ts` | Shared types (`LiveChunk.approvedBy`, `Conflict.verifiedBy`, `QueryResult.trace.latencyMs/engineMode/topScore`, `scores`, `BackendStatus`, `StageStats`, `AuditReport`), `callApi`, `callApiForm`, `fetchSnapshot` |
| `src/lib/db.ts` | Local TS engine: JSON DB + cross-process lock, seed, auth/HMAC, keyword query engine, **chunkDocument / stageDocument / stagePolicy**, merge, audit hash chain, `exportReport`, `ensureDb` |
| `backend/main.py` | FastAPI app (CORS localhost:3000, 127.0.0.1:8000). Endpoints below. `python main.py` |
| `backend/rag_engine.py` | Ollama client, hash-ngram fallback embeddings, NumPy vector store, parser+chunker, Pipeline A/B, NLI check, conflict detector, shared-DB access (lock, audit chain, token verify) |
| `backend/requirements.txt` | fastapi, uvicorn, pydantic, httpx, numpy, pypdf, python-docx, python-multipart |
| `backend/seed_policies/` | `Passwort_Richtlinie_v4.md` (series of Passwort_Policy_v2; conflicts with Onboarding), `NIS2_Incident_Playbook.md` (L2; 12 h/4 h vs Runbook 24 h → conflicts), `KI_Nutzungsrichtlinie_2026.md` (answers the "ChatGPT" fallback question once approved) |

## Python endpoints (`http://127.0.0.1:8000`, Bearer token optional → L1)
| Endpoint | Role | Notes |
|---|---|---|
| `GET /health` | – | `{python, dbReady, ollama{connected,llmModel,embedModel}, engineMode, vectorCount, liveChunks}` |
| `POST /query` `{question}` | any | Pipeline B (stateless, no history param). Returns `{ok, result: QueryResult, answer, citations[], telemetry{rewrittenQuery,rbacFilter,topScore,nliVerified,latencyMs,engineMode}}` |
| `POST /stage-file` multipart `file`/`text`, `minRole`, `replaces?`, `title?`, `author?` **or** JSON `{title,targetDoc,newText,minRole}` | L2 | Pipeline A → `{ok, diff, stats}` |
| `POST /approve/{diff_id}` `{override?}` | L3 | 409 `CONFLICT_UNRESOLVED` unless every conflict has a Sync-Ticket or `override`; moves hunks + vectors to live, audits `APPROVED_AND_MERGED` |
Errors: `{ok:false, code, error}` + HTTP status (`ApiError`).

## Pipeline B (Python, `run_query`) — 4 timed stages
1 rewrite (LLM temp 0; **validated**: refusals / answers / lost key terms ⇒ rule-based rewrite) → 2 RBAC pre-filter (`minRole ≤ role` before ranking) → 3 hybrid retrieval: dense cosine = max(original, rewrite) **and** lexical support ≥ `LEX_MIN` (0.34); gate `MIN_SCORE` 0.65 (Ollama) / 0.25 (hash) → 4 generation (temp 0, answers the *original* question, `[C1]` markers) + lexical **NLI** (numbers must occur in cited chunks, ≥ 60 % content words supported, ≥ 2 content words, `§n` refs ignored). NLI fail ⇒ extractive grounded answer. Restricted-only match ⇒ `denied` (+`RBAC_DENIED` audit); nothing ⇒ `fallback` ("Keine eindeutige Richtlinie gefunden — Halluzinationsschutz aktiv (Konfidenz < 85%)"). Confidence = `scale_score(topScore, threshold→85 %)`, fallback capped 84 %.
## Pipeline A (Python, `stage_document`)
parse (.pdf pypdf · .docx python-docx · .md/.txt) → chunk by headings (`##`, `§n`, `1.2 Titel`; ≤ 600 chars) → embed → align to **same series** (name-token Jaccard ≥ 0.5, or explicit `replaces`) by § number then cosine ≥ `align` → red/green hunks (unchanged skipped) → conflict scan vs **other** docs the actor may see: cosine ≥ 0.72 (hash 0.30) **and** topical overlap ≥ 0.25 → verdict cascade `check_conflict`: rules (same unit, different value) ± LLM JSON (temp 0). LLM-only verdicts need a shared unit + overlap ≥ 0.30 or overlap ≥ 0.70. `verifiedBy`: `rules | llm | llm+rules`. Vectors for pending hunks live under `stg:<hunkId>` in `data/vectors.json` and move to the new chunk id on approve.
## Env (Python)
`OLLAMA_BASE_URL` (http://localhost:11434) · `LLM_MODEL` (llama3.2:3b → else first non-embedding model) · `EMBED_MODEL` (nomic-embed-text → else any `*embed*`) · `MIN_SCORE` `CONFLICT_SIM` `ALIGN_SIM` `LEX_MIN` · `LLM_TIMEOUT` `REWRITE_TIMEOUT` `CONFLICT_LLM_TIMEOUT` · `CORS_ORIGINS` · `HOST` `PORT` · `POCKET_CISO_DATA_DIR` · `MAX_UPLOAD_MB`. Next: `PYTHON_BACKEND_URL`.

## Gateway/local actions (`POST /api/v1`, Bearer optional)
`health` · `authenticate{role,password}` · `logout` · `query{question}` · `create_ticket{question,reason}` · `stage_policy{title,targetDoc,newText,minRole}` · `stage_file` (multipart) · `create_sync_ticket{diffId,conflictId}` · `approve{diffId,override?}` · `reject{diffId,reason}` · `export_report` (L2+, audits `AUDIT_EXPORT`, returns `AuditReport` with `policySetHash`, chain head, `reportHash = sha256(schema|generatedAt|collection|liveVersion|policySetHash|headHash|entries)`) · `reset_demo` (L3).

## DB schema (`data/pocket-ciso-db.json`, shared by both engines)
`meta{secret,liveVersion,nextTicket,nextAudit,seededAt}` · `users[]` (L1 employee@company.at; L2 admin@company.at/admin2026; L3 ciso@company.at/ciso2026; SHA-256 hashes) · `live_chunks[]{id,docName,section,minRole,content,updatedAt,sha256,approvedBy}` · `staged_diffs[]{…,hunks[],conflicts[{…,verifiedBy}]}` · `audit_logs[]` (hash chain: `sha256(prevHash|id|timestamp|actor|role|action|target|status|detail)`; **Python must keep this byte-identical**) · `tickets[]`. `data/vectors.json`: `{engine, dim, vectors{chunkId|stg:hunkId: float[]}}`; rebuilt automatically when the embedding engine changes or the DB drifted (e.g. chunks merged by the local engine).

## Behaviour contracts / gotchas
- RBAC server-side in both engines; tokens `ROLE.exp.HMAC` (8 h); L1 switch = logout.
- Dashboard selects a newly staged diff **after** `refresh()` (selecting before the snapshot arrived got reset — fixed).
- "Dynamic proof": password question → v2 (10 Zeichen) → stage `Passwort_Richtlinie_v4.md` → sync-ticket → approve → 15 Zeichen citing v4. Unknown KI question becomes answerable after approving `KI_Nutzungsrichtlinie_2026.md`.
- Small local LLMs are unreliable → every LLM step has a validated rule-based fallback; never trust LLM output without the NLI / quote / JSON guards.
- Seeded diffs: `diff-passwort` (conflict `chk-onboard-3`), `diff-bcm` (conflict `chk-ir-7-1`).
- Theme: `localStorage["pocket-ciso-theme"]`, `.dark` on `<html>`. Session: `sessionStorage["pocket-ciso-session"]`.
- `data/`, `backend/.venv/`, `__pycache__/` are gitignored.
