# Developer 15 — review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Engine-185 full payload gate**
Review state: **ACCEPTED — Review 1**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

READY_FOR_REVIEW

Commands and raw results:

- `df -h .`, `vm_stat`: 13 GiB free before the gate. Exact output is in
  `local/foreign-ticker-continuity-e2e-2026-09-28/resources-before.txt`.
- `make regress ARGS='--all --baseline <preserved-engine-184-file>'`: exit 0; 6,979
  recomputed and compared; no field moved.
- `make derive`: exit 0; 7,247/7,247 dashboard-eligible snapshots recomputed for engine 185.
- `make export`: exit 0; 6,758/6,758 verified tickers refreshed; wrote 6,981/6,981 rows.
- `make audit`: exit 0; 279 source values and 3,411 arithmetic checks passed, zero wrong.
- `make audit-filings`: exit 0; the same 279 and 3,411 checks plus 378 published-statement
  checks passed, zero wrong and zero uncheckable.

Baseline: engine 184, SHA-256
`be40ae5261bc4827aeac4685879685e3a5a0950763543d2da62072a70587cce6`, 220,328,826 bytes,
6,979 rows, 6,979 unique tickers, and 6,979 unique CIKs. New payload: engine 185, SHA-256
`d45bfb04044b42fa8221742873be1b4c688d4b722981987052c92f356ab5ce4b`, 220,362,103 bytes,
6,981 rows, 6,981 unique tickers, and 6,981 unique CIKs.

Exact identity delta: added BRNX / `0001901215` and ZTG / `0002011458`; removed none. Across all
6,979 shared rows, only live quote, quote time/session, price statistics, FX,
price-reporting-currency values, quote-derived criteria values, market-cap-to-net-cash, three
defensive valuation results, and two price-history warnings changed. Criteria statuses, row
verdicts, pass counts, financials, annual and historical ratios, profiles, context notes, and source
accessions did not change. Exact field counts and the small note/alignment/warning deltas are in
`local/foreign-ticker-continuity-e2e-2026-09-28/payload-change-summary.txt`.

BRNX retains annual basis accession `0001213900-26-034046` and later current-ticker accession
`0001213900-26-103505`. ZTG retains annual basis accession `0001493152-26-002776` and later
current-ticker accession `0001493152-26-043703`. All statement sources remain on the annual facts.
Each ticker and CIK occurs once. Old symbols BNRG/ZGM and unsupported OTC example ABLZF are absent;
neither target CIK has an OTC row. Retained document hashes match prior accepted evidence.

Limits: filing audits cover the repository's 22-company sample; the recursive payload comparison
covers all 6,979 shared rows. Live quotes moved during export and are named above. The temporary
220 MB baseline was removed only after comparison. Another session did not touch my owned evidence
directory or developer update. A pre-existing modification to
`local/foreign-coverage-plan-2026-09-28.md` remained outside my ownership and was not changed.

Evidence: `local/foreign-ticker-continuity-e2e-2026-09-28/README.md` and its linked raw logs.

## Reviewer response

### Review 1 — accepted

Accepted. The preserved engine-184 baseline had 6,979 unique rows. Full regression recomputed all
6,979 with no field movement. Engine 185 produced 6,981 unique rows: only BRNX and ZTG were added,
with no removals. Shared-row statement data, criteria statuses, verdicts, profiles, notes, and source
accessions were unchanged; named quote/FX-derived values moved during live export. Audits passed
279 sourced, 3,411 arithmetic, and 378 filing checks with zero wrong. Both new rows retain their
annual statement basis and later F-3 ticker evidence; old and OTC aliases are absent.
