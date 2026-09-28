# Frozen pilot manifest

All source bytes are official SEC files. SHA-256 values below were computed from the inspected
copies on 2026-09-28. Duplicate working copies were removed after inspection and are not repository
source. Implementation reads the retained cache or downloads the named official SEC resource,
requires the recorded hash, calls `store_evidence`, verifies the S3 readback, and records the hash
in the extraction run. A hash mismatch stops the frozen test.

## AERO — inline IFRS, classified balance sheet

- Issuer: SEC CIK `0001561861`.
- Annual filing: `0001193125-26-197494`, Form 20-F, filed 2026-04-30.
- Retained Company Facts: `~/.cache/graham-screener/companyfacts_0001561861.json`,
  `004f852aefe86e99890486a5760c7485a6a5b2c3ad7ca0288607f2be0784766d`.
- SEC filing index: `000119312526197494/index.json`,
  `eedf4cfda3a6a7f923464b3321abd4eb3be4e4154cd1bcd53da113f439449b4c`.
- SEC primary document: `000119312526197494/d101275d20f.htm`,
  `b842756f7ce62b0ae1bd2cf14d6fc3373c61297c0a74934f020493332415326e`.
- Company Facts exposes only the DEI share count for this accession. Missing requested fields:
  `AssetsCurrent`, `Assets`, `LiabilitiesCurrent`, `Liabilities`, `Equity`,
  `EquityAndLiabilities`, and `NetCashProvidedByUsedInOperatingActivities`.
- Gold locations: HTML anchors `ixv-64800`, `ixv-64823`, `ixv-64847`, `ixv-64868`,
  `ixv-64898`, `ixv-64901`, and `ixv-65207`. The audited tables report USD thousands for
  2025-12-31; the cash-flow field covers 2025-01-01 through 2025-12-31.

## CIB — inline IFRS, unclassified bank balance sheet

- Issuer: SEC CIK `0002058897`.
- Annual filing: `0002058897-26-000125`, Form 20-F, filed 2026-04-08.
- Retained Company Facts: `~/.cache/graham-screener/companyfacts_0002058897.json`,
  `c359efed1291e6e9791b295144e5a779daad0e5f627bcc0ad47c8b361a223a3f`.
- SEC filing index: `000205889726000125/index.json`,
  `ae1d8e729b51b2a3479b2c740f0882ad6fcb4961ffd9299b45ff7654a2955c4d`.
- SEC primary document: `000205889726000125/cib-20251231.htm`,
  `f05d92a8c53a451afc40d649fed70f0384d35cc8792f00f2752c9fcd43d7a271`.
- Company Facts exposes only the DEI share count for this accession. Missing requested fields:
  `Assets`, `Liabilities`, `Equity`, `EquityAndLiabilities`, and
  `NetIncomeLossAttributableToOwnersOfParent`. `AssetsCurrent` and `LiabilitiesCurrent` are
  negative neighbour requests because the primary statement is unclassified.
- Gold locations include HTML anchors `f-102`, `f-128`, `f-146`, `f-148`, and `f-249`.
  The audited tables report COP millions for 2025-12-31 and the year then ended.

## CNI — 40-F with financial statements incorporated from an official SEC exhibit

- Issuer: SEC CIK `0000016868`.
- Annual filing: `0001104659-26-010352`, Form 40-F, filed 2026-02-04.
- Retained Company Facts: `~/.cache/graham-screener/companyfacts_0000016868.json`,
  `6ccf0b631dedd75ff5bad530ce5b0e22ed5e864515e2447709366fcb24b8ec40`.
- SEC 40-F index: `000110465926010352/index.json`,
  `50e83ef941db63b3d5020c0d851837cd873dcaedebe220cb4fb22db8d4624c89`.
- SEC 40-F document: `000110465926010352/tm261145d1_40f.htm`,
  `7490bb0e34394e55e1e349e7f3c44581fe3bfe50ee02b368a1c4b938af13ab2f`.
- The 40-F explicitly incorporates audited statements filed as Exhibit 99.2 under SEC accession
  `0000016868-26-000011`.
- SEC exhibit index: `000001686826000011/index.json`,
  `78fc8952f13a716fd9606b5ac2812020fcb6d84da3f58605b518a268868f2778`.
- SEC exhibit document: `000001686826000011/cni-20251231.htm`,
  `a3789c3b3a1a2a1b2ee15a6985c6e757c699c8c2992436ec6036b4e985fbb79d`.
- Company Facts exposes only the DEI share count on the current 40-F accession. Missing requested
  annual fields: `AssetsCurrent`, `Assets`, `LiabilitiesCurrent`, `Liabilities`,
  `LiabilitiesAndStockholdersEquity`, `NetIncomeLoss`, and
  `NetCashProvidedByUsedInOperatingActivities`.
- Gold locations in Exhibit 99.2 are HTML anchors `f-95`, `f-107`, `f-113`, `f-125`, `f-139`,
  `f-46`, and `f-290`. The audited tables report CAD millions for 2025-12-31 and the year then
  ended.

## Frozen-input limit

The primary-document files have not been written through the S3 object store or registered in
PostgreSQL. No model received them. The manifest freezes their identities without requiring
duplicate filing bytes in Git.
