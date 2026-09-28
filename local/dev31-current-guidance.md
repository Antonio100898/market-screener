# Developer 31 — current guidance

Guidance revision: 1
Updated: 2026-09-29
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — Review 1; stop**

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`,
`docs/production-architecture.md` sections “Incremental ingestion and reconciliation” and “Durable
jobs in one service”, `docs/implementation-rollout.md` increment 4, this file, and
`local/durable-ingestion-plan-2026-09-29.md`. Read the required shared code-work, applying-feature,
and ponytail skills. This is read-only investigation. Do not edit production code, tests, product
decisions, or plans. Do not call source/model APIs, mutate databases/cache, start services, commit,
stash, or switch branches.

## Affected decisions

P-02, P-03, P-04, P-05, P-09, P-17.

## Paths you own

- Developer update in `local/dev31-review-map.md`

## Investigation

Map the current automatic-work path and the smallest correct first implementation slice.

1. Inventory current SQLite/in-process jobs, FastAPI lifecycle hooks, CLI entry points, SEC and
   EDINET discovery/import functions, PostgreSQL repositories/models/migrations, and relevant tests.
2. Name code that can be reused unchanged and code that must stay as compatibility behavior.
3. Compare the implementation to every required durable-job field and rule in the architecture:
   schedule identity, status, priority, due time, attempts, retry, owner, lease expiry, generation,
   heartbeat, checkpoint, job items, stale-owner rejection, recovery, drain, and source budgets.
4. Identify dependencies on the future release/publication model. Separate work that can safely land
   now from work blocked by the owner’s freshness/outage decision or increment 5.
5. Propose the smallest milestone-1 schema/repository slice, exact files, migration shape, public
   interfaces, and focused unit/real-PostgreSQL checks. Prefer standard SQLAlchemy/PostgreSQL and
   existing dependencies.
6. Report risks, conflicting requirements, and exactly one owner decision needed next. Do not solve
   unrelated extraction gaps.

## Return protocol

Update only `## Developer update` in `local/dev31-review-map.md`. Cite files and line numbers. End
with `READY_FOR_REVIEW`, `BLOCKED`, or `CONTINUE`, and state that no writes or external calls were
made. Never mark accepted.
