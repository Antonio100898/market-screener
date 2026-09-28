# Foreign cover bounded reparse

The pre-run SQLite backup is
`~/.codex/baselines/market-screener/pre-foreign-cover-rescan-2026-09-28/screener.db`,
SHA-256 `f797c3bcbdeabca8fe870f77d1f0eda2da3dd049752f3b659bce73ef4ab482c1`.

The accepted 47 parser CIKs and 57 missing-cover CIKs produced a 104-CIK set. Four listed preferred
tickers were excluded by the existing common-security scope, so `cover_pages` processed 100.

Results:

- 226 exact R-report files retained under `~/.cache/graham-screener/covers/`.
- Retained bytes: 15,471,446.
- 30 targeted snapshots now derive `ok` at engine 182.
- 70 processed rows remain `foreign`; four preferred rows were untouched.
- Total listed `foreign` count fell from 237 to 207.
- Current reason counts: 161 cover identity, 28 unresolved ratio, 18 statement coherence.

Recovered tickers:

BBVA, BEP, BSAC, CCJ, CLWT, CNEY, CRESY, DAVA, DOX, DSX, EHLD, EMA, FAMI, GGAL,
GLBS, ICON, IMMP, IRS, MOGU, MUFG, NYXH, PAVS, PSHG, RBNE, SAN, TANH, TORO,
USAS, USEA, VOXR.

No full derive, export, payload regression, audit, or filing audit has run yet. Those gates wait for
the direct-ratio increment so the UI payload is rebuilt once.
