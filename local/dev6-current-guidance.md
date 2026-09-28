# PostgreSQL company API developer guidance

Guidance revision: 1
Updated: 2026-09-27
Owner: the reviewing session
State: **ACCEPTED — Review 2; stop**

Read this file and `local/dev6-review-map.md` before work. Update only the Developer update
section of the review map. Do not commit, stash, switch branches, or edit outside the owned paths.

## Owned paths

- `api/screener/shared_companies.py`
- `api/screener/api.py`
- `api/tests/test_shared_company_api.py`
- `api/tests/integration/test_shared_company_api_storage.py`
- Developer update in `local/dev6-review-map.md`

The migrations, importer, SQLite/source paths, and frontend are read-only. If a required change is
outside these paths, return `BLOCKED:` with the exact contract.

## Standing rules

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`,
`docs/implementation-rollout.md`, `docs/production-architecture.md`,
`local/company-import-plan-2026-09-27.md`, `local/dev4-review-map.md`,
`local/dev5-review-map.md`, and `~/.agents/skills/_shared/code-work.md`. Invoke
`applying-feature` before implementation and `ponytail` after tracing the flow. Do not add
model-facing instructions. Preserve P-02, P-03, P-04, P-09, and P-17.

## Current increment — selected company HTTP read

Add the smallest FastAPI detail read for the current PostgreSQL selection.

1. Extend the accepted repository with a case-insensitive exact current-ticker lookup. Join the
   selected snapshot to its stable issuer/security identity and exact artifact links. Missing is
   distinct from an ambiguous current ticker; do not silently choose one.
2. Add `GET /companies/{ticker}`. It reads PostgreSQL only and does not derive, fetch sources, read
   SQLite, or fall back to the old runtime path.
3. Return the selected canonical payload plus explicit immutable revision metadata, issuer source
   and identifier, stable security identifier, ticker/exchange/currency, cover title/accession,
   security basis, exact receipt ratio as a decimal string or null, and artifact role/hash pairs.
4. A missing ticker returns 404. Ambiguous current data returns an explicit server/data-integrity
   error. PostgreSQL unavailability remains visible; do not return stale SQLite data.
5. Create the engine/repository lazily so importing `screener.api` does not require PostgreSQL.
   Reuse the application's existing FastAPI object and serialization style.

Do not add list, filter, pagination, history, release, frontend, or fallback behavior.

## Checks and real evidence

- Narrow repository/API tests for exact case-insensitive lookup, 404, ambiguity, decimal-string
  ratio, artifact links, and no SQLite/source/derivation fallback.
- Opt-in real integration test: isolated migrated PostgreSQL plus real S3 import of ABT, NTES, and
  `6752.T`; call all three through FastAPI `TestClient`; compare canonical payloads with current
  SQLite and, where present, the rebuilt `dashboard.json` canonical financial fields.
- Prove NTES HTTP output keeps accession `0001104659-26-043468`, ratio `"5"`, and the three exact
  artifact hashes from milestone 2. Prove Panasonic keeps JPY and its raw-filing artifact.
- Full Python and web suites, Alembic check, and `git diff --check`.

## Queued after review

The manager runs the real end-to-end trace and closes the bounded increment. Full-universe import
stays blocked until this API proof passes review.

## Return protocol

Set Developer state to `WORKING`, `READY_FOR_REVIEW`, or `BLOCKED_EXTERNAL`. Report the full flow
trace, reuse decision, changed files, checks with raw output, real HTTP response evidence, known
limits, and whether another session touched an owned file. Never mark review accepted.
