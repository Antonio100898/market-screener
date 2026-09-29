# Immutable source observation contract implementation

Updated: 2026-09-29
State: **ACCEPTED — Review 2; stop**

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`, the “S3 contract”,
“PostgreSQL model”, and “Durable jobs in one service” sections of
`docs/production-architecture.md`, increment 4 of `docs/implementation-rollout.md`,
`local/durable-ingestion-plan-2026-09-29.md`, and `local/dev35-review-map.md`. Read and follow the
shared code-work, applying-feature, ponytail, and no-scaffolding-leaks skills.

Implement milestone 3A only: generic source items and immutable source observations for SEC and
EDINET. Do not add source clients, source calls, schedules, handlers, extraction, current snapshot
selection, or publication.

## Decisions preserved

P-02, P-03, P-04, P-05, P-09, P-17.

## Files you may change

- `api/screener/postgres.py`
- `api/screener/durable_jobs.py`
- `api/screener/source_observations.py`
- `api/migrations/versions/20260929_0006_source_observations.py`
- `api/tests/test_durable_jobs.py`
- `api/tests/test_source_observations.py`
- `api/tests/integration/test_durable_job_storage.py`
- `api/tests/integration/test_source_observation_storage.py`
- Existing migration-head assertions in `api/tests/integration/test_shared_company_storage.py`
- Developer update in `local/dev36-review-map.md`

If correctness needs another path, return `BLOCKED:` with the path and reason.

## Schema contract

Add matching SQLAlchemy Core tables and Alembic revision after `20260929_0005`.

1. `source_item`: identity, source system limited to SEC/EDINET, item kind limited to inventory,
   aggregate, filing, or resource, non-empty stable source key, optional non-empty issuer source
   identifier, optional parent item foreign key, creation time, and unique
   `(source_system, item_kind, source_key)`.
2. `source_observation`: identity, source-item foreign key, canonical 64-character observation
   hash, state limited to present/pending/unavailable/removed, canonical metadata JSONB, optional
   verified artifact foreign key, source URL without secrets, optional ETag/Last-Modified, optional
   source publication/change time, detection time, detecting durable-job foreign key, and unique
   `(source_item_id, observation_sha256)`.

A present observation requires a verified artifact. Non-present observations must not claim one.
Optional text is either null or non-empty. Use UTC-aware instants. Do not add a current-observation
pointer or deletion API.

## Repository contract

Add typed immutable records and `SourceObservationRepository`.

- Validate and canonicalize source/item identity, metadata, state, URL, validators, hashes, and
  times before SQL. Reject URLs containing credential-like query keys such as key, token, secret,
  password, or subscription-key.
- Compute the observation hash from state, canonical metadata, artifact identity, sanitized URL,
  validators, and source time. Detection time and detecting job are not revision identity.
- One method records or reuses the source item and observation and records the durable job-item
  outcome in the same transaction. That transaction must validate running status, owner,
  generation, and unexpired lease. Refactor the accepted job repository only as needed so lease and
  item lifecycle rules keep one owner. Do not expose a database connection through `JobContext` or
  any handler-facing API.
- Duplicate delivery returns the same observation. Different state, metadata, evidence, validator,
  URL, or source time creates a new immutable revision. Reuse verifies stored identity and raises a
  specific conflict if it differs.
- A stale or expired worker writes neither source rows nor job outcome.
- Provide an ordered read for one source item so revision history is inspectable.

HTTP/S3 work happens before this database transaction. `evidence_artifact` remains the verified
byte owner; the foreign key must reject an unknown artifact.

## Required proof

- Unit tests for identity, strict JSON, URL secret rejection, state/evidence rules, stable canonical
  hashing, and changed revision inputs.
- Real PostgreSQL tests for concurrent duplicate reuse; pending -> present -> removed history;
  changed metadata with unchanged bytes; parent/issuer identity; stale and expired lease rejection
  with zero source rows and zero job outcome; and database constraints.
- Real S3 test: verified upload/readback links to a present observation. A failed or mismatched
  upload cannot create an observation.
- Upgrade fresh and populated `0005` databases to `0006`; run Alembic `upgrade head`, `current`,
  `heads`, and `check`.
- Run existing durable-job tests, the normal Python suite, and `git diff --check`.
- Stop PostgreSQL/S3 cleanly if started; preserve volumes.

No external source/model APIs, secrets, SQLite/cache mutation, commit, stash, or branch switch.

## Report

Update only `## Developer update` in `local/dev36-review-map.md`. List schema/repository behavior,
exact tests and counts, real PostgreSQL/S3/migration proof, stopped services, limits, and concurrent
changes. End with `READY_FOR_REVIEW`, `BLOCKED`, or `CONTINUE`. Do not mark the review accepted.
