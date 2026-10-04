# EDINET list discovery and reconciliation

State: **ACCEPTED — independent review passed**

4A retains exact EDINET daily-list revisions, validates official v2 metadata and rows, records
filings and operation events separately, reconciles changed and missing rows, and enqueues stable
future archive-fetch jobs for supported annual XBRL. It does not download archives, map facts,
derive or stage snapshots, select current data, or publish.

Withdrawal, disclosure, metadata-edit, and viewing-expiry states remain separate. Explicit events
cannot be undone by lagging original-date lists. Daily lists are serialized by stable source item
and official process time; older or same-minute-conflicting responses fail closed. Unchanged filing
rows reuse their observation even when the aggregate response timestamp changes.

Checks: 38 focused tests; 48 real PostgreSQL/S3/runtime tests including exact readback and stale
source-time rejection; 982 Python and 113 web tests. Alembic remains `20260929_0006 (head)` with no
new operations. Independent review accepted the lifecycle-axis corrections. No external EDINET
request, archive fetch, candidate write, current selection, publication, commit, or push occurred.
