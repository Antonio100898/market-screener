# Schedule-neutral durable runtime implementation

Updated: 2026-09-29
State: **ACCEPTED — Review 1; stop**

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`, the “Durable jobs in one
service” section of `docs/production-architecture.md`, increment 4 of
`docs/implementation-rollout.md`, `local/durable-ingestion-plan-2026-09-29.md`, and
`local/dev33-review-map.md`. Read and follow the shared code-work, applying-feature, ponytail, and
no-scaffolding-leaks skills.

Implement the schedule-neutral scheduler and worker lifecycle. Do not add SEC/EDINET schedules or
handlers, FastAPI wiring, source calls, request budgets, current-snapshot selection, or publication.
Keep the legacy SQLite scheduler and API unchanged.

## Decisions preserved

P-02, P-03, P-04, P-05, P-09, P-17.

## Files you may change

- `api/screener/durable_jobs.py`
- `api/screener/job_runtime.py`
- `api/tests/test_durable_jobs.py`
- `api/tests/test_job_runtime.py`
- `api/tests/integration/test_durable_job_storage.py`
- `api/tests/integration/test_job_runtime_storage.py`
- Developer update in `local/dev34-review-map.md`

If correctness needs another path, return `BLOCKED:` with the path and reason.

## Required behavior

1. Extend `claim_next` with an optional validated set of allowed job kinds. When supplied, it may
   claim only those kinds. An empty set claims nothing. Existing callers without the filter retain
   current behavior. Add unit and real PostgreSQL proof.
2. Add `ScheduledOccurrence`, `JobContext`, and `DurableJobRuntime` in `job_runtime.py`. The runtime
   receives a repository, occurrence source, handler map, worker count, poll interval, lease
   duration, heartbeat interval, and clock. It uses standard-library threads only.
3. The coordinator asks `occurrence_source(now)` for exact occurrences, enqueues them idempotently,
   and recovers expired leases each poll. It does not define recurrence rules or source timing.
4. Workers claim only registered handler kinds. They execute handlers outside database
   transactions. Higher priority and stable ordering remain database-owned. Handler return values
   are optional final checkpoints. Exceptions call guarded `fail`. `LeaseLost` means the old worker
   writes nothing further.
5. `JobContext` exposes the immutable claimed job, a cooperative stopping signal, guarded
   checkpoint, and guarded item-outcome methods. Do not expose a database connection.
6. Renew each active lease in a small helper thread while the handler runs. Runtime stop requests
   handler cooperation but must not stop heartbeats during a clean drain. Stop and join the helper
   before completion or failure is written, so renewal cannot race a terminal update.
7. `start()` is idempotent. `stop(timeout)` atomically closes the claim gate before requesting stop,
   prevents later claims, waits up to the caller's bound, and returns whether all threads drained.
   It never pretends to kill Python threads. Forced process death leaves the running row for lease
   recovery by another process.
8. Multiple runtime instances stay safe through unique occurrences, filtered `SKIP LOCKED` claims,
   leases, and generations. Do not add leader election, advisory locks, dependencies, tables, or
   migrations.

Keep scheduler/handler failures visible in bounded runtime state or an injected callback; one bad
tick must not silently kill the coordinator. Validate positive durations, heartbeat shorter than
lease duration, worker count, handler names, and JSON-compatible returned checkpoints.

## Required proof

- Deterministic unit tests: duplicate ticks, kind filtering, priority dispatch, success, failure,
  unknown/unregistered kind left queued, automatic renewal, coordinator error survival, clean drain,
  bounded stop timeout, no claim after drain starts, and stale-owner suppression.
- Real PostgreSQL tests: two runtimes enqueue one occurrence and run it once; a worker in a child
  process is terminated after a saved checkpoint, then another repository/runtime recovers it after
  lease expiry, claims with a higher generation, and completes from the retained checkpoint.
- Run existing durable-job unit and real PostgreSQL suites and the full Python suite.
- Run Alembic `current`, `heads`, and `check`; no migration should appear.
- Run `git diff --check`. Stop PostgreSQL cleanly if started; preserve volumes.

No external APIs, secrets, SQLite/cache mutation, commit, stash, or branch switch.

## Report

Update only `## Developer update` in `local/dev34-review-map.md`. List lifecycle behavior, exact
tests and counts, real PostgreSQL process-death proof, stopped services, limits, and concurrent
changes. End with `READY_FOR_REVIEW`, `BLOCKED`, or `CONTINUE`. Do not mark the review accepted.
