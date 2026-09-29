# SEC root resource fetch implementation

Updated: 2026-09-29
State: **ACCEPTED — Review 3; stop**

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`, the “Source records and
corrections”, “Incremental ingestion and reconciliation”, “S3 contract”, and “Durable jobs in one
service” sections of `docs/production-architecture.md`, increment 4 of
`docs/implementation-rollout.md`, `local/durable-ingestion-plan-2026-09-29.md`, and the accepted
boundary in `local/dev40-review-map.md`. Read and follow the shared code-work, applying-feature,
ponytail, and no-scaffolding-leaks skills.

Implement milestone 3B3a only: fixture-driven SEC root resource fetch. Do not add a production
schedule, FastAPI wiring, leaf-file fetch, cover or Inline-XBRL parsing, evidence assembly,
derivation, candidate storage, current selection, or publication. Do not call SEC during
development or tests.

## Decisions preserved

P-02, P-03, P-04, P-05, P-09, P-17.

## Files you may change

- `api/screener/sec_ingestion.py`
- `api/screener/source_observations.py`
- `api/screener/durable_jobs.py`
- `api/screener/job_runtime.py`
- `api/tests/test_sec_ingestion.py`
- `api/tests/test_source_observations.py`
- `api/tests/test_durable_jobs.py`
- `api/tests/test_job_runtime.py`
- `api/tests/integration/test_sec_ingestion_storage.py`
- `api/tests/integration/test_durable_job_storage.py`
- `api/tests/integration/test_job_runtime_storage.py`
- Developer update in `local/dev41-review-map.md`

If correctness needs another path, return `BLOCKED:` with the path and reason.

## Required behavior

1. Add `SecResourceFetchHandler` for the existing `sec-resource-fetch` jobs. Accept exactly
   `accession`, zero-padded `cik`, supported financial `form`, archive `filename`, and positive
   `observation_id`. Validate all values before network or storage work.
2. Before fetching, load the cited SEC filing observation and prove it is the latest observation
   for that filing item, is `present`, and exactly matches accession, CIK, form, archive filename,
   and observation ID from the job. A changed, removed, unavailable, wrong-issuer, stale, or missing
   parent fails before any request.
3. Fetch exactly these four graph-defining resources through the injected `EdgarClient`:
   - complete submission text at `https://www.sec.gov/Archives/{filename}`;
   - accession inventory at
     `https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession-without-dashes}/index.json`;
   - submissions aggregate at `https://data.sec.gov/submissions/CIK{cik}.json`;
   - Company Facts aggregate at
     `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json`.
   Each has one stable source item and explicit role metadata. Accession resources are child
   resources of the filing. Issuer aggregates are stable aggregate items that can retain multiple
   observed hashes over time.
4. Store and verify exact bytes in S3 before recording each `present` observation. Preserve final
   URL, media type through the artifact, ETag, Last-Modified, detected time, parent filing identity,
   and role. Replaying identical bytes and validators reuses the artifact and observation; changed
   aggregate bytes create a new immutable observation.
5. Fully validate `index.json`, submissions JSON, and Company Facts JSON after retaining their exact
   response revisions. The accession inventory must identify the requested accession. Submissions
   and Company Facts must identify the requested accession in their documented structures. A
   malformed, partial, or mismatched response cannot complete or checkpoint the job.
6. A 404 for any required root during dissemination records an honest `pending` state for that
   resource attempt and raises so the job remains retryable. Network, 429, and 5xx errors retry
   through the existing transport and then fail the job without inventing source state. Do not
   infer `unavailable`, removal, an empty graph, or success from an incomplete response.
7. Check the stop signal before each new fetch. Use durable item outcomes so restart resumes safely
   and repeated delivery is idempotent. Save the final checkpoint only after all four roots are
   present and their graph validation passed. An expired worker may leave a verified orphan object,
   but it cannot commit an observation, item outcome, or checkpoint.

Reuse `EdgarClient.fetch`, `store_evidence`, `SourceObservationRepository.record`, the existing
source identities, and `JobContext`. Add only the narrow repository lookup needed to validate the
latest parent filing. No schema or migration is expected.

## Required proof

- Unit tests: strict parameters and exact URLs; latest-parent validation; exact roles and identities;
  malformed/mismatched inventory and aggregates; one root 404 remains pending and retryable;
  transient failure; aggregate revision; duplicate replay; stop; expired lease.
- Real PostgreSQL/S3: seed a discovered filing, fetch four fixture resources, prove byte-exact
  readback and parent/resource identities, mutate one aggregate and retain both revisions, replay
  unchanged inputs, interrupt and reconstruct repositories/runtime, then finish without duplicates.
  Prove stale ownership writes nothing beyond a verified orphan object.
- Run the accepted SEC ingestion and source-observation focused suites, their real PostgreSQL/S3
  suites, the normal full Python suite, Alembic `current`, `heads`, and `check`, and
  `git diff --check`. No migration should appear.
- Stop PostgreSQL/S3 cleanly and preserve volumes.

No external source/model APIs, secrets, SQLite/cache mutation, commit, stash, or branch switch.

## Report

Update only `## Developer update` in `local/dev41-review-map.md`. List the full flow trace, reuse
decision, exact files, behavior, checks with raw counts, real restart/replay evidence, stopped
services, limits, and concurrent changes. End with `READY_FOR_REVIEW`, `BLOCKED`, or `CONTINUE`.
Do not mark the review accepted.

## Review 1 corrections

Keep all accepted behavior above and fix these three root causes together:

1. Issuer aggregates are one source item and one source revision per exact remote response, not per
   filing job. `submissions/{cik}` and `companyfacts/{cik}` observations must not hash accession,
   form, filename, or parent IDs into canonical metadata. Two different filing jobs for the same
   issuer and identical aggregate bytes/URL/validators must reuse the same aggregate observation.
   Filing-specific graph state belongs to the job outcome/checkpoint, not the issuer aggregate's
   source identity or revision history. Preserve filing-specific metadata for accession resources.
2. A source-pending root must remain durably eligible for a later attempt. The existing child has
   the default three-attempt limit, becomes terminal `failed`, and stable re-enqueue returns that
   same dead occurrence. Fix the owning durable-job contract rather than increasing the attempt
   count or creating an unbounded chain of replacement jobs. Pending source delivery is not a
   completed fetch and is not a permanent failure. Preserve bounded handling for actual handler
   errors, deadlines, stale leases, and shutdown. Prove pending survives more than three claims and
   later completes after the fixture becomes available, including runtime restart.
3. Quarterly reconciliation can enqueue a filing older than `submissions.filings.recent` (the SEC
   keeps only about 1,000 recent rows there). The root submissions aggregate can prove the issuer
   and either name the accession in `recent` or name the older submissions file that can contain it;
   absence from `recent` alone is not evidence that an old filing is still disseminating. Keep the
   leaf/history fetch outside 3B3a, but do not make historical jobs permanently pending. Add a
   known-answer historical submissions fixture.

Apply ponytail again after tracing these contracts. Add the narrowest tests that fail on all three
old behaviors, rerun the original focused and real gates, and replace the Developer update with the
new result. No owner decision is required unless the correct durable pending state needs a product
timing policy; if so, return `BLOCKED:` with the exact decision.

## Review 2 corrections

Keep the accepted Review 1 fixes and correct two remaining failures:

1. Deferred jobs must use bounded exponential backoff with jitter, not the first-attempt delay on
   every claim. Preserve the separate operational-error attempt budget. Reuse durable state that
   already advances per claim; do not add a schema field or handler-owned retry chain unless the
   existing job identity cannot express the contract. Prove successive defers increase their retry
   caps up to the configured maximum, restart preserves the progression, actual failures remain
   bounded by `max_attempts`, and a deadline still terminates. A deadline-terminal summary must say
   that the deadline stopped the pending retry.
2. Historical submissions shard ranges must be unambiguous. Zero covering shards leaves the filing
   pending, exactly one valid covering shard is accepted, and two or more covering shards fail
   closed. Add the overlapping-range known-answer case.

Rerun the full Review 1 proof after the narrow correction and replace the Developer update. No
external API call, schema change, migration, or owner decision is needed.
