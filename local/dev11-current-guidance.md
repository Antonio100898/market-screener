# Foreign depositary ratio developer guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev11-review-map.md` before work. Update only the Developer update section
of the review map. Do not commit, stash, switch branches, or edit outside the owned paths.

## Owned paths

- `api/screener/sources/cover.py`
- `api/screener/sync.py`
- `api/screener/company_import.py`
- `api/screener/store.py`
- `api/tests/test_cover.py`
- relevant cover-only sections of `api/tests/test_sync.py`
- `api/tests/test_company_import.py`
- `api/tests/integration/test_company_import_storage.py`
- `local/foreign-ratio-parser-2026-09-28/**`
- Developer update in `local/dev11-review-map.md`

Do not edit normalization, pricing, schema/migrations, API, product, frontend, or prior reports.
Return `BLOCKED:` if another contract must change.

## Standing rules

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`,
`local/foreign-coverage-plan-2026-09-28.md`, the accepted dev8-dev10 reports, affected ratio/fetch
and importer code, and `~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature` and
`ponytail`. Preserve the exact security, positive ratio, currency, and foreign-filer rules.

## Current increment — direct scalar depositary ratios with retained evidence

The accepted inventory has 28 current unresolved ratio rows after cover reparse. Implement only
filing-backed direct scalar relations. The underlying-shares-per-receipt result must be exact
`Decimal` and unique within the evidence scope.

1. Extend `cover.depositary_ratio` generically for parenthesized digits, numeric/word fractions,
   `every N ADSs represent M shares`, reverse forms such as `six ADSs represent one share`, and
   direct B/equity/common/ordinary-share wording. Do not parse compound units or chained
   instruments as ordinary shares.
2. Scan all direct relation matches in the supplied text. Return a value only when all valid direct
   matches agree. Multiple different current/historical relations return null. WAVE is the adverse
   multiple-relation case.
3. Use the exact retained R-report bytes as ratio evidence when the structured title is truncated.
   The one unresolved depositary class may consume a unique ratio from that report only.
4. For primary-document prose, add an immutable primary-document cache path under the accession
   cover directory. Cache-first, atomic-write, explicit bounded reparse rules match accepted cover
   evidence. A source response is retained before parsing.
5. Primary-document fallback is allowed only for one unresolved exact depositary class and one
   unique direct ratio. Cache and link the exact primary document through the company importer as
   `raw_cover_primary_document`. Preserve `raw_cover_filing` and structured identity links.
6. Safe target set: the accepted report identifies 20 direct relations. FMX/KOF compound units,
   VLRS CPO chaining, WAVE conflicting relations, BBD wrong preferred class, and BMA/WDS unresolved
   evidence must remain excluded. If real evidence contradicts the expected 20, report the exact
   smaller count; never force it.
7. Add focused tests for CDLR, GMAB, JG, SNY, POM, ADAG, SY, FMS, ING, PSNY, SSL, WAVE, compound
   units, conflicting ratios, zero/negative values, and existing AMBO/NTES/OPT/TM neighbours.
8. Bump `store.ENGINE_VERSION` once, from 182 to 183, because ratio semantics change. Do not run
   live ratio rescan or full payload gates; the manager owns them after review.

## Checks and real evidence

- Focused cover/sync/import tests with exact Decimal assertions and adverse controls.
- Fixed-accession official R-report/primary-document outputs for at least six direct cases and the
  WAVE/FMX/VLRS adverse controls.
- Real S3/PostgreSQL readback of one retained primary document and unchanged accepted payload
  neighbours.
- Full Python and web suites, compileall, and `git diff --check`.
- Return the exact bounded manager CIK set and reparse command.

## Queued after review

The manager reparses the accepted ratio CIKs, derives the affected rows, then runs one full
derive/export/regression/audit/filing-audit gate for engine 183.

## Return protocol

Set Developer state to `WORKING`, `READY_FOR_REVIEW`, or `BLOCKED_EXTERNAL`. Report trace, reuse
decision, changed files, checks, official outputs, retained S3 evidence, exact recovered/adverse
cases, limits, skills, and concurrent edits. Never mark review accepted.
