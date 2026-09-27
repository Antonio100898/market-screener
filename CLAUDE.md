# Market Screener

## Product authority

Read `product.md` before any work and the linked `product/*.md` documents for all
affected areas. These are the owner's highest-priority product requirements in
this repository. Identify affected decision IDs, preserve them, and verify the
result before claiming completion. Proposals, code, and tests do not authorize
overriding them. Obtain an explicit owner decision for conflicts and record the
approved change. Follow `AGENTS.md` for the full repository workflow and gates.

Graham screening is an existing calculation set, not the product boundary.

Screens every US-listed SEC filer against Graham's Enterprising Investor criteria
(Intelligent Investor, ch. 15), from primary XBRL filings with per-figure provenance.

## Layout

```
api/   Python. Layers: sources/ (EDGAR, Yahoo, index lists) -> normalize.py
       (facts -> Snapshot) -> screens/ (criteria) -> api.py (FastAPI).
       store.py: SQLite at ~/.cache/graham-screener/screener.db; raw filings cached as files.
web/   React SPA (Vite, no runtime deps beyond React). One fetch of dashboard.json,
       all filtering/sorting client-side.
```

## Invariants — do not break

- **Criteria are numbered 1, 2, 3, 4, 5, 7.** There is no 6 (Graham's growth test
  is disclosed, never scored). Always look up by number (`byN`), never by position.
- **Snapshots are stored price-free.** Criteria 1 and 7 are settled once, at
  export, by `sync.apply_price()`. There is no client-side mirror: `web/src/screen.js`
  reads the statuses the export already settled and never recomputes a criterion
  from a price. Four documents claimed such a mirror existed until the audit of
  2026-08-21; `priceToPass()` — "what price would clear the tests" — is the only
  price arithmetic in the browser, and it is a different question. If a client-side
  refresh is ever built, that is when the two-places rule starts to apply.
- **Missing is never zero in evidence or Graham grades.** A figure with no evidence
  stays INSUFFICIENT. Company detail defaults to the flagged
  `?assume_absent_zero=true` view and can switch back to strict values; Return
  Quality uses a separate disclosed zero-assumption overlay that cannot affect a
  stored criterion or verdict.
- **Grade precedence** (pinned by `web/test/grade.test.mjs`): definitive non-price
  FAIL → BLOCKED, any uncomputable criterion → UNGRADEABLE, valuation-only fails →
  NEAR-PASS/CLOSE. The engine verdict ranks a measured FAIL above INDETERMINATE.
- **Ingest incrementally and reconcile source changes.** New filings, verified
  corrections, withdrawals, and changed source responses follow
  `product/shared-data.md`. An existing filing ID does not prove unchanged content.
  Engine changes use retained evidence: bump `store.ENGINE_VERSION`, run
  `make derive`. Never refetch to fix a code bug.
- Routine derive/export recomputes every ticker-eligible snapshot and defers
  tickerless or preferred-only cache rows until they can enter the dashboard.
  `make derive-all` remains available for exhaustive cache maintenance.
- **Stale facts are missing facts.** Instant facts >400 days older than the
  balance sheet are dropped; fundamentals >450 days older than the quote withhold
  the price criteria.
- **Foreign forms require a coherent basis.** A current 20-F/40-F filer enters
  only when that annual filing carries a US-GAAP or standard IFRS balance sheet
  in one identifiable ISO currency and its current cover exactly matches the
  ticker to supported common equity. Depositary securities additionally require
  a positive filing-backed underlying-shares-per-receipt ratio. A USD quote is
  converted into the statement currency with an explicit current or fiscal-date
  FX rate; missing FX withholds price arithmetic rather than guessing.
- **External IFRS imports are explicit adapters.** A company-specific non-SEC
  import must retain reporting currency, source document and exact source row,
  declare the configured primary security, and emit the same canonical contract.

## Proving a change

Unit tests pin synthetic fixtures; they cannot tell you what a change did to 5,892
real companies. Two harnesses answer that, and both must be run after an engine
change — every defect this project has shipped was found by a one-off script, and
several of those scripts were themselves wrong.

```sh
make regress                   # every field of every row vs the shipped payload
make audit                     # every displayed number vs the fact it names
make audit-filings             # ...and vs the statement the company published
```

`regress` calls exactly what `derive` calls — sidecar, cover ratio, export-time
enrichment — because omitting any of them invents differences. `audit` checks three
things a recomputation cannot: that a value matches the filing its own provenance
names, that computed figures are the arithmetic they claim, and that no figure is
assembled from components struck at different balance-sheet dates.

## Commands

```sh
make test      # pytest (api/.venv) + node --test (web), both required green
make derive    # recompute dashboard-eligible snapshots after an engine change
make derive-all # recompute every cached snapshot, including deferred filers
make export    # live prices + rebuild dashboard.json
make dev       # API :8000 + Vite :5173
```

## Conventions

- Python tests in `api/tests/`, JS tests in `web/test/` (`node --test`, zero deps).
- Every extracted figure carries provenance (tag, form, accession, period end);
  new figures must too.
- Disclose rather than guess: when a number rests on an assumption or a weaker
  tag, say so in the payload (`assumptions`, notes), don't silently proceed.
