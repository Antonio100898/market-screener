# Durable job repository implementation

Updated: 2026-09-29
State: **ACCEPTED — Review 2; stop**

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`, the “Durable jobs in one
service” section of `docs/production-architecture.md`, increment 4 of
`docs/implementation-rollout.md`, and `local/durable-ingestion-plan-2026-09-29.md`. Read and follow
the shared code-work, applying-feature, ponytail, and no-scaffolding-leaks skills.

Implement milestone 1 only: PostgreSQL durable-job schema and repository. Do not add a scheduler,
worker loop, FastAPI lifecycle wiring, source calls, source budgets, current-snapshot selection, or
publication behavior. Keep SQLite jobs and commands unchanged.

## Decisions preserved

P-02, P-03, P-04, P-05, P-09, P-17.

## Files you may change

- `api/screener/postgres.py`
- `api/screener/durable_jobs.py`
- `api/migrations/versions/20260929_0005_durable_jobs.py`
- `api/tests/test_durable_jobs.py`
- `api/tests/integration/test_durable_job_storage.py`
- Existing migration-head assertions in `api/tests/integration/test_shared_company_storage.py`
- Developer update in `local/dev32-review-map.md`

If the correct implementation needs another path, return `BLOCKED:` with the path and reason.

## Required contract

Add three SQLAlchemy Core tables and matching Alembic migration after `20260928_0004`.

1. `job_schedule_occurrence`: identity, non-empty schedule key, scheduled UTC instant, created UTC
   instant, and unique `(schedule_key, scheduled_for)`.
2. `durable_job`: optional unique occurrence foreign key, non-empty kind, JSONB parameters,
   constrained lifecycle status, priority, due time, attempts, maximum attempts, next retry,
   optional deadline, owner, lease expiry, ownership generation, heartbeat, JSONB checkpoint,
   bounded error summary, and created/updated/finished times. Add a claim index for runnable status,
   due/retry time, priority, and stable job identity.
3. `durable_job_item`: `(job_id, item_key)` primary key, constrained status, attempts, JSONB outcome,
   bounded error summary, and updated/finished times.

Add `DurableJobRepository` with typed returned records and lease token. Provide:

- `enqueue_scheduled`: create occurrence and job in one transaction. A repeated identical occurrence
  returns the same job. Different immutable job content for the same occurrence raises an identity
  conflict.
- `claim_next`: short `FOR UPDATE SKIP LOCKED` transaction. Claim only due, retry-ready work in
  priority-descending, due-time, stable-ID order. Increment attempt count and ownership generation;
  return after the transaction closes.
- `renew`, `save_checkpoint`, and `record_item_outcome`: require running status, matching owner and
  generation, and an unexpired lease in the same transaction.
- `complete` and `fail`: accept an optional final checkpoint and store it atomically with the state
  change. Clear active ownership. `fail` uses bounded exponential backoff, injectable deterministic
  jitter, maximum attempts, and deadline; final failure is visible.
- `recover_expired`: recover expired running jobs without allowing the former owner to write. Obey
  maximum attempts and deadline. Reclaiming must later use a higher generation.

Network or slow work never runs in these transactions. Do not add advisory locks or dependencies.
Use UTC-aware datetimes and validate inputs before SQL where useful. Let the database enforce
identity and state invariants. Do not add deletion APIs.

## Required proof

- Focused unit tests for validation and retry calculation.
- Real PostgreSQL tests for concurrent duplicate occurrence, priority claim, renewal, retry and
  terminal failure, out-of-order item outcomes, atomic final checkpoint, restart recovery,
  generation increase, and rejection of every old-owner mutation.
- Upgrade an existing `20260928_0004` database and a fresh database. Run Alembic `upgrade head`,
  `current`, `heads`, and `check`.
- Run the normal Python suite. This change does not touch the financial engine, evidence parsing,
  pricing, profiles, or serialization, so the full UI-payload gate is not required.
- Run `git diff --check`. Stop PostgreSQL and object storage cleanly if started; preserve volumes.

Do not call external APIs, read or print secrets, mutate SQLite/cache, commit, stash, switch branch,
or modify files outside the allowed list.

## Report

Update only `## Developer update` in `local/dev32-review-map.md`. List schema behavior, exact tests
and counts, real PostgreSQL/Alembic proof, stopped services, limits, and concurrent changes. End with
`READY_FOR_REVIEW`, `BLOCKED`, or `CONTINUE`. Do not mark the review accepted.
