# Market Screener handoff — 2026-09-27

Start with `HANDOFF.md`, `local/company-import-plan-2026-09-27.md`, and
`local/dev7-current-guidance.md`.

Branch: `codex/company-import` from pulled `origin/main` at `af6f8e3`.

Completed runtime repair:

- The generic SEC scan processed 1,234 foreign/pending covers. Engine 181 derived all 7,246
  eligible stale/dirty snapshots. Listed `ok` rows rose by 912 to 6,715; NTES is `ok` with its
  filing-backed 5:1 ADS ratio. The remaining 237 `foreign` rows fail the recorded evidence rules.
- Full export wrote 218.55 MB for 6,925 companies. NTES is in the UI payload. SHA-256 is
  `5ee9db25ec027d0c178151d04e778550f5a69320ba41afb32a6b8eb6c4e3fe9f`.
- `make audit` passed 279 sourced and 3,411 arithmetic figures. `make audit-filings` added 378
  published-statement checks. All three categories reported zero wrong values.
- `make regress ARGS='--all'` recomputed all 6,925 companies and reported `no field moved`.

Accepted and committed locally:

- Milestone 1 storage contract passed Review 2. Evidence is in `local/dev4-review-map.md`.
- Milestone 2 retained-evidence importer passed Review 1. Evidence is in
  `local/dev5-review-map.md`.
- Milestone 3 PostgreSQL FastAPI read passed Review 2 after OrbStack recovery. Evidence is in
  `local/dev6-review-map.md`.

Completed and committed locally:

- Milestone 4 passed against the default PostgreSQL/S3 services and a real Uvicorn process,
  including service restart. Evidence is in `local/company-import-e2e-2026-09-28/README.md`.
- Milestones 5 and 6 are accepted and committed locally. The universe import has 6,655 exact payload matches
  and 270 explicit evidence/pending failures. Evidence is in
  `local/company-import-universe-2026-09-28/README.md`.

Next action: implement the approved later-SEC-filing rule for same-class ticker changes, then obtain
owner direction on evidence-citing LLM extraction for incomplete statements. Non-`ok` foreign rows
remain excluded unless an approved evidence source satisfies the existing invariant.

Continuation: the owner asked to continue. Foreign coverage investigation is active through
`local/dev9-current-guidance.md`; the inventory passed Review 1 and no foreign safety rule is
relaxed.

Foreign continuation result: generic cover and direct-ratio work is accepted and committed at engine
184. Forty-nine foreign rows were recovered; 188 remain excluded. Full evidence and next decisions
are in `local/foreign-coverage-e2e-2026-09-28/README.md`.
Local commits: `44aed77`, `ddc2382`, and `f068de6`, plus the records commit that contains this file.
Do not push or merge without explicit owner approval.
