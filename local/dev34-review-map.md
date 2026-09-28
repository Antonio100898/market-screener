# Schedule-neutral durable runtime review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Scheduler and worker lifecycle**
Review state: **ACCEPTED — Review 1**

The implementation report belongs under `## Developer update`. The reviewer owns the header and
reviewer response.

## Developer update

### Reuse decision

`DurableJobRepository` remains the sole PostgreSQL lifecycle owner. The runtime will call its
`enqueue_scheduled`, filtered `claim_next`, `renew`, guarded checkpoint/item methods, `complete`,
`fail`, and `recover_expired` methods. No session, SQL, recurrence rule, source handler, or second
state machine will be added. Existing repository callers omit the new kind filter and keep current
behavior. Tests will exercise the runtime through this public repository contract.

### Implementation

- `claim_next(..., allowed_kinds=...)` now limits both expired-deadline cleanup and claims to the
  validated kinds. An empty set performs no SQL. Omitting the filter preserves prior behavior.
- `ScheduledOccurrence` carries an exact occurrence without a recurrence rule. The coordinator
  idempotently enqueues returned occurrences and recovers expired leases on each successful tick.
- Workers claim only registered handler kinds. Database priority/due/job ordering remains unchanged.
  Handler work runs after the claim transaction. Context checkpoint and item writes use the claimed
  lease token and current ownership generation.
- Each active handler has one standard-library heartbeat thread. It ignores clean-drain stop requests,
  then is stopped and joined before `complete` or `fail`. `LeaseLost` suppresses every later write.
- `start()` is idempotent. `stop(timeout)` closes enqueue, recovery, and claim gates first, exposes the
  cooperative stop event, returns `False` at its bound when work remains, and never claims to kill a
  Python thread. A source or database call already in flight may finish, but cannot open later work.
- Coordinator, claim, heartbeat, completion, failure, and handler errors remain visible in a bounded
  100-entry runtime error list. Invalid handler checkpoints fail visibly instead of reaching storage.

### Proof

- Focused unit command:
  `api/.venv/bin/pytest -q api/tests/test_durable_jobs.py api/tests/test_job_runtime.py`
  — **24 passed**. This covers duplicate ticks, kind filtering, priority, context writes, success,
  handler failure, unknown queued work, renewal, coordinator survival, cooperative clean drain,
  bounded timeout, no later claim or enqueue after drain starts, stale-owner suppression, invalid
  checkpoints, and idempotent start.
- Real PostgreSQL command from `api/`:
  `RUN_STORAGE_INTEGRATION=1 .venv/bin/pytest -q tests/integration/test_durable_job_storage.py tests/integration/test_job_runtime_storage.py`
  — **12 passed**. Two repositories/runtimes proposed one occurrence; PostgreSQL stored one
  occurrence/job, one worker ran it, and attempts stayed at one. A higher-priority unregistered job
  remained pending while the allowed kind was claimed.
- Forced-death proof used a spawned child process running `DurableJobRuntime`. Its handler saved
  `{"last_document":"saved-before-death"}` and the parent terminated the process. The row remained
  running at generation 1. A separate repository/runtime recovered the expired lease, claimed
  generation 2, received the retained checkpoint, and completed with
  `{"last_document":"completed-after-recovery"}`. Cleanup terminates and joins the child even when
  readiness fails.
- Full Python command from `api/`: `.venv/bin/pytest -q` — **875 passed, 19 skipped, 1 existing
  Starlette/httpx deprecation warning**.
- Alembic: `current` = `20260929_0005 (head)`; `heads` = `20260929_0005 (head)`; `check` =
  `No new upgrade operations detected.` No migration was added.
- `git diff --check` passed.
- Local PostgreSQL was started only for these checks, then stopped cleanly. Volumes were preserved.

### Limits and concurrent work

No SEC/EDINET schedule, source handler, request budget, FastAPI wiring, publication, external API
call, secret access, SQLite/cache mutation, migration, commit, stash, or branch change was made.
No concurrent change appeared in the allowed implementation paths.

READY_FOR_REVIEW

## Reviewer response

### Review 1 — accepted

Accepted after two in-review race fixes: stop now blocks post-stop enqueue/recovery, and its timeout
does not wait behind an in-flight database gate. The reviewer reran 24 focused tests, 12 real
PostgreSQL tests including child-process termination and higher-generation recovery, and the full
Python suite: 875 passed, 19 skipped, one existing warning. Alembic remains clean at
`20260929_0005`; diff check passed. PostgreSQL was stopped with its volume preserved. This accepts
the schedule-neutral runtime only. No source schedule or automatic publication is active.
