# Later-SEC ticker continuity evidence

BRNX and ZTG now pass the exact continuity contract on retained SEC bytes and reach supported
snapshots. Each later F-3 contains the exact annual class title and explicitly lists that class on
Nasdaq under the current ticker. The annual and later accessions remain separate immutable
observations.

Checks:

```text
PYTHONPATH=api api/.venv/bin/python -m pytest \
  api/tests/test_cover.py api/tests/test_evidence.py \
  api/tests/test_normalize_notes.py api/tests/test_sync.py -q
219 passed in 0.44s

PYTHONPATH=api api/.venv/bin/python \
  local/foreign-ticker-continuity-2026-09-28/verify_real_continuity.py
BRNX: status ok, balance sheet 2025-12-31, verdict FAIL.
ZTG: status ok, balance sheet 2025-09-30, verdict FAIL.
```

The bounded main-store rescan activated BRNX and ZTG and preserved both accessions in
`security_cover_observation`. It did not run full derive, export, regression, or audits; those gates
belong after review.
