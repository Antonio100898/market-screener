# Developer 25 — current guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev25-review-map.md`. Do not commit, stash, switch branches, call any
model/provider, edit unrelated paths, mutate main data, bump engine, or run UI gates.

## Paths you own

- `api/screener/sync.py`
- `api/screener/sources/inline_xbrl.py`
- `api/tests/test_sync.py`
- `api/tests/test_inline_xbrl.py`
- `local/raw-sec-direct-before-incorporation-fix-2026-09-28/**`
- Developer update in `local/dev25-review-map.md`

## Captured defect

Read `~/.agents/skills/_shared/code-work.md`. Invoke `bug-fix` and `ponytail`.

Expected: when a current annual filing's own SEC index contains a complete Inline-XBRL instance,
that direct annual source wins. Incorporation discovery is a fallback only when the direct filing
lacks the structured statement files.

Actual: `_retain_inline_annual` calls `incorporated_annual_source` before checking the annual index.
GCDT `0001926293`, NXAT `0002000756`, and HBNB `0002054507` each have valid direct extracted
instances but unrelated incorporation-by-reference language. Relationship discovery yields no
audited-statement candidate and returns `unsupported_relationship`.

**Root cause.** Source-routing order is reversed. The direct filing index is the stronger machine
evidence and must be tested before prose-based incorporation fallback.

1. Add a failing test: direct annual index has full structured files while primary text contains
   unrelated Form 6-K incorporation language/no audited statement link; result must activate a
   direct manifest without relationship parsing failure.
2. Route direct `_inline_documents` first. Only its specific missing-extracted-instance outcome may
   invoke incorporation discovery. Other direct errors remain visible. Preserve CNI fallback and all
   relationship identity checks.
3. Real read-only verification: use accepted retained bytes for the three failed CIKs and CNI.
   Confirm three direct manifests activate/derive to their existing cover outcomes and CNI still
   activates as incorporated/derives `ok`. No company-specific production rule.
4. Run focused tests, full Python suite, direct 17 verifier, CNI verifier, syntax, and diff check.
   Engine remains 187.

## Return protocol

Update only Developer update in `local/dev25-review-map.md`. Report failing-before/passing-after,
exact changes, three direct states, CNI state, checks, limits, and concurrent edits. Never accept
your own work.
