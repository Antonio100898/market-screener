# Attached purchase-rights bug-fix developer guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev12-review-map.md` before work. Update only the Developer update section
of the review map. Do not commit, stash, switch branches, or edit outside the owned paths.

## Owned paths

- `api/screener/sources/cover.py`
- `api/screener/store.py`
- `api/tests/test_cover.py`
- `local/foreign-rights-regression-2026-09-28/**`
- Developer update in `local/dev12-review-map.md`

All other paths are read-only. Return `BLOCKED:` if another contract must change.

## Standing rules

Read `AGENTS.md`, `api/AGENTS.md`, the foreign invariant, accepted dev9-dev11 reports, the exact
regression evidence in this guidance, and `~/.agents/skills/_shared/code-work.md`. Invoke `bug-fix`
before editing and `ponytail` after the cause is understood.

## Current increment — restore common shares with attached purchase rights

Expected behavior: a common/ordinary share title remains common equity when a suffix says purchase
rights are attached. Standalone rights, warrants, preferred shares, notes, ETNs, and debt remain
non-common.

Observed engine-183 regression:

- CTRM: `Common Shares, $0.001 par value, including associated Share Purchase Rights under the
  Shareholder Protection Rights Agreement` became unsupported.
- CVE: `Common shares, no par value (together with associatedcommon share purchase rights)` became
  unsupported.
- SOBO: `Common shares (including common share purchase rights)` became unsupported.

Root cause: dev9 broadened the non-common token from singular `right` to `rights?`, while the suffix
stripper recognizes only a narrower `including Preferred Stock Purchase Rights` form.

1. Fix the owning cover-class predicate generically. Strip only an attached purchase-rights suffix
   that follows an already named common/ordinary share class, including parenthetical, `together
   with`, `associated`, and filing text with a missing space such as `associatedcommon`.
2. Do not remove `rights to purchase shares`, subscription rights, standalone purchase rights,
   warrants, or preferred-class wording.
3. Add exact regression cases for CTRM, CVE, and SOBO plus the existing adverse classes. Confirm
   BBVA's `right to receive` remains accepted.
4. Bump `store.ENGINE_VERSION` from 183 to 184. Engine 183 snapshots already exist locally and must
   not share an identity with corrected semantics.
5. Run focused cover/evidence tests and the full Python suite. Do not run derive/export/payload
   gates; the manager owns the full engine-184 rerun.

## Return protocol

Set Developer state to `WORKING`, `READY_FOR_REVIEW`, or `BLOCKED_EXTERNAL`. Report the causal
trace, reuse decision, changed files, failing-before/passing-after evidence, focused/full checks,
skills, limits, and concurrent edits. Never mark review accepted.
