# Developer 21 — review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Retained statement acquisition and evidence merge**
Review state: **ACCEPTED — Review 1**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

Implementation complete. No commit made.

### Trace and reuse

- Decisions preserved: P-02, P-03, P-04, P-09.
- Current annual discovery: SEC submissions `filings.recent`.
- HTTP/rate owner: `EdgarClient`.
- Atomic retention owner: existing `sync._retain_source_bytes`.
- Parsing owner: existing `sources.inline_xbrl.parse_inline_xbrl`.
- Merge/evidence owner: `EvidenceLoader.load`.
- Derivation owner: `sync._derive_evidence`; the process worker now calls the same
  pure retained-supplement loader before constructing its bundle.
- Reuse decision: `local/raw-sec-inline-integration-2026-09-28/reuse-decision.md`.

### Changes

- Added portable cache contract at
  `sec-inline/accessions/<accession-digits>/` plus
  `sec-inline/current/<CIK>.json` under the caller's SEC cache.
- Manifest records CIK, direct annual/source metadata, parser revision, required
  roles, safe SEC URLs, SHA-256, and byte sizes. No absolute path is serialized.
- Added bounded `python -m screener.sync inline --cik CIK [--cik CIK]` job.
- The job retains index, primary document, extracted instance, FilingSummary,
  presentation linkbase (or verified issuer schema when SEC supplies no separate
  presentation file), and issuer schema. It verifies and parses every required
  byte before atomically publishing the current manifest.
- Missing extracted instance returns named
  `unsupported_relationship/incorporated_filing_relationship`. No CNI special case.
- Existing verified current bytes are reused by recorded size/hash. Partial fetches
  leave no active manifest.
- Added missing-only Company Facts merge. Existing entries win; exact duplicates
  deduplicate; same-accession/concept/unit/context value conflicts fail closed;
  underscored source and dimension fields survive.
- `EvidenceLoader.load` applies only the current verified direct annual manifest.
  Missing/non-SEC behavior is unchanged. Stale and unknown-current states are
  explicit and do not merge.
- A newly activated manifest marks only that CIK dirty. Reuse is idempotent.
- Engine version: 185 -> 186.

### Checks

- Focused: 139 passed.
- Full Python suite: 824 passed, 6 skipped, one existing Starlette warning.
- Pure parser verifier: 17 companies, 18,775 standard facts, 124/124 accepted
  anchors, all source hashes preserved.
- Integration verifier: all 17 manifests applied through `EvidenceLoader` and
  `_derive_evidence`; 13 now derive `ok`, 4 remain `foreign` for separate cover
  evidence gaps.
- `python3 -m py_compile` passed for all changed production modules.
- `git diff --check` passed.
- Main SQLite store was opened read-only by the real verifier. No source bytes
  were copied and no full derive/export/regression/audit ran.

### Real 17-case status

- `ok`: AERO (2025-12-31, 0001193125-26-197494), AGMB (2025-12-31,
  0001104659-26-047872), ALPS (2026-03-31, 0001493152-26-036364), AUGO
  (2025-12-31, 0001171843-26-002783), DAVI (2025-12-31,
  0001683168-26-003530), GCDT (2026-03-31, 0001493152-26-038530), GMTL
  (2026-06-30, 0001104659-26-108373), HBNB (2025-12-31,
  0001213900-26-049771), PAYP (2026-03-31, 0001193125-26-289382), PICS
  (2025-12-31, 0001213900-26-049950), TMCR (2025-12-31,
  0001104659-26-049527), VMET (2025-12-31, 0001104659-26-052847), YMAT
  (2025-12-31, 0001493152-26-019802).
- Still `foreign`: AHNRF and BRBI have no exact filing-cover title; NXAT has no
  exact filing-cover title; CIB has no positive filing-cover receipt ratio.
- Exact before/after results: `local/raw-sec-inline-integration-2026-09-28/results.json`.

### Limits and workspace

- Direct annual accessions only. Incorporated exhibits remain queued.
- No main-cache activation or PostgreSQL artifact import yet.
- Disk was 1.7 GiB free at start; it is 12 GiB free at final check. This work
  created only code, tests, manifests in temporary directories, and small reports.
- No other session was observed touching the owned production/test files. The
  pre-existing unrelated change in `local/foreign-coverage-plan-2026-09-28.md`
  remains untouched.

## Reviewer response

### Review 1 — accepted

Accepted. Acquisition is explicit-CIK bounded, retains and verifies every required SEC file before
manifest activation, marks only changed CIKs dirty, and is idempotent. EvidenceLoader applies only
the current direct annual manifest and merges missing facts without overriding Company Facts. The
reviewer independently observed 139 focused passes and all 17 real manifests through derivation:
13 `ok`, four blocked only by existing cover/ratio evidence. Engine 186 is justified. CNI and
PostgreSQL artifact retention remain separate increments.
