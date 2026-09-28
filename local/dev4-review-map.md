# Persistence developer review map

Developer state: **IDLE — accepted after Review 2**
Current item: **PostgreSQL company storage contract**
Review state: **ACCEPTED — Review 2**

The developer writes only under "Developer update". The reviewer writes the header and
"Reviewer response".

## Developer update

Developer state: **READY_FOR_REVIEW — guidance revision 2**

### Decisions and complete flow

Affected decisions remain intact:

- P-02: issuer, priced-security, snapshot, and current-selection rows are shared.
- P-03: PostgreSQL stores the canonical snapshot and links it to verified
  `evidence_artifact` rows; source bytes remain outside PostgreSQL.
- P-04: one mutable pointer selects the current snapshot without rewriting history.
- P-09: every earlier snapshot and all of its artifact links remain addressable.
- P-17: Python owns the SQLAlchemy metadata, Alembic migration, and repository.

`SharedCompanyRepository.store_and_select()` is the write entry point. It validates and
normalizes the SEC CIK or EDINET code, accepts a separate stable security identifier instead of
using the ticker as a key, preserves exact `Decimal` receipt ratios, and serializes the payload
with stable JSON ordering before hashing it. One transaction then inserts or verifies the issuer
and security, inserts or reuses the immutable snapshot, deduplicates artifact-role links, and
changes the security's current pointer. Reads follow the pointer to the selected snapshot or list
all retained snapshots. PostgreSQL foreign keys prevent selecting another security's snapshot.

### Reuse decision

`api/screener/postgres.py:metadata` remains the schema owner and the migration extends the single
existing Alembic chain. The repository reuses the Engine transaction boundary and PostgreSQL
`INSERT ... ON CONFLICT` pattern already owned by `SqlArtifactRepository`. Snapshot links reuse
the existing `evidence_artifact.content_sha256` identity. No company repository or PostgreSQL
company tables existed (`rg` found only the artifact repository), so a new focused repository was
required. The SQLite `store.py` path was not reused or changed because the accepted migration
keeps it working and P-17 assigns the new application schema to PostgreSQL.

### Changed files

- `api/screener/postgres.py`: issuer, priced security, immutable snapshot, artifact link, and
  current-selection SQLAlchemy Core tables with named constraints.
- `api/screener/shared_companies.py`: transactional idempotent repository and exact input checks.
- `api/migrations/versions/20260927_0002_company_snapshot.py`: one forward revision after
  `20260925_0001`; downgrade refuses destructive history removal.
- `api/tests/test_shared_companies.py`: identity conflict, SEC/EDINET identity, canonical retry,
  link dedupe, current selection/history, and exact receipt-ratio checks.
- `api/tests/integration/test_shared_company_storage.py`: isolated real-PostgreSQL migration from
  `20260925_0001`, retry, changed selection, reconnect, retained history, and exact NUMERIC check.
- `local/dev4-review-map.md`: this return only.

### Checks and raw results

Focused unit contract:

```text
$ api/.venv/bin/python -m pytest -q api/tests/test_shared_companies.py
......                                                                   [100%]
6 passed in 0.10s
```

Opt-in real PostgreSQL test. It creates an isolated database, upgrades it first to
`20260925_0001`, asserts that revision, upgrades to head, writes the same company twice, selects a
changed snapshot, disposes the engine, reconnects, proves both snapshots and the selected pointer,
and reads the exact `NUMERIC` ratio:

```text
$ cd api && RUN_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest -q tests/integration/test_shared_company_storage.py
.                                                                        [100%]
1 passed in 0.30s
```

Full Python regression after the source changes:

```text
$ cd api && .venv/bin/python -m pytest -q
ss...................................................................... [ 10%]
........................................................................ [ 20%]
........................................................................ [ 30%]
........................................................................ [ 41%]
........................................................................ [ 51%]
........................................................................ [ 61%]
........................................................................ [ 71%]
........................................................................ [ 82%]
........................................................................ [ 92%]
.....................................................                    [100%]
699 passed, 2 skipped, 1 warning in 2.34s
```

The two skips are the opt-in storage tests when `RUN_STORAGE_INTEGRATION` is absent. The warning is
the existing FastAPI/Starlette `httpx` deprecation.

Alembic against the running PostgreSQL container:

```text
$ cd api && .venv/bin/alembic -c alembic.ini current
20260927_0002 (head)
$ cd api && .venv/bin/alembic -c alembic.ini heads
20260927_0002 (head)
$ cd api && .venv/bin/alembic -c alembic.ini check
No new upgrade operations detected.
```

The initial local upgrade output was:

```text
Running upgrade  -> 20260925_0001, Create immutable evidence artifacts.
Running upgrade 20260925_0001 -> 20260927_0002, Store immutable company snapshots and their current selection.
```

The migration was revised once during development to add a stable security identifier after the
local test database had already reached head. That disposable local database was brought to the
final schema with matching `ALTER TABLE` statements; the final isolated integration test above
then proved the final migration from revision `20260925_0001` without that adjustment.

Real service restart evidence used the checked-in PostgreSQL 18.6 container. Before restart, the
same payload reused snapshot 1 and the changed payload selected snapshot 3 while retaining 1.
PostgreSQL sequences normally consume values during conflict retries, so the gap is expected:

```text
{"security_id": 1, "first_snapshot_id": 1, "repeat_snapshot_id": 1, "selected_snapshot_id": 3, "snapshot_ids": [1, 3]}
$ docker compose restart postgres && docker compose up -d --wait postgres
Container market-screener-local-postgres-1 Healthy
{"security_id": 1, "selected_snapshot_id": 3, "snapshot_ids": [1, 3], "payloads": [{"revision": 1}, {"revision": 2}]}
```

Whitespace check:

```text
$ git diff --check
[no output; exit 0]
```

### Known limits

- This increment stores current security metadata and an explicit stable security identifier. It
  does not yet model ticker/listing history; that is outside the active schema contract.
- The retained-evidence importer and FastAPI read path are queued increments and are not present.
- No UI-payload regression or filing audit was run because this new PostgreSQL path has no current
  caller and changes no engine, extraction, pricing, profile, serializer, SQLite data, or UI file.
- The service-restart evidence uses a deliberate `PERSIST.TEST` row in the isolated local
  development PostgreSQL volume. No production or retained baseline data was changed.

No other session touched an owned file while this increment ran. No commit, stash, branch switch,
or push was made.

**READY_FOR_REVIEW**

### Review 2 correction return

#### Corrected flow and reuse

`priced_security` now stores only the stable security identifier, issuer link, and creation time.
Ticker, exchange, currency, cover title/accession, security basis, and exact receipt ratio live on
each immutable `company_snapshot`.

`store_and_select()` canonicalizes the payload, normalizes and deduplicates the complete artifact
set, and hashes this evidence-bound identity:

- engine revision;
- canonical payload SHA-256;
- all snapshot security metadata, including exact receipt ratio;
- every artifact hash and role in stable order.

One transaction inserts or reuses that exact snapshot, inserts its exact links, verifies the stored
link set, and changes the current pointer. Changed metadata or links produce a different snapshot
hash and row. An exact retry reuses the row. Old metadata and links remain unchanged and readable.
The existing composite foreign key still prevents cross-security selection.

The correction keeps the Review 1 reuse decision: SQLAlchemy metadata and Alembic remain the schema
owners; PostgreSQL conflict handling and the existing `evidence_artifact` hash identity are reused.
No second persistence path was added. Revision `20260927_0002` was not changed during Review 2.

#### Review 2 changed files

- `api/screener/postgres.py`: moved changing security evidence to snapshots; stable security rows
  retain only identity; added evidence-bound snapshot SHA-256 constraints.
- `api/screener/shared_companies.py`: hashes metadata and exact links into snapshot identity, reads
  historical metadata/links, and verifies that a retry cannot append links to an old snapshot.
- `api/migrations/versions/20260927_0003_snapshot_evidence_identity.py`: forward migration from
  revision 2; backfills snapshot metadata and hashes from existing security/link rows before
  removing changing fields from `priced_security`.
- `api/tests/test_shared_companies.py`: added later-cover/ratio and changed-artifact-set pins while
  keeping identity conflict, retry, selection, retained history, dedupe, and exact ratio checks.
- `api/tests/integration/test_shared_company_storage.py`: now proves both required upgrade paths,
  including populated revision-2 migration, exact retries, evidence revisions, reconnect, and old
  link/metadata retention.
- `local/dev4-review-map.md`: this Review 2 return.

#### Checks and raw results

Focused unit contract:

```text
$ api/.venv/bin/python -m pytest -q api/tests/test_shared_companies.py
.......                                                                  [100%]
7 passed in 0.10s
```

Real PostgreSQL migration and persistence contract. The two parameter cases are
`20260925_0001 -> head` and populated `20260927_0002 -> head`:

```text
$ cd api && RUN_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest -q tests/integration/test_shared_company_storage.py
..                                                                       [100%]
2 passed in 0.49s
```

The revision-2 case writes a legacy selected snapshot and artifact before upgrading. After revision
3 it reads the migrated old accession, exact ratio, and exact old link set. Both cases then prove:
identical retry reuses one snapshot; later cover metadata/ratio creates another; identical payload
with changed artifacts creates another; all three remain after engine reconnect.

Full Python regression:

```text
$ cd api && .venv/bin/python -m pytest -q
sss..................................................................... [ 10%]
........................................................................ [ 20%]
........................................................................ [ 30%]
........................................................................ [ 40%]
........................................................................ [ 51%]
........................................................................ [ 61%]
........................................................................ [ 71%]
........................................................................ [ 81%]
........................................................................ [ 92%]
.......................................................                  [100%]
700 passed, 3 skipped, 1 warning in 2.00s
```

The three skips are the opt-in storage cases when their environment flag is absent. The one warning
is the existing FastAPI/Starlette `httpx` deprecation.

Forward migration and metadata match on the running PostgreSQL service:

```text
$ cd api && .venv/bin/alembic -c alembic.ini upgrade head
Running upgrade 20260927_0002 -> 20260927_0003, Bind security evidence and artifact sets to immutable snapshots.
$ cd api && .venv/bin/alembic -c alembic.ini current
20260927_0003 (head)
$ cd api && .venv/bin/alembic -c alembic.ini heads
20260927_0003 (head)
$ cd api && .venv/bin/alembic -c alembic.ini check
No new upgrade operations detected.
```

Real service restart used the revision-2 rows migrated in place. The exact retry reused migrated
snapshot 3. A later cover created snapshot 6. The same payload/metadata with a changed artifact set
created snapshot 7:

```text
{"retry_snapshot_id": 3, "metadata_snapshot_id": 6, "artifact_snapshot_id": 7, "snapshot_ids": [1, 3, 6, 7]}
$ docker compose restart postgres && docker compose up -d --wait postgres
Container market-screener-local-postgres-1 Healthy
{"security_id": 1, "selected": 7, "snapshots": [{"id": 1, "ticker": "PERSIST.TEST", "accession": "persistence-test-20260927", "ratio": null, "artifacts": [{"content_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "role": "facts"}]}, {"id": 3, "ticker": "PERSIST.TEST", "accession": "persistence-test-20260927", "ratio": null, "artifacts": [{"content_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "role": "facts"}]}, {"id": 6, "ticker": "PERSIST.NEW", "accession": "persistence-test-20260928", "ratio": "8", "artifacts": [{"content_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "role": "facts"}]}, {"id": 7, "ticker": "PERSIST.NEW", "accession": "persistence-test-20260928", "ratio": "8", "artifacts": [{"content_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "role": "raw_filing"}]}]}
```

Whitespace check:

```text
$ git diff --check
[no output; exit 0]
```

#### Known limits and concurrent edits

- Import and FastAPI work remain queued and were not added.
- Listing history is represented by immutable selected snapshot revisions; a separate listing-event
  query model is not part of this increment.
- UI payload/audit gates remain inapplicable because this unused PostgreSQL contract changes no
  engine, extraction, current evidence reader, price, profile, serializer, SQLite row, or UI file.
- The reviewer updated the review-map header and Review 1 response between returns. No other session
  touched an owned source, migration, or test file.

No commit, stash, branch switch, or push was made.

**READY_FOR_REVIEW — REVIEW 2**

## Reviewer response

### Review 1 — changes required

The focused unit test (`6 passed`), real PostgreSQL test (`1 passed`), Alembic current/head/check,
and tree inspection were repeated independently. Two contract errors block acceptance. First,
`priced_security` freezes `source_accession`, title, basis, and receipt ratio as identity and
`_verify_identity` rejects changes. Those values are annual evidence: the next 20-F or an ADS-ratio
change must produce a new retained revision for the same stable security. Second, snapshot identity
hashes only the canonical payload; `_link_artifacts` can append a changed artifact set to an old
snapshot, rewriting its provenance instead of retaining a new evidence revision. Guidance revision
2 requires a forward migration because `20260927_0002` has already been applied, evidence-bound
snapshot identity, and regression cases for both failures.

### Review 2 — accepted

Accepted. Tree review confirmed that stable security identity is now separate from changing cover,
listing, currency, and ADS-ratio evidence, and that the complete artifact set is part of immutable
snapshot identity. The reviewer independently observed `7 passed` in focused unit tests, `2 passed`
for real PostgreSQL upgrades from both revision 1 and populated revision 2, `700 passed, 3 skipped`
in the Python suite, Alembic `current`/`heads` at `20260927_0003`, and `No new upgrade operations
detected`. Exact retries reuse one snapshot; changed cover evidence or changed artifact sets retain
new revisions without altering old metadata or links. No commit was made.
