# Immutable source observation review map

Developer state: **IDLE — accepted after Review 2**
Current item: **Source-item and immutable-observation contract**
Review state: **ACCEPTED — Review 2**

The implementation report belongs under `## Developer update`. The reviewer owns the header and
reviewer response.

## Developer update

### Result

Implemented generic SEC/EDINET source items and immutable observations at Alembic head
`20260929_0006`.

- `source_item` has stable source/kind/key identity, canonical optional issuer identity, same-source
  parent enforcement, and immutable conflict checks.
- `source_observation` retains canonical state, metadata, verified artifact, sanitized URL, HTTP
  validators, source time, detection time, and detecting job. PostgreSQL constraints enforce valid
  states, JSON objects, hashes, evidence/state rules, foreign keys, and immutable uniqueness.
- `SourceObservationRepository.record` validates before SQL, computes a stable canonical hash,
  reuses exact duplicates, rejects stored identity conflicts, and writes the observation plus job
  item outcome in one live-lease transaction. A fresh injectable repository clock validates the
  lease and timestamps the job outcome. Detection time remains observation data and, with the
  detecting job, does not change revision identity.
- `DurableJobRepository` remains the sole owner of lease and item-outcome rules through one private
  connection-scoped helper. No connection is exposed to handlers.
- Ordered history preserves pending, present, changed-metadata, and removed revisions.
- URLs reject user info and common secret keys, including API/subscription keys and signed-token or
  signature forms. SEC issuer IDs require ten-digit zero-padded CIKs. EDINET IDs require `E` plus
  five digits.

### Proof

- Focused unit: `43 passed` for source-observation and durable-job tests.
- Real PostgreSQL/S3 and migration: `26 passed`. This covered concurrent duplicate reuse,
  pending/present/removed history, changed metadata with unchanged bytes, issuer/parent identity,
  cross-source parent rejection, stale and expired leases with zero source/outcome writes, database
  constraints, unknown artifact rejection, verified S3 upload/readback, failed/mismatched upload,
  and fresh plus populated-`0005` upgrades.
- Review 1 stale-lease reproduction passed: an observation detected before expiry but written when
  the injected repository clock reached expiry raised `LeaseLost` and left zero source-item,
  observation, and job-outcome rows. The final focused rerun was `43 passed`; the final real-storage
  rerun was `26 passed`.
- Existing SQLite metadata fixtures exposed PostgreSQL-only `btrim` on the first full run. The
  schema now uses portable `trim` and emits PostgreSQL-only hash/JSON checks only for PostgreSQL.
  The affected fixtures then passed `16 passed`.
- Full Python suite: `903 passed, 31 skipped, 1 existing Starlette deprecation warning`.
- Alembic `upgrade head`, `current`, `heads`, and `check`: head is `20260929_0006`; no new upgrade
  operations detected.
- `python -m py_compile` and `git diff --check`: clean.
- PostgreSQL and SeaweedFS stopped cleanly. Volumes were preserved.

### Limits and concurrent changes

No source client, external API, schedule, handler, extraction, current selection, or publication was
added or called. The failed/mismatched upload cases use deterministic local stores; a real local S3
upload/readback proves the accepted path. Only the assigned production, migration, test, and this
Developer update paths were changed. The manager-owned guidance and review-map files were already
untracked when work started.

READY_FOR_REVIEW

## Reviewer response

### Review 1 — correction required

The first write path used caller-supplied `detected_at` to validate lease expiry. A worker could
detect data before expiry, lose its lease during upload, then commit late using the old detection
time. The implementation was returned to separate observation time from the live lease clock.

### Review 2 — accepted

Accepted. The repository clock now validates the lease and timestamps the job outcome; detection
time cannot authorize a write. The reviewer reran 43 focused tests, 26 real PostgreSQL/S3 and
migration tests, and the full Python suite: 903 passed, 31 skipped, one existing warning. Alembic is
clean at `20260929_0006`; diff check passed. PostgreSQL and SeaweedFS were stopped with volumes
preserved. No source handler or current-data publication is active.
