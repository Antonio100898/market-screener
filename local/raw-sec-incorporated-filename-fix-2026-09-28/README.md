# Incorporated exhibit filename fix

Result: the annual relationship now owns the exact statement document. SEC
submissions still owns the source accession, form, filing date, and filing-wrapper
name. Both the wrapper and linked statement must be safe names present in the same
source filing index before activation.

The regression failed before the fix because `statements.htm` did not equal the
submissions primary document `filing-wrapper.htm`. It passes after the fix. A linked
document absent from the source index still fails closed.

## Verification

- Focused owned tests: 131 passed.
- Full Python suite: 840 passed, 6 skipped.
- Direct parser: 17 companies, 124 selected anchors, 18,775 standard facts; every
  source hash and selected anchor reproduced.
- Direct integration: all 17 rows match the accepted result. Its stored report is
  stale only because it records engine 186 and the current code is engine 187.
- Real CNI: acquisition `activated`; derivation `ok`; 40-F annual identity and 6-K
  source identity preserved; exact source document is `cni-20251231.htm`; submissions
  filing document is `cni-20251231_d2.htm`; balance reconciles.
- Python compilation and `git diff --check` passed.

`verify_cni.py` reads the accepted retained SEC cache and Company Facts, then uses a
temporary cache and database. It does not call SEC, providers, or models and does not
mutate the main cache or store. Full UI gates were intentionally left to the bounded
engine-187 delivery run.

Machine-readable CNI evidence is in `results.json`.
