# Developer 29 — current guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — Review 1; stop**

Read `product.md`, `product/shared-data.md`, this file, and `local/dev29-review-map.md`. Do not
commit, stash, switch branches, edit production code/plans/product decisions, call source/model
APIs, or mutate SQLite/cache. PostgreSQL/S3 service and test-database/object mutations are authorized
only for this integration gate.

## Affected decisions

P-02, P-03, P-04, P-09, P-17.

## Paths you own

- `api/tests/integration/test_company_import_storage.py`
- `local/engine187-real-storage-gate-2026-09-28/**`
- Developer update in `local/dev29-review-map.md`

## Current increment

**Goal.** Prove the current generic importer through real PostgreSQL and S3 for both official source
families and both SEC relationship shapes before the full 6,995-company run.

1. Read `~/.agents/skills/_shared/code-work.md`; invoke `applying-feature` and `ponytail`. Check disk/
   RAM and existing service state. Start only PostgreSQL and SeaweedFS with the checked-in compose
   file and local `.env`. Run Alembic upgrade/current/heads/check.
2. Extend the real integration fixture to cover at least:
   - AERO: direct engine-187 Inline-XBRL manifest/files;
   - CNI: incorporated 40-F wrapper plus 6-K exhibit and dual provenance;
   - Panasonic `6752.T` and Nintendo `7974.T`: all retained EDINET companies;
   - keep existing ABT/NTES/CATO boundaries unless splitting the test preserves their coverage.
3. Copy only named retained inputs to a temporary fixture cache. Do not refetch. For every imported
   company verify exact SQLite canonical payload, identity, engine 187, source/security/currency,
   every artifact role/hash, and byte-for-byte S3 readback.
4. Verify AERO direct roles, CNI annual-wrapper/source roles and dual source fields, Panasonic and
   Nintendo canonical/ZIP roles, JPY/TSE/PRIMARY_ORDINARY_SHARE, and same-hash multi-role links.
5. Run import twice and assert object, artifact, snapshot, link, current-selection, issuer, and
   security counts are unchanged. Force one interrupted sequence and prove restart completes without
   duplicate snapshots or links.
6. Restart PostgreSQL and SeaweedFS, reconstruct repositories, read all current selections and S3
   objects again, and prove IDs/hashes/payloads survive.
7. Update stale engine-dependent test pins only from observed exact payload/snapshot hashes. A payload
   hash change needs field-level explanation and blocks if not caused by accepted engine-187 data.
8. Run focused storage integration test, full storage-enabled Python suite, web suite if API payload
   contract changes (none expected), Alembic checks, syntax, and diff check. Stop services cleanly
   with volumes preserved.
9. Evidence under `local/engine187-real-storage-gate-2026-09-28/`: commands/logs, counts, company
   hashes/roles, restart proof, resources, and README. No secrets.

## Return protocol

Update only Developer update in `local/dev29-review-map.md`. Report exact test changes, real counts,
hashes, retries/restart, checks, services stopped, product decisions, limits, and concurrent edits.
Never mark accepted.
