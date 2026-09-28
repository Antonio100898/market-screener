# Developer 25 — review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Prefer direct annual instance before incorporation fallback**
Review state: **ACCEPTED — Review 1**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

Implementation complete. No commit made.

### Defect and reuse

- Decisions preserved: P-02, P-03, P-04, P-05, and P-09.
- Reused `sync._retain_inline_annual`, `_inline_documents`,
  `inline_xbrl.incorporated_annual_source`, and the existing manifest verifier.
- Reuse record:
  `local/raw-sec-direct-before-incorporation-fix-2026-09-28/reuse-decision.md`.
- The new regression test failed before the fix with
  `unsupported_relationship`; the same test passes after the fix.

### Changes

- `api/screener/sync.py`: validates the direct annual index and retained statement
  first. A coherent direct annual source wins without reading incorporation prose.
  Missing extracted statements, or a wrapper instance with the exact existing
  no-coherent-annual-balance result, may use the explicit incorporation path.
  Other fetch, identity, path, hash, parse, and conflict errors remain visible.
- `api/tests/test_sync.py`: added the unrelated-incorporation/direct-source
  regression and the CNI-shaped incoherent-wrapper fallback neighbor test.
- `local/raw-sec-direct-before-incorporation-fix-2026-09-28/`: added the reuse
  record and read-only real routing verifier.
- No production company, ticker, accession, filename, or exhibit special case.
  Engine remains 187.

### Real retained SEC evidence

- GCDT `0001926293`: `activated`, `direct_annual`, accession
  `0001493152-26-038530`; supplement `applied`; derives `ok`.
- HBNB `0002054507`: `activated`, `direct_annual`, accession
  `0001213900-26-049771`; supplement `applied`; derives `ok`.
- NXAT `0002000756`: `activated`, `direct_annual`, accession
  `0001829126-26-005357`; supplement `applied`; remains `foreign` only because
  its accepted cover has no exact ticker security title.
- CNI `0000016868`: `activated`, `incorporated_annual_exhibit`; derives `ok` from
  the 6-K source with a reconciled balance and the current 40-F annual identity.
- The verifier used temporary cache/store paths and retained bytes. Main data was
  not mutated. No provider, model, or network call ran.

### Checks

- Focused acquisition/neighbor tests: `16 passed, 97 deselected`.
- Full Python suite: `842 passed, 6 skipped`, one existing Starlette warning.
- Direct 17 parser verifier: 17 companies, 18,775 facts, 124/124 selected anchors,
  all source hashes preserved.
- Direct 17 integration build: 13 `ok`, four accepted cover-only failures; all 17
  company results equal the checked evidence. Only its recorded engine field is
  stale (`186` versus current `187`).
- New real routing verifier: three direct activations plus CNI incorporated
  activation/`ok` derivation passed.
- `py_compile` passed for owned Python paths.
- `git diff --check` passed.

### Limits and workspace

- The older standalone CNI verifier's semantic build passes, but its checked JSON
  expects eight retained reads. Direct-first validation now performs twelve, so
  that old file-level equality check is intentionally stale. The new verifier
  checks the semantic CNI contract without changing the older accepted artifact.
- No concurrent edit was observed in the owned production or test files. The
  pre-existing unrelated change in `local/foreign-coverage-plan-2026-09-28.md`
  remains untouched.

## Reviewer response

### Review 1 — accepted

Accepted. The captured direct-filing regression failed before and passes after. Direct annual
structured files are now parsed and coherence-verified before prose relationship discovery; only
missing structured files or the exact incoherent-wrapper result may reach the explicit fallback.
The reviewer independently observed 133 owned-path passes and real GCDT/HBNB/NXAT direct activation
plus CNI incorporated activation/`ok`. Engine remains 187.
