# Engine 187 real storage gate

Result: **PASS** on 2026-09-29.

The existing generic importer stored seven real retained companies through PostgreSQL and
SeaweedFS/S3. The gate covered a direct SEC 20-F, an incorporated 40-F/6-K relationship, both
retained EDINET companies, and the existing common-share, depositary-receipt, and mapping-only
boundaries.

## Product decisions

Verified P-02, P-03, P-04, P-09, and P-17. Every company used the same importer and shared
PostgreSQL/S3 owners. The importer retained immutable source bytes, selected one current snapshot,
and preserved exact payload and source identity. No source API, model API, SQLite write, cache
write, or production code path was used.

## Real counts

Final counts, in table order `evidence_artifact`, `company_snapshot`,
`company_snapshot_artifact`, `current_company_snapshot`, `issuer`, `priced_security`:

```text
35, 7, 36, 7, 7, 7
```

AERO deliberately failed after retaining its eight distinct evidence objects and before any
snapshot, link, selection, issuer, or security write. A normal retry completed it. Two complete
imports, then another import after service restart, kept all final counts and snapshot IDs
unchanged.

The one extra link over object count is intentional: AERO's presentation and schema roles point to
the same verified XSD hash. PostgreSQL stores one immutable object and two role links.

## Restart proof

The integration test disposed every client, physically restarted both Compose services, waited for
health, then reconstructed the PostgreSQL repositories, S3 client, object store, and importer.
All seven current selections retained the same IDs. Every object was read back from S3 and matched
its expected byte length and SHA-256. A post-restart import created no snapshot or link duplicate.

## Checks

```text
RUN_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest -q tests/integration/test_company_import_storage.py
1 passed in 29.23s

RUN_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest -q
857 passed, 1 warning in 22.21s

.venv/bin/python -m py_compile tests/integration/test_company_import_storage.py
exit 0

.venv/bin/alembic -c alembic.ini current
20260928_0004 (head)

.venv/bin/alembic -c alembic.ini heads
20260928_0004 (head)

.venv/bin/alembic -c alembic.ini check
No new upgrade operations detected.

git diff --check
exit 0
```

The warning is the existing FastAPI/Starlette `httpx` deprecation. The web suite was not run:
the test-only change does not change an API or UI payload contract.

## Hash result

Every canonical payload was byte-for-byte equal to the engine-187 SQLite payload. Existing
payload hashes did not change. Existing snapshot pins changed only because engine revision 187 is
part of snapshot identity. AERO, CNI, and Nintendo received their first exact real-storage pins.
See [company hashes and roles](company-hashes.md).

## Resources and limits

- Machine: Apple arm64, 10 logical CPUs, 16 GiB RAM.
- Free disk before services: 36 GiB. Free disk after the gate: 33 GiB.
- Final load averages: 2.67, 2.88, 3.17.
- Only PostgreSQL and SeaweedFS were started. Their named volumes were preserved when stopped.
- This gate covers the seven named retained companies. It does not prove the later full 6,995-row
  PostgreSQL import.
- No concurrent session edited the owned integration test or evidence directory. The pre-existing
  `local/company-import-plan-2026-09-27.md` edit was not touched.
