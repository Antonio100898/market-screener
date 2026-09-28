# Developer 30 — current guidance

Guidance revision: 1
Updated: 2026-09-29
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — Review 1; stop**

Read `product.md`, `product/shared-data.md`, this file, and `local/dev30-review-map.md`. Do not edit
production code/tests/plans/product decisions, call source/model APIs, mutate SQLite/cache, commit,
stash, or switch branches. PostgreSQL/S3/default database and generated verification processes may
be mutated only through the named importer/migration/API commands.

## Affected decisions

P-02, P-03, P-04, P-09, P-17.

## Paths you own

- `local/engine187-full-persistence-e2e-2026-09-29/**`
- Developer update in `local/dev30-review-map.md`

## Current increment

1. Read `~/.agents/skills/_shared/code-work.md`; invoke `applying-feature` and `ponytail`. Check disk/
   RAM. Start only PostgreSQL and SeaweedFS; run Alembic upgrade/current/heads/check.
2. Record pre-import counts and current engine distribution. Do not call the old engine-184 state
   current evidence.
3. Run the production full-dashboard importer against read-only SQLite/current cache. The command is
   expected to return nonzero because 274 evidence failures remain; capture complete JSON/output.
4. Reconcile exactly:
   - 6,721 current PostgreSQL selections;
   - 6,721/6,721 exact engine-187 SQLite payload hashes, zero mismatches;
   - 274 failures = 147 ticker-map mismatch + 117 pending structured facts + 10 missing ratios;
   - zero Inline-XBRL importer failures;
   - all selected snapshots have at least one artifact role.
5. Verify all 18 SEC statement manifests/source objects exist in evidence storage; all 14 eligible
   companies have complete snapshot role links; the four security failures have retained global
   evidence artifacts but no current snapshot. Verify both EDINET companies have canonical and ZIP
   objects, JPY/TSE/ordinary-share metadata, and exact payloads.
6. Run the importer a second time. Require no new objects/snapshots/links/selections/issuers/
   securities and identical 274 failures.
7. Start one real Uvicorn process. Through FastAPI `GET /companies/{ticker}`, verify the 14 SEC
   additions plus Panasonic and Nintendo against PostgreSQL and SQLite canonical payloads. CNI must
   carry dual provenance. Verify expected 404 for the four statement-solved security exclusions.
8. Stop API; physically restart PostgreSQL and SeaweedFS; reconstruct clients; repeat repository
   counts, selected IDs, S3 readback for all new SEC/EDINET evidence roles, and the 20 HTTP checks.
9. Run storage-enabled full Python tests, web tests, Alembic checks, and diff check. Stop Uvicorn and
   services cleanly with volumes preserved.
10. Evidence under `local/engine187-full-persistence-e2e-2026-09-29/`: commands/logs, before/after
    counts, reconciliation JSON, failures, object/link inventories, API results, restart proof,
    resources, and README. No secrets. Return first unexplained divergence without code changes.

## Return protocol

Update only Developer update in `local/dev30-review-map.md`. Report exact counts, parity, failures,
idempotence, API/restart, tests, stopped services, limits, and concurrent edits. Never mark accepted.
