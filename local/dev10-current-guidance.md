# Retained cover evidence developer guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev10-review-map.md` before work. Update only the Developer update section
of the review map. Do not commit, stash, switch branches, or edit outside the owned paths.

## Owned paths

- `api/screener/sources/cover.py`
- `api/screener/sync.py`
- `api/screener/company_import.py`
- `api/tests/test_cover.py`
- relevant cover-only sections of `api/tests/test_sync.py`
- `api/tests/test_company_import.py`
- `api/tests/integration/test_company_import_storage.py`
- `local/retained-cover-evidence-2026-09-28/**`
- Developer update in `local/dev10-review-map.md`

Do not edit normalization, pricing, schema/migrations, API, product, frontend, or engine-version
paths. The parser and engine-182 bump are accepted inputs. Return `BLOCKED:` if another contract is
required.

## Standing rules

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`,
`local/foreign-coverage-plan-2026-09-28.md`, accepted dev8/dev9 reports, the existing cover sync and
import flows, and `~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature` and `ponytail`.
Preserve P-02, P-03, P-04, P-09, P-17, and the foreign-filer invariant.

## Current increment — retain cover bytes and support bounded reparse

The parser repair cannot be applied reproducibly because `sync.cover_pages` stores only parsed
SQLite rows. Default logic skips an already covered accession, so manually deleting rows/refetching
would violate the retained-evidence rule.

1. Add one canonical local cache path helper for a cover report's exact bytes, keyed by immutable
   accession and report number under the existing screener cache. Use bytes, not normalized text.
2. `cover_pages.read` must parse cached bytes when present. On a source fetch, atomically retain the
   exact successful response before parsing it. A failed fetch never creates a cache artifact or
   destroys an existing parsed cover.
3. Keep default behavior unchanged: covered immutable accessions are skipped. Add an explicit
   function-only bounded reparse option that is valid only with an explicit CIK set. It may fetch a
   covered accession only when its raw cover bytes are absent; once cached, repeated reparses make
   no source request.
4. Record which R report produced the parsed rows through the cache filename; do not add a second
   database or sidecar manifest.
5. Extend the PostgreSQL company importer: when the exact current cover cache exists, retain/read it
   through S3 and link it with role `raw_cover_filing`. Keep the existing structured-cover artifact.
   Missing raw cover remains an honest historical gap and does not block the already accepted path.
6. Add unit tests for atomic cache write/read/reuse, explicit-scope requirement, default skip,
   failed fetch, and importer link/idempotency. Extend the real four-company storage test with a
   retained cover fixture and exact S3 readback.
7. Do not run the 104-CIK rescan or full universe. The manager owns live source reads after review.

## Checks and real evidence

- Focused cover sync/import tests with request counters and failure injection.
- Real local S3/PostgreSQL import readback for one cached SEC cover.
- Full Python and web suites, compileall, and `git diff --check`.
- Return the exact manager call for bounded parser/missing-cover reparse.

## Queued after review

The manager reparses the explicit 104 CIKs, derives the affected rows, and measures recovery.
Direct scalar ratio parsing follows in a fresh increment.

## Return protocol

Set Developer state to `WORKING`, `READY_FOR_REVIEW`, or `BLOCKED_EXTERNAL`. Report trace, reuse
decision, changed files, checks with raw output, S3 evidence, exact manager command, limits, skills,
and concurrent edits. Never mark review accepted.
