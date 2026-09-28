# Raw SEC statement recovery audit

## Finding

All 18 `incomplete_company_facts` rows are recoverable without an LLM.

- 17 filings expose the needed facts directly as standard IFRS Inline XBRL.
- CNI's 40-F explicitly incorporates Exhibit 99.2 from an official SEC-filed 6-K. That exhibit
  exposes standard US-GAAP Inline XBRL.
- No row needs an extension-tag mapping, unstructured extraction, or company-specific exception.
- Every selected balance reconciles. Every cited fact ID resolves in the exact primary document.

The failure is upstream of normalization: SEC Company Facts omitted current-accession statement
facts that the filed Inline XBRL instance contains.

## Classification

| Category | Count | Tickers |
|---|---:|---|
| Generic standard Inline XBRL | 17 | AERO, AGMB, AHNRF, ALPS, AUGO, BRBI, CIB, DAVI, GCDT, GMTL, HBNB, NXAT, PAYP, PICS, TMCR, VMET, YMAT |
| Generic incorporated SEC exhibit | 1 | CNI |
| Extension mapping | 0 | — |
| Unstructured LLM candidate | 0 | — |
| Unsupported | 0 | — |

The prior frozen LLM cases remain useful parser fixtures, but none represents a real LLM fallback:

- AERO: all eight checked anchors are standard IFRS facts in the current 20-F.
- CIB: standard IFRS facts are present. `AssetsCurrent` and `LiabilitiesCurrent` are correctly absent
  because the bank presents an unclassified balance sheet.
- CNI: all eight checked anchors are standard US-GAAP facts in the incorporated SEC Exhibit 99.2.

## Per-company statement result

| Ticker | Basis | Currency | Current split | Balance check | Missing checked anchors |
|---|---|---|---|---|---|
| AERO | IFRS | USD | classified | Assets = equity and liabilities | none |
| AGMB | IFRS | EUR | classified | Assets = equity and liabilities | none |
| AHNRF | IFRS | USD | classified | Assets = equity and liabilities | none |
| ALPS | IFRS | USD | classified | Assets = equity and liabilities | none |
| AUGO | IFRS | USD | partial | Assets = equity and liabilities | liabilities; operating cash flow |
| BRBI | IFRS | BRL | unclassified | Assets = equity and liabilities | current assets; current liabilities |
| CIB | IFRS | COP | unclassified | Assets = equity and liabilities | current assets; current liabilities |
| CNI | US-GAAP | CAD | classified | Assets = liabilities and equity | none |
| DAVI | IFRS | EUR | partial | Assets = equity and liabilities | current liabilities |
| GCDT | IFRS | HKD | classified | Assets = equity and liabilities | none |
| GMTL | IFRS | USD | classified | Assets = liabilities + equity | combined equity-and-liabilities total |
| HBNB | IFRS | USD | classified | Assets = equity and liabilities | none |
| NXAT | IFRS | KRW | classified | Assets = equity and liabilities | none |
| PAYP | IFRS | JPY | unclassified | Assets = equity and liabilities | current assets; current liabilities |
| PICS | IFRS | BRL | unclassified | Assets = equity and liabilities | current assets; current liabilities |
| TMCR | IFRS | USD | classified | Assets = equity and liabilities | none |
| VMET | IFRS | USD | classified | Assets = equity and liabilities | none |
| YMAT | IFRS | USD | classified | Assets = equity and liabilities | none |

Missing checked anchors do not make the source unstructured. AUGO and DAVI still carry coherent
standard IFRS balances. GMTL's separately reported Assets, Liabilities, and Equity reconcile
exactly. The four unclassified financial companies must keep current subtotals missing.

Exact concepts, periods, values, units, currencies, contexts, scales, signs, fact IDs, statement
roles, accessions, document hashes, and local-availability checks are in `inventory.json`.

## Source and scope checks

- All selected contexts identify the issuer and have no dimensions.
- All selected instant facts end on the annual report date.
- All selected duration facts end on the annual report date.
- Facts come only from standard `ifrs-full` or `us-gaap` namespaces.
- Inline `scale` and `sign` attributes are preserved beside the SEC-transformed instance value.
- Current assets never exceed assets. Current liabilities never exceed liabilities where both are
  reported.
- CNI's current 40-F wrapper and the incorporated 6-K Exhibit 99.2 are both hashed and recorded.
- Company Facts is retained for all 18. DERA sidecars exist for AHNRF, ALPS, and CNI.

## Reproduction

Official SEC files are kept outside Git at:

`~/.cache/graham-screener/raw-sec-statement-recovery/<accession-without-dashes>/`

Run:

```sh
python3 local/raw-sec-statement-recovery-2026-09-28/verify_recovery.py
```

Expected output:

```json
{"all_balance_equations_reconcile": true, "all_source_anchors_resolved": true, "categories": {"extension_mapping": 0, "generic_incorporated_exhibit": 1, "generic_inline_xbrl": 17, "unstructured_llm_candidate": 0, "unsupported": 0}, "companies": 18}
```

`source-hashes.txt` records every inspected official URL, byte count, and SHA-256. No SEC document
bytes are duplicated in Git.

## Next increment

Implement a generic SEC Inline XBRL supplement before any LLM fallback. It should run only when
Company Facts lacks the current annual statement, retain the official filing files, follow only an
explicit SEC incorporation link, accept standard taxonomy facts from undimensioned primary
statement contexts, reconcile the balance, and emit the existing fact contract. Keep missing
current subtotals missing.

After that increment, rerun the full 18-company derivation and the mandatory dashboard regression
gates. An LLM fallback should receive only filings that still lack a verified machine-readable
statement after this path.
