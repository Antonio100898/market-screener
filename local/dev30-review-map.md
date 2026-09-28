# Developer 30 — review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Full engine-187 reconciliation and API restart**
Review state: **ACCEPTED — Review 1**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

**READY_FOR_REVIEW — full engine-187 persistence and API restart gate passed.**

The production full-dashboard importer created 6,721 engine-187 current selections. All 6,721
stored payload hashes match SQLite, semantic payload equality is exact, and every selection has
evidence roles. Total current rows are 6,722 because the known non-UI `PERSIST.NEW` engine-181 row
was preserved. Final table counts are 18,845 artifacts, 20,570 snapshots, 59,694 links, 6,722
current selections, 6,722 issuers, and 6,722 securities.

The importer returned the exact expected 274 failures: 147 ticker-map mismatches, 117 pending facts,
ten missing receipt ratios, and zero other or Inline-XBRL failures. The retry created nothing,
reused all 6,721 selections, returned identical failures, and left every count unchanged.

All 18 SEC manifests and 110 file-role references read back byte-exact from S3. All 14 eligible SEC
companies have complete Inline-XBRL links. AHNRF, BRBI, CIB, and NXAT retain global evidence without
a snapshot. Both EDINET companies retain canonical and ZIP objects with exact payloads and
JPY/TSE/ordinary-share metadata.

Real Uvicorn checks passed before and after a physical PostgreSQL/SeaweedFS restart: 16 expected
companies returned exact 200 responses, four expected security exclusions returned 404, and CNI
retained 40-F plus 6-K provenance. Repository JSON and API JSON are byte-identical across restart;
the 6,721 selected snapshot IDs hash to
`68b7eacd3f9a07f398c69a3ffdb720cb7a510278fd50367d90423deba4beadc1` both times.

Checks: storage-enabled Python `857 passed` with one existing deprecation warning; web `113 passed`;
Alembic current/heads at `20260928_0004 (head)` and check clean; `git diff --check` clean. Uvicorn,
PostgreSQL, and SeaweedFS are stopped with volumes preserved. No source/model calls, cache/SQLite
writes, production edits, or commits occurred. PostgreSQL JSONB reads normalize `-0.0` to `0.0` in
186 payloads; stored pre-insert hashes and semantic values remain exact.

Evidence: `local/engine187-full-persistence-e2e-2026-09-29/README.md`.

## Reviewer response

### Review 1 — accepted

Accepted. The reviewer independently restarted PostgreSQL/S3, observed the read-only storage
verifier pass with 6,721 engine-187 UI selections plus preserved `PERSIST.NEW`, zero payload,
semantic, engine, or roleless mismatches, and exact SEC/EDINET object readback. A fresh Uvicorn run
returned 200 with exact SQLite/PostgreSQL parity for the 14 SEC additions plus Panasonic/Nintendo,
and 404 for the four required exclusions. CNI dual provenance is intact. API and services were
stopped cleanly.
