# Foreign depositary ratio review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Direct scalar depositary ratios with retained evidence**
Review state: **ACCEPTED — Review 1**

The developer writes only under "Developer update". The reviewer writes the header and
"Reviewer response".

## Developer update

Developer state: **READY_FOR_REVIEW**

- **Trace:** `cover.depositary_ratio` interprets one unique direct ADS-to-share relation;
  `sync.cover_pages` applies it to the exact current class, cached R-report, then one retained
  primary document; `CompanyImporter` verifies and links that primary evidence. Engine version is
  183. P-02, P-03, P-04, P-09, and P-17 remain unchanged.
- **Reuse decision:** extended those three existing owners and the accepted atomic cover-cache
  helper. No second parser, fetch path, schema, ticker exception, ratio guess, or admission bypass
  was added. Detail is in `local/foreign-ratio-parser-2026-09-28/reuse-decision.md`.
- **Changed files:** `api/screener/sources/cover.py`, `api/screener/sync.py`,
  `api/screener/company_import.py`, `api/screener/store.py`, `api/tests/test_cover.py`, cover-only
  sections of `api/tests/test_sync.py`, `api/tests/test_company_import.py`,
  `api/tests/integration/test_company_import_storage.py`,
  `local/foreign-ratio-parser-2026-09-28/**`, and this Developer update.
- **Official outputs:** fixed SEC accessions produced 28 cases and 19 current recoveries: CDLR,
  ERIC, GMAB, GOTU, HDB, ING, JG, NCNA, NVO, POM, PSNY, RELX, RERE, SNY, SOGP, SSL, SUZ, SY,
  and WKEY. Exact URLs, response hashes, titles, and results are in
  `local/foreign-ratio-parser-2026-09-28/real-ratio-output.json`.
- **Adverse cases:** ADAG and FMS contain different direct relations, so the accepted report's
  expected original-inventory recovery count is 18 rather than 20. FMX/KOF compound units, VLRS
  CPO chaining, WAVE conflicting counts, BBD preferred shares, and BMA/WDS unresolved evidence all
  remain null.
- **Retained evidence:** real local S3/PostgreSQL import passed and read back the 61-byte NTES
  primary fixture from
  `raw/sha256/24e991ef0f209d06357df4514bf82c76814f6509fe44d617fbfd042a2a4a7d68`.
  The retry retained 13 artifacts and 13 snapshot links; accepted payload neighbours were unchanged.
- **Checks:** focused cover/sync/import: `150 passed, 1 skipped`; real storage: `1 passed`;
  `make test`: `780 passed, 6 skipped, 1 dependency warning` and `113` web passed; compileall and
  `git diff --check`: passed with no output.
- **Manager set:** the exact bounded 28-CIK reparse command is under **Manager command** in
  `local/foreign-ratio-parser-2026-09-28/README.md`.
- **Limits:** no live ratio reparse, derive, export, regression, audit, filing audit, or UI-payload
  gate ran. The manager owns those checks after review.
- **Skills used:** `plain-writing`, `applying-feature`, and `ponytail` full. They kept the change in
  the existing extraction/evidence owners and rejected conflicting or compound evidence instead of
  expanding scope.
- **Concurrent edits:** accepted dev9/dev10 cover, sync, importer, storage, and test changes were
  already uncommitted in the shared tree and were preserved. No new concurrent overlap was observed.
  No commit, stash, branch switch, or destructive Git action was made.

## Reviewer response

### Review 1 — accepted

Accepted. The ratio owner returns one positive Decimal only when every direct relation in the
evidence scope agrees; compound units, chained instruments, preferred shares, conflicting history,
and unresolved filings remain null. Exact R-report and primary-document bytes are retained before
parsing and linked through the existing importer. The reviewer independently observed 150 focused
tests and real S3/PostgreSQL primary-document readback. Fixed official accessions support 19
recoveries and nine exclusions; the expected count was reduced rather than forcing ADAG/FMS.
Engine 183 correctly invalidates ratio-dependent snapshots. No live reparse or commit was made.
