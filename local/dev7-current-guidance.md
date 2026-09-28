# Full-universe import contract developer guidance

Guidance revision: 3
Updated: 2026-09-28
Owner: the reviewing session
State: **ACCEPTED — Review 3; stop**

Read this file and `local/dev7-review-map.md` before work. Update only the Developer update
section of the review map. Do not commit, stash, switch branches, or edit outside the owned paths.

## Owned paths

- `api/screener/postgres.py`
- `api/screener/shared_companies.py`
- `api/screener/company_import.py`
- `api/migrations/versions/20260928_0004_optional_security_evidence.py`
- `api/tests/test_shared_companies.py`
- `api/tests/test_company_import.py`
- `api/tests/integration/test_shared_company_storage.py`
- `api/tests/integration/test_company_import_storage.py`
- Developer update in `local/dev7-review-map.md`

Revisions `20260927_0002` and `20260927_0003`, API files, SQLite/source files, and frontend files are
read-only. Return `BLOCKED:` if another path is required.

## Standing rules

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`,
`docs/implementation-rollout.md`, `docs/production-architecture.md`,
`local/company-import-plan-2026-09-27.md`, the three accepted review maps, and
`~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature` before implementation and
`ponytail` after tracing the flow. Do not add model-facing instructions. Preserve P-02, P-03,
P-04, P-09, P-17, and the foreign-filer invariant.

## Current increment — explicit bulk import contract

### Review 1 correction

The first real bulk run imported 6,170 of 6,925 eligible companies and named 755 failures.
Exactly 501 failures had a stored cover row whose title or accession was empty. Of those, 485 have
an exact current SEC ticker-map match. This is the same "no usable cover" condition as `receipt is
None`, but `_prepare_sec` raises before reaching the accepted mapping fallback.

Treat a cover with missing title or accession as unavailable and use the verified ticker-map path.
Do not fall back when a depositary title exists but its ratio is missing; that remains a visible
failure. Add a regression case for an incomplete stored cover with an exact map match and one for
an incomplete cover with a map mismatch. Rerun the named checks; do not run the full universe.

### Review 2 correction

The corrected bulk run imported 485 incomplete-cover rows, but exhaustive reconciliation found 485
payload-hash mismatches. A field diff proves the only change is the old incomplete `receipt`
object becoming null. Example CATO lost its CIK, symbol, accession, and empty title from the
canonical payload. Financial fields did not move, but exact payload/provenance parity is required.

Keep the original incomplete receipt in the `EvidenceBundle` so `_derive_evidence` preserves the
same canonical payload and still decides whether the company is supported. Use the verified ticker
map only for PostgreSQL security metadata: null title/accession and
`SEC_TICKER_MAPPING_ONLY`. A current foreign filer must still fail derivation. Add a pin proving an
incomplete receipt is retained in the derived payload while PostgreSQL metadata uses the mapping
fallback, and that exact SQLite payload hash parity holds. Rerun the named checks; do not run the
full universe.

Inventory evidence: 6,925 current dashboard-eligible `ok` snapshots consist of 5,393 SEC rows with
a usable cover, 1,530 SEC rows without one, and 2 EDINET rows. The existing engine accepts the
1,530 only when current foreign-form security evidence is not required. The PostgreSQL importer
currently rejects them before derivation.

Correct this at the owning boundaries:

1. Add forward revision `20260928_0004`. Do not edit applied revisions. Security title and cover
   accession are immutable evidence when known, but must be nullable when no retained cover exists.
   Keep exact non-empty checks for non-null values. Update SQLAlchemy metadata and repository types.
2. For an SEC row without a usable cover, retain and verify the cached official
   `company_tickers.json`, confirm its verified bytes map the exact CIK to the exact ticker, and add
   artifact role `official_sec_ticker_mapping`. Derive with `receipt=None`.
3. Only an `ok` derivation may be stored. Its security basis is
   `SEC_TICKER_MAPPING_ONLY`; title, cover accession, and ratio remain null. This does not relax
   foreign rules: a current foreign filer without exact cover identity still derives `foreign` and
   remains excluded.
4. Keep the existing cover-backed and EDINET paths unchanged. A cover-backed ADS still requires a
   positive exact ratio.
5. Add an explicit bulk CLI flag. No-argument behavior remains invalid, and named-ticker behavior
   remains. Select the universe through `store.dashboard_ciks` so eligibility has one owner, map
   those CIKs to tickers, and import in stable order.
6. Each company remains its own committed import. Continue after a company failure, return a
   deterministic summary of imported/reused and failed tickers with exact reasons, and use a
   nonzero exit for any failure. A rerun completes or reuses earlier work.
7. Do not add source fetching, concurrency, job tables, schedules, API lists, or a full-universe
   test run in this increment.

## Checks and real evidence

- Unit tests: optional security evidence, exact ticker-map match, mismatched/missing map, foreign
  derivation rejection, existing cover/ADS behavior, bulk eligibility, stable order, continue on
  failure, deterministic summary, retry, and explicit CLI selection.
- Real PostgreSQL migration tests from populated revision 3 to head and empty to head.
- Real S3/PostgreSQL import for ABT, NTES, `6752.T`, plus one current `ok` SEC ticker without a
  cover. Prove exact readback, null cover fields, mapping artifact, and unchanged three-company
  hashes.
- Full Python and web suites, Alembic current/heads/check, and `git diff --check`.

## Queued after review

The manager will run the explicit full retained-universe import, reconcile every failure, restart
services, and sample HTTP payload parity. Do not run the full universe yourself.

## Return protocol

Set Developer state to `WORKING`, `READY_FOR_REVIEW`, or `BLOCKED_EXTERNAL`. Report the complete
flow trace, reuse decision, changed files, checks with raw output, migration evidence, real
four-company evidence, known limits, and concurrent edits. Never mark review accepted.
