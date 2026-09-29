# SEC recent-index discovery review map

Developer state: **IDLE — accepted after Review 2**
Current item: **SEC recent-index discovery**
Review state: **ACCEPTED — Review 2**

The implementation report belongs under `## Developer update`. The reviewer owns the header and
reviewer response.

## Developer update

### Reuse decision

Extend `EdgarClient` for exact transport metadata, use `store_evidence` for verified immutable
bytes, `SourceObservationRepository.record` for lease-guarded immutable source revisions, and the
existing schedule-occurrence identity for stable child work. Add only the SEC index parser and
handler that coordinate those owners; do not add another transport, artifact, observation, or
financial parser.

### Delivered

- `EdgarClient.fetch` returns exact bytes, final URL, content type, ETag, and Last-Modified through
  the existing request limiter and retry path. Existing Edgar methods are unchanged.
- The fixed-width form-index parser rejects header, separator, row, CIK, date, and archive-path
  drift. It preserves exact source rows and accepts the existing SEC financial family, including
  `20-F/A` and `10-QT/A`.
- `sec-recent-discovery` accepts only an explicit inclusive range of at most 31 dates. A 404 records
  an unavailable inventory revision. Other Edgar errors fail the job for normal retry.
- Available indexes are verified in S3 before their inventory revision is recorded. Filing
  revisions keep accession, issuer CIK, form, company name, filed date, archive filename, exact row,
  index date, quarter, validators, URL, and index artifact. Filing items have no inventory parent.
- Each filing revision gets one stable, immediately due `sec-resource-fetch` child. Its key includes
  the observation hash; its parameters include accession, CIK, form, filename, and observation ID.
  The child inherits parent priority.
- `DurableJobRepository.enqueue_child` checks the live parent lease in the same transaction as the
  child occurrence/job insert. `JobContext` exposes only its immutable lease token. Checkpoints are
  saved after every day's observations and children.
- Before each new uncheckpointed date, the handler checks the runtime stop signal. It raises
  `SecDiscoveryStopped` so normal job retry resumes after the last fully completed date; it never
  returns a partial final checkpoint as successful work.

### Proof

- Focused unit/runtime/source suite after the stop correction: `66 passed`. The two-day stop test
  checkpointed day one, raised, and made no request for day two.
- Real PostgreSQL/S3 durable runtime, source observation, and SEC ingestion suite: `27 passed`.
- Overlap and replay kept exact counts at 2 artifacts, 5 source items, 5 source observations, and 3
  resource-fetch children. Reconstructed repositories and runtime after restart kept those counts;
  both index objects read back byte-for-byte.
- Forced interruption left 1 artifact, 2 observations, no child, and no checkpoint. Retry reused the
  observations, created one child, and saved the checkpoint. An expired owner left only the verified
  orphan artifact: zero source items, observations, child jobs, or job-item outcomes.
- Full storage-enabled Python suite: `951 passed, 1 warning`.
- Alembic current/head/check: `20260929_0006`; no new operations and no migration.
- `git diff --check`: passed. PostgreSQL and SeaweedFS stopped cleanly with volumes preserved.

No external source/model API, secret, SQLite/cache, schedule, reconciliation, resource download,
extraction, current selection, or publication path was used or added. The changed plan and guidance
files outside this Developer update were concurrent manager work.

READY_FOR_REVIEW

## Reviewer response

### Review 1 — correction required

The handler ignored the runtime stop signal and could start more SEC dates during shutdown. It was
returned to stop before the next uncheckpointed date and leave the job retryable from its last
durable checkpoint.

### Review 2 — accepted

Accepted. The reviewer reran 66 focused tests, 27 real PostgreSQL/S3 tests, and the normal full
suite: 917 passed, 35 storage tests skipped, one existing warning. Exact index bytes, overlap replay,
interruption recovery, guarded child creation, and stale-owner orphan handling passed. Alembic stays
clean at `20260929_0006`; diff check passed. Services are stopped. No schedule, filing-resource
download, extraction, or publication is active.
