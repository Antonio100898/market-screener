# Developer 2 — current guidance

Guidance revision: 1  
Updated: 2026-09-25  
Owner: reviewing session  
State: **ACTIVE — implement the verified evidence-storage slice**

Read `AGENTS.md`, `product.md`, `product/shared-data.md`,
`product/compatibility.md`, `docs/implementation-rollout.md`,
`local/storage-stack-research-2026-09-25.md`, and
`local/dev2-review-map.md` before work. The reviewer owns this file. Do not edit it.

## Product decisions

Preserve P-02, P-03, P-04, P-09, P-14, and P-17. This increment introduces
Python-owned PostgreSQL migrations and immutable evidence objects. It must not
change the current SQLite application path, financial calculations, API output,
or frontend.

## Paths you own

- `.env.example`
- `.gitignore`
- `compose.yaml`
- `api/requirements.txt`
- `api/alembic.ini`
- `api/migrations/**`
- `api/screener/storage_config.py`
- `api/screener/postgres.py`
- `api/screener/object_store.py`
- `api/screener/artifacts.py`
- `api/tests/test_storage_config.py`
- `api/tests/test_object_store.py`
- `api/tests/test_artifacts.py`
- `api/tests/integration/test_artifact_storage.py`
- The `## Developer update` section of `local/dev2-review-map.md`

Do not edit current SQLite owners, API endpoints, source adapters, financial
logic, product documents, other `local/` files, or frontend code. Never commit,
push, stash, reset, delete existing data, or access the owner's other checkout.
Return `BLOCKED:` if another path is required.

## Required result

1. Add the exact pinned stack from the research note. Compose must use persistent
   named volumes, bind PostgreSQL and S3 only to `127.0.0.1`, use environment
   variables with safe local defaults, and include working health checks.
2. Ignore `.env`; document only local non-production values in `.env.example`.
   Do not put real credentials in Git or logs.
3. Configure synchronous SQLAlchemy using `postgresql+psycopg://`. Initialize one
   Alembic chain. It creates only `evidence_artifact`, with content hash and object
   key uniqueness, byte size, media type, creation time, and verification time.
4. Store immutable bytes at `raw/sha256/<sha256>`. Compute the hash locally, upload,
   read the object back, and verify length and SHA-256 before inserting the usable
   database row. A failed or mismatched upload must not create that row. A database
   failure may leave an unreferenced immutable object; never delete or overwrite it.
5. Repeating the same bytes is idempotent in both object storage and PostgreSQL.
   Different bytes always use a different key. Keep object-client and database
   dependencies injectable so unit tests need no cloud service.
6. Add narrow unit tests for configuration, key/hash verification, idempotency,
   and every failure boundary. Add one opt-in integration test that migrates a real
   PostgreSQL database and proves upload/hash/read/restart plus no usable row after
   a forced upload failure.
7. Create `api/.venv`, install the pinned requirements, and run the narrow unit
   tests. If Docker is available, start only this worktree's Compose project and
   run the migration plus real integration test twice with a service restart
   between reads. Stop the containers without deleting their named volumes.
8. Run the existing Python test suite after narrow tests. No financial payload
   regression gate is needed because this slice must not touch financial engine,
   serialization, pricing, or current runtime readers. Confirm those paths have
   no diff.

Keep this slice small. Do not model companies, facts, releases, jobs, users, or
workspaces yet. Do not add Prisma, async database access, Redis, a worker, MinIO,
or a general storage framework.

## Return protocol

Update only `## Developer update` in `local/dev2-review-map.md`. Set state to
`READY_FOR_REVIEW` or `BLOCKED_EXTERNAL`. Include changed paths, exact commands and
results, migration revision, unit and integration evidence, service/restart state,
known gaps, and concurrent changes observed. Send the same concise result to the
reviewer. Never approve your own work.

