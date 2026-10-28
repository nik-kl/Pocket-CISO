# comp_1 – Policy Samples (dev1)

Fictitious company corpus used as the primary dev set since the start of the project (formerly `eval/corpus/` and
`eval/eval_dev.jsonl`). All names, figures and documents are invented.

- `docs/` — 20 documents:
  - 7 PDFs and 1 Markdown file (formerly `eval/corpus/inbox/`, imported through the watched-inbox connector),
  - 3 Markdown upload samples (formerly `eval/corpus/seed/`: `KI_Nutzungsrichtlinie_2026.md`, `NIS2_Incident_Playbook.md`,
    `Passwort_Richtlinie_v4.md`; formerly staged with `stage_document` at minRole L2 — now through the connector, same
    minRole and the same staging/approval path, answers unchanged),
  - 9 dev-only documents that `eval.py` used to generate in code (`dev_only.txt`): `Besucherrichtlinie_2026.md` with an
    embedded prompt injection, and 8 robustness documents for the `robust_*` questions (PDF tables with and without blank
    lines, colloquial and multi-part questions, cross-section evidence, contradictions next to a dominating document).
- `questions.jsonl` — 244 questions (223 dev + 21 `robust_*`), schema in `eval/eval.py`.
- `dev_only.txt` — documents imported for the dev questions only; `--holdout` runs on this corpus leave them out (as before).
- No `doc_roles.json`: every document has the connector default minRole L2; L1 questions test RBAC (no L2 content).
