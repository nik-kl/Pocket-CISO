# Pocket CISO — Architecture Overview

On-premise compliance & policy RAG middleware for the DACH market. A Next.js app with an optional local AI backend (**Python FastAPI + Ollama**), a **JSON-file database** shared by both runtimes, and an **automatic fallback to a pure-TypeScript engine** when Python/Ollama are not running. The UI always works; Python only upgrades the engine.

[README.md](./README.md) = setup & pitch · [AGENT_INDEX.md](./AGENT_INDEX.md) = file/endpoint/env lookup table · this file = architecture, security model, limitations.

## Run it (2 terminals)
```bash
# demo mode
cd backend && export POCKET_CISO_DEMO=1 && source .venv/bin/activate && python main.py   # terminal 1 → :8000
POCKET_CISO_DEMO=1 npm run dev                                                           # terminal 2 → :3000
```
```bash
# production mode — both processes need the same values
export POCKET_CISO_ENV=production POCKET_CISO_SECRET='<32+ chars>' POCKET_CISO_L2_PASSWORD='<14+ chars>' POCKET_CISO_L3_PASSWORD='<14+ chars>'
cd backend && source .venv/bin/activate && python main.py        # terminal 1
npm run build && npm run start                                     # terminal 2 (NODE_ENV=production)
```
First-time setup: `npm install`; `cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`; optional `ollama pull qwen2.5:7b && ollama pull bge-m3`.

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
| `Python FastAPI + Ollama (Connected)` | `bge-m3` embeddings, NumPy cosine | `qwen2.5:7b` rewrite + grounded answer, rule + LLM conflict verdicts, PDF/DOCX/MD/TXT |
| `Python FastAPI · Rule Engine (Ollama offline)` | deterministic hash-n-gram embeddings (staging/conflicts only — **queries are answered by the local keyword engine** in this state, it measured better) | rule conflicts, PDF/DOCX/MD/TXT |
| `Local Fallback Engine (Active)` | keyword coverage + synonyms | verbatim extractive answers, rule conflicts, `.md/.txt` only |
All three share `data/pocket-ciso-db.json`; Python keeps `data/vectors.json` and re-embeds what the other engine changed. If Ollama flaps, the expensive Ollama vector set is kept in a stash and restored instead of re-embedding everything.

## Request flow and gateway protections
```
Browser ─► /api/v1 (src/app/api/v1/route.ts)
   ├─ query · stage_policy · stage_file · sync_inbox · approve ──► Python :8000 ──► Ollama :11434   (offline → same action locally; mutation timeouts are not replayed)
   └─ authenticate · tickets · reject · export_report · snapshot ──► src/lib/db.ts
```
Rate limits (sliding window per client key and action; 429 + `Retry-After`): query 20/min, authenticate 10/min, create_ticket 5/10 min, stage 20/min, sync 10/min, snapshot 180/min, others 60/min. The client key is the proxy-supplied IP only with `TRUST_PROXY=1` — and only when the proxy **overwrites** `X-Forwarded-For`, otherwise clients can forge it. Without it all callers share one `direct` bucket: its limits are widened ×10 (so one office cannot rate-limit itself), the login limit is the account ceiling only (30 failures per 15 min, then backoff), and a startup warning is logged in production. Residual risk without a proxy: a persistent attacker can keep L3 logins in backoff; per-IP isolation (verified: attacker IP locked, other IP unaffected) needs the proxy. Limits are in memory and reset on restart. Login: per (client, role) 5 failures per 15 min then exponential backoff (30 s · 2ⁿ, max 15 min) plus a higher per-account ceiling (30 per 15 min) so an anonymous caller cannot trivially lock out L3. JSON bodies are read through a 256 KB cap (413; a 150 MB body used to be buffered, ≈ 1 GB RSS). Uploads: `Content-Length` required (411), ≤ 10.25 MB (413), L2 token verified **before** `formData()`/`request.form()` is read; Python repeats this and size-checks DOCX archives (64 MB uncompressed) before parsing.

## Auth and identity
Stateless token `ROLE.expiry.HMAC` (8 h), verified by Node and Python with the same secret (byte-length-safe comparison; garbage → L1). L1 has no login by design. **Identity is still role-level:** the audit `actor` is one of three shared accounts (`employee@`, `admin@`, `ciso@company.at`). The role → directory-group mapping shown in the UI (`oidc:SG-All-Staff` / `-IT-Infrastructure` / `-CISO-Governance`) is the contract for a future OIDC/Entra rollout; individual accountability needs that SSO.

## Pipeline B — stateless compliance chat
1. **Rewrite** (LLM temp 0, validated; otherwise rules). Narrow bilingual `queryHint`/`REWRITES` entries exist for verified intents (see limitations).
2. **RBAC pre-filter** before ranking. A restricted-only match returns the **same** `fallback` as "no match": no score, role or existence hint (the audit still records `RBAC_DENIED`).
3. **Gate:** dense cosine ≥ `MIN_SCORE` (0.55 for bge-m3) **and** lexical support ≥ `LEX_MIN` (0.34; calibrated floor for multi-intent questions).
4. **Answer:** ≤ 4 chunks → LLM (temp 0, `[C1]` citations) → grounding check → otherwise extractive. A citation the model did not use is never added.
**What the numbers mean.** `trace.retrievalScore` is the raw cosine (Python) or query-term coverage (local engine); `lexicalSupport` the share of question terms found; `confidence` is kept as an alias of `retrievalScore` and **is not a probability** (see calibration below). The fallback text still says "Konfidenz < 85 %" because that wording was specified by the product owner; the actual gate is the one above. `verification` is `lexical-nli` (Python; numbers must occur in the cited chunks, ≥ 60 % content words supported, no negation / obligation↔permission / number–unit contradiction) or `extractive` (local engine: verbatim chunk text, no NLI is claimed).

## Pipeline A — ingestion, diff, conflicts, approval
Parse (`pypdf`, `python-docx` incl. tables in body order, text) → table-aware chunking (≤ 600 chars; KPI lines such as `2 Stunden` / `250.000 EUR` stay content) → embeddings → alignment with the existing **document series** (name similarity or explicit "replaces") → red/green diff + `removedChunkIds` (old chunks without counterpart) → conflict scan against other documents the actor may see (rules: same unit, different value on a shared topic — identical in both engines; Python adds cosine ≥ `CONFLICT_SIM` and an LLM verdict, `verifiedBy` shows which) → pending diff. The shown author is the verified session; a client name appears as "angegeben: …". Re-submitting an identical pending diff returns the existing one (safe after a gateway timeout).

**Approval semantics** (identical in both engines): (1) stale-base check — every old chunk the diff touches must still be live with unchanged content, else 409 `STALE_DIFF`; (2) every conflict needs a Sync-Ticket in state **resolved**, or `override` with a reason ≥ 10 chars (the audit lists the accepted conflict ids and the reason); (3) merge: changed hunks replace their old chunk; for **complete document uploads, old chunks without counterpart are removed** (the diff shows them in a red "ENTFERNT" block) — pasted rule text is a partial update and removes nothing (a single pasted clause used to delete all other sections of the document), remaining chunks of the replaced series are renamed to the new document (hash refreshed), vectors move to the new ids. Not done: conflicts are not re-evaluated at approval time.

## Zero-touch connector (`backend/watched_inbox/`)
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
- No tests or CI in the repository (the repo rules forbid new files); QA scripts were scratch-only.
- Node and Python still differ in synonym/stop lists and the hard-coded query hints; the conflict *rule* and file-name handling are aligned. `NODE_ENV=production` set only for Node (e.g. `next start`) while Python runs in demo mode makes the secrets disagree — export the same variables for both.
**Keyword mode:** the local engine answers without an LLM and still gave an unsupported answer for 2 of 19 out-of-scope questions (10.5 %); answers from `Local`/`Rule-based` engines carry a visible "Keyword-Modus · geringe Präzision" badge.
**RAG (66-question evaluation, 8 documents / 79 chunks, ground truth written for this corpus)**
| Mode | correct before → after | false fallback | false answer |
|---|---|---|---|
| Python + Ollama (qwen2.5:7b, bge-m3) | 70.2 % → **76.6 %** | 14.9 % → 10.6 % | 0 % → 0 % |
| Python rule engine (hash embeddings; retrieval measured directly, no longer used for gateway queries) | 36.2 % → 34.0 % | 46.8 % → 46.8 % | 5.3 % → 5.3 % |
| Local fallback engine (keyword) | 55.3 % → 59.6 % | 25.5 % → 25.5 % | 10.5 % → 10.5 % |
"Before" = original code and mis-chunked corpus (KPI values split into headings); "after" = all fixes. No restricted content leaked to L1 in any mode. Displayed confidence did not separate right from wrong answers (before: 0.888 vs 0.876; after, honest retrieval score: 0.663 vs 0.625), while the raw dense score of the best chunk separates answerable from unanswerable questions very well (AUROC 0.997) — the gate works, the number is just not a probability.
Experiments (Python + Ollama): hints disabled → 66.0 % (−8.5 pp; exactly the USB-in-train ×2, critical-risk-acceptance and emergency-USB questions ⇒ these acceptance items are hint-driven); `LEX_MIN=0.20` → **91.5 %** correct but 5.3 % false answers (1/19: a prompt-injection question mentioning "CISO"); `LEX_MIN=0.50` → 70.2 %; `MIN_SCORE=0.50` → 76.6 %; `MIN_SCORE=0.60` → 59.6 %. A stricter multi-intent floor in the local engine lowered correctness (59.6 → 53.2 %) without removing its two false answers, so it was reverted.
Ranked proposals (benefit / cost / risk, estimated effect on this set unless stated):
1. **Use lexical overlap as a ranking signal instead of a hard gate** (e.g. `LEX_MIN` 0.20 with `MIN_SCORE` 0.55): +15 pp correct, +5 pp false answers on this set; S effort; risk — the margin at 0.55 is thin (lowest answerable score 0.550) and the set is small ⇒ validate on a held-out set first.
2. **Replace the hard-coded hints by general bilingual query expansion** (glossary or one extra LLM multi-query call, ~1–2 s): keeps the 4 hint-dependent questions without overfitting; M effort.
3. **Parent-section context / chunk overlap** (heading chunk vs clause chunk is why "Lösegeld zahlen?" and "Kühlschrank" cite the wrong chunk): est. +2–4 questions; M.
4. **Hybrid BM25 + dense with reciprocal-rank fusion**, then a **cross-encoder reranker** (e.g. bge-reranker-v2-m3, +1–2 s): better precision than a lexical gate; M–L.
5. **Version/conflict-aware answering** (document status/effective date in ranking; show conflicting live chunks): "ab welchem Risikowert Geschäftsführung" answered 12 (BIA) while the risk policy says 6 — the corpus contradicts itself and the answer does not say so; M.
6. **Real entailment** (NLI cross-encoder or LLM judge, sentence-level citation check) instead of the heuristic guard (now catches negation, obligation/permission swaps and number–unit errors, nothing deeper); M.
7. **Calibrated confidence** (logistic regression on the retrieval score + lexical support from a labelled set); S once a held-out set exists.
8. **Held-out regression set + CI kept outside prompts/hints** — prerequisite for 1–7; S.
9. Vector database only when scale demands it (JSON + NumPy is fine below ~10⁴ chunks).
