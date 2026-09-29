# SEC rebuilt-index reconciliation review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Rebuilt-quarter reconciliation investigation**
Review state: **ACCEPTED — Review 1**

The investigation report belongs under `## Developer update`. The reviewer owns the header and
reviewer response.

## Developer update

### Finding

No schema change is needed. The accepted immutable source items and observations can reconcile one
quarter without a current pointer. A removed filing can cite the retained, verified quarterly
inventory observation in its canonical metadata. The repository must validate that witness in the
same live-lease transaction that records the removal.

### Bounded contract

- Job kind: `sec-index-reconciliation`. Strict parameters: `{"year": YYYY, "quarter": 1..4}`.
  Source URL: `https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/form.idx`.
  One job handles one quarter. This chooses neither a schedule nor a history horizon.
- Inventory source item: `SEC/inventory/quarterly-index/{year}Q{quarter}`. Job-item key:
  `quarterly-index:{year}:Q{quarter}`. Repeated rebuild checks remain distinct durable schedule
  occurrences; identical retained bytes reuse the immutable observation
  (`api/screener/durable_jobs.py:134-222`, `api/screener/source_observations.py:109-216`).
- Final checkpoint: `{"year": YYYY, "quarter": Q, "inventory_observation_id": N,
  "inventory_observation_sha256": "..."}`. Save it only after every present revision has its
  observation and child, and every removal has its witnessed observation. Existing checkpoint and
  child writes already reject stale leases (`api/screener/job_runtime.py:43-76`,
  `api/screener/sec_ingestion.py:127-147`, `api/screener/durable_jobs.py:174-222`).
- Priority: recent discovery stays `30` (`api/tests/integration/test_sec_ingestion_storage.py:223-231`).
  Reconciliation occurrences use `20`; later historical scheduling may use a lower priority without
  changing this handler. The database already claims higher priority first
  (`api/screener/durable_jobs.py:320-339`).

Fetch, retain, and fully parse the whole index before recording inventory success or comparing
filings. A 404, transport error, malformed row, partial body, or parser error records no removal and
advances no checkpoint. `parse_form_index` already fails closed on changed headers, separators,
short rows, invalid CIK/date/path, and retains the exact source row
(`api/screener/sec_ingestion.py:55-106`). `store_evidence` remains the byte boundary; a verified
orphan is safe if parsing or lease validation later fails.

### Exact latest-quarter query

Add `SourceObservationRepository.latest_sec_financial_filings(quarter)` using two bounded steps:

1. Select distinct `source_item_id` values from SEC filing observations whose
   `canonical_metadata->>'quarter' = :quarter`.
2. For only those IDs, select the latest observation with PostgreSQL `DISTINCT ON
   (source_item_id)`, ordered by `source_item_id, detected_at DESC,
   source_observation_id DESC`; keep rows whose latest metadata still names that quarter.

This reads only filing items ever assigned to the proven quarter, then one immutable history per
candidate. It does not scan other quarters in application code or add a mutable current pointer.
The existing identity and history order support the query (`api/screener/postgres.py:389-516`,
`api/screener/source_observations.py:218-229`). Add an index only after a real query plan proves it
is needed; the first bounded slice does not need another table or migration.

Compare the rebuilt rows by accession against that latest-state map. The filing comparison fields
are exactly `form`, `company_name`, zero-padded `cik`, `filing_date`, `archive_filename`, and
`accession`; `quarter` is partition scope. Daily `index_date`, inventory URL, validators, and
inventory artifact hash are evidence, not filing metadata changes.

- New accession, a latest `removed` accession that reappears, or changed comparison fields: record
  one `present` filing observation backed by the quarterly index artifact, then enqueue one
  `sec-resource-fetch` child. Child identity remains CIK + accession + filing observation hash, as
  in recent discovery (`api/screener/sec_ingestion.py:197-252`). Duplicate delivery reuses both.
- Same latest `present` metadata: record no filing revision and no child. The retained quarterly
  inventory proves it was checked without manufacturing a revision for every unchanged filing.
- Latest `present` accession absent from the successfully parsed rebuilt index: record `removed`,
  with no artifact and no child. Latest `removed` absent again creates nothing. Daily-index absence,
  another quarter, an unavailable index, or a failed parse proves nothing.

Removal metadata must contain the prior filing comparison fields plus
`reason=absent_from_rebuilt_quarter`, `quarter`, `inventory_source_item_id`,
`inventory_observation_id`, `inventory_observation_sha256`, and
`inventory_artifact_sha256`. Add a repository method that, under the same live-lease transaction,
loads that observation, verifies it is a `present` SEC inventory for the same quarter with the named
artifact/hash, then inserts/reuses the `removed` observation and job-item outcome. This preserves
the accepted rule that non-present observations carry no artifact while making their evidence
directly verifiable (`api/screener/source_observations.py:124-216`,
`api/screener/postgres.py:448-516`). Immutable observations are retained, so a JSON witness needs no
new foreign-key column.

### Smallest build and checks

Reuse `SecIndexFiling`, `parse_form_index`, `EdgarClient.fetch`, `store_evidence`,
`SourceObservationRepository`, `DurableJobRepository.enqueue_child`, and `JobContext`. Extend only:

- `api/screener/sec_ingestion.py`: quarterly URL/parameter validation and
  `SecQuarterlyReconciliationHandler`.
- `api/screener/source_observations.py`: bounded latest-quarter query and atomic witnessed-removal
  write, by refactoring the existing transaction body rather than creating a second writer.
- `api/tests/test_sec_ingestion.py`: strict parameters/URL; unchanged, new, changed, reappeared, and
  removed cases; no removal for 404/transport/partial/malformed input; stable children; no child for
  removal; stop/retry/checkpoint; stale lease.
- `api/tests/integration/test_sec_ingestion_storage.py`: seed recent observations, reconcile one
  fixture quarter, prove exact S3 inventory readback, latest states, witnessed removal, changed/new
  children only, duplicate replay, interruption, process/repository restart, and stale-owner writes
  nothing beyond a verified orphan.

Focused tests must also prove a filing known only in another quarter is untouched, changed metadata
creates one immutable revision, repeated absence does not duplicate removal, and deleting or
altering the cited inventory identity makes witness validation fail. No API/current snapshot,
extraction, publication, production schedule, or owner freshness decision belongs in this slice.

No production code, database/cache, service, or external API was changed or called. Only this
Developer update was edited.

READY_FOR_REVIEW

## Reviewer response

### Review 1 — accepted with build requirement

Accepted. No schema change is needed: immutable removal metadata may cite a retained quarterly
inventory observation, provided the repository validates that witness in the same live-lease
transaction. Every unchanged filing must still receive a durable job-item outcome tied to that
inventory witness before checkpointing; raw presence must not become an unrecorded success. A 404,
unavailable response, transport failure, or parse failure records no removal and must leave the job
retryable rather than complete the partition.
