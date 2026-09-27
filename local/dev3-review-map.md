# Developer 3 review map — Increment 1B

## Manager scope

Integrate the finished application changes, preserve the real runtime baseline,
and run every required release gate without changing the owner checkout.

## Developer update

Developer state: **READY_FOR_REVIEW**

The finished-session application changes are integrated in the dedicated
worktree. The real SQLite/source/UI baseline is preserved outside the repository,
the engine-181 payload was rebuilt and audited, and the P-13/P-16 percentile UI
  bug found during review is fixed. The only check not rerun is the real
PostgreSQL/S3 restart test because Docker Desktop cannot start on this machine.

### Reuse and flow

The existing owners were extended instead of duplicated:

- `normalize.py` maps SEC/IFRS evidence into canonical facts with original
  provenance. Its existing IFRS alias, short-term-debt rollup, and CapEx tag
  paths now recognize continuing-operations OCF, bank repo funding, and oil/gas
  cash CapEx. `store.ENGINE_VERSION` invalidates older derived snapshots.
- `ownerEarnings.js` owns ten-slot FCF/revenue and CapEx/OCF histories.
  Cash-flow evidence uses those same latest ten fiscal-year slots, so older
  evidence cannot avoid the required zero score.
  `screen.js` owns Return Quality. `App.jsx` computes one percentile cohort over
  the full payload, renders the result, and discloses both final divisors.
- The correct P-13/P-16 flow is: percentile-weight the six available inputs,
  transfer ten points of weight from ROE to D/E when D/E is at least one, then
  divide the weighted base by the bounded debt divisor and the CapEx/OCF divisor.
  Missing penalties stay unassessed; missing OCF or FCF returns zero.

### Changed paths

- Deleted `.agents/skills/share-screener/SKILL.md` and
  `.agents/skills/share-screener/agents/openai.yaml`.
- Integrated `api/screener/normalize.py`, `api/screener/store.py`,
  `api/tests/test_normalize_balance.py`, and `api/tests/test_normalize_notes.py`.
- Integrated `web/src/App.jsx`, `web/src/ownerEarnings.js`, `web/src/screen.js`,
  `web/src/styles.css`, `web/test/owner-earnings.test.mjs`, and
  `web/test/return-quality.test.mjs`.
- Updated `product.md` and `product/personal-research.md` to state that P-16
  supersedes P-11's original equal-weight wording without removing the required
  FCF/revenue input.
- Added `local/baseline-2026-09-25/README.md` and generated
  `local/baseline-2026-09-25/regress-engine181.txt`.

The ten source/test files first matched the owner checkout byte-for-byte. The
owner diff and worktree diff both had SHA-256
`ED7529D0FC40CF65B3D56B01AEC51770D4E085464A2E51EA60AEAE70869FEDEA`
over `43815` bytes. Review then intentionally changed `App.jsx`, `screen.js`,
`ownerEarnings.js`, `return-quality.test.mjs`, and the two product documents to
restore and document accepted P-13/P-16 behavior.

### Baseline evidence

External directory:
`C:\Users\amwor\.codex\baselines\market-screener\baseline-2026-09-25`

- `screener.db`: 591175680 bytes, SHA-256
  `5BA48CDFD4DC2C92D7A50C31D0F93964B0F295F4992315865BCBDBAD3C11E0CA`.
  A fresh read-only `PRAGMA integrity_check` returned `ok`; `snapshot` has 20412
  rows.
- `dashboard.json`: 211011167 bytes, SHA-256
  `AA590429396E2940B19169731521EC76934C11F7D43C3B3FA578DA8F15E010D1`.
  It is engine 180 with 6676 rows and no duplicate CIK or ticker.
- `source-cache.tar.zst`: 3875989746 bytes, SHA-256
  `74EED3F7393A63FF1DB224C9608E3AE0ECC22CFBE23F00E67C6D9FECBB0F40DC`.
  A complete listing found 27089 files and zero forbidden entries.

All three hashes were re-read successfully on 2026-09-27. Full inventory and
table counts are in `local/baseline-2026-09-25/README.md`.

### Verification results

- `.venv\Scripts\python.exe -m pytest -q tests/test_normalize_balance.py
  tests/test_normalize_notes.py tests/test_artifacts.py tests/test_object_store.py
  tests/test_storage_config.py`: `233 passed in 2.41s`.
- `.venv\Scripts\python.exe -m pytest -q`: `693 passed, 1 skipped` in
  `16.90s`. The skip is the opt-in real storage test. One existing
  FastAPI/Starlette deprecation warning remains.
- `node --test test/owner-earnings.test.mjs test/return-quality.test.mjs` before
  review correction: `28 passed`.
- `node --test test/owner-earnings.test.mjs test/return-quality.test.mjs` after
  the final correction: `33 passed, 0 failed`. Focused tests prove both
  divisors, missing penalties, missing OCF/FCF, bounded scores, high-debt weight
  transfer, and that cash-flow evidence outside the latest ten fiscal years
  cannot avoid zero.
- Full `node --test` after correction: `113 passed, 0 failed`. Vite emitted its
  existing shutdown-race dependency-scan noise, but the runner exited zero.
- `npm run build`: 51 modules transformed; production build passed.
- Full-universe regression before export: `4322 changes across 6676 companies`.
  These are the intended 4314 `owner_earnings` changes plus three same-CIK ticker
  updates, three context-note updates, and two linked analysis-route updates.
  There were no `criteria` or `verdict` field changes.
- The saved compact rerun after live export reports 4355 changes across 6676
  companies and 484 exact field paths. Its additional 33 changes are six
  `annual_ratios` price-derived paths updated by the export's live price-history
  refresh. Report SHA-256:
  `469E8688CFC1684E18582E3B8E812BA03B2BDF982262C226FF55D23A8AB51F14`.
- The engine changes affect the expected field family: oil/gas CapEx and IFRS OCF
  provenance, annual cash-flow bridges, FCF, per-share values, CapEx floors,
  reconciliation, operating-return inputs, and rolling ten-year owner-earnings
  history. The complete path/count list is the saved regression report.
- The three ticker changes preserve issuer identity:
  CIK `0002099681` MOT -> MEAOF, `0001663038` QTZM -> QGAI, and
  `0001409253` NAFS -> THRC. Baseline and rebuilt payloads both have 6676 rows,
  zero duplicate CIKs, and zero duplicate tickers.
- `.venv\Scripts\python.exe -m screener.sync derive`: `0
  dashboard-eligible snapshots predate engine v181`; the shared database had
  already been derived by the finished session.
- `.venv\Scripts\python.exe -m screener.sync export`: fetched/refreshed 6464
  verified tickers and wrote 211.21 MB for all 6676 companies. Rebuilt payload
  SHA-256 is `F1A8F0864E1B231FEDCD9E7327A2B38B5F3707DCCF02E37ED5964E755EF6602E`.
- `.venv\Scripts\python.exe -m screener.audit`: 279 sourced values and 3411
  arithmetic figures passed; zero wrong.
- `.venv\Scripts\python.exe -m screener.audit --filings`: 279 sourced values,
  3411 arithmetic figures, and 378 published-statement values passed; zero wrong.
- Final real UI calculation over the rebuilt 6676 rows: zero
  non-finite/out-of-range
  scores, zero cash-flow-missing rows with a nonzero score, 4572 rows with at
  least one assessed penalty, minimum 0, maximum 97.40801497907508.
- `docker compose config --quiet`, Alembic `heads`, and `git diff --check`
  passed. The final `git diff --check` emitted only Windows line-ending
  warnings. Alembic head is `20260925_0001`.

### Known environment limit

Docker Desktop 4.60.1 crashes before its daemon starts:

`starting services: initializing Inference manager: ... remove
C:\Users\amwor\AppData\Local\Docker\run\dockerInference: The file cannot be
accessed by the system.`

The exact entry resolves to
`C:\Users\amwor\AppData\Local\Docker\run\dockerInference`; it is a zero-byte
reparse point last written `2026-09-25T16:38:09Z`. After stopping every Docker
Desktop/backend process, both `Move-Item` and `Rename-Item` failed with `The file
cannot be accessed by the system`. No backup path was created, the entry was not
deleted, no Docker data or volume was changed, and all Docker processes started
for the attempt are stopped.

Therefore the current run could not execute the opt-in PostgreSQL/S3 migration,
upload/read, forced-failure, restart-retention, `alembic current`, or `alembic
check` checks. Increment 2's accepted review evidence records that all of those
real checks passed before this Docker runtime failure, including clean exit code
0 for both services after restart. The application `store.py` change in this
increment is only the SQLite derivation `ENGINE_VERSION` bump; the focused and
full Python suites passed.

This Docker limitation does not block review: Increment 2's accepted evidence
covers the real storage checks, and the current increment's storage-facing code
is limited to the SQLite engine-version bump verified by the Python suites.

No commit or push was made. No other session touched the owned source paths while
this increment ran.

**READY_FOR_REVIEW**

## Reviewer response

**ACCEPTED.** Independent review verified the ten-slot cash-flow evidence rule,
P-11/P-16 supersession, both disclosed score divisors, 33 focused web tests, the
production build, baseline hashes, SQLite integrity, normalization checks, and
current storage unit tests. Increment 2's accepted real PostgreSQL/S3 evidence is
sufficient because this increment changed only SQLite's engine version in
`store.py`; the Docker Desktop failure is a recorded machine limitation.
