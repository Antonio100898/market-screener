# Scheduler and worker lifecycle review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Runtime lifecycle investigation**
Review state: **ACCEPTED — Review 1**

The investigation report belongs under `## Developer update`. The reviewer owns the header and
reviewer response.

## Developer update

### Finding

The lifecycle slice is not blocked by the open freshness decision. Build one generic runtime around
the accepted `DurableJobRepository`, but do not wire it into FastAPI yet. With no approved source
schedules or source handlers, starting it from the application lifespan would only add PostgreSQL
startup failures or consume unknown jobs. FastAPI wiring can safely wait for the SEC schedule slice.

The runtime needs no leader election, new table, dependency, source call, or publication path. Every
application process may propose the same occurrence: the database unique key makes that idempotent
(`api/screener/durable_jobs.py:166-212`, `api/screener/postgres.py:238-260`). Workers may compete:
`claim_next` orders by priority, due time, and stable job ID, then uses `FOR UPDATE SKIP LOCKED`
(`api/screener/durable_jobs.py:214-295`). All handler work starts after that transaction closes.

### Current lifecycle and compatibility

- FastAPI starts and stops only the legacy quote scheduler (`api/screener/api.py:37-46`). Keep this
  unchanged. It protects the existing SQLite application until cutover.
- The legacy scheduler is one daemon thread. It polls every 30 seconds, derives its due time from
  SQLite, and joins only the scheduler thread on shutdown (`api/screener/jobs.py:100-207`). The
  active sync thread and `/sync`, `/sync/cancel`, and `/sync/status` behavior remain untouched
  (`api/screener/jobs.py:51-97`, `api/screener/api.py:219-244`).
- PostgreSQL connection settings already have one owner in `StorageSettings` and
  `create_postgres_engine` (`api/screener/storage_config.py:7-51`,
  `api/screener/postgres.py:390-391`). The runtime receives a repository; it must not create a
  second settings path or share sessions across threads.
- Reuse repository enqueue, claim, renew, complete, fail, and recovery exactly as accepted. Every
  state-changing call already checks owner, generation, and unexpired lease in one short transaction
  (`api/screener/durable_jobs.py:297-551`).

### Smallest implementation

Add only `api/screener/job_runtime.py` and its tests. Public types:

1. `ScheduledOccurrence`: the exact arguments for `enqueue_scheduled`. It contains no recurrence or
   SEC/EDINET timing rule.
2. `JobContext`: the claimed immutable job plus guarded `save_checkpoint` and
   `record_item_outcome` helpers. It never exposes an unguarded database session.
3. `DurableJobRuntime(repository, occurrence_source, handlers, *, worker_count=1,
   poll_interval, lease_duration, heartbeat_interval, clock)`, with idempotent `start()` and a
   draining `stop()`.

`occurrence_source(now)` returns configured occurrences. For this slice, production passes none;
tests provide fixed occurrences and a fixed clock. A coordinator thread enqueues each returned
occurrence and calls `recover_expired` every poll. Bounded worker threads call `claim_next`; one
worker is the default. A claimed kind is dispatched from the handler mapping. Unknown kinds fail
visibly. A handler returns an optional final checkpoint; the worker calls `complete`. An exception
calls `fail`. Repository `LeaseLost` means another owner won, so the old worker records nothing.

Each running handler gets one small heartbeat thread. It calls `renew` until that handler returns.
This is the minimum standard-library design that can renew a lease while the handler itself blocks
on parsing or network work. No `ThreadPoolExecutor`, cron package, advisory lock, or in-memory
leader is needed.

`stop()` first stops scheduling and claiming, then joins workers without killing them. Heartbeats
continue during this clean drain, so completed handlers can record their final state. Python cannot
safely kill a working thread. If the process is forcibly terminated, the database row remains
`running`; after its lease expires, another process calls `recover_expired`, waits for the bounded
retry time, claims it with a higher generation, and resumes from its checkpoint. The old generation
cannot later checkpoint or complete (`api/screener/durable_jobs.py:468-524`).

Priority is database-owned. Configured current-update occurrences use a higher numeric priority
than backfill, so all queued current work is claimed first. Running backfill is not preempted.
Per-source request budgets belong with the SEC/EDINET handlers in later milestones, not this runtime.

### Checks for the build increment

- `api/tests/test_job_runtime.py`: fixed clock and local handlers prove duplicate scheduler ticks,
  priority dispatch, handler success/failure, unknown kind, automatic renewal, clean drain, no new
  claim after drain starts, and stale-owner suppression.
- `api/tests/integration/test_job_runtime_storage.py`: real PostgreSQL proves two runtime instances
  create one occurrence, claim a job once, and recover a worker terminated in a child process with a
  higher generation and retained checkpoint.
- Run both focused files, existing `api/tests/test_durable_jobs.py`, the real durable-job storage
  suite, and the full Python suite. No engine, extraction, evidence, pricing, profile, or
  serialization changes are involved, so the UI payload gate is not triggered.

No product decision blocks this slice. Freshness/outage policy blocks source intervals and automatic
publication, not the schedule-neutral lifecycle. Applying-feature fixed the observable lifecycle
boundary; ponytail kept the design to standard-library threads and the accepted repository;
no-scaffolding-leaks confirmed no model-facing text is involved.

No production, database, cache, service, or external API change was made. Only this Developer update
was changed.

READY_FOR_REVIEW

## Reviewer response

### Review 1 — accepted with build requirements

Accepted. The schedule-neutral runtime can land before the freshness decision and without FastAPI
wiring. The build must add two guards: workers may claim only kinds registered in that runtime, so
an old or specialized process cannot fail another queue's job; and `stop(timeout)` must report
whether clean drain finished while exposing a cooperative stop signal to handlers. Heartbeat renewal
must stop and join before completion/failure is written. Forced process death remains lease-recovery
behavior, not a claim that Python threads can be killed safely.
