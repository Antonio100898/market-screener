# Attached purchase-rights regression review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Restore common shares with attached purchase rights**
Review state: **ACCEPTED — Review 1**

The developer writes only under "Developer update". The reviewer writes the header and
"Reviewer response".

## Developer update

Developer state: **READY_FOR_REVIEW**

- **Causal trace:** `cover.is_common_equity_security` owns the cover-title class decision used by
  sync, evidence selection, and foreign normalization. Engine 183 expanded the shared rejection
  token to plural `rights?`, but its attached-rights stripper handled only an `including ...`
  subset. CTRM, CVE, and SOBO therefore reached the rights rejection despite naming common shares.
- **Reuse decision:** extended the predicate's existing normalization step. It now strips only a
  trailing `including` or `together with` purchase-rights clause when common or ordinary shares are
  already named before that clause. The filed title stays unchanged. No ticker rule, caller guard,
  identity alias, or second class path was added. Detail is in
  `local/foreign-rights-regression-2026-09-28/reuse-decision.md`. P-02, P-03, P-04, P-09, and the
  foreign-filer invariant remain unchanged.
- **Changed files:** `api/screener/sources/cover.py`, `api/screener/store.py`,
  `api/tests/test_cover.py`, `local/foreign-rights-regression-2026-09-28/reuse-decision.md`, and
  this Developer update. Engine version is 184.
- **Regression cases:** exact CTRM, CVE, and SOBO titles now pass. CVE's `associatedcommon` filing
  text is covered. BBVA's exact `right to receive` ADS title still passes. Standalone purchase and
  subscription rights, rights to purchase shares, preferred shares with attached common-share
  rights, attached rights followed by warrants, standalone warrants, notes, and ETNs still fail.
- **Failing before:**
  `PYTHONPATH=api api/.venv/bin/pytest -q api/tests/test_cover.py -k common_class_with_attached_rights`
  failed at the accepted-title assertion before the predicate change.
- **Passing after:** the same case passed: `1 passed, 36 deselected`. Focused cover/evidence tests:
  `49 passed in 0.10s`. Full Python suite: `780 passed, 6 skipped, 1 existing dependency warning in
  2.74s`. Focused and full checks were rerun after the final adverse case was added. Compileall and
  `git diff --check` passed with no output.
- **Skills:** `plain-writing`, `bug-fix`, and `ponytail` full. They kept the fix in the shared
  predicate and limited the exception to a suffix after an already named common class.
- **Limits:** per guidance, no derive, export, regression, audit, filing audit, live source fetch,
  or UI-payload gate ran. The manager still owns the full engine-184 rerun.
- **Concurrent edits:** accepted dev9-dev11 changes were already present in the owned source, store,
  and test files before this increment and were preserved. No concurrent overlap was observed. No
  commit, stash, branch switch, or destructive Git action was made.

## Reviewer response

### Review 1 — accepted

Accepted. The fix stays in the shared cover-class predicate and strips only a trailing attached
purchase-rights clause after an already named common/ordinary class. Exact CTRM, CVE, SOBO, and
BBVA titles pass; standalone/subscription rights, warrants, preferred shares, notes, ETNs, and debt
remain rejected. The reviewer independently observed 49 focused tests. The developer observed 780
Python tests with six skips. Engine 184 separates corrected semantics from local engine-183 rows.
No payload gate or commit was made.
