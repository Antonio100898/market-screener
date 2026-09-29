# SEC rebuilt-quarter reconciliation review map

Developer state: **IDLE — accepted after Review 1**
Current item: **SEC rebuilt-quarter reconciliation**
Review state: **ACCEPTED — Review 1**

The implementation report belongs under `## Developer update`. The reviewer owns the header and
reviewer response.

## Developer update

### Built

- Added strict `sec-index-reconciliation` handling for one `{year, quarter}` and the exact SEC
  quarterly `form.idx` URL. The handler retains bytes, parses the complete fixture before inventory
  success, records unavailable 404 evidence but raises for retry, checks stop state, and checkpoints
  only after every filing outcome and required child is durable.
- Added the bounded PostgreSQL latest-quarter query. Database ordering uses detection time then
  observation ID and returns only filing items whose latest observation still belongs to the
  requested quarter.
- Added atomic witnessed removal. The transaction validates a present SEC quarterly inventory,
  exact quarter/item/observation/artifact hashes, a present prior filing, and that the supplied prior
  is still latest. Removal metadata keeps all comparison fields and the full inventory witness.
- New, changed, and reappeared filings retain one present observation and stable
  `sec-resource-fetch` child. Unchanged and already-removed filings get witnessed durable outcomes
  without revisions or children. A rebuilt CIK is passed to the immutable item identity, so a CIK
  disagreement fails closed.

### Proof

- `api/.venv/bin/pytest -q api/tests/test_sec_ingestion.py api/tests/test_source_observations.py`:
  **53 passed**.
- Real PostgreSQL/S3 focused source and SEC suites: **17 passed**. The fixture reconciliation proved
  exact quarterly-index S3 readback, unchanged/changed/new/removed states, another quarter untouched,
  changed/new children only, witnessed removal, reconstructed repositories, and duplicate replay
  with unchanged observation and child counts. Witness tests reject missing, altered, wrong-quarter,
  unavailable, wrong-artifact, and non-latest evidence.
- Real PostgreSQL/S3 durable job/runtime plus source/SEC suites: **30 passed**.
- `RUN_STORAGE_INTEGRATION=1 .venv/bin/pytest -q`: **966 passed**, one existing FastAPI/httpx
  deprecation warning.
- Alembic `current`, `heads`, and `check`: `20260929_0006` is current/head; no upgrade operations.
  No migration exists in the diff. `git diff --check` passed.
- PostgreSQL and SeaweedFS were stopped cleanly. Named volumes were preserved.

### Limits and concurrent work

No external SEC/model API or secret was used. This slice does not add schedules, history horizons,
resource downloads, extraction, current selection, publication, or UI payload changes. No tracked
change outside the allowed implementation/test paths was observed; the guidance and review map are
the only additional untracked files.

READY_FOR_REVIEW

## Reviewer response

### Review 1 — accepted

Accepted after adding two review guards: a rebuilt CIK must pass immutable filing identity, and a
removal may cite only the filing's latest observation. The reviewer reran 53 focused tests, 17 real
PostgreSQL/S3 source and SEC tests, and the normal full suite: 928 passed, 38 storage tests skipped,
one existing warning. Changed/new children, unchanged outcomes, witnessed removal, other-quarter
isolation, exact index readback, and replay passed. Alembic remains clean at `20260929_0006`; diff
check passed. Services are stopped. No production schedule or publication is active.
