# Market Screener

This repository builds Market Screener from primary filings with per-figure
provenance. Graham screening is an existing calculation set, not the product boundary.

## Product authority

Read `product.md` before any work, then read the linked `product/*.md` documents
for every area the work affects. They record the owner's decisions and are the
highest-priority product requirements in this repository. Architecture proposals,
existing code, and tests do not authorize changing those decisions.

Before editing, identify the affected decision IDs and how the work preserves
them. Verify those decisions through relevant checks before claiming completion.
If the work conflicts with a decision, explain the conflict and obtain an explicit
owner decision before implementing it. Record approved changes in the product
documents; label recommendations and unresolved questions separately.

Keep current behavior working during migration. A target design does not claim
that its services or features already exist.

## Repository map

- `api/`: Python. Data flows from `sources/` through `normalize.py` into
  `screens/`, then through `api.py` (FastAPI). `store.py` manages SQLite at
  `~/.cache/graham-screener/screener.db`; raw filings are cached as files.
- `web/`: React/Vite SPA. It fetches `dashboard.json` once and performs filtering
  and sorting client-side.
- The large Markdown files at the root are design and audit records. Consult the
  relevant one before changing behavior, but do not treat plans as newer than
  tests and implementation.

## Invariants

- Criteria are numbered 1, 2, 3, 4, 5, and 7. There is no criterion 6: Graham's
  growth test is disclosed but never scored. Look criteria up by number (`byN`),
  never by array position.
- Snapshots are stored without prices. At export, `sync.apply_price()` settles
  criteria 1 and 7. The browser does not recompute them; `priceToPass()` only
  answers what price would clear the tests.
- Missing data is never zero in reported fields or Graham grades: missing evidence
  remains `INSUFFICIENT`. Company detail defaults to the disclosed
  `?assume_absent_zero=true` view (with an explicit strict-mode switch), and Return
  Quality ranks on a separate disclosed zero-assumption overlay; neither may alter
  the strict stored criteria or verdict.
- Grade precedence, pinned by `web/test/grade.test.mjs`, is: definitive non-price
  `FAIL` -> `BLOCKED`; any uncomputable criterion -> `UNGRADEABLE`; valuation-only
  failures -> `NEAR-PASS`/`CLOSE`. A measured `FAIL` outranks `INDETERMINATE`.
- Use incremental ingestion for new filings and verified source changes. Reconcile
  corrections, withdrawals, and mutable source responses as defined in
  `product/shared-data.md`; an existing filing ID is not proof it is unchanged.
  For engine changes, bump `store.ENGINE_VERSION` and run `make derive` from
  retained evidence; do not refetch to repair a code defect.
- Routine derive/export recomputes every ticker-eligible snapshot and defers
  tickerless or preferred-only cache rows until they can enter the dashboard.
  Use `make derive-all` only for exhaustive cache maintenance.
- Instant facts more than 400 days older than the balance sheet are missing.
  Fundamentals more than 450 days older than the quote withhold price criteria.
- Current 20-F/40-F filers enter only when the annual filing carries a coherent
  US-GAAP or standard IFRS balance sheet in one identifiable ISO currency and its
  current cover exactly matches the ticker to supported common equity. A
  depositary security also requires a positive filing-backed
  underlying-shares-per-receipt ratio. For a USD-listed security with non-USD
  statements, price arithmetic requires an explicit current or fiscal-date FX
  rate; unavailable FX withholds price ratios instead of guessing.
- A non-SEC IFRS company can enter only through an explicit company-specific
  adapter/import. It must retain statement currency, source document and exact
  source row, declare the configured primary security, and emit the same
  canonical contract as SEC normalization.
- Every extracted figure carries provenance: tag, form, accession, and period end.
  New figures must preserve it. Disclose assumptions and weaker tags in the
  payload rather than silently guessing.

## Validation

- Python tests live in `api/tests/`; JavaScript tests live in `web/test/`.
- Run `make test` for the normal suite (pytest plus `node --test`).
- After an engine change, also run `make regress`, `make audit`, and
  `make audit-filings`. These validate real-company output and filing provenance,
  which synthetic unit fixtures cannot cover.
- `make audit-filings` and several data/export commands use the network.
- Do not claim checks passed when dependencies, cached data, credentials, or
  network access prevented them. Report exactly what ran and what did not.

### Mandatory UI-payload regression gate

After every engine, extraction, evidence, pricing, profile, or serialization
change, validate against the exact `dashboard.json` currently used by the UI.
Unit tests alone are never sufficient.

1. Preserve the pre-change UI payload as the baseline; do not overwrite it first.
2. Run `make regress` against the full universe, not only a sample.
3. Rebuild through the same export/enrichment path the UI consumes.
4. Run `make audit` and, for changed provenance or extraction, `make audit-filings`.
5. Explain every changed field and verdict. Intended changes require filing-backed
   evidence; unexplained changes are regressions and block completion.
6. Check payload identities and UI-derived values, including criteria, verdict,
   market cap, valuation multiples, yields, historical ratios, alignment/profile
   results, notes, source accessions, row count, and ticker/CIK uniqueness.

Never report "zero regressions" when the database, raw filing cache, price inputs,
or baseline UI payload are unavailable. Report the change as unverified and stop
before treating it as release-ready.

## Common commands

```sh
make install   # create api/.venv and install Python and Node dependencies
make test      # Python and web tests
make derive    # recompute dashboard-eligible snapshots, without refetching
make derive-all # recompute every cached snapshot, including deferred filers
make export    # add live prices and rebuild dashboard.json
make dev       # FastAPI on :8000 and Vite on :5173
```

The Makefile uses POSIX paths and shell commands. On native Windows without
`make`, use WSL/Git Bash or invoke the equivalent tools directly; do not rewrite
the Makefile merely to work around the current agent environment.

## Working conventions

- Keep changes focused and preserve unrelated user edits.
- Add or update the narrowest relevant tests with behavioral changes.
- Treat `CLAUDE.md` as compatibility guidance for Claude Code. Keep its shared
  project invariants synchronized when an invariant changes.
