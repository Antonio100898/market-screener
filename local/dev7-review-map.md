# Full-universe import contract review map

Developer state: **IDLE — accepted after Review 3**
Current item: **Explicit bulk import contract**
Review state: **ACCEPTED — Review 3**

The developer writes only under "Developer update". The reviewer writes the header and
"Reviewer response".

## Developer update

Developer state: **READY_FOR_REVIEW — guidance revision 3, Review 2 correction**

### Review 2 correction

The verified ticker map now owns only PostgreSQL security metadata for an incomplete cover. The
original incomplete receipt remains in `EvidenceBundle`, so `sync._derive_evidence` keeps the exact
canonical payload and provenance that SQLite stored. The same derivation call remains the support
gate: an incomplete current foreign receipt still produces `foreign` and cannot be stored.

Unit pins prove the exact incomplete receipt reaches derivation and the stored canonical payload,
while PostgreSQL receives null title/accession/ratio with `SEC_TICKER_MAPPING_ONLY`. The real
four-company test now uses CATO, pins its exact receipt object, SQLite payload hash, snapshot hash,
and all three artifact hashes, and still pins the unchanged ABT, NTES, and `6752.T` hashes.

### Review 1 correction

An SEC cover row with an empty title or accession now enters the same verified
`official_sec_ticker_mapping` fallback as an absent or rejected cover for PostgreSQL metadata. The
importer stores null cover fields with `SEC_TICKER_MAPPING_ONLY` and still requires an exact
CIK/ticker match in verified mapping bytes.
A complete depositary title with no ratio does not fall back and remains a visible failure.

Added unit cases cover empty title and empty accession independently, both with matching and
mismatching ticker maps, plus a complete ADS cover with a missing ratio. No schema, migration,
storage, EDINET, cover-backed success, bulk-selection, or summary contract changed in Review 1.

### Decisions, flow, and reuse

P-02, P-03, P-04, P-09, P-17, and the foreign-filer invariant remain intact.

- `store.dashboard_ciks` remains the sole bulk-eligibility owner. The importer maps those CIKs
  back to retained tickers and sorts them case-insensitively before work starts.
- `EvidenceLoader.identity` remains the cover-identity owner. A usable cover follows the accepted
  path unchanged. An SEC row without a usable cover now retains the cached official
  `company_tickers.json`, verifies the S3 readback, and requires an exact CIK/ticker pair in those
  verified bytes. An absent cover derives with `receipt=None`; an incomplete retained cover stays
  in the derivation bundle for payload parity while PostgreSQL metadata remains null.
- `store_evidence` and `ImmutableObjectStore.read_verified` remain the evidence boundary.
  The new artifact role is `official_sec_ticker_mapping`.
- `sync._derive_evidence` remains the only financial derivation path. Only `ok` can reach
  PostgreSQL. A current foreign filer without cover evidence still derives `foreign` and is not
  stored.
- `SharedCompanyRepository.store_and_select` remains the per-company transaction owner. Revision
  4 allows null title/accession only; non-null values still have exact non-empty database checks.
  A successful conflict-safe `RETURNING` distinguishes a new snapshot from a reused one.
- `--all-dashboard` is the only bulk CLI mode. No arguments and combining the flag with named
  tickers both exit 2. Named-ticker behavior remains fail-fast. Bulk mode continues after ordinary
  per-company exceptions, emits one stable JSON summary with imported, reused, and failed tickers
  plus exact reasons, and exits 1 when any ticker failed.

This is the correct-from-zero shape: SQLite decides eligibility; retained verified evidence builds
one bundle; the existing derivation owner decides support; one PostgreSQL transaction stores and
selects each successful company; the coordinator reports the independent outcomes.

### Changed files

- `api/screener/postgres.py`: nullable cover fields and matching non-empty checks.
- `api/screener/shared_companies.py`: optional cover types and conflict-safe created/reused result.
- `api/screener/company_import.py`: verified ticker-map fallback, bulk selection, summary, and CLI.
- `api/migrations/versions/20260928_0004_optional_security_evidence.py`: forward-only nullable
  evidence revision.
- `api/tests/test_shared_companies.py`, `api/tests/test_company_import.py`: repository, fallback,
  exact map, foreign rejection, eligibility/order/failure/retry/summary, and CLI contracts.
- `api/tests/integration/test_shared_company_storage.py`: empty, revision-2, and populated
  revision-3 upgrades to head, null readback, and PostgreSQL constraints.
- `api/tests/integration/test_company_import_storage.py`: real four-company import and unchanged
  accepted hashes.
- `local/dev7-review-map.md`: this return only.

### Checks and raw results

Focused unit contract:

```text
$ cd api && .venv/bin/python -m pytest -q tests/test_company_import.py tests/test_shared_companies.py
.............................                                            [100%]
29 passed in 0.39s
```

Real migration and four-company import:

```text
$ cd api && RUN_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest -q \
    tests/integration/test_shared_company_storage.py \
    tests/integration/test_company_import_storage.py
....                                                                     [100%]
4 passed in 3.67s
```

All real PostgreSQL/S3 integration tests:

```text
$ cd api && RUN_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest -q tests/integration
......                                                                   [100%]
6 passed, 1 warning in 6.21s
```

Complete Python suite with real storage enabled:

```text
$ cd api && RUN_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest -q
........................................................................ [100%]
736 passed, 1 warning in 8.74s
```

The warning is the existing FastAPI/Starlette `httpx` deprecation.

Web suite after the Review 1 correction:

```text
$ cd web && npm test --silent
tests 113; pass 113; fail 0; duration_ms 520.198583
```

CLI discovery:

```text
$ cd api && .venv/bin/python -m screener.company_import
exit 2: provide named tickers or --all-dashboard
$ cd api && .venv/bin/python -m screener.company_import --help
--all-dashboard  import every SQLite dashboard-eligible company
```

Final migration state:

```text
$ cd api && .venv/bin/alembic -c alembic.ini upgrade head
Running upgrade 20260927_0003 -> 20260928_0004
$ cd api && .venv/bin/alembic -c alembic.ini current
20260928_0004 (head)
$ cd api && .venv/bin/alembic -c alembic.ini heads
20260928_0004 (head)
$ cd api && .venv/bin/alembic -c alembic.ini check
No new upgrade operations detected.
```

`git diff --check` and explicit whitespace checks for the untracked owned files returned no output.
Compileall returned no output. No full-universe import was run.

### Real four-company evidence

The isolated database migrated from empty to head. ABT was imported first; ABT, NTES, `6752.T`,
and incomplete-cover SEC ticker CATO were then imported and rerun through the CLI. Exact verified
object bytes matched every retained input. Every PostgreSQL payload matched the current SQLite payload.
Final counts were `(artifacts, snapshots, links, selections, issuers, securities)`:

```text
(11, 4, 11, 4, 4, 4)
```

CATO read back with `security_title`, `source_accession`, and `receipt_ratio` all null, basis
`SEC_TICKER_MAPPING_ONLY`, while its canonical payload kept the exact retained receipt:

```text
{"cik":"0000018255","symbol":"CATO","accn":"0000018255-26-000004",
 "title":"","ratio":null}
```

Exact identities:

```text
snapshot  e05d0e9331c03f98fead822a9d7c6c074c598ed3815bf62a8b848dc5e8f6af4e
payload   4d757a5939529317b050aa734c31e98330e101afe7a3596bf34d189e0363dd35
facts     27fc6f288357a9cbe9f901c2ac6128d8da5dd214e606bb6d8b99051520ecf758
sidecar   ab5d13787d491937b3837179d2c578b28fddde9ea91c07df60c8f6418e0b4dc4
ticker map 91ed71e82bdb9d5306ed644b45311a040427678ca5944a81a1afc1c789145cad
```

The accepted three-company snapshot/payload hashes stayed unchanged:

```text
ABT     21dde22f1f89c1c6a354e471e0bbdbf2d803da3430c3a81307dd4e2b4905a6cc
        3f65756336803204f7c55725494b42437ec0af2e104d4a674796ea381d50170f
NTES    7b0a5a4a37f593382216ea15dc24526cc562875f0f6a1a80da1586a3c9df15f5
        5d6501bf32b5a30a6c08b16f879425351f1a7c4e8cade72650ef0cd9d7b63496
6752.T  8e9330adb67e0c19818e60a8cccc75599301f2cffc8527d08d861c82a50a0ce7
        5593ade3e5bad5f63ee630a5dc02c80c81ee09b1534fef269e56ff75cfb4330d
```

The populated revision-3 migration preserved its selected snapshot, accession, exact ratio, and
artifact link. Revision 4 accepted null cover fields and rejected empty non-null title and accession
values in real PostgreSQL.

### Known limits and concurrent state

- The manager's first full retained-universe import stored 6,170 rows and reported 755 failures.
  Review 1 added 485 incomplete-cover rows; Review 2 then found their only payload difference was
  the lost receipt provenance. That loss is corrected and pinned. The corrected full-universe
  rerun and remaining reconciliation are still queued for the manager. Bulk mode will report
  unresolved pairs instead of guessing.
- UI payload regression, export, audit, and filing-audit gates were not run because guidance forbids
  the full-universe run in this increment and the SQLite engine, extraction, payload serializer,
  existing snapshots, and UI reader are unchanged. Exact real four-company SQLite parity and the
  unchanged accepted hashes provide the bounded evidence for this PostgreSQL-only path.
- Integration databases were dropped. Content-addressed S3 objects remain reusable. The default
  local PostgreSQL database was migrated forward to revision 4; no application row was deleted.
- `api/screener/api.py` and other accepted increment files were already modified or untracked before
  this work. No concurrent edit to an owned source, migration, or test file was observed.

No commit, stash, branch switch, push, source fetch, or full-universe import was made in this
developer session.

## Reviewer response

### Review 1 — changes required

The schema, eligibility, verified mapping, derivation, per-company transaction, and checks are
accepted. One importer branch blocks acceptance. The first real bulk run stored 6,170 of 6,925
eligible rows and reported 755 failures. Of those, 501 had a stored cover row with empty title or
accession; 485 have an exact current SEC ticker-map match. `_prepare_sec` only falls back when
`receipt is None`, then raises `stored cover identity is incomplete` for this equivalent unusable
state. Guidance revision 2 requires the same verified mapping fallback for incomplete covers and
pins both matching and mismatching cases. Missing depositary ratios must remain failures.

### Review 2 — changes required

The fallback routing and checks pass, but exhaustive real-universe reconciliation blocks
acceptance: all 485 newly imported incomplete-cover rows have a payload hash different from SQLite.
A field diff on CATO shows only `receipt.cik`, symbol, accession, and its empty title disappeared.
The mapping fallback must govern PostgreSQL security metadata without erasing the retained receipt
from the `EvidenceBundle` and canonical payload. Guidance revision 3 pins exact payload/provenance
parity and keeps foreign derivation as the support gate.

### Review 3 — accepted

Accepted. The original incomplete receipt remains in `EvidenceBundle` and the canonical payload,
while PostgreSQL security metadata uses null title/accession and mapping-only basis. The reviewer
observed 29 focused tests and 4 real migration/import tests; the developer observed all 6 real
integrations, 736 storage-enabled Python tests, and 113 web tests. Real CATO evidence pins exact
SQLite payload parity and retained receipt provenance. No commit was made.
