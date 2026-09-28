# Persistence developer guidance

Guidance revision: 2
Updated: 2026-09-27
Owner: the reviewing session
State: **ACCEPTED — Review 2; stop**

Read this file and `local/dev4-review-map.md` before work. Update only the Developer update
section of the review map. Do not commit, stash, switch branches, or edit outside the owned paths.

## Owned paths

- `api/screener/postgres.py`
- `api/screener/shared_companies.py`
- `api/migrations/versions/20260927_0002_company_snapshot.py`
- `api/migrations/versions/20260927_0003_snapshot_evidence_identity.py`
- `api/tests/test_shared_companies.py`
- `api/tests/integration/test_shared_company_storage.py`
- Developer update in `local/dev4-review-map.md`

If a required change is outside these paths, return `BLOCKED:` with the file and contract.

## Standing rules

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`,
`docs/implementation-rollout.md`, `docs/production-architecture.md`,
`local/company-import-plan-2026-09-27.md`, and `~/.agents/skills/_shared/code-work.md`.
Invoke `applying-feature` before implementation and `ponytail` after the flow is understood.
Do not add model-facing instructions. Keep P-02, P-03, P-04, P-09, and P-17 intact.

## Current increment — correction after Review 1

Review 1 found two root-cause contract errors. Correct them without editing migration
`20260927_0002`, because it has been applied. Add a forward `20260927_0003` revision.

1. `priced_security` may keep only stable identity. Annual cover accession, title, security basis,
   receipt ratio, ticker/listing fields, and currencies can change. Store their historical value on
   the immutable snapshot or another immutable observation selected with it. A new 20-F and a new
   ADS ratio for the same stable security must create/select a new snapshot, not raise an identity
   conflict, and the old values must remain readable.
2. Artifact roles and hashes are part of a snapshot's immutable identity. A repeated identical
   import reuses the snapshot. The same canonical payload with a changed artifact set or changed
   security-basis evidence creates a new snapshot. No retry may add links to an old snapshot.
3. Keep one transaction for snapshot, exact links, and current selection. Preserve the existing
   cross-security selection constraint and exact `NUMERIC` ratio.

The correct shape is stable issuer/security identity plus immutable evidence-bound snapshot
revisions. PostgreSQL owns the constraints. Do not add import logic or FastAPI.

## Checks and real evidence

- Narrow unit tests for identity conflicts, idempotent immutable snapshot writes, artifact-link
  deduplication, current selection, and retained prior snapshots.
- Add pins for a later annual cover/ratio on the same security and for identical payloads with a
  changed artifact set. Prove the old metadata and exact old links remain unchanged.
- Opt-in integration test against real PostgreSQL. Prove both upgrade paths:
  `20260925_0001 -> head` and `20260927_0002 -> head`. Repeat, change evidence, restart, and retain
  every prior snapshot and exact link set.
- Alembic `current`, `heads`, and `check`; `git diff --check`.
- Record exact commands and raw results in `local/dev4-review-map.md`.

## Queued after review

The next fresh developer session will build the retained-evidence importer on this accepted contract.

## Return protocol

Set Developer state to `WORKING`, `READY_FOR_REVIEW`, or `BLOCKED_EXTERNAL`. Report the full flow
trace, reuse decision, changed files, checks with raw output, real database evidence, known limits,
and whether another session touched an owned file. Never mark the review accepted.
