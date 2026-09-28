# Company import end-to-end run

Date: 2026-09-28. Branch: `codex/company-import`.

## Flow

Retained local evidence -> verified content-addressed S3 objects -> canonical derivation ->
immutable PostgreSQL snapshots -> current selection -> real Uvicorn/FastAPI HTTP response.

The command `python -m screener.company_import ABT NTES 6752.T` ran twice against the default
local services. Both runs reused snapshot IDs 8, 9, and 10.

| Ticker | HTTP | Snapshot | Payload SHA-256 | Result |
|---|---:|---:|---|---|
| ABT | 200 | 8 | `3f65756336803204f7c55725494b42437ec0af2e104d4a674796ea381d50170f` | SQLite equal; dashboard fields equal; 3 artifacts |
| NTES | 200 | 9 | `5d6501bf32b5a30a6c08b16f879425351f1a7c4e8cade72650ef0cd9d7b63496` | SQLite equal; dashboard fields equal; 5:1 ADS; 3 artifacts |
| 6752.T | 200 | 10 | `5593ade3e5bad5f63ee630a5dc02c80c81ee09b1534fef269e56ff75cfb4330d` | SQLite equal; dashboard fields equal; JPY ordinary share; 2 artifacts |

NTES retained cover accession `0001104659-26-043468`. Panasonic retained EDINET document
`S100YETA` and raw filing hash
`95cc16e5afa261db468633743e8e46cf0be0b827b6fee5c4d625d87327cbc35f`.

PostgreSQL and SeaweedFS were restarted. Both became healthy. A new Uvicorn process then returned
the same snapshot IDs and hashes for all three companies. The server shut down cleanly afterward.

## Checks

- Real isolated API integration: `1 passed`.
- Focused API/repository tests: `15 passed`.
- Full tests: `715` Python passed, `5` opt-in skips, and `113` web passed.
- Alembic current/head: `20260927_0003`; check found no pending operations.
- Runtime payload: 6,925 companies; full regression reported no moved field.
- Audit: 279 sourced values and 3,411 arithmetic figures passed.
- Filing audit: 378 published-statement values passed. No wrong values.
