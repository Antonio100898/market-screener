# Storage reuse decision

## Decision

Extend the current owners. Do not duplicate the financial engine.

Keep the source adapters, `api/screener/evidence.py::EvidenceBundle`,
`api/screener/normalize.py::build_snapshot`,
`api/screener/screens/enterprising.py::evaluate`,
`api/screener/sync.py::apply_price`, and the current payload behavior as parity
contracts. Make `api/screener/store.py` the persistence facade, but first remove
its raw-connection leaks. Add an object-store boundary behind evidence loading.

This preserves P-02/P-03/P-04/P-05/P-09 for shared official data and revisions,
and P-07/P-10 by keeping private workspaces separate from shared facts. It also
preserves P-14: prove the flow locally before hosted deployment work.

## Reuse as behavior contracts

- Source parsing and company-specific adapters.
- Evidence assembly semantics: Company Facts plus dimensioned and cover evidence.
- Canonical normalization, missing-data behavior, provenance, and screening.
- Engine-version invalidation and dirty-evidence intent, expressed in a portable
  repository contract rather than a mutable SQLite row.
- Price and FX application, including criteria 1 and 7 and historical ratios.
- Existing FastAPI serialization and React filter/sort behavior for parity tests.
- Focused SQLite and UI tests as behavior examples during migration.

## Encapsulate or replace

- Raw `sqlite3.Connection`, SQL strings, row objects, exception handling, IDs,
  collations, PRAGMAs, and hand-written schema mutation.
- Direct cache paths in `api/screener/sources/edgar.py::EdgarClient` and
  `api/screener/evidence.py::EvidenceLoader`.
- The mutable one-snapshot-per-CIK model. It cannot represent retained revisions,
  release membership, or an atomic active release.
- File-only publication. Local `Path.replace` is safe for one file but cannot
  atomically publish PostgreSQL facts, derived values, and a release pointer.
- Global `tracked` and portfolio keys. They lack an owner, workspace revision,
  and audit history.
- Direct SQL outside the store module. A backend switch at `connect` alone would
  leave SQLite syntax and transactions throughout callers.

## Existing platform surface

The committed application has no Dockerfile/Compose file, PostgreSQL driver,
SQLAlchemy, Alembic, S3 client, migration directory, database URL, bucket config,
or object-store interface. `api/requirements.txt` contains only FastAPI, Uvicorn,
HTTPX, pytest, openpyxl, and xlrd. Current environment settings cover SEC/EDINET
access, one write token, and quote scheduling.

SQLAlchemy/Alembic and local S3-compatible storage appear only in planning
documents. They are proposals, not installed or proven choices. Select one Python
migration owner before adding dependencies; do not add Prisma as a second owner of
the same application schema.

## Smallest coherent first slice

Implement only the storage contract from rollout increment 2:

1. Add explicit local configuration for one PostgreSQL database and one private
   S3-compatible bucket. Keep secrets outside Git and bind services locally.
2. Add a single Python-owned migration chain with only migration metadata and an
   evidence-artifact table. Do not port all 14 SQLite tables yet.
3. Add an object interface that stores immutable bytes at
   `raw/sha256/<content-hash>` and reads them back with hash verification.
4. Write the object first. Mark an artifact usable in PostgreSQL only after upload
   and read/hash verification succeeds. A failed upload must leave no usable row.
5. Make retries idempotent on content hash and source revision. Restart both local
   services and prove the same bytes and metadata remain readable.
6. Leave current SQLite and application reads untouched in this slice. Do not add
   a second live writer or claim migration parity.

This is the smallest slice because it establishes the required evidence ordering
and durable schema owner without changing financial meaning. The next checked
slice can move one SEC company and one supported Japanese company through retained
evidence, PostgreSQL, existing derivation, and API parity before bulk import.

## Acceptance evidence for that slice

- Local database write, restart, and read pass.
- Object upload, hash/read verification, restart, and idempotent retry pass.
- Forced upload failure creates no usable evidence record.
- The migration starts from an empty database and upgrades an earlier test schema.
- No current SQLite data, cache file, or dashboard payload is deleted or overwritten.
- No shared official row receives a user/workspace owner, and no private row exists
  without an owner boundary.

## Gaps that block release claims, not the next slice

- No runtime baseline, cache, database, or UI payload was inspected here.
- Current source retention is incomplete: bulk archives, daily indexes, and cover
  source documents are not retained as immutable evidence.
- Current hourly quotes are automatic, but filing discovery and publication are not.
- Current schema has no observation history, accepted-value history, release model,
  stale-publisher guard, or multi-user ownership.
- Current tests do not prove PostgreSQL, object storage, restore, release races,
  tenant isolation, or full client parity.
