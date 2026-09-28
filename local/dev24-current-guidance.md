# Developer 24 — current guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev24-review-map.md`. Do not commit, stash, switch branches, call any
model/provider, edit unrelated paths, mutate the main cache/store, or run full UI gates.

## Paths you own

- `api/screener/sync.py`
- `api/screener/sources/inline_xbrl.py`
- `api/tests/test_sync.py`
- `api/tests/test_inline_xbrl.py`
- `local/raw-sec-incorporated-filename-fix-2026-09-28/**`
- Developer update in `local/dev24-review-map.md`

## Rules and captured defect

Read `~/.agents/skills/_shared/code-work.md`. Invoke `bug-fix` and `ponytail`.

Expected: an annual wrapper's explicit same-CIK SEC link identifies the incorporated statement
document inside a Form 6-K accession. The source filing index proves that document belongs to the
accession. SEC `submissions.primaryDocument` identifies the 6-K wrapper, not necessarily its audited
statement exhibit.

Actual CNI failure: annual link names `cni-20251231.htm`; source 6-K metadata names primary document
`cni-20251231_d2.htm`; sync rejects before reading the source index with
`incorporated SEC document does not match submissions`.

**Root cause.** `_retain_inline_annual` treats source-filing `primaryDocument` as the incorporated
document authority. The annual relationship link plus source filing index own that decision.

1. Reproduce the exact mismatch with a focused failing test before editing.
2. Keep source accession/form/filed identity from submissions. Treat the annual relationship's
   linked document as the statement source only after the same-accession SEC index contains it and
   the retained parser contract verifies it. Optionally retain the filing wrapper name separately;
   do not replace exact exhibit provenance with it.
3. Preserve all existing CIK/accession/form/date/link/ambiguity/path/hash checks. An unlisted linked
   document must fail closed. No CNI names or identifiers in production.
4. Tests: linked exhibit differs from source primary and passes; linked file absent from index
   fails; direct annual unchanged; other relationship negatives unchanged.
5. Real evidence without main mutation: use the accepted retained cache to build the CNI manifest,
   verify source document `cni-20251231.htm`, derive `ok`, and preserve 40-F/6-K dual provenance.
6. Run focused tests, full Python suite, direct 17 verifier, real CNI verifier, syntax, and diff
   check. No engine bump: this restores the already-approved engine-187 contract.

## Return protocol

Update only Developer update in `local/dev24-review-map.md`. Report failing-before/passing-after
evidence, exact diff, checks, real CNI result, known limits, and whether another session touched
owned files. Never mark accepted.
