# SEC root resource fetch review map

Developer state: **IDLE — accepted after Review 3**
Current item: **SEC root resource fetch**
Review state: **ACCEPTED — Review 3**

`local/dev41-current-guidance.md` is the assignment. The developer writes only under
`## Developer update`; the reviewer writes the header and `## Reviewer response`.

## Developer update

### Result

Fixed both Review 2 blockers while preserving the Review 1 corrections and keeping milestone 3B3a
bounded to SEC root fetch. Deferred source delivery now has durable exponential backoff independent
of the operational error budget. Historical submissions validation now requires exactly one shard
range to cover the filing date.

P-02, P-03, P-04, P-05, P-09, and P-17 remain preserved. No schema, migration, production schedule,
leaf fetch, parsing, evidence assembly, derivation, selection, or publication was added.

### Reuse decision and flow trace

- Reused `EdgarClient.fetch()` for the existing bounded SEC transport and exact response bytes,
  `store_evidence()` for content-addressed upload/readback and verified artifact rows,
  `SourceObservationRepository.record()` for lease-checked immutable observations and durable item
  outcomes, and `JobContext.save_checkpoint()` for the final lease-checked checkpoint. The retry
  owner remains `DurableJobRepository` and `DurableJobRuntime`; no handler-specific replacement-job
  chain or new status/schema was added.
- The only source-observation repository addition is
  `SourceObservationRepository.latest_sec_filing()`. It loads the cited observation, requires an SEC
  filing item, and rejects a missing or non-latest observation.
- Handler flow: validate the exact five job parameters before I/O; load the cited latest parent;
  require `present` and exact accession, CIK, form, filename, source item, and observation identity;
  then fetch complete submission text, accession `index.json`, submissions, and Company Facts in
  that order.
- Each successful response goes through verified S3 storage before its `present` observation.
  Accession text and inventory are child resource items. Submissions and Company Facts are stable
  issuer aggregate items. Accession metadata keeps filing and parent identity. Aggregate canonical
  metadata is exactly issuer CIK plus role, so two filing jobs reuse one observation for identical
  aggregate bytes, final URL, and validators. Filing-specific graph state stays in the job
  parameters, item outcome, and checkpoint.
- The three JSON roots are strict UTF-8 JSON. Inventory must name the exact accession directory and
  valid unique resource names. Submissions must identify the issuer and either name the accession
  and form in `filings.recent` or name one valid historical submissions file whose documented date
  range covers the filing. Company Facts must name the issuer and contain a fact for the exact
  accession and form. Invalid, partial, ambiguous, or mismatched structures cannot
  checkpoint.
- A required-root 404 records a source `pending` observation and raises `SecResourcePending`. A
  valid aggregate response that does not yet cover the filing retains only its exact issuer-level
  `present` observation; the filing job item records its own `pending` graph outcome. Aggregate
  source history is not polluted with filing-specific states.
- `SecResourcePending` is a `JobDeferred`. The runtime calls the new lease-checked
  `DurableJobRepository.defer()`, which uses the existing bounded retry policy and `retry_wait` state
  but restores the claim attempt. Durable ownership generation advances on every claim and now
  drives the pending retry cap, so backoff grows with jitter from the base delay to the configured
  maximum and survives restart. Pending delivery does not spend the actual error budget. Deadlines
  still make a defer terminal with an explicit deadline-stopped-pending summary. Handler errors,
  expired leases, and failed completion still use the existing bounded failure path.
- Stop is checked before every fetch. The checkpoint is saved only after all four roots are present
  and validated. It stores one stable observation hash per role. Identical bytes and validators
  reuse artifacts and observations; changed aggregate bytes add one immutable revision.

### Files

- `api/screener/sec_ingestion.py`: handler, four root identities/URLs, strict parameters,
  filing-independent aggregate metadata, historical shard validation, pending behavior, and final
  checkpoint.
- `api/screener/source_observations.py`: latest cited SEC filing lookup.
- `api/screener/durable_jobs.py`: lease-checked durable defer that preserves the error attempt budget
  while using durable ownership generation for bounded exponential backoff and explicit deadline
  termination.
- `api/screener/job_runtime.py`: separate `JobDeferred` handling; normal exceptions still fail
  through the bounded path.
- `api/tests/test_sec_ingestion.py`: exact URLs and roles, final URL/validators/media types,
  parameters, parents, issuer aggregate reuse across two filing jobs, historical submissions shard,
  zero/one/two covering shard cases, malformed/mismatched JSON, four 404 positions, transient
  failure, replay, revision, stop, and expired lease.
- `api/tests/test_source_observations.py`: lookup input validation before SQL.
- `api/tests/test_job_runtime.py`: pending handlers defer without becoming failures.
- `api/tests/integration/test_durable_job_storage.py`: more-than-three defers, stable re-enqueue,
  increasing caps through the configured maximum, restart-preserved progression, bounded actual
  failures, deadline summary, later completion, and stale-owner rejection.
- `api/tests/integration/test_sec_ingestion_storage.py`: real PostgreSQL/S3 bytes, identities,
  revisions, replay, pending outcome isolation, more-than-three pending claims, runtime restart,
  stale parent, and expired owner.
- `local/dev41-review-map.md`: this developer update only.

### Checks and evidence

- SEC/source-observation unit suites:
  `cd api && .venv/bin/pytest -q tests/test_sec_ingestion.py tests/test_source_observations.py`
  — **92 passed**.
- Durable-job/runtime unit suites:
  `cd api && .venv/bin/pytest -q tests/test_durable_jobs.py tests/test_job_runtime.py`
  — **25 passed**.
- Real SEC/source-observation PostgreSQL/S3 suites:
  `cd api && RUN_STORAGE_INTEGRATION=1 .venv/bin/pytest -q tests/integration/test_sec_ingestion_storage.py tests/integration/test_source_observation_storage.py`
  — **23 passed**.
- Real durable-job/runtime PostgreSQL suites:
  `cd api && RUN_STORAGE_INTEGRATION=1 .venv/bin/pytest -q tests/integration/test_durable_job_storage.py tests/integration/test_job_runtime_storage.py`
  — **16 passed**.
- Combined real gate — **39 passed**.
- Real flow proved byte-exact readback for all four fixtures; accession roots had the filing parent;
  issuer aggregates had stable unparented item identities and issuer-only metadata; changing only
  Company Facts added one artifact and one observation; unchanged replay added neither.
- Unit flow with two distinct filing jobs for one issuer and identical aggregate responses produced
  six total root observations: four filing-specific accession observations plus two shared aggregate
  observations. The old behavior produced eight.
- The interrupted job retained its first two observations. Reconstructed engines, object stores,
  repositories, handler, and job context completed it with exactly four resource observations and
  four durable item outcomes. No duplicate observation survived replay.
- Real aggregate-pending proof retained one byte-exact `present` aggregate revision. Its filing job
  item carried `state=pending`; no aggregate pending observation or checkpoint was created.
- Real durable defer proof made four pending claims beyond the former three-attempt ceiling. Each
  defer returned the same job to `retry_wait` with attempts restored to zero. Stable re-enqueue
  returned that same job. After engine, object store, repository, handler, and runtime
  reconstruction, available fixture bytes completed it with `attempts=1` and
  `ownership_generation=5`.
- Real full-jitter proof produced successive pending delays of **30 seconds, 1, 2, 4, 8, 16, and 32
  minutes, then 1 hour and 1 hour**. Reconstructing the repository after the first defer preserved
  generation and produced the 1-minute second delay. Normal handler failures remain terminal at
  `max_attempts`; a pending retry beyond its deadline is terminal and says the deadline stopped it.
- The historical known-answer fixture omitted the filing from `filings.recent` and supplied
  `CIK0000001234-submissions-001.json` with a covering date range; all four roots validated and the
  job checkpointed. Zero covering shards remain pending. Two covering shards are ambiguous and fail
  without a checkpoint. Missing, malformed, or reversed shard metadata also fails closed.
- Real stale ownership proof left the verified complete-submission object/artifact only. It wrote no
  resource item, observation, item outcome, or checkpoint. Real stale-parent proof made zero source
  requests.
- Normal Python suite: `cd api && .venv/bin/pytest -q` — **968 passed, 47 skipped**, with the existing
  FastAPI/httpx deprecation warning. Skips are the opt-in storage tests; their affected focused
  suites passed separately above.
- Alembic: `current` = **20260929_0006 (head)**; `heads` = **20260929_0006 (head)**; `check` =
  **No new upgrade operations detected**. No migration was added.
- `git diff --check` — clean.
- PostgreSQL and SeaweedFS were stopped cleanly with `docker compose stop postgres seaweedfs`.
  Both report `Exited (0)`; named volumes were preserved.

### Limits and concurrent changes

- No external SEC or model API was called. Fixture transport only. No secrets, SQLite cache, or UI
  payload were changed. This slice does not change engine, extraction, pricing, profile, or
  serialization behavior, so the UI-payload regression gate is not triggered.
- Review 2 changed only the assignment and reviewer-owned response. No unrelated tracked changes
  appeared. No commit, push, stash, or branch switch was performed.

**READY_FOR_REVIEW**

## Reviewer response

### Review 1 — changes required

Three findings block acceptance. First, issuer aggregate items are stable by CIK, but their
canonical metadata contains filing accession, form, filename, and parent IDs; because metadata is
part of the observation hash, identical submissions or Company Facts bytes become duplicate source
revisions for different filings. Second, a pending root raises through the normal three-attempt job
limit, after which stable re-enqueue returns the same terminal job, so later SEC dissemination can
never recover. Third, submissions validation checks only `filings.recent`, while rebuilt-quarter
reconciliation can enqueue older filings represented by `filings.files`; those jobs become
permanently pending. The reviewer reproduced the aggregate hash split, reran 89 focused tests, 22
real PostgreSQL/S3 tests, and the full normal suite (964 passed, 43 storage tests skipped, one
existing warning). Alembic remains clean at `20260929_0006`; services are stopped. Guidance revision
2 returns the root contracts and required proofs.

### Review 2 — changes required

Review 1's three defects are fixed, but two findings still block acceptance. Deferred jobs always
calculate `retry_at(attempts=1)` while restoring the operational attempt count, so a permanently
pending filing retries at the first 0–30 second cap forever instead of backing off toward the
configured one-hour maximum. Also, historical submissions validation returns the first covering
shard and silently accepts a second overlapping range, so ambiguous source structure does not fail
closed. The reviewer reran 116 focused tests; the manager reran 38 real PostgreSQL/S3/runtime tests
and the full normal suite (967 passed, 46 storage tests skipped, one existing warning). Alembic is
clean at `20260929_0006`; services are stopped. Guidance revision 3 names the two corrections and
their proof.

### Review 3 — accepted

Accepted. Review 2's remaining defects are fixed: deferred work now backs off by durable ownership
generation to the configured one-hour cap without spending the operational error budget, deadline
termination names the deadline, and overlapping historical shard ranges fail closed. The reviewer
found no further issues. The manager reran 117 focused tests, 39 real PostgreSQL/S3/runtime tests,
and the normal full suite: 968 passed, 47 storage tests skipped, one existing warning. Alembic stays
clean at `20260929_0006`; diff check passed. PostgreSQL and SeaweedFS are stopped. No external API,
publication, selection, or UI-payload behavior changed.
