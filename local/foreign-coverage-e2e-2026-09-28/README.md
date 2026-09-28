# Foreign coverage end-to-end result

Date: 2026-09-28. Branch: `codex/company-import`. Engine: 184.

## Coverage result

- Original listed `foreign` snapshots: 237.
- Recovered through generic cover parsing: 30.
- Recovered through direct filing-backed depositary ratios: 19.
- Final listed `foreign` snapshots: 188.
- Final reasons: 161 cover/security identity, nine unresolved ratio, 18 incomplete standard
  statements.

The 49 recovered foreign tickers are:

BBVA, BEP, BSAC, CCJ, CDLR, CLWT, CNEY, CRESY, DAVA, DOX, DSX, EHLD, EMA, ERIC,
FAMI, GGAL, GLBS, GMAB, GOTU, HDB, ICON, IMMP, ING, IRS, JG, MOGU, MUFG, NCNA,
NVO, NYXH, PAVS, POM, PSHG, PSNY, RBNE, RELX, RERE, SAN, SNY, SOGP, SSL, SUZ,
SY, TANH, TORO, USAS, USEA, VOXR, WKEY.

Five separate `pending_facts` rows also became complete during the engine rebuild: AMX, EVO, KEP,
NBP, and PKX.

## Evidence retention

- Exact rendered SEC cover reports retained: 266.
- Exact primary documents retained for ratio evidence: 13.
- Cover and primary bytes are cached by immutable accession before parsing.
- PostgreSQL snapshots link matching raw covers as `raw_cover_filing` and matching primary evidence
  as `raw_cover_primary_document`.

## UI payload gates

- Frozen engine-181 payload: 6,925 rows, SHA-256
  `5ee9db25ec027d0c178151d04e778550f5a69320ba41afb32a6b8eb6c4e3fe9f`.
- Engine-184 payload: 6,979 rows, SHA-256
  `be40ae5261bc4827aeac4685879685e3a5a0950763543d2da62072a70587cce6`.
- Added: 54. Removed: 0. Duplicate ticker/CIK: 0.
- Full pre-export regression: 112 intended disclosure/provenance changes only. No criterion,
  verdict, financial value, peer cohort, or baseline recomputation failure changed.
- Audit: 279 sourced and 3,411 arithmetic checks passed; zero wrong.
- Filing audit: 378 published-statement checks passed; zero wrong.

The 112 baseline changes remove 21 stale or wrong cover identities and their context notes, and add
seven explicit evidence gaps. Newly recovered foreign rows were absent from the baseline and enter
as new rows rather than field changes.

## PostgreSQL/API result

- Current UI-eligible companies: 6,979.
- Exact engine-184 PostgreSQL payload matches: 6,705.
- Payload mismatches or stale imported engine selections: 0.
- Withheld: 274: 147 current SEC ticker-map mismatches, 117 pending structured facts, and ten
  unresolved depositary ratios.
- One unrelated `PERSIST.NEW` development row remains outside the UI universe.

After restarting PostgreSQL and SeaweedFS, real Uvicorn requests returned HTTP 200 for NTES, BBVA,
CDLR, SUZ, Panasonic `6752.T`, and mapping-only CATO. Every response used engine 184. BBVA/CDLR/SUZ
included exact retained cover artifacts and filing-backed ratios.

## Checks

- Storage-enabled Python: 786 passed; one existing dependency warning.
- Web: 113 passed.
- Alembic current/head: `20260928_0004`; no pending operation.
- Final whitespace and diff checks passed.

## Remaining product decisions

- Twenty-four changed-symbol rows and 46 stale/OTC mappings need an approved security-identity
  policy. Normalization aliases are not safe.
- Eighteen incomplete Company Facts rows need approved official-source adapters, one source family
  or company at a time.
- Wrong security classes, unregistered classes, conflicting/compound ratios, and rows with no
  filing-backed ratio remain excluded.
