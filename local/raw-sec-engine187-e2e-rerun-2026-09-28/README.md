# Engine 187 full gate rerun

**Result: READY_FOR_REVIEW.** The complete engine-187 release gate passed. No production code,
tests, product decisions, prompts, credentials, commits, branches, or stashes changed.

## Baseline and output

- Baseline: engine 185, 6,981 rows, 6,981 unique tickers, 6,981 unique CIKs,
  220,362,103 bytes, SHA-256
  `d45bfb04044b42fa8221742873be1b4c688d4b722981987052c92f356ab5ce4b`.
- New payload: engine 187, 6,995 rows, 6,995 unique tickers, 6,995 unique CIKs,
  220,681,011 bytes, SHA-256
  `358b03211d87e3db8ffca57d9244d6244295225cac6eb3bd250196890d3014a5`.
- Exact identity delta: 14 additions, no removals, no ticker/CIK reassignment, no duplicate ticker
  or CIK.
- Added: AERO, AGMB, ALPS, AUGO, CNI, DAVI, GCDT, GMTL, HBNB, PAYP, PICS, TMCR, VMET,
  YMAT.
- The temporary baseline was removed only after every comparison. Its final recorded identity is
  in `14-final-resources-and-cleanup.log`.

## Bounded acquisition

- First 18-CIK run: four activated and fourteen reused. Activated: CNI, GCDT, NXAT, HBNB.
- Every manifest and retained file hash verified. CNI is `incorporated_annual_exhibit`; the other
  17 are `direct_annual`.
- Every target was dirty or engine-stale before derive.
- Second run: 18 reused. All manifest hashes and dirty timestamps stayed unchanged.

## Added-row provenance

| Ticker | Annual filing | Statement source |
|---|---|---|
| AERO | 20-F `0001193125-26-197494` | same filing |
| AGMB | 20-F `0001104659-26-047872` | same filing |
| ALPS | 20-F `0001493152-26-036364` | same filing |
| AUGO | 20-F/A `0001171843-26-002783` | same filing |
| CNI | 40-F `0001104659-26-010352` | 6-K `0000016868-26-000011`, `cni-20251231.htm` |
| DAVI | 20-F/A `0001683168-26-003530` | same filing |
| GCDT | 20-F `0001493152-26-038530` | same filing |
| GMTL | 20-F `0001104659-26-108373` | same filing |
| HBNB | 20-F `0001213900-26-049771` | same filing |
| PAYP | 20-F `0001193125-26-289382` | same filing |
| PICS | 20-F `0001213900-26-049950` | same filing |
| TMCR | 20-F `0001104659-26-049527` | same filing |
| VMET | 20-F `0001104659-26-052847` | same filing |
| YMAT | 20-F `0001493152-26-019802` | same filing |

All 14 rows passed checks for exact identity, annual date, statement source, criteria numbers,
verdict, financial contract, annual ratios, profiles, notes, and source hashes. UI-derived grade,
market cap, current ratio, P/B, price-to-pass, three-year P/E, and Return Quality were evaluated for
all 14. Honest missing fields remain null; they were not converted to zero.

## Required exclusions

- AHNRF: absent; engine-187 `foreign`; no exact filing-cover security title.
- BRBI: absent; engine-187 `foreign`; no exact filing-cover security title.
- NXAT: absent; engine-187 `foreign`; no exact filing-cover security title.
- CIB: absent; engine-187 `foreign`; no positive filing-backed depositary receipt ratio.

Their statements are retained and verified, but the security gate remains closed. No unsupported
alias or OTC row entered the payload.

## Regression and audits

- Full regress: 6,981/6,981 baseline companies recomputed and compared; no engine-owned field
  moved.
- Derive: 7,247/7,247 dashboard-eligible stale snapshots completed.
- Export: 6,772 verified ticker price/history jobs completed; 6,995 rows written.
- Source audit: 279 correct, 0 wrong.
- Arithmetic audit: 3,411 correct, 0 wrong.
- Filing audit: 378 correct, 0 wrong.

The exact post-export comparison found 56 changed shared-row paths. Every path is classified:

- 24 live quote/time/FX paths and their direct valuation dependants.
- 27 validated market-history paths, including refreshed price statistics and explicit rejected-
  history warnings. BTLN historical price multiples moved with a split-basis warning.
- 5 peer-set paths caused by the 14 additions. Peer counts/medians changed for 168 existing rows;
  all 67 changed context-note lists changed only `Peer efficiency` notes.

There are no unexplained paths.

## Resource and execution notes

- Lowest recorded free disk: 9.3 GiB, above the 1 GiB stop floor. Final free disk: 9.7 GiB.
- Lowest recorded memory-free percentage during named stages: 37%.
- One heavy production command ran at a time. No Screener process remained at cleanup.
- HEAD stayed `846a36b7585a3a102ee9401375a23793883ad4fc` throughout. No other session touched the
  owned evidence or review-map paths.
- `00-baseline-and-resources.log` records one unavailable plain `python` launcher; the repository
  virtual environment immediately produced the required baseline counts.
- `11-targets-and-exclusions.log` used an overly strict local check that required every accepted
  financial field to be non-null. `11b-targets-and-exclusions-corrected.log` applies the product
  contract: required fields and provenance must exist, while disclosed missing evidence remains
  null. The corrected check passed.

## Raw evidence

- `00-baseline-and-resources.log`
- `01-inline-activation.log`
- `02-manifest-and-dirty-verification.log`
- `03-inline-idempotent.log`
- `04-idempotence-verification.log`
- `05-regress.log`
- `06-derive.log`
- `07-export.log`
- `08-audit.log`
- `09-audit-filings.log`
- `10-full-payload-diff.log`
- `11-targets-and-exclusions.log`
- `11b-targets-and-exclusions-corrected.log`
- `12-change-classification.log`
- `13-ui-derived-targets.log`
- `14-final-resources-and-cleanup.log`
