# Current data, storage, API, and UI flow

This traces committed application code at `697ef247`. A `path::symbol` citation
names the file and symbol that owns the behavior.

## End-to-end flow

```text
source discovery/fetch
  -> retained local evidence files
  -> EvidenceLoader bundle
  -> normalization and screening
  -> SQLite snapshot without live price
  -> export price/FX enrichment
  -> atomic dashboard.json replacement
  -> FastAPI file/API delivery
  -> React one-time load and client-side filter/sort
```

### 1. Commands and discovery

- `Makefile` calls `api/screener/sync.py::main` for bootstrap, bulk, metadata,
  daily, derive, events, cover, quotes, export, status, and imports.
- FastAPI `POST /sync` calls `api/screener/jobs.py::start`, which dispatches the
  same sync functions in one background thread. Only one job runs at a time.
- `api/screener/sync.py::_index_tickers` reads SEC's ticker map through
  `api/screener/sources/edgar.py::EdgarClient`, reconciles cover-page identity,
  upserts companies, resolves symbol reuse, and commits.
- `api/screener/sync.py::daily` reads SEC daily indexes, records newer filing
  dates, asks `api/screener/store.py::needs_refetch` which companies need current
  Company Facts, then derives and writes snapshots.
- `api/screener/sync.py::bulk` streams the SEC Company Facts archive and derives
  each company. The archive stays in memory; only per-company facts are retained.
- `api/screener/sync.py::metadata`, `api/screener/sync.py::events`, and
  `api/screener/sync.py::cover_pages` fetch SEC submissions, event indexes, and
  annual cover evidence. The bulk submissions archive and daily indexes are not
  retained. Cover evidence becomes rows, not retained source documents.
- `api/screener/sync.py::dera_sync` retains DERA archives and merges selected
  dimensioned facts into per-company sidecars.
- `api/screener/sync.py::import_edinet` retains each EDINET archive and atomically
  replaces its canonical company-facts JSON. `api/screener/sync.py::import_ifrs_workbooks`
  does the same for the configured company-specific workbook adapter output.

`bootstrap` is described as local-cache work, but it calls
`api/screener/sync.py::_index_tickers`. That call can refresh the SEC ticker map
when its 24-hour local cache is absent or stale. `derive` also calls that path.
They are not unconditionally network-free.

### 2. Retained evidence

`api/screener/sources/edgar.py::EdgarClient` owns the current filesystem cache at
`~/.cache/graham-screener`.

| Retained item | Current writer | Current reader |
|---|---|---|
| `company_tickers.json` | `api/screener/sources/edgar.py::_cached` | `api/screener/sync.py::_index_tickers` |
| `companyfacts_<cik>.json` | `api/screener/sources/edgar.py::_cached`, `api/screener/sync.py::bulk`, and foreign import paths | `api/screener/evidence.py::EvidenceLoader` |
| `dera_<quarter>.zip` | `api/screener/sources/dera.py::download` | `api/screener/sources/dera.py::harvest` |
| `dimensioned_<cik>.json` | `api/screener/sources/dera.py::merge_into_sidecars` | `api/screener/sources/dera.py::load_sidecar` |
| `edinet/<document_id>.zip` | `api/screener/sync.py::import_edinet` | the same import on retry |
| `screener.db` | `api/screener/store.py::connect` and store mutators | sync, jobs, API, evidence, regression, and coverage code |

`api/screener/evidence.py::EvidenceLoader` is the common evidence consumer. Its
`load` method combines Company Facts, the DERA sidecar, the stored ticker, and the
stored cover/receipt identity into `api/screener/evidence.py::EvidenceBundle`.
It reads files and SQL directly, so the consumer contract is reusable but the
current storage access is not portable.

### 3. Normalize, derive, and persist

- `api/screener/sync.py::_derive_evidence` passes the bundle to
  `api/screener/sync.py::_derive`.
- `_derive` calls `api/screener/normalize.py::build_snapshot`, then
  `api/screener/screens/enterprising.py::evaluate`. This produces the canonical
  financial snapshot and Graham result before a live market price is applied.
- `api/screener/store.py::put_snapshot` writes one JSON snapshot per CIK with the
  code-owned `api/screener/store.py::ENGINE_VERSION`.
- `api/screener/sync.py::derive` reads immutable cached evidence in worker
  processes, but sends all SQLite writes back through the parent process. This
  preserves the current one-writer behavior.

Snapshot state transitions:

```text
new/changed retained evidence -> snapshot_dirty
engine_version below current  -> needs_recompute
pending filing facts          -> pending_filing + prior usable snapshot retained
successful derive             -> snapshot(status=ok, current engine) + dirty cleared
unsupported/no facts/error    -> non-ok status, excluded from dashboard_rows
eligible ok snapshot          -> dashboard export candidate
```

`api/screener/store.py::needs_recompute` queues old engine versions and dirty
rows. Routine derive/export can defer tickerless and preferred-only cache rows.
`api/screener/store.py::dashboard_rows` publishes only usable, ticker-eligible
common-equity snapshots.

### 4. Price, FX, and publication

`api/screener/sync.py::export` reads `dashboard_rows`, then uses
`api/screener/sources/prices.py::YahooPriceProvider` for quotes, history, and
directional USD-to-reporting-currency FX.

- `api/screener/store.py::set_price_history` and
  `api/screener/store.py::set_fx_history` retain reusable market series in SQLite.
- `api/screener/sync.py::apply_price` settles criteria 1 and 7. The browser does
  not recompute those criteria.
- `api/screener/sync.py::_price_the_ratio_history` applies date-aligned historical
  prices and FX to historical ratios.
- Export also adds profiles and index memberships, removes internal-only fields,
  writes `dashboard.json.tmp`, and uses `Path.replace` to publish
  `api/screener/static/dashboard.json` atomically on the local filesystem.

The publication is not atomic across SQLite and the JSON file. Price/FX history
can commit before file replacement, while `last_export` commits after replacement.
There is no immutable release ID, active-release pointer, or stale-publisher check.

### 5. FastAPI delivery and current automatic work

- `api/screener/api.py::dashboard` serves the whole payload with `FileResponse`.
- `api/screener/api.py::company_dashboard` reads the payload row and combines it
  with local evidence, price history, and FX history for detail output.
- `api/screener/api.py::_snapshot_for` powers `/fundamentals` and `/screen`. It may
  fetch live EDGAR data, rebuild a snapshot in memory, and fetch Yahoo market data;
  it does not use the published snapshot row as the sole query source.
- `api/screener/api.py::_lifespan` starts
  `api/screener/jobs.py::start_hourly_quotes`. Hourly quotes are the only automatic
  server-lifetime refresh. Filing discovery, ingestion, derive, and full export
  remain command/UI-triggered.
- Built frontend files are served by `StaticFiles` from
  `api/screener/static/ui` after API routes are registered.

`daily`, `bulk`, and `derive` update SQLite but do not publish a new dashboard
file. A later export or eligible quote refresh is required.

### 6. React loading and private local state

- `web/src/App.jsx::load` fetches `/dashboard.json?t=<timestamp>` once, stores all
  rows in React state, and filters and sorts them in the browser.
- `web/src/Detail.jsx::Detail` fetches `/company/{ticker}/dashboard` for the detail
  view.
- `web/src/LoadBar.jsx::LoadBar` starts and polls sync jobs. The app can start a
  derive when snapshots are stale, so part of refresh still depends on UI activity.
- `web/vite.config.js::defineConfig` proxies FastAPI routes in development and
  builds the SPA into the API static directory.
- `web/src/view.js::loadView` and the intrinsic-value tool persist UI choices in
  browser `localStorage`. `web/src/api.js::token` also reads an access token from
  browser storage.

SQLite private/local tables are `tracked`, `portfolio`, `portfolio_trade`,
`portfolio_asset`, and `portfolio_cash`. Their APIs call store helpers and commit
per mutation. They have no user or workspace owner key, so they are local
single-user data, not the P-07/P-10 multi-user boundary.

## SQLite schema and transactions

`api/screener/store.py::SCHEMA` defines 14 tables.

| Area | Tables and key constraints |
|---|---|
| Shared identity/current output | `company` (CIK primary key), `snapshot` (CIK primary key, engine/status/data), `snapshot_dirty` (CIK primary key), `pending_filing` (CIK primary key) |
| Retained market/source metadata | `price_history` (CIK primary key), `fx_history` (base/counter primary key), `filing_event` (CIK/accession/item primary key), `security_cover` (CIK/symbol primary key), `sync_state` (key primary key) |
| Private/local | `tracked` (CIK primary key), `portfolio` (case-insensitive unique name), `portfolio_trade` (portfolio/external ID unique, BUY/SELL check), `portfolio_asset` (BOND/CRYPTO check), `portfolio_cash` (portfolio primary key) |

Portfolio child tables declare foreign keys, but `api/screener/store.py::connect`
does not enable SQLite foreign-key enforcement. Most shared tables do not declare
a foreign key to `company`. JSON, decimals, dates, and timestamps are mostly TEXT.

`connect` enables WAL and a ten-second busy timeout. `api/screener/store.py::migrate`
is a hand-written additive schema/data repair and commits; no migration revision
tool exists. Shared-data store helpers normally leave commit ownership to sync.
Sync commits in bounded batches, so cancellation or a later failure keeps earlier
committed work. Private mutators commit inside their helpers. `jobs.start` commits
success and cancellation and rolls back the current uncommitted batch on error.

SQLite-specific behavior includes PRAGMAs, `sqlite_master`, `?` placeholders,
`GLOB`, `COLLATE NOCASE`, `INSERT OR IGNORE/REPLACE`, `AUTOINCREMENT`, `lastrowid`,
SQLite exception classes, and raw SQLite row objects. Direct SQL also exists outside
the store module in `api/screener/api.py::_snapshot_for`,
`api/screener/evidence.py::EvidenceLoader`, `api/screener/sync.py::_index_tickers`,
and regression/coverage code. Replacing only `store.connect` cannot switch backends.

## Existing checks that define behavior

- Evidence and invalidation:
  `api/tests/test_evidence.py::test_loader_always_adds_dimensioned_and_cover_evidence`
  and
  `api/tests/test_evidence.py::test_changed_cover_invalidates_current_snapshot_until_recomputed`.
- Snapshot lifecycle:
  `api/tests/test_sync.py::test_store_roundtrip_and_staleness`,
  `api/tests/test_sync.py::test_pending_filing_keeps_recomputed_last_complete_snapshot_and_retries`,
  and
  `api/tests/test_sync.py::test_routine_recompute_defers_ineligible_cache_rows_until_they_can_surface`.
- Price/FX/export:
  `api/tests/test_sync.py::test_price_settles_valuation_criteria`,
  `api/tests/test_sync.py::test_store_roundtrips_a_directional_fx_history`, and
  `api/tests/test_sync.py::test_quote_only_export_updates_all_rows_and_retains_a_dated_quote_on_failure`.
- Scheduling: `api/tests/test_jobs.py::test_due_scheduler_starts_one_universe_quote_job_and_records_attempt`
  and the other scheduler tests in that file.
- Private/local persistence: `api/tests/test_portfolio.py::test_store_round_trips_trade_snapshot`
  and `api/tests/test_portfolio.py::test_store_round_trips_manual_assets_and_liquid_cash`.
- Browser behavior: `web/test/grade.test.mjs`, `web/test/index-valuation.test.mjs`,
  `web/test/return-quality.test.mjs`, `web/test/proxy.test.mjs`, and
  `web/test/detail-render.test.mjs`.

No direct first-load `App` or `/dashboard.json` route test was found. No backend
contract, PostgreSQL, object-store, migration, restore, tenant-isolation, or
release-race test exists.
