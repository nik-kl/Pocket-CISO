# Pocket CISO — Architecture Overview (Stage 4: zero-touch ingestion & closed-loop remediation)

On-premise Compliance & Policy RAG middleware for the DACH market. A Next.js app with a real local AI backend: **Python FastAPI + Ollama** (LLM + embeddings), a **JSON-file database** shared by both runtimes, and an **automatic fallback to a pure-TypeScript engine** when Python/Ollama are not running — so the product (and a live pitch) never breaks.

## Run it (2 terminals)
```bash
# Terminal 1 — AI backend (Python FastAPI + Ollama; explicit demo mode)
cd backend && export POCKET_CISO_DEMO=1 && source .venv/bin/activate && python main.py          # http://127.0.0.1:8000
# Terminal 2 — web app
POCKET_CISO_DEMO=1 npm run dev                                    # http://localhost:3000
```
First-time setup:
```bash
cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt   # Python ≥ 3.10
ollama pull qwen2.5:7b && ollama pull bge-m3                                                           # optional, enables the real LLM path
npm install
```
Optional: `AUTO_SYNC=1 python main.py` additionally scans `backend/watched_inbox/` every 30 s (`AUTO_SYNC_SECONDS`). Everything also works with **only** `POCKET_CISO_DEMO=1 npm run dev` (local fallback engine). In explicit demo mode, demo logins are `admin2026` (L2 IT-Admin) and `ciso2026` (L3 CISO); L1 needs no password. Sample uploads: `backend/seed_policies/*.md`.

For a non-demo deployment, set `POCKET_CISO_SECRET` (at least 32 characters), `POCKET_CISO_L2_PASSWORD`, and `POCKET_CISO_L3_PASSWORD` in both processes before startup. The HMAC secret is never written to `data/pocket-ciso-db.json`; demo mode generates a local `data/.auth-secret` with restrictive permissions for the two local processes. Reset Demo is disabled unless `POCKET_CISO_DEMO=1` is explicitly set.

## Engine modes (sidebar shows the live state, polled every 8 s)
| Sidebar | What runs | Retrieval | Answer / diff / conflicts |
|---|---|---|---|
| `Python FastAPI + Ollama (Connected)` | Python + Ollama | `bge-m3` embeddings, NumPy cosine | `qwen2.5:7b` rewrite + grounded answer, LLM-assisted conflict verdicts, PDF/DOCX/MD/TXT |
| `Python FastAPI · Rule Engine (Ollama offline)` | Python only | deterministic hash-n-gram embeddings (offline) | rule-based rewrite, extractive answers, rule-based conflicts, PDF/DOCX/MD/TXT |
| `Local Fallback Engine (Active)` | Node (`src/lib/db.ts`) | keyword coverage + synonyms | extractive answers, rule-based conflicts, `.md/.txt` only |

All three read/write the **same** `data/pocket-ciso-db.json` (atomic `mkdir` lock + atomic rename). Python additionally keeps `data/vectors.json`; it re-embeds anything the other engine changed, so you can stop/start Python mid-session.

## Request flow
```
Browser (React)
  └─► /api/v1  (src/app/api/v1/route.ts — hybrid gateway)
        ├─ query · stage_policy · stage_file · sync_inbox · approve ──► Python FastAPI :8000 ──► Ollama :11434
        │       (offline → same action on the local engine; mutation timeouts are NOT retried locally)
        └─ authenticate · tickets (create/resolve) · reject · export_report · snapshot ──► src/lib/db.ts
                              both engines ──► data/pocket-ciso-db.json (+ data/vectors.json)
```
Auth is a stateless HMAC token (`ROLE.expiry.signature`, secret supplied through `POCKET_CISO_SECRET` or the explicit-demo local secret file). Python verifies the same token and enforces RBAC on its own. Legacy `meta.secret` values are ignored and removed on the next Node persistence cycle.

## Pipeline B — stateless compliance chat (`POST /query`)
Four timed stages (milliseconds shown in the telemetry bar):
1. **Isolated rewrite** — LLM (temp 0.0) turns the question into an explicit German search query. Small models sometimes *answer* instead of rewriting, so the output is validated (no refusals, key terms preserved) or replaced by a rule-based rewrite. No history is ever passed.
2. **RBAC pre-filter** — only chunks with `minRole ≤ role` are ranked. If only restricted chunks match, the answer is `RBAC Clearance Denied (Requires Lx)` and `RBAC_DENIED` is audited.
3. **Hybrid retrieval + threshold gate** — dense cosine (max of original/rewrite) must reach `MIN_SCORE` (0.55 for `bge-m3`; was 0.65 with `nomic-embed-text`) and the chunk must contain the relevant question terms. Single-intent questions use the 34 % lexical gate; multi-intent questions use a calibrated 12–34 % floor so a cloud-assessment chunk and a separate risk-approval chunk (or an incident-role chunk and a reporting-deadline chunk) can both be retrieved. A small, bounded bilingual hint layer maps only verified English intents — USB-loss sanctions, emergency USB storage, US SaaS/strictly confidential, Tier 2/RPO, and critical-risk acceptance — to their German policy vocabulary. Unknown English questions receive no generic boost. The dense BGE-M3 gate remains unchanged, preventing the relaxed lexical floor from accepting unsupported topics. Below the gate → hard fallback "Keine eindeutige Richtlinie gefunden — Halluzinationsschutz aktiv (Konfidenz < 85%)" + Jira/ServiceNow ticket button. The raw score is displayed as confidence with the threshold pinned to 85 %.
4. **Grounded generation + NLI verification** — LLM (temp 0.0) answers the *original* question with `[C1]` citations and receives up to four high-scoring chunks so every sub-question can be addressed. A lexical NLI check requires every number to occur in the cited chunks and ≥ 60 % of content words to be supported; otherwise the answer is replaced by an extractive, guaranteed-grounded one. Citations open the Source Chunk Inspector (raw text, SHA-256, approval time, approver, similarity).

## Pipeline A — ingestion, diff, conflict detection (`POST /stage-file`)
Parse (`pypdf`, `python-docx` incl. tables in body order, text) → semantic, **table-aware** chunking by headings (`##`, `§n`, `1.2 Titel`, ≤ 600 chars, SHA-256 per chunk) → embeddings → alignment with the existing **document series** (name-token similarity or explicit "replaces") → red/green diff (unchanged chunks skipped) → **cross-document conflict scanner**: cosine ≥ 0.68 (bge-m3) and shared vocabulary, then a verdict cascade: *rules* (same unit, different value: "12 Stunden ↔ 24 Stunden") ± *LLM JSON* (temp 0.0, validated quote). A 3B model flags far too many false positives on its own (measured: 5 "conflicts" for one password policy, 1 real), so LLM-only verdicts are accepted only with numeric context or very strong topical overlap. Each conflict shows `verifiedBy`. The CISO must create a **Sync-Ticket** per conflict or tick the explicit override before `Approve & Merge` (enforced server-side, 409 otherwise). Approval writes chunks + vectors to the live collection, removes superseded chunks and appends `APPROVED_AND_MERGED` to the audit chain.

## Zero-touch connector — `backend/watched_inbox/` (Stage 4)
*"Pocket CISO syncs documents in defined cycles and stages changes for CISO approval."* Drop a `.pdf/.docx/.md/.txt` into `backend/watched_inbox/` (stand-in for a SharePoint/SMB share) and press **Sync Inbox** in the Policy Diff view — or run the backend with `AUTO_SYNC=1` for a 30 s cycle. For every file the connector computes the SHA-256, skips it when `meta.ingestedFiles[file]` already has that hash or a diff of the same name is still pending, and otherwise stages it as *Auto-Connector (watched_inbox)* with `minRole L2`. Staging, hash bookkeeping and the `AUTO_CONNECTOR_SYNC` audit entry are written in **one atomic DB update**, so a crash can never stage a file twice. The CISO still approves every change — the connector only fills the queue. The shipped `BCM_Wiederanlauf_Matrix_2026.md` states *Tier 1 RTO = 2 Stunden*, which the conflict scanner flags against the live 24 h rules. Python handles all four formats; the Node fallback handles `.md/.txt` and reports PDF/DOCX as "needs Python".

**Tables:** a Markdown (or DOCX) table is no longer flattened into paragraphs. Each data row becomes a self-contained sentence — `Tabelle [§2 Wiederanlaufzeiten (RTO/RPO)]: System-Tier: Tier 1 (Kritisch) | RTO: 2 Stunden | RPO: 15 Minuten.` — so embeddings, retrieval and the "same unit, different value" conflict rule work on BCM/risk matrices. Rows are packed into atomic ≤ 600-char chunks under their section heading.

## Closed-loop tickets
Tickets are actionable: in *Knowledge DB & Audit Log* a ticket row expands to the **Enterprise Webhook Payload** (what the Jira/ServiceNow connector would POST: `projectKey "SEC"`, `issueType "Policy-Conflict"`, `sha256`, `conflictingDoc`, `slaDue`) and, for L2/L3, **Als erledigt markieren (Resolve)** → `resolve_ticket` sets `status: resolved`, audits `TICKET_RESOLVED` and marks the linked staging conflict as addressed (shown as "Sync-Ticket CISO-… · erledigt"). *Print / PDF Summary* opens the printable **NIS-2 Governance Attestation** (chain integrity, `policySetHash`, active policies with SHA-256, latest CISO approvals) — use *Drucken / Als PDF speichern*.

## Multilingual acceptance test
The ten-query regression matrix was executed against the Python/Ollama path with L3 clearance. The current matrix returns grounded cited answers for queries 1–6, 8 and 9 at 86–90% confidence; queries 7 and 10 return the hard hallucination-protection fallback below 85%. The targeted bilingual hints cover the IAM sanction catalog for USB loss in a train and the BIA's emergency USB storage location. The answers cite the exact policy chunks, including the BIA's financial criticality thresholds and staffing/storage controls.

## Identity mapping
The RBAC switcher shows the enterprise directory group each level maps to in an SSO rollout: `L1 → oidc:SG-All-Staff`, `L2 → oidc:SG-IT-Infrastructure`, `L3 → oidc:SG-CISO-Governance` (tooltip, active-row subtitle, clearance pill). Today roles are password-elevated demo identities; the group names are the contract for the Entra ID / OIDC claim mapping.

## UI
Left sidebar (workspaces, live backend status, RBAC identity switcher with password elevation), top bar (breadcrumb, legal pill, Light/Dark/System, clearance). Views: **Ask Pocket CISO** · **Policy Diff & Sign-Off** (drag-and-drop staging modal with live scanner steps, **Sync Inbox**) · **Knowledge DB & Audit Log** (chunks, hash-chained audit trail, tickets with webhook inspector & resolve, **NIS-2 audit report export as JSON/CSV + printable attestation**).

## Audit & compliance artifact
Append-only audit log, each entry hash-chained (`sha256(prevHash|id|timestamp|actor|role|action|target|status|detail)`), re-verified on every snapshot — Node and Python write the same format. *Export NIS-2 Audit Report* (L2+) downloads: active policy chunks with SHA-256 / approval time / approver, the complete audit log, tickets, chain head hash, `policySetHash` and a `reportHash`; an auditor can recompute all of them independently (the export itself is audited as `AUDIT_EXPORT`). CSV cells starting with `= + - @` are neutralised against spreadsheet formula injection.

## Security notes
Role passwords: SHA-256 + `timingSafeEqual`, 5 failures/min → 429. Known demo credentials are available only when `POCKET_CISO_DEMO=1`; non-demo startup fails unless L2/L3 passwords are configured. Python listens on `127.0.0.1` only, CORS limited to the web origin, uploads ≤ 10 MB with an extension allow-list, file names sanitised, PDF page cap, chunk cap. Uploaded policy text is untrusted input to the LLM: outputs are constrained (JSON-only conflict verdict with quote validation, NLI-checked answers). Queries are not persisted (zero retention; only explicit tickets keep the question). The HMAC secret is external to the JSON DB (`POCKET_CISO_SECRET` in deployments, permissions-restricted `data/.auth-secret` only for explicit demo mode). For production: Argon2id + SSO, PostgreSQL (append-only audit table), Qdrant, TLS, a reverse proxy.

## Verified behaviour (E2E)
Scripted browser run (Playwright, 26 checks) against live Ollama: Python connected → real `.md` upload → LLM+rules conflict → Sync-Ticket → merge → same question now answers "15 Zeichen"; previously-unknown KI question becomes answerable after approving the KI policy; JSON export re-verifies (chain, policy-set hash, report hash); Python stopped → sidebar flips to *Local Fallback*, queries and `.md` staging/approval keep working, PDF upload gives a clear "needs Python" message; Python restarted → vectors resync and answer from chunks merged offline. Real `.pdf`/`.docx` uploads were verified over HTTP.

## Verification policy (Stage 4)
Stage 4 was verified with `npx tsc --noEmit` and `python -m py_compile backend/main.py backend/rag_engine.py` only (no browser/E2E re-run). The 26-check E2E above predates the connector, table chunking and ticket resolution.

## Roadmap to 100 %
Qdrant/pgvector + PostgreSQL instead of JSON files · SSO (OIDC/SAML) · Jira/ServiceNow REST for tickets · re-ranker + larger multilingual embedding model (e.g. `bge-m3`) · OCR for scanned PDFs · multi-tenant collections.
