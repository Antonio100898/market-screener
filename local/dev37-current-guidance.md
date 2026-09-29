# SEC recent-index discovery implementation

Updated: 2026-09-29
State: **ACCEPTED — Review 2; stop**

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`, the “Incremental ingestion
and reconciliation”, “S3 contract”, and “Durable jobs in one service” sections of
`docs/production-architecture.md`, increment 4 of `docs/implementation-rollout.md`,
`local/durable-ingestion-plan-2026-09-29.md`, and `local/dev35-review-map.md`. Read and follow the
shared code-work, applying-feature, ponytail, and no-scaffolding-leaks skills.

Implement milestone 3B1 only: fixture-driven SEC recent form-index discovery. Do not add a
production schedule, FastAPI wiring, quarterly removal reconciliation, filing-resource downloads,
extraction, current snapshot selection, or publication. Do not call SEC during development or
tests.

## Decisions preserved

P-02, P-03, P-04, P-05, P-09, P-17.

## Files you may change

- `api/screener/durable_jobs.py`
- `api/screener/job_runtime.py`
- `api/screener/sec_ingestion.py`
- `api/screener/sources/edgar.py`
- `api/tests/test_durable_jobs.py`
- `api/tests/test_job_runtime.py`
- `api/tests/test_sec_ingestion.py`
- `api/tests/integration/test_durable_job_storage.py`
- `api/tests/integration/test_sec_ingestion_storage.py`
- Developer update in `local/dev37-review-map.md`

If correctness needs another path, return `BLOCKED:` with the path and reason.

## Required behavior

1. Add a public SEC transport result that returns exact bytes, final URL, media type, ETag, and
   Last-Modified through the existing `EdgarClient` request limiter/retry path. Keep existing Edgar
   APIs unchanged. Tests inject responses; make no live calls.
2. Parse SEC `form.YYYYMMDD.idx` fixed-width content into exact form, company name, ten-digit CIK,
   filing date, archive filename, and dashed accession. Preserve the existing accepted financial
   form family, including amendments and transition variants. Reject a changed/malformed index
   shape instead of returning a silent partial list.
3. Add `sec-recent-discovery` handler for an explicit inclusive date range from durable job
   parameters. Bound one job to at most 31 dates. There is no recurrence rule or default overlap.
4. For each available day, retain the exact raw index through verified S3 storage, record one
   present inventory observation, then record each financial filing observation with exact row and
   quarter provenance. Filing source-item identity is accession and issuer CIK; do not give it an
   inventory parent because a later rebuilt index is another valid source.
5. A confirmed 404/non-filing day records an unavailable inventory observation and durable outcome.
   Transient transport errors fail/retry the job; they are not source unavailability.
6. After each filing observation, enqueue one stable `sec-resource-fetch` child job guarded by the
   same live lease. Add the smallest repository method needed so an expired worker cannot enqueue.
   The child key includes the filing observation hash; parameters carry accession, CIK, form,
   filename, and observation ID. It inherits parent priority and is due immediately. Repeated
   delivery reuses the child.
7. Save a checkpoint only after every day item has a durable observation/outcome and every filing
   has a durable child job. A retry or overlapping date range reuses artifacts, observations, and
   child jobs. A crash between any two steps can repeat work without loss or duplicates.
8. Expose the read-only lease token from `JobContext`; never expose a database connection. S3/HTTP
   work remains outside PostgreSQL transactions.

Use one injected `EdgarClient`, object store, artifact repository, source-observation repository,
durable-job repository, and clock. Do not create a second financial parser or call derivation.

## Required proof

- Unit tests for fixed-width parsing, amendments/transition forms, malformed shape, range/parameter
  validation, 404 versus transient failure, stable child identity, and live-lease guarded enqueue.
- Fixture index includes domestic and foreign filers plus an amendment; prove the same generic path.
- Real PostgreSQL/S3 test runs an overlapping discovery twice and after repository/runtime restart:
  exact index bytes read back; one inventory revision; one filing revision per accession/source
  revision; one child job per filing observation; unchanged counts on replay.
- Forced interruption after observation but before child/checkpoint must recover without loss.
- Expired owner can create no child/checkpoint; an already uploaded orphan artifact is allowed.
- Run existing durable runtime/source-observation suites, full Python suite, Alembic checks, and
  `git diff --check`. No migration should appear.
- Stop PostgreSQL/S3 cleanly; preserve volumes.

No external source/model APIs, secrets, SQLite/cache mutation, commit, stash, or branch switch.

## Report

Update only `## Developer update` in `local/dev37-review-map.md`. List behavior, exact tests and
counts, real restart/replay evidence, stopped services, limits, and concurrent changes. End with
`READY_FOR_REVIEW`, `BLOCKED`, or `CONTINUE`. Do not mark the review accepted.
