# Developer 31 — review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Durable automatic ingestion investigation**
Review state: **ACCEPTED — Review 1**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

### Finding

The durable-job layer does not exist yet. Current automatic work is one daemon thread with
process-local state and a SQLite timestamp. The smallest correct first slice is a PostgreSQL-only
job repository. It can land before any production schedule is chosen or any source is called.

The correct boundary is: a scheduler inserts an idempotent schedule occurrence; a worker claims a
persisted job under a lease; SEC/EDINET work runs outside the claim transaction; each database
result and checkpoint commits only after checking the same owner and ownership generation. Source
adapters do not own job state, and the browser never starts this path.

### Current path

- FastAPI starts and stops only the in-process quote scheduler in its lifespan
  (`api/screener/api.py:37-46`). The scheduler polls every 30 seconds, computes due time from
  SQLite `sync_state`, and starts one process-local thread (`api/screener/jobs.py:123-207`). Job
  status, cancellation, owner state, and progress exist only in module globals
  (`api/screener/jobs.py:16-22`, `api/screener/jobs.py:51-97`). The active worker is a daemon and is
  not drained by the lifespan shutdown.
- CLI and UI entry points call the same SQLite sync functions. `POST /sync` starts a thread
  (`api/screener/api.py:219-244`); the CLI exposes SEC, EDINET, derive, export, and related commands
  (`api/screener/sync.py:3418-3479`); Make targets remain direct CLI wrappers (`Makefile:11-54`).
- SEC discovery scans daily index files, stores only a `last_daily_index` string, then refetches
  Company Facts for affected issuers (`api/screener/sync.py:2432-2495`). A failed day is skipped in
  memory; a later successful day can advance the cursor past it. There is no durable item outcome
  for that missed day. `EdgarClient` has useful transport retry and a shared per-instance 10/sec
  limiter, but not a cross-process/current-versus-backfill budget
  (`api/screener/sources/edgar.py:31-51`, `api/screener/sources/edgar.py:92-114`).
- EDINET lists one date, downloads one ZIP, maps it, and writes SQLite/files directly
  (`api/screener/sources/edinet.py:25-56`, `api/screener/sync.py:2569-2643`). It has no retry,
  request budget, cursor, source-status history, or withdrawal/edit handling. The weekday GitHub
  workflow restores a cache and runs this CLI (`.github/workflows/edinet-sync.yml:18-35`,
  `.github/workflows/edinet-sync.yml:107-136`); it is not the shared PostgreSQL job system.
- PostgreSQL currently has only artifact, issuer, security, snapshot, artifact-link, and current
  snapshot tables (`api/screener/postgres.py:30-235`). The Alembic head is
  `20260928_0004` (`api/migrations/versions/20260928_0004_optional_security_evidence.py:11-39`).
  There are no schedule, job, item, checkpoint, lease, or source-cursor tables.

### Reuse and compatibility

- Reuse unchanged: `ImmutableObjectStore` plus `store_evidence` for verified S3 bytes
  (`api/screener/artifacts.py:31-67`); EDINET XBRL parsing/filtering and its canonical mapper;
  `sync._derive_evidence` as the single financial derivation path; PostgreSQL engine/config and the
  existing Alembic chain.
- Reuse after a narrow storage-facing adapter exists: `EdgarClient` request/rate-limit behavior and
  `EdinetClient` fetch methods. Their filesystem writes and direct SQLite orchestration are not the
  durable worker boundary.
- Keep as compatibility behavior until cutover: `jobs.py`, `/sync`, all existing `screener.sync`
  commands, SQLite `sync_state`/`pending_filing`, and the GitHub EDINET workflow. Do not dual-write
  their job state into PostgreSQL.
- `CompanyImporter` is a migration tool: it reads retained SQLite/files and then stores/selects a
  PostgreSQL snapshot (`api/screener/company_import.py:94-175`,
  `api/screener/company_import.py:499-555`). Reuse its retention and derivation owners, not the
  importer itself, for live ingestion.
- `SharedCompanyRepository.store_and_select` opens its own transaction and changes the current
  pointer immediately (`api/screener/shared_companies.py:79-170`). A future leased worker cannot
  use that method to publish safely. Candidate storage, release validation, expected-parent checks,
  and atomic activation belong to increment 5.

### Required durable-job gap

| Required rule | Current implementation | Gap |
|---|---|---|
| Schedule identity | Quote due time inferred from three SQLite timestamps | No unique persisted occurrence |
| Kind and parameters | Command name plus filtered kwargs in memory | Not durable |
| Status and priority | Process dict; single worker | No database status or current-over-backfill ordering |
| Due time | Recomputed from the last attempt | Not stored; no general schedules |
| Attempts, retry, deadline | Source-local retries; one quote-attempt timestamp | No job limit, bounded jitter, next retry, or deadline |
| Owner, lease, generation, heartbeat | None | Crash or replacement ownership cannot be proved |
| Checkpoint | Coarse `last_daily_index` string | No guarded structured checkpoint |
| Job items | None | Partial and out-of-order batches are not recoverable |
| Stale-owner rejection | None | An expired worker can still write |
| Recovery and drain | Scheduler restarts; active daemon worker is not joined | No periodic lease recovery or clean worker drain |
| Source budgets | SEC limiter is per client instance; EDINET has none | No server-wide bound or current-work priority |

### Smallest milestone-1 slice

Add no scheduler, worker, source calls, or publication yet.

1. Add Alembic revision `20260929_0005_durable_jobs.py` after `20260928_0004` and matching table
   definitions in `api/screener/postgres.py`:
   - `job_schedule_occurrence`: identity, `schedule_key`, `scheduled_for`, creation time, and a
     unique `(schedule_key, scheduled_for)` constraint.
   - `durable_job`: identity, optional unique occurrence FK, kind, JSONB parameters, constrained
     status, priority, due time, attempts, max attempts, next retry, deadline, owner, lease expiry,
     ownership generation, heartbeat, JSONB checkpoint, error summary, and created/updated/finished
     times. Add an eligible-claim index ordered by status/due/retry/priority.
   - `durable_job_item`: `(job_id, item_key)` primary key, status, attempts, JSONB outcome, error
     summary, and updated/finished times. Item changes must share the parent job's guarded
     transaction.
2. Add `api/screener/durable_jobs.py` with one `DurableJobRepository`. Public operations:
   `enqueue_scheduled`, `claim_next`, `renew`, `save_checkpoint`, `record_item_outcome`, `complete`,
   `fail`, and `recover_expired`. Claim uses `FOR UPDATE SKIP LOCKED`, closes the claim transaction
   before returning, and returns a lease token `(job_id, owner, generation)`. Every later mutation
   checks that token and an unexpired lease in the same transaction. Reclaim increments generation.
   `fail` applies bounded exponential backoff with injectable jitter, max attempts, and deadline.
3. Add focused unit tests in `api/tests/test_durable_jobs.py` for validation and deterministic retry
   calculation. Add real-PostgreSQL checks in
   `api/tests/integration/test_durable_job_storage.py` for concurrent duplicate occurrence,
   priority claim, renewal, retry/final failure, out-of-order item outcomes, atomic checkpoint plus
   completion, restart recovery, generation increase, and rejection of the old owner's checkpoint
   and completion.
4. Extend the migration-head assertions in
   `api/tests/integration/test_shared_company_storage.py:27-75` to cover upgrade from `0004` and
   expect `0005`. Run the focused tests, real PostgreSQL migration/concurrency tests, normal Python
   suite, and `alembic current`, `heads`, and `check`.

Do not add an advisory-lock leader in milestone 1. PostgreSQL uniqueness makes duplicate schedulers
safe, and `SKIP LOCKED` makes competing workers safe. Leadership, worker drain, source budgets, and
FastAPI wiring belong to milestone 2.

### Publication dependency

Milestone 1 and the scheduler/worker lifecycle can land now. SEC/EDINET discovery may later retain
evidence and durable outcomes without making it current. Automatic selection/publication is blocked
until increment 5 supplies candidate releases, an expected-parent check, and history. Production
intervals and outage presentation are also blocked by the open product decision.

### One owner decision needed next

Approve one freshness/outage policy: the maximum allowed lag for SEC and EDINET, and whether the
last validated release remains visible with a clear stale warning when a source is overdue but no
specific correction is known. A known correction or withdrawal must remain unavailable/pending,
not merely stale.

No production, database/cache, or external writes or calls were made. Only this Developer update
was changed.

READY_FOR_REVIEW

## Reviewer response

### Review 1 — accepted

Accepted. The cited paths confirm the finding: automatic work is process-local and SQLite-backed;
PostgreSQL has no durable job contract; the SEC/EDINET transport, parsing, derivation, and verified
object code can be reused. Milestone 1 must keep enqueue atomic with its schedule occurrence, guard
every job-item mutation with the live lease token, and save any final checkpoint in the same
transaction as completion or failure. It must not select a current company snapshot or add source
scheduling.
