# Foreign evidence investigation review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Complete foreign evidence inventory**
Review state: **ACCEPTED — Review 1**

The developer writes only under "Developer update". The reviewer writes the header and
"Reviewer response".

## Developer update

Return for review:

- **Trace:** opened `~/.cache/graham-screener/screener.db` read-only, loaded each retained
  `companyfacts_<CIK>.json`, resolved cover identity through `EvidenceLoader.identity`, and called
  `normalize.build_snapshot`. All 237 rows reproduced the stored `foreign` result through an exact
  `UnsupportedFilerError`.
- **Reuse decision:** the report reuses `sources/cover.py`, `EvidenceLoader.identity`,
  `normalize._current_supported_foreign_annual`, and `normalize._reject_foreign`. It recommends
  fixes only in those existing owners or in `sync.cover_pages`; it adds no second eligibility path.
- **Reconciled counts:** 237 total = 192 cover identity + 27 depositary ratio + 18 statement
  coherence. Cover subclasses = 47 parser, 24 symbol mismatch, 11 wrong class, 46 stale/OTC,
  seven unregistered, and 57 missing retained cover. Ratio subclasses = 18 cover-title ratios,
  six primary-document ratios, zero separate DEI ratio facts, one wrong-class pairing, and two with
  no current filing-backed ratio. All 18 statement rows have incomplete Company Facts and zero
  current standard balance anchors.
- **Evidence:** retained SQLite cover rows, retained SEC Company Facts, and bounded official SEC
  primary documents. No more than three official examples are reported per subclass. Exact
  accessions and source locations are in the machine inventory.
- **Reports:** `local/foreign-coverage-investigation-2026-09-28/README.md`, `inventory.json`,
  `inventory.csv`, and the reproducible `build_inventory.py`.
- **Verification:** running the builder observed 237 unique tickers, 237 unique CIKs, 237 current
  annual accessions, and 237 `UnsupportedFilerError` results. Engine split is 233 on 181 and four
  listed preferred rows on 178.
- **Limits:** 57 missing-cover rows were not batch-refetched; their exact finding is missing retained
  cover evidence and the external cause remains unverified. No statement exhibits or adapters were
  imported. No derive/export/regression/audit was needed for this read-only increment.
- **Skills used:** `plain-writing`, `applying-feature`, and `ponytail` full. They kept the report
  outcome-led, framed the complete support path, and limited recommendations to generic owning
  functions without ticker exceptions.
- **Concurrent changes:** application/import/migration/test work under `api/` and the dev4-dev7
  local reports were already uncommitted and continued in the shared worktree. This increment did
  not edit or overwrite them. It changed only the assigned dev8 review update and investigation
  report paths.

## Reviewer response

### Review 1 — accepted

Accepted. The reviewer reran the inventory builder and observed 237 unique tickers, CIKs, and
current accessions with the exact 192 cover / 27 ratio / 18 statement split. The complete subclass
sets reconcile without overlap. Primary SEC samples support the parser, identity, class, ratio, and
statement boundaries; ADAG's filing, for example, carries its 1.25 relation across continued cover
rows. The 57 rows with no retained cover remain explicitly unverified externally and are not
claimed recoverable. The report changes no application behavior. No commit was made.
