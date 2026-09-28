# Scheduler and worker lifecycle investigation

Updated: 2026-09-29
State: **ACCEPTED — Review 1; stop**

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`, the “Durable jobs in one
service” section of `docs/production-architecture.md`, increment 4 of
`docs/implementation-rollout.md`, `local/durable-ingestion-plan-2026-09-29.md`, and the accepted
durable-job repository. Read and follow the shared code-work, applying-feature, ponytail, and
no-scaffolding-leaks skills.

Investigate the smallest correct scheduler and worker lifecycle slice. Do not edit production code,
tests, product decisions, or plans. Do not call external APIs, start services, mutate databases or
cache, commit, stash, or switch branch.

## Decisions preserved

P-02, P-03, P-04, P-05, P-09, P-17.

## File you may change

- Developer update in `local/dev33-review-map.md`

## Questions to settle

1. Map FastAPI lifespan, process start/stop, current legacy quote scheduler, storage settings, and
   the accepted durable-job repository. Name compatibility behavior that must stay untouched.
2. Define the smallest runtime that schedules configured occurrences, recovers expired leases,
   claims by priority, executes handlers outside transactions, renews leases, records final state,
   and drains safely on shutdown.
3. Keep source schedules and handlers absent. The runtime must be testable with deterministic local
   handlers and clocks; it must not choose SEC/EDINET intervals or publish company snapshots.
4. Explain how more than one application process stays safe without depending on one in-memory
   leader. Use the accepted unique occurrence and `SKIP LOCKED` contracts.
5. Define interruption behavior. Python cannot kill a working thread safely: state what clean drain
   does, what forced process death leaves behind, and how the next process recovers it.
6. Identify exact files, public interfaces, configuration, and focused unit/real-PostgreSQL tests.
   Keep the implementation small and use standard library concurrency.
7. Name any product decision that blocks this lifecycle slice. Do not ask for refresh timing unless
   the runtime cannot remain schedule-neutral.

## Report

Update only `## Developer update` in `local/dev33-review-map.md`. Cite files and lines. End with
`READY_FOR_REVIEW`, `BLOCKED`, or `CONTINUE`, and confirm no production/database/external changes.
