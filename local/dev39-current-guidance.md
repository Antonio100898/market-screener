# SEC rebuilt-quarter reconciliation implementation

Updated: 2026-09-29
State: **ACCEPTED — Review 1; stop**

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`, the “Incremental ingestion
and reconciliation” and “S3 contract” sections of `docs/production-architecture.md`, increment 4 of
`docs/implementation-rollout.md`, `local/durable-ingestion-plan-2026-09-29.md`, and
`local/dev38-review-map.md`. Read and follow the shared code-work, applying-feature, ponytail, and
no-scaffolding-leaks skills.

Implement milestone 3B2 only: fixture-driven SEC rebuilt-quarter reconciliation. Do not add a
production schedule, FastAPI wiring, history horizon, filing-resource downloads, extraction,
current selection, or publication. Do not call SEC during development or tests.

## Decisions preserved

P-02, P-03, P-04, P-05, P-09, P-17.

## Files you may change

- `api/screener/source_observations.py`
- `api/screener/sec_ingestion.py`
- `api/tests/test_source_observations.py`
- `api/tests/test_sec_ingestion.py`
- `api/tests/integration/test_source_observation_storage.py`
- `api/tests/integration/test_sec_ingestion_storage.py`
- Developer update in `local/dev39-review-map.md`

If correctness needs another path, return `BLOCKED:` with the path and reason.

## Required behavior

1. Add `sec-index-reconciliation` for strict `year` and quarter `1..4` parameters. Fetch exactly
   `https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/form.idx`. One job handles one
   quarter; no recurrence or historical range is chosen.
2. Retain the exact raw index in S3 and fully parse it before recording inventory success or
   comparing filings. A 404/unavailable response may record an unavailable inventory observation,
   but must raise so the job stays retryable. Transport, partial, or parse failure records no
   removal and no checkpoint. A verified orphan artifact is safe.
3. Add a bounded PostgreSQL query for the latest observation of SEC filing items ever assigned to
   the requested quarter. Use database ordering by detection time and observation ID; do not add a
   current pointer or load other quarters into application memory.
4. Compare only form, company name, CIK, filing date, archive filename, accession, and quarter.
   New, changed, or reappeared filings get one present observation backed by the quarterly index and
   one guarded `sec-resource-fetch` child. Exact unchanged filings get no fake revision or child,
   but must get a durable job-item outcome naming the latest filing observation and the quarterly
   inventory witness.
5. A latest-present accession absent from the complete rebuilt index gets one removed observation,
   no artifact, and no child. Repeated absence after latest-removed creates nothing new but still
   gets a durable checked outcome. Filings known only in another quarter are untouched.
6. Removal metadata contains the prior comparison fields, reason
   `absent_from_rebuilt_quarter`, quarter, inventory item ID, inventory observation ID/hash, and
   inventory artifact hash. Add one repository method that validates this present SEC quarterly
   inventory witness and records/reuses the removal plus job outcome in the same live-lease
   transaction. Altered, missing, wrong-quarter, unavailable, or wrong-artifact witnesses fail.
7. Check the stop signal before new work. Save the final quarter checkpoint only after every present,
   unchanged, and removed item has a durable outcome and every needed child exists. Retry and
   replay are idempotent; stale owners write nothing after their lease expires.

Use the accepted SEC transport, parser, object/artifact stores, observation repository, guarded
child enqueue, and job context. No schema or migration is expected.

## Required proof

- Unit tests: strict parameters/URL; unchanged/new/changed/reappeared/removed/repeated-removed;
  another quarter untouched; witness validation failures; 404/transport/malformed inputs cannot
  remove or checkpoint; stop/retry; stable child identity; no child for unchanged/removal.
- Real PostgreSQL/S3: seed recent filing observations, reconcile a fixture quarter, verify exact
  index readback, latest states, witnessed removal, changed/new children only, unchanged outcomes,
  duplicate replay, interruption, reconstructed repositories/runtime, and stale-owner zero writes
  beyond a verified orphan.
- Run accepted SEC recent-discovery, source-observation, durable runtime tests, full Python suite,
  Alembic checks, and `git diff --check`. No migration should appear.
- Stop PostgreSQL/S3 cleanly; preserve volumes.

No external source/model APIs, secrets, SQLite/cache mutation, commit, stash, or branch switch.

## Report

Update only `## Developer update` in `local/dev39-review-map.md`. List behavior, exact tests and
counts, real evidence, stopped services, limits, and concurrent changes. End with
`READY_FOR_REVIEW`, `BLOCKED`, or `CONTINUE`. Do not mark the review accepted.
