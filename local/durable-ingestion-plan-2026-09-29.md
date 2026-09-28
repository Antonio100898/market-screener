# Durable automatic ingestion delivery plan

## Goal

Continue platform increment 4 after the accepted PostgreSQL/S3 company import. One Python service
must discover and process SEC and EDINET updates without a browser request, keep work in
PostgreSQL, retain source bytes in S3, resume safely after interruption, and never let an expired
worker publish results. The same generic path serves every supported company.

## Product decisions

This work preserves P-02, P-03, P-04, P-05, P-09, and P-17. The owner must still decide the
freshness target and what users see during a source outage before automatic publication is enabled.

## Boundaries

- Reuse the current SEC and EDINET clients, retained-evidence contract, financial derivation, and
  PostgreSQL company repository. Do not create another extractor or calculation path.
- Keep the SQLite application and current in-process quote scheduler working until cutover is
  separately accepted. Do not add live dual writes.
- PostgreSQL owns schedule occurrences, jobs, items, leases, attempts, checkpoints, and outcomes.
- Network and parsing work happens outside database transactions. Every commit checks the current
  lease generation. Repeated work is safe.
- Source request limits apply across current updates and backfill. Current updates win.
- Do not implement publication intervals or outage presentation until the owner decision is
  recorded in `product/shared-data.md`.

## Milestones

| Milestone | State | Acceptance check |
|---|---|---|
| 0. Existing-path and gap investigation | accepted | Exact reuse map and smallest safe slice recorded in `local/dev31-review-map.md` |
| 1. PostgreSQL durable-job schema and repository | accepted | 14 unit, 13 real PostgreSQL/migration, and 865 full Python tests; Alembic at `20260929_0005` |
| 2. Scheduler and worker lifecycle | accepted | 24 unit and 12 real PostgreSQL tests; clean drain, bounded stop, duplicate runtime, child-process death, checkpoint recovery pass |
| 3. SEC incremental discovery and reconciliation | active | New, amended, changed, removed, pending, duplicate, overlap, and rebuilt-index cases persist honest outcomes |
| 4. EDINET incremental discovery and reconciliation | queued | New, amended, edited, withdrawn, parent-linked, duplicate, overlap, and expired-download cases persist honest outcomes |
| 5. Automatic end-to-end publication gate | blocked on owner decision and increment 5 publication model | Browsers closed; due work produces one validated current release; outage and restart behavior match policy |

## Verification

- Narrow deterministic tests for each transition and failure.
- Real PostgreSQL concurrency, lease, restart, and Alembic checks.
- Real S3 retained-object checks for source slices.
- Stubbed source failures first; bounded live SEC and EDINET calls only when the slice requires them.
- Engine/evidence changes also run the full UI payload regression and filing audit gates.
- Independent review after every milestone. Accepted increments may be committed locally; no push or
  merge is authorized.
