# Developer 2 — review map

Current item: **verified evidence-storage slice**
Review state: **ACCEPTED — revision 3; uncommitted by owner policy**

The developer writes only under `## Developer update`. The reviewer writes only
under `## Reviewer response` and this header.

## Developer update

Developer state: **READY_FOR_REVIEW**

Review 2 fix: SeaweedFS now has `stop_grace_period: 30s`. A normal
`docker compose stop postgres seaweedfs` completed in about four seconds; Docker
reported exit code 0 and `OOMKilled=false` for both services. After the clean stop,
`docker compose up -d --wait postgres seaweedfs` returned both healthy and the real
integration test passed (`1 passed`), reading the retained fixed artifact and its
single PostgreSQL row. A final normal stop again returned exit code 0 for both.
Both named volumes remain. `docker compose config --quiet` and `git diff --check`
passed.

Review 1 fix: local PostgreSQL now defaults to host port 55432 in Compose,
`.env.example`, `StorageSettings`, and the pinned unit test. Verification used no
port or database URL override:

- `.venv/Scripts/python.exe -m pytest -q tests/test_storage_config.py
  tests/test_object_store.py tests/test_artifacts.py`: `11 passed`.
- `docker compose config --quiet`: passed.
- `docker compose up -d --wait postgres seaweedfs`: both services healthy;
  PostgreSQL bound `127.0.0.1:55432` and S3 bound `127.0.0.1:8333`.
- With only `RUN_STORAGE_INTEGRATION=1`, `.venv/Scripts/python.exe -m pytest -q
  tests/integration/test_artifact_storage.py`: `1 passed`.
- `git diff --check`: passed. Containers are stopped and both named volumes remain.

Implemented revision `20260925_0001` for one `evidence_artifact` table and the
immutable `raw/sha256/<hash>` write/verify/record flow. The object write uses an
S3 conditional create, reads the bytes back, verifies length and SHA-256, and only
then records the row. PostgreSQL conflicts return the existing row.

Changed paths:

- `.env.example`, `.gitignore`, `compose.yaml`
- `api/requirements.txt`, `api/alembic.ini`, `api/migrations/**`
- `api/screener/storage_config.py`, `postgres.py`, `object_store.py`, `artifacts.py`
- `api/tests/test_storage_config.py`, `test_object_store.py`, `test_artifacts.py`
- `api/tests/integration/test_artifact_storage.py`

Checks run:

- `python -m venv api/.venv` and
  `api/.venv/Scripts/python.exe -m pip install -r api/requirements.txt`:
  installed the pinned storage dependencies successfully.
- `.venv/Scripts/python.exe -m pytest -q tests/test_storage_config.py
  tests/test_object_store.py tests/test_artifacts.py`: `11 passed`.
- `RUN_STORAGE_INTEGRATION=1 .venv/Scripts/python.exe -m pytest -q
  tests/integration/test_artifact_storage.py`: `1 passed` before restart and
  `1 passed` after restarting PostgreSQL and SeaweedFS. The second run reused the
  same object and database row. Forced upload failure left no row.
- `.venv/Scripts/python.exe -m alembic -c alembic.ini current` and `heads`:
  both reported `20260925_0001 (head)`.
- `.venv/Scripts/python.exe -m alembic -c alembic.ini check`:
  `No new upgrade operations detected.`
- `.venv/Scripts/python.exe -m pytest -q`: `687 passed, 1 skipped` in 17.54s.
  The skip is the opt-in integration test. One existing FastAPI/Starlette
  dependency deprecation warning remains.
- `git diff --check`, Python `compileall`, and `docker compose config --quiet`:
  passed. Current SQLite owners, API, source adapters, financial engine, frontend,
  and Makefile have no diff from this increment.

Service state: pinned PostgreSQL and SeaweedFS images became healthy on localhost.
Port 55432 is the checked-in local PostgreSQL default because another local process
owns 5432. Both services survived a restart with data intact. Containers are now
stopped with exit code 0; named volumes remain.

Concurrent changes observed: planning/product files were already modified or
untracked in this worktree. Another process owned host port 5432. I did not edit
those files or the owner's checkout. No known implementation gap remains inside
this increment; production S3 compatibility stays for the later hosted rehearsal.

## Reviewer response

### Review 1 — changes requested

The implementation and recorded checks are otherwise coherent, but the checked-in
defaults cannot start on this development machine: host port 5432 is already in
use, and the successful run required an undocumented 55432 override. Make 55432
the Compose, `.env.example`, and `StorageSettings` local default. Update the narrow
test to pin that contract, then rerun narrow tests, Compose configuration, and the
real integration test with no port override. Keep the named volumes and current
SQLite application untouched.

### Review 2 — changes requested

The port fix and all functional checks passed independently. SeaweedFS still exits
137 on a normal `docker compose stop`; Docker reports `OOMKilled=false`, and its
log shows graceful shutdown reached the filer just as Compose killed it at the
default deadline. Add a bounded `stop_grace_period` long enough for `weed mini`
to finish, then prove a normal Compose stop exits SeaweedFS with code 0 and that
the next start can read the retained artifact. Do not remove the named volume.

### Review 3 — accepted

Accepted after both fixes. Independent checks observed: Compose started the pinned
services healthy with no overrides; 12 storage tests passed against real
PostgreSQL and S3; Alembic current/head/check agreed at `20260925_0001`; service
restart preserved the row and object; normal Compose stop finished in 2.89 seconds
with both containers at exit 0 and `OOMKilled=false`; 687 Python tests passed with
one opt-in skip; 97 web tests passed after installing the locked dependencies;
`git diff --check` passed; current SQLite, financial, API, source, frontend, and
Makefile paths have no implementation diff. No commit was made because the owner
has not authorized commits.
