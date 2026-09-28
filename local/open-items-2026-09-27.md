# Open items

- Done: generic cover scan and engine-181 derive. Listed `ok` rows rose from 5,803 to 6,715;
  `foreign` is 237, `pending_facts` is 55, `no_xbrl` is 49, and no snapshot is dirty.
- Done: full export wrote an engine-181 payload with 6,925 companies. NTES is present with its
  5:1 filing-backed ADS ratio and a dated quote. Payload SHA-256:
  `5ee9db25ec027d0c178151d04e778550f5a69320ba41afb32a6b8eb6c4e3fe9f`.
- Done: payload audit passed 279 sourced values and 3,411 arithmetic figures; filing audit also
  passed 378 published-statement values, with zero wrong values in every category.
- Done: full regression recomputed all 6,925 exported companies and reported no moved field.
- Open: decide whether "all foreign tickers" means all evidence-supported tickers or requires a
  new source/identity policy for the 237 rows the existing safety contract rejects.
- Active: read-only official-evidence inventory for those 237 foreign rows. Plan:
  `local/foreign-coverage-plan-2026-09-28.md`.
- Accepted and committed: the 237-row evidence inventory. Active: generic cover parser and
  classification fixes for the 47 parser rows; bounded rescan will also test the 57 missing-cover
  rows.
- Accepted and committed: generic cover parser/classification at engine 182. Active: bounded rescan
  of the 47 parser and 57 missing-cover CIKs before direct ratio work.
- The bounded rescan is paused until exact cover bytes are retained. Active: local cover-byte cache,
  explicit bounded reparse, and S3 importer linkage.
- Accepted and committed: retained exact cover bytes, bounded reparse, and S3 cover linkage. Active:
  preserve SQLite and run the explicit 104-CIK reparse.
- Done: bounded reparse processed 100 eligible CIKs and recovered 30 foreign rows. Active: direct
  filing-backed depositary-ratio parsing with retained primary-document evidence.
- Accepted and committed: direct ratio parsing at engine 183. Official evidence supports 19 current
  recoveries and nine safe exclusions. Active: bounded 28-CIK reparse and full engine gates.
- Done: engine-184 foreign coverage gates. Forty-nine foreign rows recovered; UI has 6,979 rows;
  regression changes are disclosure/provenance only; audits report zero wrong. Evidence:
  `local/foreign-coverage-e2e-2026-09-28/README.md`.
- Decided: a later official SEC filing may prove a same-class ticker change. Unsupported OTC aliases
  stay excluded. Implementation remains open for the affected rows.
- Owner decision required: whether an evidence-citing LLM extractor may supply candidates for the
  18 incomplete Company Facts rows, subject to deterministic checks and fail-closed publication.
- Operational limit: only 21 GiB disk remains. Screener volumes are bounded; unrelated OrbStack
  images/build cache and Horizon worktrees own most pressure. No unrelated data was removed.
- Accepted and committed: milestone 1 storage contract at Alembic `20260927_0003`.
- Accepted and committed: retained-evidence importer for ABT, NTES, and Panasonic `6752.T` with
  exact S3 readback and SQLite payload parity.
- Accepted and committed: FastAPI selected-company read. Real S3/PostgreSQL/FastAPI proof returned
  200 for all three companies with exact SQLite and dashboard-field parity.
- Done: live default-service import, Uvicorn HTTP reads, and service-restart reads passed for ABT,
  NTES, and `6752.T`. Evidence: `local/company-import-e2e-2026-09-28/README.md`.
- Accepted and committed after Review 2: full-universe import contract. Of 6,925 current `ok` snapshots, 5,393 SEC companies have
  cover identity, 1,530 SEC companies have no usable retained cover, and 2 are EDINET companies.
- Done: full retained-universe PostgreSQL import and failure reconciliation. 6,655 payloads match
  SQLite exactly; 270 rows are withheld for 147 ticker-map mismatches, 112 pending-facts states,
  and 11 missing depositary ratios. Evidence:
  `local/company-import-universe-2026-09-28/README.md`.
- The two bulk-import defects found during reconciliation are fixed. Old superseded PostgreSQL
  snapshots remain immutable history; current selections have zero payload mismatches.
- Open: 270 dashboard-eligible SQLite rows are withheld from PostgreSQL: 147 are absent or changed
  in the current SEC ticker map, 112 have newer filing facts still pending, and 11 lack a proven
  depositary ratio. Owner policy/source work is required before any of these can be included.
- Local commits are complete. Push and merge are not authorized.
