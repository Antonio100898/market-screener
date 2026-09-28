# Durable job repository review map

Developer state: **IDLE — accepted after Review 2**
Current item: **PostgreSQL durable-job schema and repository**
Review state: **ACCEPTED — Review 2**

The implementation report belongs under `## Developer update`. The reviewer owns the header and
reviewer response.

## Developer update

### Implementation

- Added Alembic revision `20260929_0005` and matching SQLAlchemy Core metadata for unique schedule
  occurrences, durable jobs, and per-item outcomes. Named constraints enforce lifecycle states,
  active ownership, retry/finish times, attempts, deadlines, bounded errors, and identities. The
  claim index covers status, due/retry time, descending priority, and stable job ID.
- Added typed job, item, claim, and lease records in `DurableJobRepository`. Scheduled enqueue is
  atomic and idempotent; conflicting immutable content raises `JobIdentityConflict`. Claim uses a
  short `FOR UPDATE SKIP LOCKED` transaction and closes before return. JSON inputs reject
  non-standard `NaN` and infinity values before SQL.
- Every renewal, checkpoint, item outcome, completion, and failure locks and verifies the running
  row, owner, ownership generation, and unexpired lease in the same transaction. Completion and
  failure save an optional final checkpoint atomically. Expired leases recover to bounded retry or
  visible terminal failure; a replacement claim receives a higher generation.
- Retry uses capped exponential delay with injectable jitter. Source/network work, scheduling,
  publication, and SQLite behavior were not added or changed.

### Proof

- `cd api && .venv/bin/pytest -q tests/test_durable_jobs.py`: **14 passed**.
- `RUN_STORAGE_INTEGRATION=1 .venv/bin/pytest -q tests/integration/test_durable_job_storage.py
  tests/integration/test_shared_company_storage.py`: **13 passed** against PostgreSQL 18.6. This
  covered concurrent duplicate occurrence, identity conflict, a two-worker single-claim race,
  database state constraints, priority/stable claim order, renewal, retry and terminal failure,
  deadline, out-of-order item outcomes, atomic final checkpoint, restart recovery, higher
  generation, and rejection of all five old-owner mutations.
- Migration tests upgraded fresh, `0002`, `0003`, and populated `20260928_0004` databases to
  `20260929_0005`. The preserved local database also upgraded from `0004` to `0005`.
- `alembic current`: `20260929_0005 (head)`; `alembic heads`: `20260929_0005 (head)`;
  `alembic check`: `No new upgrade operations detected.`
- `cd api && .venv/bin/pytest -q`: **865 passed, 16 skipped, 1 existing Starlette warning**. The
  skipped storage tests were run separately with the storage flag above.
- `git diff --check`: passed. PostgreSQL was stopped cleanly; SeaweedFS was never started. Volumes
  remain preserved.

### Limits and concurrent work

This milestone does not add the scheduler, worker loop, FastAPI lifecycle, source calls, source
budgets, current-snapshot selection, or publication. No external API was called and no SQLite/cache
data was changed. The only pre-existing working-tree files at start were the untracked Dev32
guidance and review map; no conflicting production edits appeared.

READY_FOR_REVIEW

## Reviewer response

### Review 1 — corrections required

Strict JSON validation admitted `NaN`/`Infinity`, and the real PostgreSQL suite did not race two
workers for one job. The implementation was returned for those two focused corrections.

### Review 2 — accepted

Accepted. Strict JSON now rejects non-JSON numbers before SQL. The added PostgreSQL race gives one
job to exactly one of two workers. The reviewer reran 14 focused unit tests, 13 real PostgreSQL and
migration tests, and the full Python suite: 865 passed, 16 skipped, one existing warning. Alembic is
clean at `20260929_0005`; diff check passed. PostgreSQL was stopped with its volume preserved.
