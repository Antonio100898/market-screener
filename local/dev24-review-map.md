# Developer 24 — review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Incorporated exhibit filename fix**
Review state: **ACCEPTED — Review 1**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

Implemented the incorporated exhibit filename fix without a company-specific rule.

Affected decisions are P-02, P-03, P-04, and P-09. The reuse decision is recorded in
`local/raw-sec-incorporated-filename-fix-2026-09-28/reuse-decision.md`.

Failing-before evidence:

- Added `test_incorporated_statement_document_may_differ_from_source_filing_primary`.
- Before the production edit it failed with `error` instead of `activated`, reproducing
  the exact linked-statement/submissions-primary mismatch.

Diff:

- `sync._retain_inline_annual` keeps accession, form, filing date, and wrapper name from
  submissions; confirms both wrapper and linked statement in the source index; uses the
  annual relationship's linked document as exact statement provenance.
- The manifest stores the submissions name as `source.filing_document` and the linked
  statement as `source.document`.
- Manifest verification validates the optional filing-wrapper name with the existing
  archive-name allowlist. Existing manifests remain compatible.
- Tests cover the differing names and an unlisted linked statement. Existing direct,
  identity, form, date, ambiguity, path, partial-fetch, and hash checks remain intact.

Checks:

- Focused owned files: 131 passed.
- Full Python: 840 passed, 6 skipped, one existing Starlette deprecation warning.
- Direct parser verifier: 17 companies, 124 selected anchors, 18,775 facts; all anchors
  and source hashes reproduced.
- Direct integration recomputation exactly matches all 17 accepted rows; the stored
  result differs only in `engine_version` 186 -> 187.
- Existing CNI verifier: `ok`, supplement `applied`, balance reconciled.
- New acquisition verifier: `activated`, then `ok`; annual 40-F
  `0001104659-26-010352`, source 6-K `0000016868-26-000011`, statement
  `cni-20251231.htm`, filing wrapper `cni-20251231_d2.htm`.
- `compileall` and `git diff --check` passed.

Evidence is in `local/raw-sec-incorporated-filename-fix-2026-09-28/README.md` and
`results.json`. The verifier used only accepted retained bytes plus a temporary cache
and database. No main cache/store mutation, network/model call, engine bump, UI gate,
or commit occurred. Full engine-187 UI gates remain with the reviewing session.

No other session touched the owned files during this increment.

## Reviewer response

### Review 1 — accepted

Accepted. The focused regression failed before the fix and passes after it. Submissions remains the
owner of source accession/form/filed and filing-wrapper name; the explicit annual relationship plus
same-accession SEC index owns the exact statement exhibit. The reviewer independently observed 131
focused passes and real CNI acquisition `activated` followed by derivation `ok`, with exact wrapper,
exhibit, 40-F, and 6-K identities. Engine remains 187.
