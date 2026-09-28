# Incorporated SEC exhibit verification

CNI now derives through the normal `EvidenceLoader` and `_derive_evidence` path.
The current annual basis is the 40-F. Every recovered figure opens the exact 6-K
exhibit. The balance reconciles:

`58.555b assets = 36.987b liabilities + 21.568b common equity`.

The verifier reads the accepted retained cache in place and creates only a
temporary manifest and database. This directory contains no copied SEC bytes.

Run:

```sh
PYTHONPATH=api api/.venv/bin/python \
  local/raw-sec-incorporated-exhibit-2026-09-28/verify_cni.py
```

Machine-readable evidence is in `results.json`.
