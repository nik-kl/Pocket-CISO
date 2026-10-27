# Pocket CISO — Architecture Overview

On-premise compliance & policy RAG middleware for the DACH market. A Next.js app with an optional local AI backend (**Python FastAPI + Ollama**), a **JSON-file database** shared by both runtimes, and an **automatic fallback to a pure-TypeScript engine** when Python/Ollama are not running. The UI always works; Python only upgrades the engine.

[README.md](../README.md) = setup & pitch · [AGENT_INDEX.md](./AGENT_INDEX.md) = file/endpoint/env lookup table · this file = architecture, security model, limitations.

## Run it (2 terminals)
```bash
# demo mode
POCKET_CISO_DEMO=1 npm run engine                                                        # terminal 1 → :8000
POCKET_CISO_DEMO=1 npm run dev                                                           # terminal 2 → :3000
```
```bash
# production mode — both processes need the same values
export POCKET_CISO_ENV=production POCKET_CISO_SECRET='<32+ chars>' POCKET_CISO_L2_PASSWORD='<14+ chars>' POCKET_CISO_L3_PASSWORD='<14+ chars>'
npm run engine                                                   # terminal 1
npm run build && npm run start                                     # terminal 2 (NODE_ENV=production)
```
First-time setup: `npm install`; `python3 -m venv engine/.venv && engine/.venv/bin/pip install -r engine/requirements.txt`; optional `ollama pull qwen2.5:7b && ollama pull bge-m3`.

## Demo vs production
| | Demo (`POCKET_CISO_DEMO=1`, not production) | Production (`POCKET_CISO_ENV=production` or `NODE_ENV=production`) |
|---|---|---|
| First start | seeded: 9 live chunks, 2 pending diffs, backdated audit entries | **empty** DB: three role accounts + one genuine `DB_INITIALIZED` audit entry |
| Passwords | `admin2026` / `ciso2026` unless env overrides them | `POCKET_CISO_L2_PASSWORD` / `_L3_PASSWORD` mandatory, ≥ 14 chars (scrypt, salt per process, never stored) |
| Signing secret | `POCKET_CISO_SECRET` or auto-generated `data/.auth-secret` (0600, next to the data!) | `POCKET_CISO_SECRET` ≥ 32 chars mandatory, never written to disk |
| `reset_demo` | L3 only | 403 `DISABLED_IN_PRODUCTION` |
Demo flags are ignored when production is on. Missing configuration yields a generic 503 `AUTH_CONFIG_MISSING` to clients and the details (variable names) in the server log.

## Engine modes
| Sidebar | Retrieval | Answer / diff / conflicts |
|---|---|---|
| `Python FastAPI + Ollama (Connected)` | BM25 + `bge-m3` dense, RRF | `qwen2.5:7b` answerability judge + phrasing with span verifier, rule + LLM conflict verdicts at staging, PDF/DOCX/MD/TXT |
| `Python FastAPI · Rule Engine (Ollama offline)` | BM25 (hash-n-gram embeddings for staging only) | labelled search hits only, rule conflicts, PDF/DOCX/MD/TXT |
| `Local Fallback Engine (Active)` | keyword coverage + synonyms | labelled search hits only, rule conflicts, `.md/.txt` only |
All three share `data/pocket-ciso-db.json`; Python keeps `data/vectors.json` and re-embeds what the other engine changed. If Ollama flaps, the expensive Ollama vector set is kept in a stash and restored instead of re-embedding everything.

## Request flow and gateway protections
```
Browser ─► /api/v1 (src/app/api/v1/route.ts)
   ├─ query · stage_policy · stage_file · sync_inbox · approve ──► Python :8000 (whenever up) ──► Ollama :11434   (Python down → same action locally; mutation timeouts are not replayed)
   └─ authenticate · tickets · reject · export_report · snapshot ──► src/lib/db/
```
Rate limits (sliding window per client key and action; 429 + `Retry-After`): query 20/min, authenticate 10/min, create_ticket 5/10 min, stage 20/min, sync 10/min, snapshot 180/min, others 60/min. The client key is the proxy-supplied IP only with `TRUST_PROXY=1` — and only when the proxy **overwrites** `X-Forwarded-For`, otherwise clients can forge it. Without it all callers share one `direct` bucket: its limits are widened ×10 (so one office cannot rate-limit itself), the login limit is the account ceiling only (30 failures per 15 min, then backoff), and a startup warning is logged in production. Residual risk without a proxy: a persistent attacker can keep L3 logins in backoff; per-IP isolation (verified: attacker IP locked, other IP unaffected) needs the proxy. Limits are in memory and reset on restart. Login: per (client, role) 5 failures per 15 min then exponential backoff (30 s · 2ⁿ, max 15 min) plus a higher per-account ceiling (30 per 15 min) so an anonymous caller cannot trivially lock out L3. JSON bodies are read through a 256 KB cap (413; a 150 MB body used to be buffered, ≈ 1 GB RSS). Uploads: `Content-Length` required (411), ≤ 10.25 MB (413), L2 token verified **before** `formData()`/`request.form()` is read; Python repeats this and size-checks DOCX archives (64 MB uncompressed) before parsing.

## Auth and identity
Stateless token `ROLE.expiry.HMAC` (8 h), verified by Node and Python with the same secret (byte-length-safe comparison; garbage → L1). L1 has no login by design. **Identity is still role-level:** the audit `actor` is one of three shared accounts (`employee@`, `admin@`, `ciso@company.at`). The role → directory-group mapping shown in the UI (`oidc:SG-All-Staff` / `-IT-Infrastructure` / `-CISO-Governance`) is the contract for a future OIDC/Entra rollout; individual accountability needs that SSO.

## Pipeline B — stateless compliance chat
1. **RBAC view first:** document frequencies, vocabulary and candidates come only from chunks the role may read. A restricted-only match looks exactly like no match (the audit still records `RBAC_DENIED`).
2. **Question parts:** ≤ 4 parts (sentence ends; inside question sentences also at und/oder/comma + question word or modal verb); other sentences are context; text addressed to the assistant ("ignoriere alle Anweisungen …") is dropped and such spans in documents are never used.
3. **Retrieval:** BM25 over key terms (glossary, compound and typo alternatives; words that occur nowhere in the role's corpus are capped) + `bge-m3` dense, reciprocal-rank fusion, 12 candidates with ≤ 3 per document, exact-fact chunks for value questions.
4. **Gate:** sentence windows must cover ≥ 0.5 of the key-term weight with no missing term > 0.5 (windows from up to 3 sections of one document may be combined; table rows stand alone), or the best dense chunk reaches 0.62 (dev curve: highest unanswerable part 0.6045). Then the LLM answerability judge (JA/NEIN, temp 0, seed 7, no reply = NEIN) must confirm; parts left open are re-judged against the windows of the answered parts.
5. **Contradictions:** a deterministic rule (another document, same quantity and unit dimension, disjoint values; a document named with "laut/gemäß/according to" is not contradicted; intervals are not compared with durations) turns the result into kind `search` labelled "Mögliche Widersprüche – nicht als Antwort bestätigt" with every source and value, never an answer.
6. **Answer:** the LLM may only rephrase the confirmed spans (≤ 3 sentences); each sentence passes a span verifier (numbers + units, negation, modality, named roles, content words) or is replaced by the verbatim span. Unconfirmed windows are returned as kind `search` ("Fundstellen, nicht als Antwort bestätigt"), nothing above the floor as `fallback`.
**What the numbers mean.** `trace.retrievalScore` is the best dense cosine of the selected windows (lexical coverage without Ollama), filled for every kind; `lexicalSupport` the key-term coverage; `confidence` is an alias and **not a probability**. `trace.parts[]` lists per part coverage, dense, judge and the reason when not answered. The ticket text still says "Konfidenz < 85 %" (product-owner wording); the gate is the one above.

## Pipeline A — ingestion, diff, conflicts, approval
Parse (`pypdf`, `python-docx` incl. tables in body order, text) → table-aware chunking (≤ 600 chars; KPI lines such as `2 Stunden` / `250.000 EUR` stay content) → embeddings → alignment with the existing **document series** (name similarity or explicit "replaces") → red/green diff + `removedChunkIds` (old chunks without counterpart) → conflict scan against other documents the actor may see (rules: same unit, different value on a shared topic — identical in both engines; Python adds cosine ≥ `CONFLICT_SIM` and an LLM verdict, `verifiedBy` shows which) → pending diff. The shown author is the verified session; a client name appears as "angegeben: …". Re-submitting an identical pending diff returns the existing one (safe after a gateway timeout).

**Approval semantics** (identical in both engines): (1) stale-base check — every old chunk the diff touches must still be live with unchanged content, else 409 `STALE_DIFF`; (2) every conflict needs a Sync-Ticket in state **resolved**, or `override` with a reason ≥ 10 chars (the audit lists the accepted conflict ids and the reason); (3) merge: changed hunks replace their old chunk; for **complete document uploads, old chunks without counterpart are removed** (the diff shows them in a red "ENTFERNT" block) — pasted rule text is a partial update and removes nothing (a single pasted clause used to delete all other sections of the document), remaining chunks of the replaced series are renamed to the new document (hash refreshed), vectors move to the new ids. Not done: conflicts are not re-evaluated at approval time.

## Zero-touch connector (`data/inbox/`, `WATCHED_INBOX_DIR`)
`.pdf/.docx/.md/.txt`; dotfiles, symlinks, other extensions ignored; > 10 MB and unreadable/empty files are reported as failed; file names are NFC-normalised and keep umlauts. Per file: SHA-256 → skip when unchanged or a diff of the same name is pending → stage as *Auto-Connector (watched_inbox)* with `minRole L2` → one atomic DB write records the hash and appends `AUTO_CONNECTOR_SYNC`. Triggers: `sync_inbox` action / `POST /sync-inbox` and, with `AUTO_SYNC=1`, a 30 s thread. The CISO still approves everything.

## Audit trail and report
Hash-chained log (not append-only storage): `v: 2` entries hash a JSON array of the nine fields; legacy entries (no `v`) hash the `|`-joined fields; both verify. `meta.auditAnchor` (HMAC over entry count + head hash, written by both engines) makes deleting the newest entries or rewriting the chain **detectable by anyone holding the secret** — with the demo secret file next to the data that protection is weak, and in production the secret protects only if it stays out of reach of the person editing the data. Verified by running: field tamper → `brokenAt AUD-xxxx`, tail truncation → `ANCHOR`, content edit of a policy → recomputed `contentHash` ≠ stored `sha256`, interleaved Node/Python writes stay valid, legacy chains stay valid. Denials are sampled (≤ 1 entry per actor/action/minute, suppressed count in the next entry). The snapshot shows the last 200 entries (`auditTotal` for the rest; the chain is verified over all). `export_report` (L2+, itself audited) returns policies with recomputed content hashes, `policySetHash`, the full log, tickets and a `reportHash` over the canonical JSON of the whole body; CSV additionally lists tickets and neutralises formula-injection cells. Printable attestation: *Print / PDF Summary*.

## Verification performed (QA pass on branch `qa-hardening`)
Everything below was executed against running servers, not just read: `tsc`, `next build`, `py_compile`; first start in demo and production; gateway action × token matrix (79 requests, no 5xx, correct 403s); L1/L2 leakage checks; dynamic proof (10 → 15 Zeichen) in Node-only and Python+Ollama mode; KI policy answerable after approval; watched-inbox scenarios in both engines (13 checks each); hostile PDF/DOCX uploads (empty, encrypted, 320 pages, corrupt, 400 MB zip bomb); 48 concurrent mixed Node/Python calls; stale/foreign lock handling; Python killed mid-request; browser flows incl. XSS payloads in questions, document titles/text, authors and tickets (nothing executed; payloads rendered as text); backend status label in all three states. Test scripts were kept outside the repo.

## Known limitations and RAG proposals
**Security / operations**
- Role-level identity only (shared accounts); no SSO. L1 is unauthenticated by design, so L1 ticket creation is capped/idempotent instead of authenticated.
- Audit log is tamper-evident, not tamper-proof or append-only; the anchor secret must be kept outside the data directory (production) — demo mode cannot give that guarantee. No external anchoring/signing service.
- Rate limits are per process and in memory; per-IP needs `TRUST_PROXY=1` behind a reverse proxy. Every transaction rewrites the whole JSON file and each snapshot verifies the whole chain (fine for MVP scale, not for tens of thousands of entries).
- Unit tests (`eval/tests/`) and the eval harness (`eval/eval.py`) exist, but no CI runs them.
- Node and Python still differ in synonym/stop lists (Node only returns search hits); the conflict *rule* and file-name handling are aligned. `NODE_ENV=production` set only for Node (e.g. `next start`) while Python runs in demo mode makes the secrets disagree — export the same variables for both.
**RAG quality (dev set, 2026-10-22; `qwen2.5:7b` digest 845dbda0ea48, `bge-m3`; ground truth written for the dev corpus, holdout not yet run)**
| Mode | correct (of 188 answerable) | false answers | search hits (hits@3) | abstentions | contradictions (of 17) |
|---|---|---|---|---|---|
| Python + Ollama | 92 | 0 | 84 (73) | 8 | 14, 0 false flags |
| Python without Ollama | 0 (search hits only) | 0 | 175 (159) | 13 | 14, 0 false flags |
| Local engine | 0 (search hits only) | 0 | 152 (133) | 36 | — |
All 35 unanswerable questions refused in every mode, 0 leakage, 0 number/negation/modality errors, deterministic over two passes; p50/p95 619/3144 ms with Ollama. 21 `robust_*` questions on invented documents (tables without blank lines, colloquial wording, multi-part, cross-section, contradictions next to a dominating document): 4/17 correct, 0 false. A stronger judge (`qwen3:14b`) answered 144/188 but gave 9 false answers and p95 10.5 s, so it is not used. A judge with evidence duty (`JUDGE_EVIDENCE=1`: the judge names the answering span, then a deterministic answer-type check runs at that span) is only a measurement variant: with `qwen2.5:7b` 99/205 correct but 2 false answers (B-6, W-2) and 11 incomplete instead of 6; in the judge replay `qwen3:14b` still confirmed 10 wrong inputs, so `JUDGE_MODEL=qwen3:14b` was not run on the dev set.
**Known RAG limits:** the 7B judge rejects many correct windows (most remaining misses: combined sections, colloquial questions, multi-part pools); who-questions need a recognised role noun ("Werkschutz", "Facility Management" are not); contradictions S-5, R-3, I-17 are not found; dev numbers are tuned on the dev set — the holdout run is still open.
