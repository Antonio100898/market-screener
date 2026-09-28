# Developer 21 — current guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev21-review-map.md`. Do not commit, stash, switch branches, call any
model/provider, edit importer/PostgreSQL/API/UI/plans/owner decisions, or change unrelated cache.

## Paths you own

- `api/screener/sources/inline_xbrl.py`
- `api/screener/evidence.py`
- `api/screener/sync.py`
- `api/screener/store.py`
- `api/tests/test_inline_xbrl.py`
- `api/tests/test_evidence.py`
- `api/tests/test_sync.py`
- `local/raw-sec-inline-integration-2026-09-28/**`
- Developer update in `local/dev21-review-map.md`

Read-only real source cache:

- `~/.cache/graham-screener/raw-sec-statement-recovery/`
- accepted parser/audit evidence under `local/raw-sec-*-2026-09-28/`

Disk has about 2.5 GiB free. Do not duplicate the 204 MB source cache or run full derive/export.

## Rules

Read `~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature` and `ponytail`. Trace every
caller before editing. Reuse the accepted pure parser, current `EdgarClient`, atomic retention,
`EvidenceLoader`, existing Company Facts merge conventions, and engine invalidation.

## Current increment — retained direct annual supplements

**Correct shape from zero.** A bounded source job retains the exact current annual SEC structured
filing files before parsing and writes a hash-bound manifest only after all required bytes verify.
`EvidenceLoader` reads that manifest, calls the pure parser, and merges only missing facts into the
existing Company Facts namespaces. Normalization and derivation remain unchanged owners.

1. Trace current annual selection, `EdgarClient` request/cache behavior, atomic byte retention,
   EvidenceLoader construction in every derive/API/import caller, fact merge/deduplication, dirty
   snapshots, and engine version. Record the reuse decision.
2. Define the production cache contract under the caller's SEC cache directory. Use one immutable
   accession directory plus one small current-CIK manifest. The manifest includes CIK, annual/source
   metadata, required filenames, URLs, SHA-256, sizes, and parser-contract revision. No absolute
   machine path enters it.
3. Add a bounded acquisition function/CLI path that requires an explicit CIK set. Discover the
   current 20-F/40-F files from official SEC filing metadata. Retain index, primary, extracted
   instance, FilingSummary, presentation linkbase, and schema bytes atomically before parsing.
   Existing verified bytes are reused by hash. Partial fetches never publish a manifest.
4. This increment supports direct annual filings only. If the facts live in another accession,
   return a named unsupported relationship for the later CNI increment; do not hardcode CNI.
5. Add a pure missing-only merge. Existing Company Facts entries always win. Exact supplement
   duplicates deduplicate. A supplement conflict with an existing same accession/concept/unit/
   context fails closed. Preserve all underscored source fields.
6. `EvidenceLoader.load` applies a current verified supplement when present. No manifest means the
   old behavior exactly. A stale manifest whose annual accession is not the current annual fails
   closed or is ignored with an explicit state; it may not silently supply old facts.
7. Mark a snapshot dirty only when a new verified current manifest becomes active. Bump engine
   version because current statement meaning changes. Do not refetch Company Facts.
8. Tests: atomic retention before parse; bounded CIK required; official URL/path safety; missing/
   hash/partial/stale manifest; no-manifest compatibility; missing-only merge; existing conflict;
   dimension preservation; dirty/idempotent behavior; all EvidenceLoader callers still work.
9. Real evidence without duplicate bytes: point a test/verifier at the accepted separate cache and
   create only small manifests in a temporary directory referencing the explicit retained root.
   Derive all 17 direct cases through `EvidenceLoader` and `_derive_evidence`; record exact status,
   current ticker, source accession, balance date, and changed/remaining failure. No main-store
   mutation yet.
10. Run focused tests, full Python suite, real 17-case verifier, syntax, and `git diff --check`.
    Do not run full derive/export/regression/audits until incorporated-exhibit support and review.

## Queued after review

Generic incorporated-exhibit discovery/provenance for CNI, then bounded main-cache activation,
full 18-company derive, UI gates, and PostgreSQL artifact import.

## Return protocol

Update only Developer update in `local/dev21-review-map.md`. Report trace, exact changes, engine
version, raw checks, 17 real statuses, cache/disk behavior, reuse decision, known limits, and
whether another session touched owned files. Never mark accepted.
