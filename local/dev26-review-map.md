# Developer 26 — review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Engine-187 full gate rerun**
Review state: **ACCEPTED — Review 1**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

**READY_FOR_REVIEW — engine-187 full gate passed.**

- Preserved baseline matched exactly: engine 185, 6,981 rows, 6,981 unique tickers/CIKs,
  220,362,103 bytes, SHA-256
  `d45bfb04044b42fa8221742873be1b4c688d4b722981987052c92f356ab5ce4b`.
- First bounded inline run returned four `activated` (CNI, GCDT, NXAT, HBNB) and fourteen
  `reused`. All 18 manifests and retained-file hashes verified; CNI is incorporated and the other
  17 are direct. Every row was dirty or engine-stale.
- Second inline run returned 18/18 `reused`; manifest hashes and dirty timestamps were unchanged.
- Full regress recomputed and compared 6,981/6,981 baseline companies: no engine-owned field
  moved.
- `make derive` completed 7,247/7,247. `make export` completed 6,772 price/history jobs and wrote
  engine 187 with 6,995 rows, SHA-256
  `358b03211d87e3db8ffca57d9244d6244295225cac6eb3bd250196890d3014a5`.
- Exact identity delta is the expected 14 additions: AERO, AGMB, ALPS, AUGO, CNI, DAVI, GCDT,
  GMTL, HBNB, PAYP, PICS, TMCR, VMET, YMAT. No removals, reassignment, duplicate ticker, duplicate
  CIK, unsupported alias, or OTC row entered.
- All 14 additions passed annual/source provenance, criteria/verdict, financial contract, annual
  ratios, profiles, notes, source hashes, and UI-derived-value checks. CNI exposes 6-K
  `0000016868-26-000011` / `cni-20251231.htm` plus annual 40-F
  `0001104659-26-010352`.
- AHNRF, BRBI, and NXAT remain engine-187 `foreign` for no exact filing-cover title. CIB remains
  `foreign` for no positive filing-backed depositary ratio. All four are absent from the payload.
- Exact common-row comparison found 56 changed paths, all explained: 24 live quote/time/FX and
  direct dependants; 27 validated price-history paths; 5 peer-set paths caused by the 14 additions.
  The 67 changed context-note lists changed only Peer efficiency notes. No unexplained path.
- Audits: 279 source + 3,411 arithmetic + 378 filing checks, zero wrong.
- Lowest recorded disk was 9.3 GiB; final was 9.7 GiB. Lowest recorded stage memory-free value was
  37%. No Screener process remained. The temporary baseline was removed after its final hash was
  recorded.
- HEAD remained `846a36b7585a3a102ee9401375a23793883ad4fc`. No other session touched owned paths.
  No code, tests, decisions, prompts, credentials, commit, stash, or branch changed.

Complete evidence: `local/raw-sec-engine187-e2e-rerun-2026-09-28/README.md`.

## Reviewer response

### Review 1 — accepted

Accepted. The reviewer independently confirmed the engine-187 payload hash, 6,995 unique ticker/CIK
rows, exact 14 additions, four required exclusions, and CNI's 6-K source plus 40-F annual identity.
Full regression reported no engine-owned movement across all 6,981 baseline rows. Audits reported
279 sourced, 3,411 arithmetic, and 378 filing checks with zero wrong. All shared-row movements are
classified as live quote/FX/history or expected peer-set effects.
