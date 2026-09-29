# SEC rebuilt-index reconciliation investigation

Updated: 2026-09-29
State: **ACCEPTED — Review 1; stop**

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`, the “Incremental ingestion
and reconciliation” and “S3 contract” sections of `docs/production-architecture.md`, increment 4 of
`docs/implementation-rollout.md`, `local/durable-ingestion-plan-2026-09-29.md`, and the accepted SEC
recent-discovery/source-observation code. Read and follow the shared code-work, applying-feature,
ponytail, and no-scaffolding-leaks skills.

Investigate the smallest correct rebuilt-quarter reconciliation slice. Do not edit production code,
tests, product decisions, or plans. Do not call SEC or any external API, start services, mutate
databases/cache, commit, stash, or switch branch.

## Decisions preserved

P-02, P-03, P-04, P-05, P-09, P-17.

## File you may change

- Developer update in `local/dev38-review-map.md`

## Questions to settle

1. Define the exact full/quarterly form-index source identity, explicit job parameters, item keys,
   checkpoint, and priority. Do not choose schedule frequency or history horizon.
2. Define how to find the latest known state of SEC financial filing items for one proven quarter
   without adding a current pointer or scanning unrelated history.
3. Define changed-metadata and removal rules. Only a successfully retained and fully parsed rebuilt
   quarter may prove absence. A daily-index omission, 404, parse error, partial range, or another
   quarter proves nothing.
4. Preserve exact removal evidence even though a removed resource has no artifact. Link its metadata
   to the retained quarterly inventory observation/hash in a verifiable way without weakening the
   accepted state/evidence constraint.
5. Define idempotent child-fetch behavior for present revisions and no child for removals. A stale
   owner must create no observation, child, or checkpoint.
6. Return exact reuse seams, files/interfaces, focused unit/real PostgreSQL/S3 tests, and any schema
   need. Keep this slice schedule-neutral and publication-free.

## Report

Update only `## Developer update` in `local/dev38-review-map.md`. Cite files and lines. End with
`READY_FOR_REVIEW`, `BLOCKED`, or `CONTINUE`, and confirm no production/database/external changes.
