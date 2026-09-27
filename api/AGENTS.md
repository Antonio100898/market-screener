# Database rules

These rules apply to every file under `api/`. Read the repository `AGENTS.md`,
`product.md`, `product/shared-data.md`, and the affected architecture section
before changing persistence. Database work must preserve P-02, P-03, P-04, P-09,
and P-17.

## Ownership and boundaries

- Python owns the application PostgreSQL schema, migrations, and data access.
  Use SQLAlchemy and Alembic. Next.js calls FastAPI; it never queries the
  application database or manages its schema.
- Change schema only through the single Alembic migration chain. Runtime code
  must not call `metadata.create_all()` or create tables opportunistically.
- S3 owns exact captured source bytes. PostgreSQL owns structured observations,
  selections, releases, jobs, and private workspaces. Do not use one as an
  untracked replacement for the other.
- FastAPI, jobs, and agent tools use the same repository and selection rules.
  Do not create a second financial-data path.
- Until PostgreSQL cutover is explicitly accepted, keep the SQLite application
  path working. Do not add dual writes or silently switch production readers.

## Migrations

- Never edit a migration that has been applied anywhere. Add a new migration.
- Use expand, migrate, contract: add compatible schema, move data with verified
  code, switch readers, then remove old schema only after old readers are retired.
- Keep migrations small and predictable. Large backfills belong in restartable,
  checkpointed jobs, not one deployment transaction.
- Avoid destructive downgrades. Correct immutable evidence and financial history
  with a forward migration. Deletion needs explicit owner approval and a tested
  recovery path.
- Keep SQLAlchemy metadata and Alembic revisions synchronized. Run `alembic check`.
- Name constraints and indexes. Add uniqueness and check constraints for business
  identities and invariants; application validation alone is not enough.
- Test each migration from an empty database and from the previous migration head
  using real PostgreSQL. SQLite does not prove PostgreSQL behavior.
- Measure locks for table rewrites, constraints, and indexes on populated data.
  Do not ship a migration that can hold an unbounded production lock.
- Run migrations once per deployment. Application replicas must wait for a
  compatible schema; they must not race to migrate it.

## Data truth and history

- Use stable issuer, security, filing, artifact, and observation identities.
  A ticker, display name, or latest filing date is not a stable key.
- Store captured evidence at `raw/sha256/<content_sha256>`. Upload it, read it
  back, and verify its size and SHA-256 before inserting a usable artifact row.
- Never hold a database transaction open during an S3, source, model, or other
  network call. An uploaded but unreferenced object is safe; a database row that
  points to missing or unverified evidence is not.
- Keep source observations and revisions immutable. A correction adds evidence
  and a new selection; it does not overwrite the old value.
- Represent the default answer through an explicit selected observation and
  published data release. Switch the active release atomically.
- Every financial figure keeps its unit or currency, period, scope, source
  identity, exact source location, extraction revision, and observation time.
- Keep company corrections, source changes, extraction fixes, and calculation
  changes as different cause types.
- Missing evidence is `NULL`, never zero. Preserve the difference in queries,
  constraints, calculations, and API serialization.
- Use PostgreSQL `NUMERIC` and Python `Decimal` for exact financial amounts.
  Never persist financial amounts through binary floating-point conversion.
- Use `TIMESTAMPTZ` for instants and store UTC. Use `DATE` for financial dates
  that do not represent an instant. Name source, detection, and activation times
  separately.
- Keep schema revision, data release, calculation engine revision, formula
  revision, and workspace revision as separate identities.

## Transactions and concurrency

- Keep transactions short. Commit related rows and their checkpoint or release
  pointer together.
- Design writes for at-least-once execution. Repeating a job or request must not
  duplicate artifacts, observations, history entries, or schedule occurrences.
- Enforce idempotency with stable keys and database constraints. Handle expected
  conflicts explicitly; do not use broad exception handling as deduplication.
- Lock only the rows needed to claim work or publish. Release locks before slow
  parsing, model calls, or network I/O.
- A job result may commit only while its lease and ownership generation are still
  valid. An expired worker must not publish after a replacement takes ownership.
- Compare the expected parent release when publishing. A stale publisher must
  rebase and revalidate instead of replacing newer data.
- Do not share a SQLAlchemy connection or session across concurrent requests,
  threads, tasks, or background jobs.

## Queries and private data

- Parameterize every query. Never build SQL from user text, formula text, sort
  names, or filter values.
- Every private row has a stable owner ID. Apply ownership checks inside every
  private read and write, including agent tools and streaming endpoints.
- Personal formulas may read shared published facts but may write only private
  workspace records. They never change official observations or selections.
- Store queryable identity, ownership, revision, status, and filter fields in
  typed columns. Use validated JSONB for nested configuration and formula trees,
  not as a substitute for the whole relational model.
- Bind list pagination, counts, sorting, and personal calculations to one data
  release. Never mix pages from different releases.
- Add indexes for measured query paths. Check plans and representative data;
  avoid unbounded scans, N+1 queries, and loading the full universe into memory.

## Security and operations

- Keep credentials in environment or deployment secret settings. Never commit,
  print, return, or embed them in database URLs shown to users.
- Use a migration identity for DDL and a lower-privilege application identity for
  runtime access in deployed environments.
- Bind local PostgreSQL and object storage to loopback only.
- Database health, source freshness, and publication freshness are different
  signals. A healthy connection does not mean users see fresh data.
- Back up PostgreSQL and retained objects, then prove a coordinated restore.
  Never delete a database, schema, volume, bucket, migration history, or retained
  evidence without explicit owner approval and a verified target.

## Required checks

- Add the narrowest unit tests for each rule or query change.
- Run affected tests against real PostgreSQL when behavior depends on its types,
  constraints, transactions, locking, conflict handling, or JSONB.
- Test duplicate delivery, retries, partial failure, restart, concurrent writers,
  and stale ownership where the changed path can encounter them.
- Prove failed or mismatched object uploads cannot create usable artifact rows.
- Run migrations, `alembic current`, `alembic heads`, and `alembic check` against
  the tested database. Confirm a normal service restart preserves accepted data.
- Engine, extraction, evidence, pricing, profile, and serialization changes also
  require the repository's full UI-payload regression and filing audit gates.
- Record the exact commands, observed results, and any unverified checks. An edit
  or a passing mock test alone does not prove database behavior.
