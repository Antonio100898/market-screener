# Retained-evidence import developer review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Restartable company import**
Review state: **ACCEPTED — Review 1**

The developer writes only under "Developer update". The reviewer writes the header and
"Reviewer response".

## Developer update

Developer state: **READY_FOR_REVIEW — guidance revision 2**

### Flow and decisions

P-02, P-03, P-04, P-09, and P-17 remain intact. The CLI requires one or more named tickers and
opens the accepted SQLite store read-only. For each ticker it resolves the current issuer,
security, exchange, and retained cover or EDINET adapter metadata. Stable security identifiers use
the issuer identity, never the ticker:

- `sec:<CIK>:primary-listed-security`
- `edinet:<code>:primary-ordinary`

SEC imports retain exact cached Company Facts, an optional dimensioned sidecar, and the structured
cover identity returned by the existing SQLite evidence loader. EDINET imports retain exact
canonical facts and every raw ZIP named by its report manifest. Each byte string goes through
`store_evidence`, then `ImmutableObjectStore.read_verified`; JSON and ZIP parsing happen only after
that readback. The honest artifact roles are `official_api_facts`, `dimensioned_facts`,
`structured_cover_identity`, `canonical_edinet_facts`, and `raw_filing`.

Verified bytes reconstruct one `EvidenceBundle`. The importer calls only `sync._derive_evidence`
and rejects every status except `ok`. It then calls
`SharedCompanyRepository.store_and_select`, whose existing transaction commits one company and
its current selection. An interrupted batch therefore leaves completed companies committed;
rerunning the same CLI reuses content hashes and evidence-bound snapshot identities.

### Reuse decision

- `evidence.EvidenceLoader.identity` remains the SQLite security-identity owner.
- `artifacts.store_evidence` and `ImmutableObjectStore.read_verified` remain the only evidence
  storage and verification path.
- `sync._derive_evidence` remains the only normalization, calculation, provenance, and payload
  serializer path.
- `SharedCompanyRepository.store_and_select` remains the PostgreSQL snapshot/selection owner.
- `store.ENGINE_VERSION` remains the engine revision owner.

No financial, provenance, serialization, object-storage, or PostgreSQL write rule was copied.

### Changed files

- `api/screener/company_import.py`: named-ticker retained-evidence importer and CLI.
- `api/tests/test_company_import.py`: SEC, ADS, EDINET, missing input, failed derivation, identical
  retry, and interrupted-batch cases.
- `api/tests/integration/test_company_import_storage.py`: real read-only SQLite, S3, isolated
  migrated PostgreSQL, CLI retry, exact readback, identity, and payload parity.
- `local/dev5-review-map.md`: this return.

### Checks and raw results

Focused unit contract:

```text
$ api/.venv/bin/python -m pytest -q api/tests/test_company_import.py
.......                                                                  [100%]
7 passed in 0.56s
```

Real retained-evidence integration used the current ABT, NTES, and `6752.T` files, the real local
S3 service, and a new PostgreSQL database upgraded from empty to `20260927_0003`. It imported ABT
alone, reran all three through the CLI, then repeated the same CLI import. Exact artifact bytes were
read back and compared with their retained SQLite/cache inputs. Every selected PostgreSQL payload
equalled the current SQLite snapshot after canonical JSON normalization.

```text
$ cd api && RUN_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest -q -s tests/integration/test_company_import_storage.py
1 passed in 6.82s
```

Stable final counts were `(artifacts, snapshots, links, current selections, issuers, securities)`:

```text
(8, 3, 8, 3, 3, 3)
```

Real identities and hashes:

```text
ABT snapshot 1
  snapshot 21dde22f1f89c1c6a354e471e0bbdbf2d803da3430c3a81307dd4e2b4905a6cc
  payload  3f65756336803204f7c55725494b42437ec0af2e104d4a674796ea381d50170f
  official_api_facts         5d751f73769f5c0a1f9bee0de16cb39d8961cf3f1ebb5558d555bb2678afb4ff
  dimensioned_facts          db9c96427a23143e38bdcbefeda0d4abccffb782a3c140f2e3ce9efa283f1107
  structured_cover_identity  0968c9f6024ebe71ff192ce99eeadc7b1cc7e1c0ed0b82fd0454ebb36f4cb49b

NTES snapshot 3
  snapshot 7b0a5a4a37f593382216ea15dc24526cc562875f0f6a1a80da1586a3c9df15f5
  payload  5d6501bf32b5a30a6c08b16f879425351f1a7c4e8cade72650ef0cd9d7b63496
  official_api_facts         03ed93bf459aaa2aa101c99a9c39e89213e80f6d13a908c8fc72f7c4ac6b755c
  dimensioned_facts          af730296ec376df2b356ab8b13a0f5c444db54b690789991cc9dda3219be1541
  structured_cover_identity  6fe18bb3506dacb7907095d00967143d913ddcd39645b78b92407edaf0565152

6752.T snapshot 4
  snapshot 8e9330adb67e0c19818e60a8cccc75599301f2cffc8527d08d861c82a50a0ce7
  payload  5593ade3e5bad5f63ee630a5dc02c80c81ee09b1534fef269e56ff75cfb4330d
  canonical_edinet_facts     c3aa914176544f50d19f3c0ffb16fab67f6e6170d9735939ddd9af3622e11ac4
  raw_filing                 95cc16e5afa261db468633743e8e46cf0be0b827b6fee5c4d625d87327cbc35f
```

The real selected NTES snapshot kept accession `0001104659-26-043468` and exact PostgreSQL
`NUMERIC` ratio `5`. Panasonic kept `JPY` quote currency and `PRIMARY_ORDINARY_SHARE` basis.

Full Python regression after the final CLI integration check:

```text
$ cd api && .venv/bin/python -m pytest -q
707 passed, 4 skipped, 1 warning in 4.54s
```

The four skips are the opt-in storage tests without `RUN_STORAGE_INTEGRATION=1`. The warning is the
existing FastAPI/Starlette `httpx` deprecation.

Normal repository suite:

```text
$ make test
707 passed, 4 skipped, 1 warning in 4.41s
tests 113; pass 113; fail 0
```

Alembic and whitespace checks:

```text
$ cd api && .venv/bin/alembic -c alembic.ini current
20260927_0003 (head)
$ cd api && .venv/bin/alembic -c alembic.ini heads
20260927_0003 (head)
$ cd api && .venv/bin/alembic -c alembic.ini check
No new upgrade operations detected.
$ git diff --check
[no output; exit 0]
```

CLI discovery also passed: `python -m screener.company_import --help` requires
`tickers [tickers ...]`; there is no full-universe default.

### Known limits and concurrent edits

- This is the bounded named-company importer. Full-universe import, automatic refresh, and the
  FastAPI PostgreSQL read path remain queued.
- SEC cover artifacts are labelled structured derived identity. Raw cover HTML was not retained or
  claimed. EDINET ZIPs are the retained raw filings.
- The isolated PostgreSQL test database was dropped. The eight content-addressed test objects stay
  reusable in local S3; no retained SQLite/cache input was changed.
- UI payload regression and filing-audit gates were not rerun because this adds a new PostgreSQL
  import caller without changing the engine, extraction, existing SQLite snapshots, UI payload,
  or current application reader. Exact three-company SQLite payload parity was proven instead.
- The reviewer changed the guidance and review-map header to revision 2 to authorize the required
  integration-test rename. No other session touched an owned source or test file while this
  increment ran.

No commit, stash, branch switch, push, source fetch, or full-universe import was made.

**READY_FOR_REVIEW**

## Reviewer response

### Review 1 — accepted

Accepted. Tree review confirmed source-network-free named imports, verified S3 readback before
parsing, one call to the existing derivation owner, stable issuer-based security identity, exact
artifact roles, and per-company restart behavior. The reviewer independently observed `7 passed`
in focused tests, `1 passed` in the real three-company S3/PostgreSQL test, stable counts of 8
artifacts/3 snapshots/8 links/3 selections, exact SQLite payload equality, NTES accession and 5:1
ratio, Panasonic JPY/ordinary-share basis, Alembic head/check, `707 passed, 4 skipped` in Python,
and 113 passing web tests. The full 6,925-company runtime regression also reported no moved field.
No commit was made.
