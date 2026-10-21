# Pocket CISO

Pocket CISO is an on-premise compliance and policy assistant for DACH organizations. It combines a Next.js workspace with a local FastAPI/Ollama RAG engine to answer policy questions with citations, stage document changes for CISO approval, detect cross-document conflicts, and preserve an auditable governance trail.

The prototype is designed for a controlled enterprise demo:

- **Pipeline B — Ask Pocket CISO:** stateless, RBAC-filtered retrieval with German and English query support, grounded answers, citations, telemetry, and a hard fallback when evidence is insufficient.
- **Pipeline A — Policy Diff & Sign-Off:** PDF, DOCX, Markdown, and TXT ingestion; semantic red/green diffs; table-aware chunking; conflict detection; ticket-backed approval; and merge to the live policy collection.
- **Zero-touch ingestion:** the flat `backend/watched_inbox/` folder acts as a SharePoint/SMB connector stand-in. Manual sync or an optional polling cycle stages changed files for CISO review.
- **Closed-loop governance:** Jira/ServiceNow-style webhook payload previews, ticket resolution, hash-chained audit logs, JSON/CSV export, and a printable executive summary.
- **Hybrid resilience:** the browser uses Python first when available and falls back to the local TypeScript engine when Python or Ollama is offline.

> **Disclaimer:** Pocket CISO is an assistive prototype. It is not legal advice and does not provide legally binding approval. Answers are based only on indexed, approved policy content.

## Quick start

### 1. Install dependencies

```bash
npm install
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..
```

For the full local AI path, install Ollama and download the configured models:

```bash
ollama pull qwen2.5:7b
ollama pull bge-m3
```

The UI and Python rule engine can still run when Ollama is unavailable.

### 2. Start the system in demo mode

Use two terminals from the repository root:

**Terminal 1 — FastAPI backend**

```bash
cd backend
export POCKET_CISO_DEMO=1
source .venv/bin/activate
python main.py
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
cd backend
export POCKET_CISO_DEMO=1
export AUTO_SYNC=1
source .venv/bin/activate
python main.py
```

Place `.pdf`, `.docx`, `.md`, or `.txt` files in `backend/watched_inbox/`. Files are SHA-256 deduplicated and staged with L2 connector clearance; the CISO must still approve them.

## Production configuration

Never run a deployment with the seeded demo credentials. Start **both** processes with the same values:

```bash
export POCKET_CISO_ENV=production
export POCKET_CISO_SECRET='replace-with-at-least-32-random-characters'
export POCKET_CISO_L2_PASSWORD='at-least-14-characters'
export POCKET_CISO_L3_PASSWORD='at-least-14-characters'

# terminal 1
cd backend && source .venv/bin/activate && python main.py
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
3. Review the rewrite, RBAC filter, retrieval score, lexical support, verification mode (grounding check or verbatim extract), engine mode, and latency in **Under the Hood**. The retrieval score is a raw similarity, not a probability.
4. Open a citation to inspect the exact source chunk, SHA-256, approval metadata, and similarity.
5. Unsupported questions trigger the hallucination-protection fallback and offer a CISO-office ticket.

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
  │     └── Qwen 2.5 7B + BGE-M3, NumPy vectors, document parsing
  └── TypeScript fallback engine
        └── keyword retrieval, local staging, audit and ticket workflows

Both engines
  ├── data/pocket-ciso-db.json
  ├── data/.db.lock
  └── Python: data/vectors.json
```

The gateway routes query and policy mutations to Python first. Query requests may fall back when Python is unavailable; mutation timeouts are returned rather than replayed locally to avoid double application. Authentication, ticket resolution, audit export, and demo reset remain local gateway actions.

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

## Flat repository layout

Documentation and operational entry points stay at the repository root. Application code remains in the compact existing `src/` layout, and backend runtime code stays flat in `backend/`:

```text
README.md
AGENT_INDEX.md
ARCHITECTURE_OVERVIEW.md
backend/
  main.py
  rag_engine.py
  requirements.txt
  seed_policies/
  watched_inbox/
src/
  app/
  components/
  lib/
data/                         # runtime state; gitignored
```

See [AGENT_INDEX.md](./AGENT_INDEX.md) for the file-to-symbol lookup and [ARCHITECTURE_OVERVIEW.md](./ARCHITECTURE_OVERVIEW.md) for the detailed pipeline and governance model.

## Verification

Run the mandated static checks:

```bash
rm -f tsconfig.tsbuildinfo && npx tsc --noEmit
backend/.venv/bin/python -m py_compile backend/main.py backend/rag_engine.py
```

These are the static checks. Behavioural checks (RBAC matrix, dynamic proof, inbox scenarios, hostile uploads, concurrency) were run against live servers during the QA pass; see *Verification performed* in `ARCHITECTURE_OVERVIEW.md`.

## Current limitations and roadmap

This is a local MVP, not a production certification platform. See [ARCHITECTURE_OVERVIEW.md](./ARCHITECTURE_OVERVIEW.md) → *Known limitations and RAG proposals* for measured numbers. In short:

- role-level identity only (shared accounts) — OIDC/Entra ID SSO and individual actors are the next step;
- tamper-evident (hash chain + HMAC anchor), not append-only: PostgreSQL / an external anchor or signing service is needed for stronger guarantees;
- in-memory, per-process rate limits (per-IP needs a reverse proxy with `TRUST_PROXY=1`);
- retrieval quality: hard-coded bilingual query hints, a strict lexical gate (see the ranked proposals), contradictions between documents are not surfaced in answers, and the grounding check is heuristic (polarity, modality, number/unit), not a real entailment model;
- no automated test suite or CI in the repository yet;
- OCR for scanned PDFs, multi-tenant collections, real Jira/ServiceNow connectors.
