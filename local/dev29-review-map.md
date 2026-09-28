# Developer 29 — review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Real SEC/EDINET storage gate**
Review state: **ACCEPTED — Review 1**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

Implemented only the real integration gate in
`api/tests/integration/test_company_import_storage.py`. The existing generic importer now runs the
same storage test for ABT, NTES, AERO, CNI, Panasonic `6752.T`, Nintendo `7974.T`, and CATO. The
fixture copies only named retained inputs. SEC files come from each verified active manifest;
EDINET copies only the two named canonical files and ZIPs.

The gate verifies:

- exact engine-187 SQLite canonical payload equality for all seven companies;
- SEC/EDINET issuer and stable security identities, exchange, quote/reporting currency, source
  accession, security basis, and receipt ratio;
- AERO direct 20-F roles, CNI 40-F wrapper plus 6-K source roles and dual provenance, and both
  EDINET canonical/ZIP pairs;
- every artifact role/hash and byte-for-byte S3 readback;
- the AERO XSD's same hash linked under both presentation and schema roles;
- one forced AERO interruption after eight objects were retained but before any issuer, security,
  snapshot, link, or current-selection write;
- two normal imports and one post-restart import without duplicate objects, snapshots, links,
  selections, issuers, or securities;
- a physical PostgreSQL and SeaweedFS restart followed by repository/client reconstruction, stable
  snapshot IDs, current-selection reads, and verified S3 reads.

Final counts were `(35, 7, 36, 7, 7, 7)` for evidence objects, snapshots, role links, current
selections, issuers, and securities. Exact company payload/snapshot hashes and all new artifact
roles are in `local/engine187-real-storage-gate-2026-09-28/company-hashes.md`. Existing payload
hashes were unchanged. The first focused run exposed stale snapshot pins from engine 184; only the
observed engine-187 snapshot hashes changed because engine revision is part of snapshot identity.

Checks:

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

The one warning is the existing FastAPI/Starlette `httpx` deprecation. No web suite ran because no
API/UI payload contract changed. PostgreSQL and SeaweedFS are stopped with volumes preserved. No
source/model API was called; SQLite and the retained cache stayed read-only. P-02, P-03, P-04,
P-09, and P-17 are preserved.

Evidence and commands are in `local/engine187-real-storage-gate-2026-09-28/README.md`. Limits: this
proves seven named companies, not the full 6,995-company PostgreSQL import. No concurrent session
edited the owned integration test or evidence directory. A pre-existing edit to
`local/company-import-plan-2026-09-27.md` was left untouched. No commit, stash, branch switch, or
push was made.

## Reviewer response

### Review 1 — accepted

Accepted. The reviewer independently reran the real focused test and observed `1 passed`, then
confirmed Alembic `20260928_0004` current/head with no pending operation and stopped both services.
Seven retained companies preserve exact engine-187 payloads and byte-exact S3 evidence across
interruption, idempotent retries, and physical PostgreSQL/S3 restart. AERO and CNI prove both SEC
relationship shapes; Panasonic and Nintendo prove the full retained EDINET set. Only engine-
dependent snapshot identities changed; canonical payload hashes did not.
