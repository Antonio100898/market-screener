# Foreign cover parser review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Generic cover identity parser fixes**
Review state: **ACCEPTED — Review 1**

The developer writes only under "Developer update". The reviewer writes the header and
"Reviewer response".

## Developer update

Developer state: **READY_FOR_REVIEW**

- **Trace:** `cover.securities` owns filing-stated 12(b) title/symbol pairs;
  `store.set_cover` retains the unchanged source cell; `EvidenceLoader.identity` selects the exact
  priced ticker, including one explicit component of an unmodified compound cell;
  `normalize._reject_foreign` keeps the supported-common and positive receipt-ratio gates.
- **Reuse decision:** extended those existing owners. No ticker list, stored alias, OTC rule,
  ratio guess, or second identity path was added. Full reasoning is in
  `local/foreign-cover-parser-2026-09-28/reuse-decision.md`. P-02, P-03, P-04, and P-09 remain
  unchanged.
- **Changed files:** `api/screener/sources/cover.py`, `api/screener/evidence.py`,
  `api/screener/store.py`, `api/tests/test_cover.py`, `api/tests/test_evidence.py`, and
  `local/foreign-cover-parser-2026-09-28/**`. Engine version is 182.
- **Behavior:** recognizes current 12(b) label variants; preserves continuation rows and blank
  parallel-column positions; rejects no-symbol `true`/`F`/`None` values; keeps `SUZB3/SUZ`
  intact while matching exact component `SUZ`; accepts common classes with attached purchase
  rights and receipt wording such as `right to receive`; still rejects standalone rights,
  warrants, notes, ETNs, preferred classes, and debt. Ambiguous compound matches return no class.
- **Real filing outputs:** fixed current SEC accessions were fetched for BBVA, BEP, AZN, SUZ,
  AURE, ADAG, HON, PPG, GLP, LX, and TM. BBVA/BEP/SUZ/ADAG select their intended class; AURE and
  AZN retain no exact ticker; HON/PPG/GLP/LX/TM keep their safe neighbouring class pairings. Every
  parsed row and source URL is in
  `local/foreign-cover-parser-2026-09-28/real-cover-output.json`.
- **Expected cases:** of the 47 parser rows, 28 clear the cover gate, IMMP and SUZ move to the
  separate unresolved-ratio gate, and 17 remain excluded. The exact 47-CIK bounded rescan/derive
  set and ticker groups are in `local/foreign-cover-parser-2026-09-28/README.md`.
- **Raw checks:** focused cover/evidence/sync tests: `100 passed in 0.25s`; full Python suite:
  `739 passed, 6 skipped, 1 deprecation warning in 2.88s`; compileall: passed with no output;
  `git diff --check`: passed with no output.
- **Skills used:** `plain-writing`, `applying-feature`, and `ponytail` full. They kept the change in
  the existing parser/identity owners and prevented a stored-symbol alias workaround.
- **Limits:** direct scalar ratio parsing is unchanged. The 57 missing-cover CIKs were not fetched.
  Per guidance, no derive, export, regression, audit, filing audit, or UI-payload gate ran.
- **Concurrent edits:** `api/screener/api.py`, `api/screener/postgres.py`, migrations, importer,
  shared-company files/tests, and other local reports were already modified or untracked in the
  shared worktree. This increment did not touch them. No concurrent edit overlapped an owned file.

## Reviewer response

### Review 1 — accepted

Accepted. Tree review confirmed the change stays in cover parsing, exact component selection, and
class predicates; it adds no ticker alias or admission bypass. The reviewer independently observed
100 focused tests and reran the 11 fixed-accession SEC cases. BBVA, BEP, SUZ, and ADAG parse as
common classes; AURE/AZN remain without an exact usable ticker; HON/PPG/GLP/LX/TM keep safe
pairings. Engine version 182 correctly invalidates classification-dependent snapshots. No full
derive/export gate or commit was made.
