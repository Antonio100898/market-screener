# SEC incremental discovery review map

Developer state: **IDLE — accepted after Review 1**
Current item: **SEC discovery and reconciliation investigation**
Review state: **ACCEPTED — Review 1**

The investigation report belongs under `## Developer update`. The reviewer owns the header and
reviewer response.

## Developer update

### Finding

The SEC handler must not land on the accepted tables alone. They can retain verified bytes and
per-job outcomes, but they cannot say which SEC resource produced those bytes, preserve mutable
metadata/status revisions, or prove a filing/resource became pending, unavailable, or removed.
The smallest correct next increment is two PostgreSQL source-observation tables plus one repository.
The SEC handler follows as a separate increment. Neither slice selects a current snapshot.

### Current SEC path and reuse seams

- `sync.daily` reads daily form indexes, collapses all matching filings for one CIK to one date,
  writes only `last_daily_index`, and then refetches Company Facts for affected issuers
  (`api/screener/sync.py:2432-2487`). A failed day is skipped, yet a later successful day can move
  the cursor beyond it (`api/screener/sync.py:2443-2463`). Accession, form, archive filename, and
  per-item outcomes are lost. There is no rebuilt quarterly/full-index reconciliation.
- SEC transport is reusable. One `EdgarClient` instance enforces the SEC request interval across
  its threads and retries network/429/5xx failures (`api/screener/sources/edgar.py:31-51`,
  `api/screener/sources/edgar.py:92-114`). `company_facts` and the ticker map use a TTL filesystem
  cache that overwrites the prior JSON; `submissions` is fetched without retained revisions
  (`api/screener/sources/edgar.py:53-79`). Live ingestion must reuse transport, not its cache as the
  record of source history.
- Submissions parsing can be reused for filing metadata, identity, cover discovery, and material
  event interpretation (`api/screener/sync.py:1635-1699`, `api/screener/sync.py:1735-1789`,
  `api/screener/sync.py:1868-1889`). The current submissions response itself is not retained.
- Filing-resource helpers already validate accession/path identities, enumerate Inline-XBRL files,
  hash exact bytes, and verify a complete annual statement before activating its local manifest
  (`api/screener/sync.py:1792-1915`, `api/screener/sync.py:1918-2083`). Cover parsing and security
  matching remain reusable (`api/screener/sync.py:2186-2428`). The current code treats a verified
  manifest or stored cover accession as permanently reusable, so it cannot record a same-identity
  resource or metadata revision (`api/screener/sync.py:1930-1936`,
  `api/screener/sync.py:2249-2264`).
- `ImmutableObjectStore` and `store_evidence` are the correct retained-byte boundary: content goes
  to `raw/sha256/<hash>`, is read back and verified, then receives an artifact row
  (`api/screener/object_store.py:27-95`, `api/screener/artifacts.py:31-67`). Network calls must finish
  before the database observation transaction.
- `EvidenceLoader` plus `sync._derive_evidence` remain the single extraction/derivation path
  (`api/screener/evidence.py:70-137`, `api/screener/sync.py:520-551`). `CompanyImporter` proves how
  retained SEC artifacts feed that path (`api/screener/company_import.py:190-280`), but it is a
  migration tool and calls `store_and_select`, which immediately changes the current snapshot
  (`api/screener/company_import.py:138-175`, `api/screener/shared_companies.py:130-161`). Discovery,
  fetch, and extraction jobs must not call it before increment 5 owns release selection.

### Why the accepted schema is insufficient

- `evidence_artifact` identifies content only. It has no SEC item/resource key, URL, validators,
  retrieval time separate from verification, source status, or revision chain
  (`api/screener/postgres.py:31-47`).
- `company_snapshot_artifact` links bytes only to a derived snapshot and role
  (`api/screener/postgres.py:186-212`). It cannot represent evidence discovered before a valid
  snapshot, a delayed download, unchanged facts with changed source metadata, or a removal.
- `durable_job_item` is scoped to one job and allows only succeeded/failed. A retry updates that
  row in place (`api/screener/postgres.py:355-387`, `api/screener/durable_jobs.py:346-411`). It is
  operational evidence, not immutable cross-job source history.
- `current_company_snapshot` is a mutable selection pointer (`api/screener/postgres.py:214-236`).
  Using it for discovery would publish before the correction/history rules exist.

### Smallest schema slice

Add Alembic revision `20260929_0006_source_observations.py`, matching metadata in
`api/screener/postgres.py`, and `api/screener/source_observations.py`.

1. `source_item`: stable `item_id`; `source_system`; constrained `item_kind` (`inventory`,
   `aggregate`, `filing`, `resource`); stable `source_key`; optional issuer source identifier;
   optional parent item FK; creation time; unique `(source_system, item_kind, source_key)`.
   Examples are `daily-index/2026-09-29`, `submissions/0000012345`, an SEC accession, and
   `<accession>/<archive-path>`.
2. `source_observation`: immutable identity; source-item FK; canonical `observation_sha256`;
   detected time; optional SEC publication/change time; constrained state (`present`, `pending`,
   `unavailable`, `removed`); exact normalized metadata JSONB; optional artifact hash FK; URL
   without secrets; optional ETag and Last-Modified; detecting durable-job FK; unique
   `(source_item_id, observation_sha256)`. The hash covers state, metadata, artifact identity,
   validators, and source time, but not detection time. Duplicate delivery reuses one revision;
   changed bytes, metadata, or state creates another immutable revision.

No separate cursor table is needed yet. A discovery job's checkpoint is the persisted cursor; the
next occurrence reads the latest successful checkpoint and starts from a configured overlap. A
checkpoint advances only after every partition item has an observation/outcome or a durable child
job. Existing schedule-occurrence uniqueness can identify child work with a stable key containing
the source item and observation hash. This avoids a second job identity mechanism.

The repository must validate the live lease in the same transaction that inserts/reuses the source
item and observation and records its job-item outcome. Upload and HTTP work stay outside that
transaction. An expired generation writes nothing. Do not add a current-observation pointer: the
ordered immutable revisions are sufficient until increment 5 defines authoritative selection.

Focused schema checks:

- Unit: identity/status/JSON/hash validation; canonical duplicate reuse; changed metadata/state
  makes a new revision.
- Real PostgreSQL: two jobs race the same observation and store one row; `pending -> present ->
  removed` remains three revisions; changed aggregate metadata with unchanged bytes remains visible;
  stale/expired lease stores neither observation nor job outcome; migration from `0005`, empty
  migration, `alembic current/heads/check`.
- Real S3: verified upload/readback can be linked; failed or mismatched upload creates no usable
  observation. Reuse the existing storage fixtures in
  `api/tests/integration/test_artifact_storage.py:28-68`.

### Next SEC handler slice

After the schema is accepted, add `api/screener/sec_ingestion.py` and focused fixture tests. Keep
four job kinds and boundaries:

1. `sec-recent-discovery` (highest SEC priority): parameters are an explicit bounded date range and
   overlap; item key `daily-index:<date>`. Store the index revision, parse full filing identities,
   record new/amended/changed filing metadata, and enqueue stable fetch work before checkpointing.
2. `sec-index-reconciliation` (below current discovery; historical partitions below current-quarter
   reconciliation): parameters identify one full/quarterly rebuilt partition; item key
   `quarterly-index:<year>:<quarter>`. Compare only that proven partition and record removed or
   changed filing observations. Absence outside the fetched partition proves nothing.
3. `sec-resource-fetch`: parameters identify one source item/observation and bounded resource set;
   item keys are `<accession>/<safe-path>` or `companyfacts:<CIK>` / `submissions:<CIK>`. Store and
   verify bytes first, then atomically record `present`; record `pending`/`unavailable` without
   pretending a missing download is a removal. Duplicate delivery reuses the observation/artifact.
4. `sec-candidate-extraction`: consumes only verified retained artifacts through the existing
   Inline-XBRL/cover/EvidenceLoader/derivation code and stores a non-current candidate in the later
   publication model. Do not implement this job in the discovery slice and do not call
   `store_and_select`.

One process-owned SEC ingestion object must create one shared `EdgarClient` and serve both recent
and reconciliation handlers. Run SEC kinds through one runtime worker initially: concurrency is
bounded at one, the existing per-instance rate limit covers every request, and database priority
guarantees current jobs run before the next backfill partition. Backfill is split into one bounded
partition per job, so it yields between partitions. No interval, overlap length, or reconciliation
horizon is chosen here.

Fixture proof before any live call:

- New filing and `/A` amendment create distinct filing observations and fetch jobs.
- Same daily/submissions/Company-Facts payload delivered twice creates no duplicate.
- Same aggregate identity with changed bytes or metadata creates a new revision.
- Filing index visible before a document is downloadable records pending; retry later links exact
  verified bytes without losing pending history.
- Rebuilt quarterly index removal records removed; a daily-index omission does not.
- Crash after observation/child enqueue but before checkpoint repeats safely; restart with overlap
  loses no item; expired owner cannot record a late outcome.
- Offline real PostgreSQL/S3 run processes the fixtures twice and after restart with identical
  counts and byte-exact artifact readback. No API/current-snapshot change is expected, so the full
  UI payload gate is not triggered unless extraction or serialization changes.

No owner decision blocks these two slices. The open freshness/outage decision blocks production
schedules and automatic publication only.

No production code, database/cache, service, or external API was changed or called. Only this
Developer update was edited.

READY_FOR_REVIEW

## Reviewer response

### Review 1 — accepted

Accepted. Content-addressed artifacts, snapshots, and mutable job-item outcomes cannot represent SEC
resource identity or revision history. Build the two-table source-observation contract before any
SEC handler. The write API must keep live-lease validation, source-item/observation insertion, and
the job-item outcome in one transaction without exposing a database connection to a handler. State
constraints must prevent a `present` resource observation without verified evidence. No current
source pointer, current company selection, source schedule, or publication belongs in this slice.
