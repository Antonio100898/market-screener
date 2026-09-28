# Foreign cover parser developer guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev9-review-map.md` before work. Update only the Developer update section
of the review map. Do not commit, stash, switch branches, or edit outside the owned paths.

## Owned paths

- `api/screener/sources/cover.py`
- `api/screener/evidence.py`
- `api/screener/store.py`
- `api/tests/test_cover.py`
- `api/tests/test_evidence.py`
- relevant cover-only sections of `api/tests/test_sync.py`
- `local/foreign-cover-parser-2026-09-28/**`
- Developer update in `local/dev9-review-map.md`

Do not edit normalization, pricing, API, PostgreSQL, product, or frontend paths. Return `BLOCKED:`
if another contract must change.

## Standing rules

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`,
`local/foreign-coverage-plan-2026-09-28.md`, the accepted dev8 report/review map, affected source
and caller code, and `~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature` before the
change and `ponytail` after tracing the flow. Preserve the foreign-filer invariant.

## Current increment — generic cover identity parser fixes

The accepted inventory identifies 47 `empty_parser_failure` rows. Fix only generic extraction and
classification causes proven by current filing covers:

1. Preserve continued/multi-row cover titles and their exact symbol pairing. A title split across
   rows must remain one class; it must not consume the next class or debt symbol.
2. Reject boolean/placeholder values such as `true` and `F` as trading symbols.
3. Keep compound symbol cells intact enough to select the exact priced class without aliasing a
   different security.
4. Refine the non-common predicate so phrases such as `right to receive one ordinary share` do not
   turn a real ADS into a rights security. Actual warrants, subscription rights, notes, ETNs,
   preferred classes, and purchase rights remain rejected.
5. Do not add ticker lists, symbol aliases, OTC rules, or ratio guesses. The 24 symbol mismatches,
   11 wrong classes, 46 stale/OTC mappings, and seven unregistered classes remain excluded.
6. Add the narrowest parser/predicate fixtures from BBVA, BEP, AZN, SUZ, AURE, and ADAG. Preserve
   HON, PPG, GLP, LX, TM, and existing cover tests as neighbours.
7. Exercise `cover.securities` against the real current SEC cover pages for at least four affected
   cases and two adverse controls. Record exact parsed rows under
   `local/foreign-cover-parser-2026-09-28/`.
8. Bump `store.ENGINE_VERSION` because loader/classification semantics change. Do not derive/export
   or run the full payload gates in this developer increment; the manager owns them after review.

## Checks and real evidence

- Focused cover, evidence, and cover-page sync tests.
- Real current filing parser output for the named affected cases and adverse classes.
- Full Python suite, compileall, and `git diff --check`.
- Return the exact expected affected CIK set for the manager's bounded cover rescan and derive.

## Queued after review

The manager performs a bounded rescan of parser and missing-cover CIKs, then full derive/export,
regression, audit, and filing audit. Direct scalar ratio parsing is a separate fresh increment.

## Return protocol

Set Developer state to `WORKING`, `READY_FOR_REVIEW`, or `BLOCKED_EXTERNAL`. Report the trace,
reuse decision, changed files, raw checks, real filing outputs, expected recovered/rejected cases,
skills used, limits, and concurrent edits. Never mark review accepted.
