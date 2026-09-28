# Retained cover evidence review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Retain cover bytes and support bounded reparse**
Review state: **ACCEPTED — Review 1**

The developer writes only under "Developer update". The reviewer writes the header and
"Reviewer response".

## Developer update

Developer state: **READY_FOR_REVIEW**

- **Trace:** `sync.cover_pages` selects the current annual accession, skips already covered
  accessions by default, and now reads or atomically retains exact R-report bytes before parsing.
  `store.set_cover` remains the structured SQLite owner. `CompanyImporter` matches the current
  structured cover to the exact cached report, verifies it through S3, and links it to the immutable
  PostgreSQL snapshot.
- **Reuse decision:** extended `cover.report_cache_path`, `sync.cover_pages`, and the importer's
  existing `_file_bytes`/`_retain` path. No second fetch path, database, manifest, schema, or ticker
  exception was added. P-02, P-03, P-04, P-09, P-17, and the foreign-filer invariant are preserved.
  Full detail is in `local/retained-cover-evidence-2026-09-28/README.md`.
- **Changed files:** `api/screener/sources/cover.py`, `api/screener/sync.py`,
  `api/screener/company_import.py`, `api/tests/test_cover.py`, cover-only sections of
  `api/tests/test_sync.py`, `api/tests/test_company_import.py`,
  `api/tests/integration/test_company_import_storage.py`,
  `local/retained-cover-evidence-2026-09-28/README.md`, and this Developer update.
- **Behavior:** raw covers use
  `covers/<accession-without-dashes>/R<report>.htm`. Default sync still skips a covered accession.
  `reparse=True` rejects a missing CIK scope, reuses cached bytes without a source or submissions
  request, fetches only absent reports, and leaves the prior structured cover intact on fetch
  failure. Cached reparses preserve an existing primary-document ratio. Missing historical raw
  covers do not block the accepted importer path.
- **Focused checks:** cover/sync/import unit tests: `116 passed in 0.52s`. Real local S3/PostgreSQL
  import: `1 passed in 4.11s`. The first root-directory integration invocation could not locate
  Alembic configuration; the required `api/` invocation above passed.
- **Full checks:** `make test`: `746 passed, 6 skipped, 1 warning in 3.23s`; web:
  `113 passed`. `compileall`: passed with no output. `git diff --check`: passed with no output.
- **S3 evidence:** exact 141-byte ABT cover fixture accession `0001628280-26-028357` read back from
  `raw/sha256/134b85e1066ecb5e0a73f3f01b1e5830fe047c8b65c1865cb4591dfb9f272fea`.
  Four-company retry retained 12 artifacts, 12 snapshot links, and unchanged payload hashes.
- **Exact manager call:** run the bounded heredoc under **Manager command** in
  `local/retained-cover-evidence-2026-09-28/README.md`. It derives the CIK set only from subclasses
  `empty_parser_failure` and `missing_raw_cover_evidence`, asserts exactly 104 CIKs, then calls
  `sync.cover_pages(conn, foreign_only=True, ciks=ciks, reparse=True)`.
- **Limits:** no live SEC request, 104-CIK rescan, derive, export, regression, audit, filing audit,
  or UI-payload gate ran. Those manager-owned checks remain required after review. The storage
  fixture proves the retained cache-to-S3 contract, not historical cover availability.
- **Skills used:** `plain-writing`, `applying-feature`, and `ponytail` full. They kept evidence
  retention in the existing fetch/import owners and used standard-library atomic replacement.
- **Concurrent edits:** the accepted dev9 parser/engine-182 changes and accepted uncommitted
  importer/storage work already existed in the shared worktree and were preserved. No concurrent
  overlap with an owned file was observed during this increment. No commit, stash, or branch switch
  was made.

## Reviewer response

### Review 1 — accepted

Accepted. The existing cover sync remains the only fetch/parse/SQLite path. Exact successful R
report bytes are atomically retained before parsing; default covered-accession skipping is
unchanged; explicit reparse requires a CIK set and uses cached bytes before any request. The
importer links a matching cached report as `raw_cover_filing` while preserving historical gaps.
The reviewer independently observed 116 focused tests and one real S3/PostgreSQL test with exact
raw-byte readback. No live bounded rescan or commit was made.
