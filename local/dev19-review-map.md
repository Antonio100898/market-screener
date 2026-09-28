# Developer 19 — review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Raw SEC deterministic recovery audit**
Review state: **ACCEPTED — Review 1**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

**READY_FOR_REVIEW — guidance revision 1**

Trace: reproduced all 18 `incomplete_company_facts` rows. Each retained Company Facts file has only
1–4 facts from the current annual accession. The official SEC extracted instances contain the
missing statement facts under standard taxonomies. Seventeen are generic Inline-XBRL recoveries:
AERO, AGMB, AHNRF, ALPS, AUGO, BRBI, CIB, DAVI, GCDT, GMTL, HBNB, NXAT, PAYP, PICS, TMCR, VMET,
and YMAT. CNI is one generic incorporated-exhibit recovery: its 40-F explicitly incorporates the
standard US-GAAP facts in 6-K Exhibit 99.2. Extension mapping: 0. Unstructured/LLM candidates: 0.
Unsupported: 0.

Evidence is in `local/raw-sec-statement-recovery-2026-09-28/`: `inventory.json` carries exact
concepts, periods, units/currencies, values, contexts, scales, signs, source IDs, statement roles,
documents, accessions, availability, and reconciliation; `inventory.csv` is the compact complete
inventory; `source-hashes.txt` records 112 inspected official URL/hash/size rows; README explains
the result; `verify_recovery.py` deterministically rebuilds and verifies it from the separate SEC
cache. No SEC document bytes are duplicated in Git.

All 18 selected balances reconcile. Current-asset and current-liability bounds pass wherever those
subtotals exist. BRBI, CIB, PAYP, and PICS correctly remain unclassified. AUGO and DAVI have partial
current splits but coherent standard balances. GMTL reconciles Assets to separately filed
Liabilities plus Equity. Every selected fact ID resolves in the exact primary document. CNI retains
both the incorporating 40-F and source 6-K exhibit identities.

Reuse: preserve standard facts in the existing Company Facts shape; reuse
`normalize._ifrs_as_us_gaap`, `normalize._reject_foreign`, `EvidenceLoader.load`,
`sync._derive_evidence`, `store_evidence`, `ImmutableObjectStore.put_verified`, and
`CompanyImporter._retain`. `dera.harvest` remains the SEC quarterly-data-set owner. EDINET and IFRS
workbook adapters are contract references, not a second SEC normalizer. CNI needs both source and
incorporating accessions in per-figure provenance; the current single accession field may need a
narrow extension.

Checks: `python3 local/raw-sec-statement-recovery-2026-09-28/verify_recovery.py` passed with 18/18,
category totals 17 + 1, all anchors resolved, and all balance equations reconciled. Python syntax,
JSON/CSV counts, unique tickers, category totals, and trailing whitespace passed. `git diff --check`
reported no tracked-file errors; the explicit whitespace check covered the new untracked evidence
files. No model/provider call or production edit was made.

Evidence gap: this audit proves the current 18 filing revisions only. It does not prove security
identity, ADS ratios, FX, full ten-year coverage, or end-to-end dashboard admission after merging
the facts.

Recommended next increment: implement one generic SEC Inline-XBRL supplement before LLM fallback.
Run it only after current annual Company Facts fails, follow only explicit SEC exhibit links, retain
all source files immutably, accept only standard undimensioned primary-statement facts that
reconcile, merge missing facts without overrides, then run the full 18-company derivation and UI
regression gates.

## Reviewer response

### Review 1 — accepted

Accepted. The reviewer independently rebuilt the 18-row inventory and observed 17 standard inline-
XBRL recoveries plus one explicitly incorporated SEC exhibit, with zero extension, LLM, or
unsupported rows. Every cited source anchor resolves, all selected facts are standard and
undimensioned, all annual durations have the expected fiscal span, and all balance equations
reconcile. This proves the current 18 revisions only; end-to-end admission remains for the build
increment.
