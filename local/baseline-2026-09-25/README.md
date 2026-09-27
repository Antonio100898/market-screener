# Runtime baseline — 2026-09-25

This baseline preserves the real local data and exact UI payload before the
engine-181 derive/export. The source cache was read only. No source fetch,
derive, or export ran before these copies completed.

## Source identity

- Source checkout: `C:\Dev\graham-screener`
- Worktree: `C:\Users\amwor\.codex\worktrees\market-screener-platform\graham-screener`
- Source and worktree commit: `697ef24750ea5a40f545073498168dc33070ef43`
- Finished-session diff SHA-256: `ED7529D0FC40CF65B3D56B01AEC51770D4E085464A2E51EA60AEAE70869FEDEA`
- Finished-session diff bytes: `43815`
- The worktree diff over the 12 integrated paths has the same SHA-256 and byte count.
- The ten integrated source/test files initially had byte-identical SHA-256
  hashes to the owner checkout. Review then found that the integrated percentile
  UI omitted the accepted P-13/P-16 divisors. `web/src/App.jsx`,
  `web/src/screen.js`, and `web/test/return-quality.test.mjs` now intentionally
  differ to restore those product rules. The two obsolete `share-screener` skill
  files are deleted.

## External artifacts

Directory: `C:\Users\amwor\.codex\baselines\market-screener\baseline-2026-09-25`

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `screener.db` | 591175680 | `5BA48CDFD4DC2C92D7A50C31D0F93964B0F295F4992315865BCBDBAD3C11E0CA` |
| `dashboard.json` | 211011167 | `AA590429396E2940B19169731521EC76934C11F7D43C3B3FA578DA8F15E010D1` |
| `source-cache.tar.zst` | 3875989746 | `74EED3F7393A63FF1DB224C9608E3AE0ECC22CFBE23F00E67C6D9FECBB0F40DC` |

The ignored worktree copy at `api/screener/static/dashboard.json` has the same
payload SHA-256. The source payload was last written at
`2026-09-25T16:49:20Z`. The final consistent SQLite backup completed at
`2026-09-25T17:22:20Z`.

## Source-cache inventory

- Included files: `27089`
- Included uncompressed bytes: `23824458593`
- Archive entries: `27091` (`27089` files plus the root and `edinet` directory)
- Forbidden archive entries: `0`
- Excluded: `screener.db*`, `backups/`, `baselines/`, and
  `regression-baselines/`

`tar -tf` read the complete archive successfully. The source files were stable:
their newest write was `2026-09-24T20:24:58Z`, before preservation began.

## SQLite verification

`PRAGMA integrity_check` returned `ok` from the external backup in read-only
mode. Table row counts:

| Table | Rows |
|---|---:|
| `company` | 21436 |
| `filing_event` | 10665 |
| `fx_history` | 15 |
| `pending_filing` | 171 |
| `portfolio` | 1 |
| `portfolio_asset` | 1 |
| `portfolio_cash` | 1 |
| `portfolio_trade` | 2 |
| `price_history` | 6369 |
| `security_cover` | 7725 |
| `snapshot` | 20412 |
| `snapshot_dirty` | 9 |
| `sync_state` | 6 |
| `tracked` | 12 |

## UI payload verification

Python parsed both copied payloads successfully. The preserved payload has:

- generated: `2026-09-25T16:49:16+00:00`
- engine version: `180`
- rows: `6676`
- duplicate non-empty CIKs: `0`
- duplicate non-empty tickers: `0`
- top-level keys: `engine_version`, `generated`, `rows`

## Initial exact-integration file hashes

| Path | SHA-256 |
|---|---|
| `api/screener/normalize.py` | `D681A9F7694ACDAE62E1FF3C86CC4DD24D0BF20A1106A6488D0CCFFF108B3693` |
| `api/screener/store.py` | `9C1FEDCCBE689D7EA3E5EB73D61ACA1D13C185F7FF87F66B859300EBAC4A3D13` |
| `api/tests/test_normalize_balance.py` | `B5C426B032AAD304F9E04A1BCBD286BE68A77FF6DDAD34C76D71CDEABE7D84F4` |
| `api/tests/test_normalize_notes.py` | `4BEC36A355EC0EB21196276A75E043284D489780F59197E02FB5537C25B9B4A1` |
| `web/src/App.jsx` | `412AE0601CFE522F66EB5760A9CDB9F18D4B60422E6C808911C68B0A10184593` |
| `web/src/ownerEarnings.js` | `16E164D0278C5D9304C631651F4B692631CD60D10C7D4BB55C50B45966DA4193` |
| `web/src/screen.js` | `CA3C78810625638261F9500CB43404E02FED93BBCCF568F6A72B0C30449343F7` |
| `web/src/styles.css` | `B4675044CEE5489420A9C3B1655F504EED608AB03ADEE0F746227393CF028476` |
| `web/test/owner-earnings.test.mjs` | `1E1D1158945E6922D904C74668D0ACC3CD6D54286017241B4ADBC1E03F0EC76D` |
| `web/test/return-quality.test.mjs` | `93FEA3D21B9B2E09407E6EF526C3B2B156AB37BA624EBA9B682581217F5C5E35` |
