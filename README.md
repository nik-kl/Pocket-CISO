# Pocket CISO

Pocket CISO is an on-premise compliance and policy assistant for DACH organizations. It combines a Next.js workspace with a local FastAPI/Ollama RAG engine to answer policy questions with citations, stage document changes for CISO approval, detect cross-document conflicts, and preserve an auditable governance trail.

The prototype is designed for a controlled enterprise demo:

- **Pipeline B — Ask Pocket CISO:** stateless, RBAC-filtered retrieval with German and English query support, grounded answers, citations, telemetry, and a hard fallback when evidence is insufficient.
- **Pipeline A — Policy Diff & Sign-Off:** PDF, DOCX, Markdown, and TXT ingestion; semantic red/green diffs; table-aware chunking; conflict detection; ticket-backed approval; and merge to the live policy collection.
- **Zero-touch ingestion:** the `data/inbox/` folder (gitignored; sample documents in `dummy-data/comp_1_policy-samples/docs/`) acts as a SharePoint/SMB connector stand-in. Manual sync or an optional polling cycle stages changed files for CISO review.
- **Closed-loop governance:** Jira/ServiceNow-style webhook payload previews, ticket resolution, hash-chained audit logs, JSON/CSV export, and a printable executive summary.
- **Hybrid resilience:** the browser uses Python first when available and falls back to the local TypeScript engine when Python or Ollama is offline.

> **Disclaimer:** Pocket CISO is an assistive prototype. It is not legal advice and does not provide legally binding approval. Answers are based only on indexed, approved policy content.

## Quick start

### 1. Install dependencies

```bash
npm install
python3 -m venv engine/.venv
engine/.venv/bin/pip install -r engine/requirements.txt
```

For the full local AI path, install Ollama and download the configured models:

```bash
ollama pull qwen2.5:7b
ollama pull bge-m3
```

The UI and Python rule engine can still run when Ollama is unavailable.

### 2. Start the system in demo mode

Use two terminals from the repository root:

**Terminal 1 — FastAPI engine**

```bash
POCKET_CISO_DEMO=1 npm run engine          # = engine/.venv/bin/python -m engine.api
```

**Terminal 2 — Next.js frontend**

```bash
POCKET_CISO_DEMO=1 npm run dev
```

Open <http://localhost:3000>. The Python API listens on <http://127.0.0.1:8000>.

Demo credentials:

- **L1 Employee:** `employee@company.at` — no password
- **L2 IT-Admin:** `admin@company.at` / `admin2026`
- **L3 CISO:** `ciso@company.at` / `ciso2026`

These credentials are available only when an explicit demo flag is set. `POCKET_CISO_DEMO_MODE=1` is accepted as a compatibility alias.

### 3. Optional watched-inbox polling

The UI’s **Sync Inbox** action is always available. To also poll the inbox every 30 seconds:

```bash
POCKET_CISO_DEMO=1 AUTO_SYNC=1 npm run engine
```

Place `.pdf`, `.docx`, `.md`, or `.txt` files in `data/inbox/` (or set `WATCHED_INBOX_DIR`); for the demo copy the samples with `mkdir -p data/inbox && cp dummy-data/comp_1_policy-samples/docs/* data/inbox/`. Files are SHA-256 deduplicated and staged with L2 connector clearance; the CISO must still approve them.

## Lokal starten

Einmalig: Abhängigkeiten installieren und die Konfiguration anlegen.

```bash
npm install
python3 -m venv engine/.venv
engine/.venv/bin/pip install -r engine/requirements.txt
cp .env.example .env        # Platzhalter in .env durch eigene Werte ersetzen
```

Dann zwei Terminals im Repository-Root; in **beiden** zuerst die Env laden (beide Prozesse brauchen dieselben Werte):

```bash
set -a; source .env; set +a
npm run engine              # Terminal 1: Python-Engine auf http://127.0.0.1:8000
npm run dev                 # Terminal 2: UI auf http://localhost:3000
```

Login: L1 ohne Passwort; L2 mit dem Wert von `POCKET_CISO_L2_PASSWORD`, L3 mit dem Wert von `POCKET_CISO_L3_PASSWORD` aus der `.env`.
Ohne Demo-Flag startet die Datenbank leer; Richtlinien kommen über Upload oder `data/inbox/` (Beispiele: `cp dummy-data/comp_1_policy-samples/docs/* data/inbox/`, dann *Sync Inbox*).

## Production configuration

Never run a deployment with the seeded demo credentials. Start **both** processes with the same values:

```bash
export POCKET_CISO_ENV=production
export POCKET_CISO_SECRET='replace-with-at-least-32-random-characters'
export POCKET_CISO_L2_PASSWORD='at-least-14-characters'
export POCKET_CISO_L3_PASSWORD='at-least-14-characters'

# terminal 1
npm run engine
# terminal 2
npm run build && npm run start
```

In production mode:

- demo flags are ignored; L2/L3 passwords (≥ 14 characters) and the secret (≥ 32 characters) are mandatory;
- a missing database starts **empty** with the role accounts and one genuine `DB_INITIALIZED` audit event — no demo policies, diffs or backdated history;
- passwords are checked with scrypt and are never stored in `data/`; the HMAC secret is never written to disk;
- `reset_demo` is disabled (403);
- missing configuration returns a generic 503 `AUTH_CONFIG_MISSING` (details only in the server log);
- put a reverse proxy with TLS in front and set `TRUST_PROXY=1` so rate limits work per client IP. **The proxy must overwrite (not append to) `X-Forwarded-For`** — otherwise a client can forge it and bypass the limits. Without `TRUST_PROXY=1` all users share one rate-limit bucket (widened ×10).

In demo mode (`POCKET_CISO_DEMO=1`) a local `data/.auth-secret` is generated with owner-only permissions when no `POCKET_CISO_SECRET` is supplied. Keep data directories out of backups that are readable by untrusted users: they contain the audit log and policy content.

## Main workflows

### Ask a policy question

1. Select an RBAC role in the sidebar.
2. Ask a question in **Ask Pocket CISO**.
3. Review the question parts, RBAC filter, retrieval score, lexical support, verification (span verifier, or "nicht verifiziert" for search hits), engine mode, and latency in **Under the Hood**. The retrieval score is a raw similarity, not a probability.
4. Open a citation to inspect the exact source chunk, SHA-256, approval metadata, and similarity.
5. Results are one of three kinds: a verified **answer**, labelled **search hits** (sources found but not confirmed as an answer, including "Mögliche Widersprüche" between documents), or a **fallback** ("Keine belegbare Antwort im freigegebenen Bestand.") with a CISO-office ticket.

### Stage and approve a policy update

1. Open **Policy Diff & Sign-Off**.
2. Click **+ Stage Policy Update** to upload a PDF/DOCX/Markdown/TXT file or paste policy text.
3. Choose the minimum clearance and optional replacement document.
4. Review semantic red/green hunks and cross-document conflict alerts.
5. Create a Sync-Ticket per conflict and resolve it (Knowledge DB & Audit Log), or tick the CISO override and give a reason (≥ 10 characters; audited with the conflict ids).
6. Approve and merge: changed clauses replace their predecessors, clauses that no longer exist are removed, and the document series is renamed to the new version. A stale base (live data changed since staging) is rejected.

### Sync the watched inbox

Use **Sync Inbox** beside the staging button, or enable `AUTO_SYNC=1`. The connector:

- scans supported files without following symlinks or dotfiles;
- compares file SHA-256 values with `meta.ingestedFiles`;
- skips unchanged files and already-pending diffs;
- preserves table rows as self-contained semantic statements;
- records staging, hashes, and `AUTO_CONNECTOR_SYNC` audit entries atomically.

### Resolve tickets and export evidence

In **Knowledge DB & Audit Log**:

- expand a ticket to inspect its Jira/ServiceNow-style webhook payload;
- resolve tickets as an L2/L3 user with a resolution note;
- export the NIS-2 audit report as JSON or CSV;
- open **Print / PDF Summary** to print the governance summary.

The report includes active policies with recomputed content hashes, the hash-chain status, recent approvals, a policy-set hash, and a report hash over the whole report (policies, audit log, tickets). The audit log is hash-chained and anchored with an HMAC, which makes tampering detectable — it is not append-only storage and not a legal certification.

External signing or anchoring (and individual user identities via SSO) is still required when the audit store itself must be treated as independently tamper-evident.

## Architecture

```text
Browser
  │
  ▼
Next.js /api/v1 gateway
  ├── Python FastAPI :8000 ── Ollama :11434
  │     └── BM25 + BGE-M3 (RRF), Qwen 2.5 7B judge + verified phrasing, document parsing
  └── TypeScript fallback engine
        └── keyword search hits, local staging, audit and ticket workflows

Both engines
  ├── data/pocket-ciso-db.json
  ├── data/.db.lock
  └── Python: data/vectors.json
```

The gateway routes query and policy mutations to Python whenever it is up (without Ollama, Python returns search hits only). Queries fall back to the local engine (search hits only) when Python is unavailable; mutation timeouts are returned rather than replayed locally to avoid double application. Authentication, ticket resolution, audit export, and demo reset remain local gateway actions.

## Backend endpoints

The FastAPI service exposes:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Backend, Ollama, model, vector, and database status |
| `POST /query` | Stateless RBAC-filtered RAG query with citations and telemetry |
| `POST /stage-file` | Upload or stage policy text for semantic diff and conflict scanning |
| `POST /sync-inbox` | Scan and stage changed watched-inbox documents |
| `POST /approve/{diff_id}` | Approve a staged diff and merge it into the live collection |

The Next.js gateway also exposes local actions for authentication, tickets, rejection, audit export, snapshots, and demo reset.

## Repository layout

```text
README.md
docs/                 AGENT_INDEX.md (lookup table), ARCHITECTURE_OVERVIEW.md
engine/               Python engine: api.py (FastAPI), config, llm, store, staging, inbox,
                      ingest/ (parse, chunk), search/ (normalize, facts, lexical, dense),
                      answer/ (gate, judge, verify, compose, contradictions, pipeline), glossary.json
eval/                 eval.py (harness, --company), tests/ — code only
dummy-data/           fictitious companies: <comp>/docs/, questions.jsonl, overview.md, optional doc_roles.json, dev_only.txt, forbidden_markers.txt
src/                  Next.js: app/ (UI + /api/v1 gateway), components/, lib/ (db/ = local TS engine)
data/                 runtime state incl. data/inbox/; gitignored
```

See [docs/AGENT_INDEX.md](./docs/AGENT_INDEX.md) for the file-to-symbol lookup and [docs/ARCHITECTURE_OVERVIEW.md](./docs/ARCHITECTURE_OVERVIEW.md) for the detailed pipeline and governance model.

## Verification

Run the mandated static checks:

```bash
rm -f tsconfig.tsbuildinfo && npx tsc --noEmit && npm run build
engine/.venv/bin/python -m compileall -q engine eval
npm run test:engine                      # 73 tests, no Ollama needed
engine/.venv/bin/python eval/eval.py --label X [--company comp_2] --modes ollama,noollama,node   # dev eval → /tmp/pocket-ciso-rag/results/X
```

Model switch only via `LLM_MODEL` (default `qwen2.5:7b`); `LLM_NUM_CTX` (4096) and `LLM_KEEP_ALIVE` (30m) tune Ollama. Latest dev numbers (0 false answers, 92/188 correct with Ollama) are in `docs/ARCHITECTURE_OVERVIEW.md`. Behavioural checks (RBAC matrix, dynamic proof, inbox scenarios, hostile uploads, concurrency) were run against live servers during the QA pass; see *Verification performed* in `ARCHITECTURE_OVERVIEW.md`.

## Current limitations and roadmap

This is a local MVP, not a production certification platform. See [docs/ARCHITECTURE_OVERVIEW.md](./docs/ARCHITECTURE_OVERVIEW.md) → *Known limitations and RAG proposals* for measured numbers. In short:

- role-level identity only (shared accounts) — OIDC/Entra ID SSO and individual actors are the next step;
- tamper-evident (hash chain + HMAC anchor), not append-only: PostgreSQL / an external anchor or signing service is needed for stronger guarantees;
- in-memory, per-process rate limits (per-IP needs a reverse proxy with `TRUST_PROXY=1`);
- retrieval quality: the 7B answerability judge is strict (about half of the answerable dev questions end as search hits, not answers), the sentence verifier is heuristic (numbers/units, polarity, modality, roles), and the dev numbers are not yet confirmed on a holdout set;
- unit tests and an eval harness exist, but no CI;
- OCR for scanned PDFs, multi-tenant collections, real Jira/ServiceNow connectors.
