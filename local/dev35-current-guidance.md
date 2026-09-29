# SEC incremental discovery investigation

Updated: 2026-09-29
State: **ACCEPTED — Review 1; stop**

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`, the “Incremental ingestion
and reconciliation”, “S3 contract”, and “Durable jobs in one service” sections of
`docs/production-architecture.md`, increment 4 of `docs/implementation-rollout.md`, and
`local/durable-ingestion-plan-2026-09-29.md`. Read and follow the shared code-work,
applying-feature, ponytail, and no-scaffolding-leaks skills.

Investigate the smallest correct SEC incremental discovery and reconciliation slice. Do not edit
production code, tests, product decisions, or plans. Do not call SEC or any external API, start
services, mutate databases or cache, commit, stash, or switch branch.

## Decisions preserved

P-02, P-03, P-04, P-05, P-09, P-17.

## File you may change

- Developer update in `local/dev35-review-map.md`

## Questions to settle

1. Map current SEC daily-index, submissions, Company Facts, filing-resource, cover/Inline-XBRL,
   retained-object, derivation, and PostgreSQL import paths. Cite exact reuse seams.
2. Prove whether the accepted artifact/snapshot/job tables can honestly retain source resource
   revisions, metadata changes, pending downloads, removals, and reconciliation outcomes. If not,
   define the smallest additional source-observation schema needed before a handler can land.
3. Separate discovery from fetch, extraction, candidate derivation, and publication. This slice must
   not select a new current snapshot because release/history rules belong to increment 5.
4. Define durable job kinds, stable item keys, parameters, checkpoint/overlap behavior, and priority
   for recent discovery versus rebuilt quarterly/full-index reconciliation. Never advance past an
   item without a stored outcome or pending job.
5. Handle new filings, amendments, changed mutable aggregate responses, delayed downloadable files,
   duplicate delivery, source removals, and rebuilt indexes. State what can be proven with fixtures
   before any bounded live SEC call.
6. Define how one shared SEC client/request budget is reused across current and backfill handlers in
   one service process. Do not choose a production interval or reconciliation horizon.
7. Return the smallest build milestones, exact files/interfaces, migration needs, and focused
   unit/real-PostgreSQL/S3 tests. Name any owner decision that blocks code now.

## Report

Update only `## Developer update` in `local/dev35-review-map.md`. Cite files and lines. End with
`READY_FOR_REVIEW`, `BLOCKED`, or `CONTINUE`, and confirm no production/database/external changes.
