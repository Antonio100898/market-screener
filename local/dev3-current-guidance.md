# Developer 3 guidance — Increment 1B

## Goal

Integrate the finished owner-session application changes into the dedicated
worktree, preserve the current real runtime data before any rebuild, and prove
the combined branch against the full baseline and storage checks.

## Product decisions

Preserve P-02, P-03, P-04, P-09, P-11, P-13, P-16, and P-17. Read `product.md`
and the linked documents before editing. Product documents are manager-owned for
this increment; do not edit them.

## Owned paths

- `.agents/skills/share-screener/**` (delete the two obsolete files)
- `api/screener/normalize.py`
- `api/screener/store.py`
- `api/tests/test_normalize_balance.py`
- `api/tests/test_normalize_notes.py`
- `web/src/App.jsx`
- `web/src/ownerEarnings.js`
- `web/src/screen.js`
- `web/src/styles.css`
- `web/test/owner-earnings.test.mjs`
- `web/test/return-quality.test.mjs`
- `local/baseline-2026-09-25/**`
- the Developer update section of `local/dev3-review-map.md`
- external backup directory `C:\Users\amwor\.codex\baselines\market-screener\baseline-2026-09-25`
- ignored worktree payload `api/screener/static/dashboard.json`

Do not edit any other paths. Do not commit or push.

## Integration source

Use the exact uncommitted diffs from `C:\Dev\graham-screener` for the owned
source and test files. Apply source changes with `apply_patch`. After applying,
prove each integrated source/test file has the same SHA-256 hash as the owner
checkout. Delete the obsolete share-screener skill files with `apply_patch`.

## Baseline preservation

Do this before any derive, export, or source fetch:

1. Create the external backup directory.
2. Use Python's SQLite backup API to copy
   `C:\Users\amwor\.cache\graham-screener\screener.db` consistently.
3. Copy the current owner payload
   `C:\Dev\graham-screener\api\screener\static\dashboard.json` to both the
   external backup and the ignored worktree runtime path.
4. Archive the current source cache, excluding `screener.db*`, `backups/`,
   `baselines/`, and `regression-baselines/`.
5. Record hashes, byte/file counts, timestamps, SQLite integrity/table counts,
   source revision, and integrated diff identity in a tracked manifest under
   `local/baseline-2026-09-25/`.
6. Prove the copied payload and database are readable and match their recorded
   hashes. Never modify or remove the source cache.

## Verification

Run one large check at a time because local memory is limited:

1. focused Python normalization/storage tests
2. full Python suite
3. full web suite
4. full-universe regression against the preserved payload
5. rebuild through the real export/enrichment path
6. audit
7. audit-filings because extraction/provenance changed
8. storage integration/restart/failure tests after the `store.py` change

Use the repository's documented commands or their Windows equivalents. If a
check cannot run because of credentials, network, cache, or dependencies, record
the exact command and failure. Do not call the increment complete. Do not claim
zero regressions without the full UI-payload comparison.

## Handoff

Create `local/dev3-review-map.md` with changed paths, exact commands and observed
results, baseline artifact paths and hashes, behavior changes mapped to product
decisions, remaining limits, and a Developer update ending with either
`READY_FOR_REVIEW` or `BLOCKED`.
