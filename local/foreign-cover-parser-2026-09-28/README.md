# Generic foreign cover parser result

## Result

The parser now keeps exact 12(b) title/symbol pairs across label variants, continuation rows,
parallel columns, reversed order, and filing-stated compound symbol cells. The evidence loader can
select an exact ticker component from one unchanged compound cell. Boolean/no-symbol rows are
discarded. Common classes with attached purchase rights or a receipt's right to receive shares
remain common; standalone rights, warrants, notes, ETNs, preferred classes, and debt remain rejected.

Expected result for the 47 inventory parser rows after the manager's bounded rescan:

- 28 clear the cover-identity gate: BBVA, BEP, BSAC, CCJ, CLWT, CNEY, CRESY, DAVA, DOX, DSX,
  EHLD, EMA, FAMI, GGAL, GLBS, ICON, IRS, MOGU, MUFG, PAVS, PSHG, RBNE, SAN, TANH, TORO,
  USAS, USEA, VOXR.
- 2 move to the separate unresolved-ratio gate: IMMP, SUZ.
- 17 remain excluded because the current cover has no exact usable ticker class: AURE, AZN, AZUL,
  DPU, DSGX, EPWKF, FURY, GLAS, GNS, KAZR, LKNCY, MCRP, NA, PLTYF, SGRX, SXTC, ZCMD.

## Bounded rescan and derive set

Use these 47 CIKs for this parser increment:

```text
0000067088 0000842180 0000891478 0000901832 0000909327 0000933267
0001009001 0001026662 0001027552 0001034957 0001050140 0001062579
0001114700 0001127248 0001286973 0001318885 0001432364 0001433309
0001481241 0001499780 0001506184 0001514597 0001533232 0001588084
0001656081 0001701261 0001723980 0001735556 0001743971 0001751876
0001765850 0001767582 0001780785 0001785566 0001847806 0001848731
0001872302 0001900720 0001907909 0001912847 0001938865 0001941131
0001993431 0001995574 0002031009 0002032779 0002039060
```

The accepted inventory's 57 missing-cover CIKs are outside this developer increment. The manager's
queued rescan may add them as a separate set.

## Real SEC evidence

`verify_real_covers.py` fetched fixed current accessions and wrote every parsed row to
`real-cover-output.json`.

Observed exact-class result:

```text
BBVA BBVA common ratio=1
BEP  BEP  common
AZN  none
SUZ  SUZB3/SUZ (exact component SUZ) common ratio unresolved
AURE none
ADAG ADAG common ratio unresolved
HON  HON  common
PPG  PPG  common
GLP  GLP  common
LX   LX   common ratio=2
TM   TM   common ratio unresolved
```

BBVA, BEP, SUZ, and ADAG are affected foreign cases. AURE and AZN are adverse controls. HON, PPG,
GLP, LX, and TM prove common/debt collision and ordering neighbours remain intact.

## Trace and reuse

See `reuse-decision.md`. The existing flow remains: `cover.securities` parses, `store.set_cover`
retains exact classes, `EvidenceLoader.identity` selects the exact priced ticker, and
`normalize._reject_foreign` applies the common-class and receipt-ratio gates. P-02, P-03, P-04,
and P-09 remain unchanged.

## Checks

```text
PYTHONPATH=api api/.venv/bin/pytest -q api/tests/test_cover.py api/tests/test_evidence.py api/tests/test_sync.py
100 passed in 0.25s

PYTHONPATH=api api/.venv/bin/pytest -q api/tests
739 passed, 6 skipped, 1 deprecation warning in 2.88s

api/.venv/bin/python -m compileall -q api/screener api/tests local/foreign-cover-parser-2026-09-28/verify_real_covers.py
passed, no output

git diff --check
passed, no output
```

## Limits

This increment does not parse new receipt-ratio forms, add ticker aliases, change OTC identity,
admit 12(g)-only classes, or fetch the 57 missing-cover rows. It did not run derive, export,
regression, audit, filing audit, or UI-payload gates; those remain manager-owned after review.
