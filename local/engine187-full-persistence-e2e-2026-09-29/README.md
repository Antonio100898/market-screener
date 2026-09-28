# Engine 187 full persistence and restart gate

Result: **PASS** on 2026-09-29.

The production importer persisted the complete engine-187 supported universe through the default
PostgreSQL and SeaweedFS stores. The real FastAPI route returned the same canonical payloads before
and after a physical service restart. No source API or model API was called, and SQLite/cache files
were opened read-only.

## Product decisions

This gate verified P-02, P-03, P-04, P-09, and P-17. PostgreSQL remained the single application
database, SeaweedFS/S3 retained immutable evidence, one current engine-187 selection was shared by
all API clients, and evidence links remained attached to every selected snapshot.

## Repository counts

| Table | Before | After first import | After retry, restart, and tests |
|---|---:|---:|---:|
| `evidence_artifact` | 18,677 | 18,845 | 18,845 |
| `company_snapshot` | 13,849 | 20,570 | 20,570 |
| `company_snapshot_artifact` | 40,120 | 59,694 | 59,694 |
| `current_company_snapshot` | 6,706 | 6,722 | 6,722 |
| `issuer` | 6,706 | 6,722 | 6,722 |
| `priced_security` | 6,706 | 6,722 | 6,722 |

The final current set is 6,721 engine-187 UI companies plus the known non-UI development row
`PERSIST.NEW` on engine 181. It was preserved and counted separately.

## Full import and parity

The first `python -m screener.company_import --all-dashboard` run returned nonzero as required:

- 6,721 new current engine-187 selections;
- 274 expected failures;
- 147 current SEC ticker-map mismatches;
- 117 pending structured-fact cases;
- ten missing filing-backed depositary ratios;
- zero other failure classes and zero Inline-XBRL failures.

The retry returned `imported=0`, `reused=6721`, and the same 274 failures. All six repository counts
were unchanged. The complete retry output, including every reused ticker and failure reason, is in
`second-import.json`; exact buckets are in `failure-summary.json`. The first command's stdout was
not redirected before it ran and the terminal view truncated its long ticker list.
`first-import-reconstructed.json` preserves the complete equivalent result by combining the proven
first-run counts with the retry's exact successful-ticker and failure sets; it is labelled
reconstructed rather than raw output.

All 6,721 stored pre-insert `payload_sha256` values equal the canonical engine-187 SQLite hashes.
All 6,721 JSONB values are semantically equal to SQLite. PostgreSQL normalized `-0.0` to `0.0` when
reading 186 JSONB payloads; this is numerical equality, not a stored-hash mismatch. There were zero
engine mismatches and zero selected snapshots without evidence roles.

## SEC and EDINET evidence

- All 18 current SEC manifests and their 110 source-file role references were found in PostgreSQL,
  read back from S3, and matched the retained bytes.
- All 14 eligible SEC additions have every Inline-XBRL role linked to their selected snapshot.
- AHNRF, BRBI, CIB, and NXAT have retained global evidence objects and no current snapshot. Named
  production importer calls failed closed as expected: three `foreign`, one missing receipt ratio.
- Panasonic `6752.T` and Nintendo `7974.T` have exact SQLite payloads, canonical JSON and ZIP
  objects, JPY quote currency, TSE exchange, and `PRIMARY_ORDINARY_SHARE` identity.
- CNI retains the 40-F annual accession and the incorporated 6-K source accession.

## API and restart

One real Uvicorn process served `GET /companies/{ticker}` with lifespan disabled to prevent unrelated
scheduled network work. Before restart and after restart:

- the 14 SEC additions plus Panasonic and Nintendo returned 200;
- every canonical payload equalled SQLite and every revision equalled PostgreSQL;
- AHNRF, BRBI, CIB, and NXAT returned 404;
- CNI exposed 40-F annual plus 6-K source provenance.

PostgreSQL and SeaweedFS were physically restarted. The selected snapshot-ID digest remained
`68b7eacd3f9a07f398c69a3ffdb720cb7a510278fd50367d90423deba4beadc1`. The complete storage and API
JSON files compare byte-for-byte equal across restart.

## Checks

```text
RUN_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest -q
857 passed, 1 existing Starlette/httpx warning in 40.08s

npm test --silent
113 passed

.venv/bin/alembic -c alembic.ini current
20260928_0004 (head)

.venv/bin/alembic -c alembic.ini heads
20260928_0004 (head)

.venv/bin/alembic -c alembic.ini check
No new upgrade operations detected.

git diff --check
exit 0
```

## Resources and limits

- Apple arm64, 10 logical CPUs, 16 GiB RAM.
- Free disk: 33 GiB before services; 27 GiB at final cleanup. Other concurrent workloads caused
  temporary disk movement; the PostgreSQL database itself was about 215 MiB during import.
- Only PostgreSQL, SeaweedFS, and the two bounded Uvicorn processes were started for this gate.
- Uvicorn and both storage services are stopped. Named volumes are preserved.
- No production code, tests, product decisions, SQLite data, cache data, or commits changed.
- The pre-existing edit to `local/company-import-plan-2026-09-27.md` was not touched.
