# Foreign depositary ratio bounded reparse

The pre-run SQLite backup is
`~/.codex/baselines/market-screener/pre-foreign-ratio-rescan-2026-09-28/screener.db`,
SHA-256 `3c90ca1cb6f6bd3a6a8d1da310c266d9231dc5afe3ea96872ffb114b26c05892`.

The accepted 28-CIK set was reparsed from retained/fetched official R reports and primary
documents. Thirteen exact primary documents are retained under the accession cache, totaling
109,529,055 bytes.

Targeted engine-183 derivation result:

- 19 became `ok`.
- Nine remained `foreign` because evidence conflicts, uses compound/chained instruments, names a
  preferred class, or has no unique current ratio.
- Listed foreign reasons immediately after the targeted run: 161 cover identity, nine ratio, and
  18 statement coherence.

Recovered: CDLR, ERIC, GMAB, GOTU, HDB, ING, JG, NCNA, NVO, POM, PSNY, RELX,
RERE, SNY, SOGP, SSL, SUZ, SY, WKEY.

Full regression then found a separate attached-purchase-rights classification regression affecting
CTRM, CVE, and SOBO. Engine 183 is therefore not release-ready; the fix is tracked as milestone 2C.
