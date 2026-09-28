# Developer 28 — current guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — Review 1; stop**

Read `product.md`, `product/shared-data.md`, this file, and `local/dev28-review-map.md`. Do not
commit, stash, switch branches, call source/model APIs, start services, mutate real PostgreSQL/S3/
SQLite/cache, or edit EDINET/API/UI/plans/product decisions.

## Affected decisions

P-02, P-03, P-04, P-09, and P-17. Preserve missing-vs-zero and all existing security gates.

## Paths you own

- `api/screener/sources/inline_xbrl.py`
- `api/screener/company_import.py`
- `api/tests/test_inline_xbrl.py`
- `api/tests/test_company_import.py`
- `api/tests/integration/test_company_import_storage.py`
- `local/engine187-sec-import-2026-09-28/**`
- Developer update in `local/dev28-review-map.md`

## Current increment

**Observed gap.** `CompanyImporter._prepare_sec` derives raw Company Facts and enumerates none of
the verified engine-187 Inline-XBRL manifest/source files. Fourteen dashboard companies therefore
derive `foreign`; 128 required artifact-role links are omitted.

**Correct shape from zero.** The source owner verifies and enumerates its retained manifest/files.
The existing importer stores and reads back every object through S3, applies the same verified
supplement through `EvidenceLoader`, derives one canonical payload, and publishes the immutable
snapshot with exact role/hash links. Unsupported securities still fail before selection.

1. Read `~/.agents/skills/_shared/code-work.md`; invoke `applying-feature` and `ponytail`. Trace
   importer preparation, `_retain`, `EvidenceLoader`, snapshot identity/artifact links, retry, and
   real storage test helpers. Record reuse decision.
2. Add one public Inline-XBRL retained-artifact enumerator. It must reuse manifest verification,
   return manifest bytes plus each verified role/path/media type/hash/size, preserve duplicate
   role links to one content hash, reject stale/partial/unsafe/tampered manifests, and expose no
   absolute path in stored metadata.
3. Extend only the SEC importer branch. When a current manifest exists, retain/read back manifest
   and every named file before derivation, then apply the current supplement through the existing
   source/evidence owner. No manifest keeps prior behavior exactly.
4. Artifact role names must distinguish manifest, direct annual files, annual wrapper files, and
   incorporated source files. Same bytes under two roles remain two snapshot links and one object.
5. Existing Company Facts, DERA, cover, ticker mapping, and EDINET roles remain unchanged. No
   duplicate parser, financial rule, ticker exception, or migration.
6. Derivation errors still withhold selection. For AHNRF, BRBI, NXAT, and CIB, all statement bytes
   must be retained/read back, then the existing exact cover/ratio error must remain.
7. Tests: no-manifest compatibility; direct and incorporated artifact enumeration; tamper/stale/
   unsafe/duplicate-role behavior; supplement reaches derive; 14 exact payloads; four unchanged
   failures; object deduplication; artifact links; idempotent retry/snapshot reuse.
8. Real evidence with in-memory stores only: run the exact 18 manifest companies. Require 18
   verified manifests, 110 file roles, 108 unique source hashes, 18 manifest objects, 14 exact
   SQLite payload hashes, and four exact security failures. Repeat and prove object/snapshot reuse.
9. Run focused tests, full Python suite, real 18-case verifier, syntax, and diff check. Do not run
   full universe or services; those are milestone 9.

## Return protocol

Update only Developer update in `local/dev28-review-map.md`. Report exact code, roles/counts,
checks, 14 payloads, four failures, reuse evidence, decisions, limits, and concurrent edits. Never
mark accepted.
