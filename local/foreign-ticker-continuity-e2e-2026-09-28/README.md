# Engine-185 full payload gate

**Result: READY_FOR_REVIEW.** BRNX and ZTG entered the UI payload with exact later-SEC
same-class evidence. No existing company was removed. No shared company changed filing facts,
source accessions, criteria status, verdict, or historical ratios.

Affected product decisions: P-02, P-03, P-04, and P-09. This gate preserved one official payload,
retained separate annual and later filing evidence, and kept all shared calculations on the annual
statement facts.

## Payload identity

Engine 184 baseline, preserved before derive:

- SHA-256 `be40ae5261bc4827aeac4685879685e3a5a0950763543d2da62072a70587cce6`
- 220,328,826 bytes
- 6,979 rows; 6,979 unique tickers; 6,979 unique CIKs

Engine 185 UI payload:

- SHA-256 `d45bfb04044b42fa8221742873be1b4c688d4b722981987052c92f356ab5ce4b`
- 220,362,103 bytes
- 6,981 rows; 6,981 unique tickers; 6,981 unique CIKs
- Added: `BRNX` / `0001901215`, `ZTG` / `0002011458`
- Removed: none

The temporary 220 MB baseline was deleted after all comparisons completed.

## Shared-row comparison

All 6,979 shared rows were compared recursively. Changes were limited to the live quote refresh:

- Market-state time: 6,610 rows; quote time: 3,263; quote: 2,985; price statistics: 2,974.
- Price session: 969 rows, all `REGULAR` to `PRE`.
- Reporting-currency quote and FX: 228 rows each.
- Quote-derived criterion values: criterion 1 in 1,225 rows, criterion 5 in 329, and criterion 7
  in 754. There were zero criterion status changes and zero verdict changes.
- Quote-derived market-cap-to-net-cash: 164 rows. No separate market-cap field exists in this
  payload.
- Defensive valuation moved `PASS` to `FAIL` for PNRG, EEFT, and AFCG. Their overall profile
  verdicts did not change.
- Price-history warning state changed for IDXG and TANH. Both are provider-history refresh state,
  not filing data.
- Twelve criterion notes changed only because refreshed quote dates or prices changed displayed
  age/yield wording. Exact text is in `payload-change-summary.txt`.

Unchanged across every shared row: financials, annual EPS/revenue/net income, historical ratios,
source accessions, context notes, Graham profile metadata, pass counts, and verdicts. The exact
field counts are in `payload-change-summary.txt`.

## BRNX and ZTG evidence

Both ticker and CIK counts are exactly one.

- BRNX: annual 20-F `0001213900-26-034046` under BNRG; later F-3
  `0001213900-26-103505` identifies the same ordinary-share class on Nasdaq as BRNX. Statement
  facts remain sourced to the annual 20-F. Balance sheet 2025-12-31; verdict `FAIL`.
- ZTG: annual 20-F `0001493152-26-002776` under ZGM; later F-3
  `0001493152-26-043703` identifies the same Class A ordinary shares on Nasdaq as ZTG. Statement
  facts remain sourced to the annual 20-F. Balance sheet 2025-09-30; verdict `FAIL`.

The retained annual and later document SHA-256 values match the prior accepted evidence. BNRG,
ZGM, and the unsupported OTC example ABLZF are absent. Neither target CIK has an OTC row.

## Commands and observed results

```text
make regress ARGS='--all --baseline <preserved-engine-184-file>'
6979 recomputed; 6979 compared; no field moved

make derive
7247/7247 engine-185 snapshots recomputed; done

make export
6758/6758 verified tickers refreshed; wrote 6981/6981 companies

make audit
279 filing-source values ok, 0 wrong
3411 arithmetic checks ok, 0 wrong

make audit-filings
279 filing-source values ok, 0 wrong
3411 arithmetic checks ok, 0 wrong
378 published-statement checks ok, 0 wrong
0 payload figures uncheckable
```

`make audit` and `make audit-filings` use the repository's 22-company audit sample. The recursive
payload comparison covered the full shared 6,979-row baseline.

## Evidence files

- `baseline-identity.txt`, `new-identity.txt`: exact payload identities and uniqueness counts.
- `regress.log`, `derive.log`, `export.log`: complete production command output.
- `payload-change-summary.txt`: exact changed-field counts and small exceptional deltas.
- `target-payload.json`, `target-check.txt`: BRNX/ZTG facts, accessions, calculations, and aliases.
- `retained-evidence-sha256.txt`: hashes of both annual covers and both later filings.
- `audit.log`, `audit-filings.log`: complete audit output.
- `resources-before.txt`, `resources-after.txt`: disk and memory observations.
