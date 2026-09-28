# PostgreSQL company API developer review map

Developer state: **IDLE — accepted after Review 2**
Current item: **Selected company HTTP read**
Review state: **ACCEPTED — Review 2**

The developer writes only under "Developer update". The reviewer writes the header and
"Reviewer response".

## Developer update

Developer state: **READY_FOR_REVIEW — guidance revision 1**

### Flow and decisions

P-02, P-03, P-04, P-09, and P-17 remain intact. `GET /companies/{ticker}` calls a lazily created
`SharedCompanyRepository`. The repository performs a case-insensitive exact match only across
`current_company_snapshot`, joins the immutable snapshot to its stable security and issuer, and
reads that snapshot's exact artifact-role/hash links on the same connection. Historical tickers
do not match after the current selection moves. No match returns 404; two current securities with
the same ticker return an explicit 500 integrity response; SQLAlchemy/PostgreSQL failures return
503. No branch calls SQLite, source clients, normalization, derivation, or the old detail route.

The response keeps `canonical_payload` unchanged and adds separate immutable revision metadata,
issuer source/identifier, stable security identifier, ticker/exchange/currency, cover
title/accession, security basis, exact receipt ratio as a normalized decimal string or null, and
the exact artifact pairs.

### Reuse decision

- `SharedCompanyRepository` remains the PostgreSQL selection owner; the new lookup extends it.
- `create_postgres_engine()` and `StorageSettings` remain the configured engine owners.
- `screener.api:app` remains the only FastAPI application and existing dict serialization style is
  reused.
- The cached repository factory runs only on the first company request. Importing `screener.api`
  with an invalid database URL succeeds, proving import does not create or validate PostgreSQL.
- No financial, evidence, selection, or old-runtime behavior was copied.

### Changed files

- `api/screener/shared_companies.py`: selected-company contract and exact current-ticker lookup.
- `api/screener/api.py`: lazy repository, `GET /companies/{ticker}`, response serializer, and
  explicit integrity/unavailability responses.
- `api/tests/test_shared_company_api.py`: lookup, moved ticker, ambiguity, 404, 503, exact decimal,
  artifacts, no fallback, and lazy import tests.
- `api/tests/integration/test_shared_company_api_storage.py`: opt-in real S3/PostgreSQL import and
  FastAPI parity test for ABT, NTES, and `6752.T`, including SQLite/dashboard parity and exact
  milestone-2 evidence hashes.
- `local/dev6-review-map.md`: this return.

### Completed checks and raw output

Focused API plus existing repository contract:

```text
$ api/.venv/bin/python -m pytest -q api/tests/test_shared_company_api.py api/tests/test_shared_companies.py
...............                                                          [100%]
15 passed, 1 warning in 0.63s
```

The warning is the existing FastAPI/Starlette `httpx` deprecation.

Full normal suite:

```text
$ make test
715 passed, 5 skipped, 1 warning in 2.16s
tests 113; pass 113; fail 0
```

The five skips are opt-in storage tests without `RUN_STORAGE_INTEGRATION=1`.

Syntax, migration-head, and whitespace checks:

```text
$ api/.venv/bin/python -m compileall -q api/screener/shared_companies.py api/screener/api.py api/tests/test_shared_company_api.py api/tests/integration/test_shared_company_api_storage.py
[no output; exit 0]
$ cd api && .venv/bin/alembic -c alembic.ini heads
20260927_0003 (head)
$ git diff --check
[no output; exit 0]
```

### Real PostgreSQL/S3 HTTP evidence

After the owner-approved OrbStack restart, the isolated real check passed from an empty migrated
database through retained evidence, real S3, PostgreSQL, the lazy repository, and FastAPI
`TestClient`:

```text
$ cd api && RUN_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest -q -s tests/integration/test_shared_company_api_storage.py
Running upgrade  -> 20260925_0001, Create immutable evidence artifacts.
Running upgrade 20260925_0001 -> 20260927_0002, Store immutable company snapshots and their current selection.
Running upgrade 20260927_0002 -> 20260927_0003, Bind security evidence and artifact sets to immutable snapshots.
1 passed, 1 warning in 3.02s
```

All three lowercase HTTP requests returned 200. Each `canonical_payload` exactly equalled the
current SQLite payload and every shared non-export-enriched field equalled the rebuilt
`dashboard.json` row.

Real response pins:

```text
ABT
  status 200; issuer SEC:0000001800; security sec:0000001800:primary-listed-security
  snapshot 21dde22f1f89c1c6a354e471e0bbdbf2d803da3430c3a81307dd4e2b4905a6cc
  payload  3f65756336803204f7c55725494b42437ec0af2e104d4a674796ea381d50170f

NTES
  status 200; accession 0001104659-26-043468; receipt ratio "5"
  snapshot 7b0a5a4a37f593382216ea15dc24526cc562875f0f6a1a80da1586a3c9df15f5
  official_api_facts         03ed93bf459aaa2aa101c99a9c39e89213e80f6d13a908c8fc72f7c4ac6b755c
  dimensioned_facts          af730296ec376df2b356ab8b13a0f5c444db54b690789991cc9dda3219be1541
  structured_cover_identity  6fe18bb3506dacb7907095d00967143d913ddcd39645b78b92407edaf0565152

6752.T
  status 200; currency JPY; basis PRIMARY_ORDINARY_SHARE; receipt ratio null
  snapshot 8e9330adb67e0c19818e60a8cccc75599301f2cffc8527d08d861c82a50a0ce7
  raw_filing 95cc16e5afa261db468633743e8e46cf0be0b827b6fee5c4d625d87327cbc35f
```

Final migration checks against the running PostgreSQL service:

```text
$ cd api && .venv/bin/alembic -c alembic.ini current
20260927_0003 (head)
$ cd api && .venv/bin/alembic -c alembic.ini heads
20260927_0003 (head)
$ cd api && .venv/bin/alembic -c alembic.ini check
No new upgrade operations detected.
```

The integration test dropped its isolated database; querying `pg_database` afterward returned
`[]` for `ms_company_api_%`. No retained SQLite/cache file or source input was changed. This new
PostgreSQL-only endpoint does not alter the engine, extraction, pricing, profile, existing export
serializer, SQLite data, or UI payload; its real test instead proves exact current SQLite payload
and rebuilt-dashboard canonical-field parity. The reviewer updated this review map's header and
Review 1 response while the increment ran; no other session touched an owned source or test file.
No commit, stash, branch switch, push, source fetch, or full-universe import was made.

**READY_FOR_REVIEW**

## Reviewer response

### Review 1 — real evidence blocked

Tree review accepted the selected-snapshot join, exact ticker handling, lazy engine creation,
decimal-string ratio, artifact serialization, 404/500/503 behavior, and absence of SQLite/source
fallback. The reviewer independently observed `15 passed` in the focused repository/API suite and
clean whitespace. Milestone acceptance is blocked because Docker control calls time out, port
55432 accepts TCP without completing a PostgreSQL handshake, and the real three-company HTTP test
has no result. Do not claim HTTP parity until the local PostgreSQL/S3 services recover and the
named integration test passes.

### Review 2 — accepted

Accepted after the owner-approved OrbStack restart. The developer and reviewer independently ran
the real isolated PostgreSQL/S3/FastAPI test; both observed `1 passed`. ABT, NTES, and `6752.T`
returned HTTP 200, exact SQLite payload equality, and rebuilt-dashboard canonical-field equality.
NTES retained accession `0001104659-26-043468`, ratio `"5"`, and its three exact artifact hashes;
Panasonic retained JPY, ordinary-share basis, and its raw filing hash. Independent final checks
also observed 15 focused tests, 715 Python tests, 113 web tests, Alembic at `20260927_0003` with no
pending operations, and clean whitespace. No commit was made.
