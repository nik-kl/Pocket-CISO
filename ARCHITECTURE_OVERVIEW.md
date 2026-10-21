# Pocket CISO — Architecture Overview (95 % MVP)

On-premise Compliance & Policy RAG middleware for the DACH market. A Next.js app with a real local AI backend: **Python FastAPI + Ollama** (LLM + embeddings), a **JSON-file database** shared by both runtimes, and an **automatic fallback to a pure-TypeScript engine** when Python/Ollama are not running — so the product (and a live pitch) never breaks.

## Run it (2 terminals)
```bash
# Terminal 1 — AI backend (Python FastAPI + Ollama)
cd backend && source .venv/bin/activate && python main.py          # http://127.0.0.1:8000
# Terminal 2 — web app
npm run dev                                                        # http://localhost:3000
```
First-time setup:
```bash
cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt   # Python ≥ 3.10
ollama pull llama3.2:3b && ollama pull nomic-embed-text                                                # optional, enables the real LLM path
npm install
```
Everything also works with **only** `npm run dev` (local fallback engine). Demo logins: `admin2026` (L2 IT-Admin), `ciso2026` (L3 CISO); L1 needs no password. Sample uploads: `backend/seed_policies/*.md`.

## Engine modes (sidebar shows the live state, polled every 8 s)
| Sidebar | What runs | Retrieval | Answer / diff / conflicts |
|---|---|---|---|
| `Python FastAPI + Ollama (Connected)` | Python + Ollama | `nomic-embed-text` embeddings, NumPy cosine | `llama3.2:3b` rewrite + grounded answer, LLM-assisted conflict verdicts, PDF/DOCX/MD/TXT |
| `Python FastAPI · Rule Engine (Ollama offline)` | Python only | deterministic hash-n-gram embeddings (offline) | rule-based rewrite, extractive answers, rule-based conflicts, PDF/DOCX/MD/TXT |
| `Local Fallback Engine (Active)` | Node (`src/lib/db.ts`) | keyword coverage + synonyms | extractive answers, rule-based conflicts, `.md/.txt` only |

All three read/write the **same** `data/pocket-ciso-db.json` (atomic `mkdir` lock + atomic rename). Python additionally keeps `data/vectors.json`; it re-embeds anything the other engine changed, so you can stop/start Python mid-session.

## Request flow
```
Browser (React)
  └─► /api/v1  (src/app/api/v1/route.ts — hybrid gateway)
        ├─ query · stage_policy · stage_file · approve ──► Python FastAPI :8000 ──► Ollama :11434
        │       (offline → same action on the local engine; mutation timeouts are NOT retried locally)
        └─ authenticate · tickets · reject · export_report · snapshot ──► src/lib/db.ts
                              both engines ──► data/pocket-ciso-db.json (+ data/vectors.json)
```
Auth is a stateless HMAC token (`ROLE.expiry.signature`, key in the DB). Python verifies the same token and enforces RBAC on its own.

## Pipeline B — stateless compliance chat (`POST /query`)
Four timed stages (milliseconds shown in the telemetry bar):
1. **Isolated rewrite** — LLM (temp 0.0) turns the question into an explicit German search query. Small models sometimes *answer* instead of rewriting, so the output is validated (no refusals, key terms preserved) or replaced by a rule-based rewrite. No history is ever passed.
2. **RBAC pre-filter** — only chunks with `minRole ≤ role` are ranked. If only restricted chunks match, the answer is `RBAC Clearance Denied (Requires Lx)` and `RBAC_DENIED` is audited.
3. **Hybrid retrieval + threshold gate** — dense cosine (max of original/rewrite) must reach `MIN_SCORE` (0.65 for `nomic-embed-text`) **and** the chunk must contain ≥ 34 % of the question's terms (incl. synonyms). *Why hybrid:* measured on German text, `nomic-embed-text` scores irrelevant questions 0.58–0.66 and relevant ones 0.70–0.78, so a bare 0.65 cut-off lets off-topic questions through. Below the gate → hard fallback "Keine eindeutige Richtlinie gefunden — Halluzinationsschutz aktiv (Konfidenz < 85%)" + Jira/ServiceNow ticket button. The raw score is displayed as confidence with the threshold pinned to 85 %.
4. **Grounded generation + NLI verification** — LLM (temp 0.0) answers the *original* question with `[C1]` citations. A lexical NLI check requires every number to occur in the cited chunks and ≥ 60 % of content words to be supported; otherwise the answer is replaced by an extractive, guaranteed-grounded one. Citations open the Source Chunk Inspector (raw text, SHA-256, approval time, approver, similarity).

## Pipeline A — ingestion, diff, conflict detection (`POST /stage-file`)
Parse (`pypdf`, `python-docx`, text) → semantic chunking by headings (`##`, `§n`, `1.2 Titel`, ≤ 600 chars, SHA-256 per chunk) → embeddings → alignment with the existing **document series** (name-token similarity or explicit "replaces") → red/green diff (unchanged chunks skipped) → **cross-document conflict scanner**: cosine ≥ 0.72 and shared vocabulary, then a verdict cascade: *rules* (same unit, different value: "12 Stunden ↔ 24 Stunden") ± *LLM JSON* (temp 0.0, validated quote). A 3B model flags far too many false positives on its own (measured: 5 "conflicts" for one password policy, 1 real), so LLM-only verdicts are accepted only with numeric context or very strong topical overlap. Each conflict shows `verifiedBy`. The CISO must create a **Sync-Ticket** per conflict or tick the explicit override before `Approve & Merge` (enforced server-side, 409 otherwise). Approval writes chunks + vectors to the live collection, removes superseded chunks and appends `APPROVED_AND_MERGED` to the audit chain.

## UI
Left sidebar (workspaces, live backend status, RBAC identity switcher with password elevation), top bar (breadcrumb, legal pill, Light/Dark/System, clearance). Views: **Ask Pocket CISO** · **Policy Diff & Sign-Off** (drag-and-drop staging modal with live scanner steps) · **Knowledge DB & Audit Log** (chunks, hash-chained audit trail, tickets, **NIS-2 audit report export as JSON/CSV**).

## Audit & compliance artifact
Append-only audit log, each entry hash-chained (`sha256(prevHash|id|timestamp|actor|role|action|target|status|detail)`), re-verified on every snapshot — Node and Python write the same format. *Export NIS-2 Audit Report* (L2+) downloads: active policy chunks with SHA-256 / approval time / approver, the complete audit log, tickets, chain head hash, `policySetHash` and a `reportHash`; an auditor can recompute all of them independently (the export itself is audited as `AUDIT_EXPORT`). CSV cells starting with `= + - @` are neutralised against spreadsheet formula injection.

## Security notes
Role passwords: SHA-256 + `timingSafeEqual`, 5 failures/min → 429. Python listens on `127.0.0.1` only, CORS limited to the web origin, uploads ≤ 10 MB with an extension allow-list, file names sanitised, PDF page cap, chunk cap. Uploaded policy text is untrusted input to the LLM: outputs are constrained (JSON-only conflict verdict with quote validation, NLI-checked answers). Queries are not persisted (zero retention; only explicit tickets keep the question). `data/` holds the HMAC secret → gitignored. For production: Argon2id + SSO, secret outside the data file, PostgreSQL (append-only audit table), Qdrant, TLS, a reverse proxy.

## Verified behaviour (E2E)
Scripted browser run (Playwright, 26 checks) against live Ollama: Python connected → real `.md` upload → LLM+rules conflict → Sync-Ticket → merge → same question now answers "15 Zeichen"; previously-unknown KI question becomes answerable after approving the KI policy; JSON export re-verifies (chain, policy-set hash, report hash); Python stopped → sidebar flips to *Local Fallback*, queries and `.md` staging/approval keep working, PDF upload gives a clear "needs Python" message; Python restarted → vectors resync and answer from chunks merged offline. Real `.pdf`/`.docx` uploads were verified over HTTP.

## Roadmap to 100 %
Qdrant/pgvector + PostgreSQL instead of JSON files · SSO (OIDC/SAML) · Jira/ServiceNow REST for tickets · re-ranker + larger multilingual embedding model (e.g. `bge-m3`) · OCR for scanned PDFs · multi-tenant collections.
