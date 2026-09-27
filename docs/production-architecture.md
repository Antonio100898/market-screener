# Market Screener architecture

Status: proposed design, rewritten from the owner's requirements on 2026-09-25.
The [product index](../product.md) owns accepted decisions. This document proposes
how to implement them. S3, PostgreSQL, durable ingestion, and personal AI editing
are target capabilities, not claims about the current local application.

## System boundary

One Python service serves FastAPI requests and runs scheduled jobs. PostgreSQL
stores shared financial data, publication state, jobs, and private workspaces.
S3 retains captured source documents. Next.js provides the React frontend and
requests filtered results from FastAPI (owner decision P-12).

```text
SEC / EDINET / other approved sources
                  |
          automatic source jobs
                  |
             S3 revisions
                  |
       extract -> validate -> select facts
                  |
       PostgreSQL shared official data
                  |
       standard metrics + published revision
                  |
       personal formula evaluation <--- private definitions <--- dashboard AI
                  |
              FastAPI
                  |
         Next.js / React clients
```

All users share official inputs. A private formula changes a derived answer, not
the reported fact. Quotes, FX, and standard ratios are shared inputs/derivations
with their own provenance; they are not presented as official reported figures.

Initially use one continuously running service instance, one web process, bounded
I/O threads, and bounded child processes for heavy computation. Reserve resources
for HTTP traffic. A sleeping or request-only host cannot meet automatic refresh
requirements. S3 and PostgreSQL are managed infrastructure, not extra Python apps.

Keep ingestion, storage, fact selection, evaluation, and HTTP handling in separate
modules in the same codebase. A separate worker deployment is unnecessary initially;
it remains possible if measured ingestion load harms API responsiveness. No Redis,
message broker, or microservice split is required for the first implementation.

## Source records and corrections

There are three distinct things to track:

1. A filing identity: SEC accession or EDINET document ID.
2. A captured resource revision: the exact bytes downloaded at a given time.
3. A financial observation: a value reported for a defined period and scope.

Do not use ticker or last filing date as the identity of a filing. Keep stable
issuer and security IDs, with source identifiers and time-aware ticker mappings.

An amendment may add a new filing. A later normal filing may restate comparative
years. A source may correct metadata, change access status, or remove a resource.
Some amendments change no financial values. Therefore neither “newest document
replaces all facts” nor “stored document IDs never need checking” is correct.
Source evidence and references are in [shared data](../product/shared-data.md).

SEC Company Facts and submissions JSON are mutable source aggregates, not
immutable individual filings. The SEC updates these APIs as filings disseminate.
Capture their responses by hash and retain per-fact accession provenance; fetch
the original filing evidence needed to reproduce accepted values.
[SEC API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)

## Incremental ingestion and reconciliation

Bootstrap the approved universe once, then run independent jobs for:

- Discovery of new filings and source status events.
- Fetching resources that are new, changed, missing, or previously unavailable.
- Reconciliation of known source inventories and metadata.
- Extraction and re-selection of affected facts.
- Quote, FX, and security metadata refresh.
- Historical backfill in bounded batches.
- Re-derivation after parser or calculation changes, using retained S3 evidence.

Use a persisted source cursor plus a configurable recent overlap window. Deduplicate
by source identity and resource revision. A discovered document can remain pending
until its downloadable files or aggregate facts become available. Advance discovery
only after each discovered item has a durable outcome or pending work record.

For SEC, reconcile rebuilt full/quarterly indexes as well as recent submissions.
The SEC notes that older daily indexes do not reflect later removals and that
full/quarterly indexes are rebuilt weekly. Reconcile the covered historical
partitions, not only the current quarter.
[SEC source-change guidance](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)

For EDINET, process operation events, parent-document links, withdrawal/edit/
disclosure states, and recheck affected original-date metadata. Reconcile older
covered dates on a bounded rolling schedule. Distinguish source retention expiry
from a withdrawal; do not infer financial invalidity from a missing download alone.
[EDINET API specification](https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf)

Use reliable conditional HTTP validators where available. Otherwise compare
metadata and periodically validate relevant resources. A content hash deduplicates
stored bytes, but cannot prove a remote resource is unchanged without checking it.
Some verification downloads are unavoidable when the source offers no change token.
Record this cost separately from ordinary new-filing ingestion.

Schedules, overlap windows, and reconciliation horizons are configuration values.
Proposed starting policy: intraday discovery, a daily catch-up, weekly SEC index
reconciliation, and budgeted historical metadata checks. Actual intervals require
an owner-approved freshness target and measurements against provider limits.

Current updates have priority over backfill. Historical jobs yield between batches;
they cannot occupy the entire source request budget or delay daily discovery.
Ten-year coverage records available fiscal periods, gaps, and unsupported sources.
It is not a promise that ten years of every document type can be downloaded.

## S3 contract

Keep private objects at content-addressed keys, such as `raw/sha256/<hash>`.
PostgreSQL maps source identity and resource path to each observed hash. Record
retrieval time, source URL without secrets, media type, byte size, and HTTP validators.

Write and verify the object before committing an artifact row that ingestion may
use. An upload and PostgreSQL transaction are not one atomic operation: retries
must tolerate an uploaded orphan or an existing identical object. Reconcile orphans
after a grace period; never remove referenced evidence as temporary data.

A parser version and artifact hash identify an extraction attempt. Preserve raw
values and exact source locations. Replacing a parser does not overwrite the source.
Retain historical evidence for reproducibility, but restrict withdrawn/non-public
documents from public serving. Retention/access rules must accommodate source
restrictions; S3 retention is not permission to republish every captured byte.

## PostgreSQL model

Start with the tables needed for these responsibilities, reusing current payload
contracts where possible. This is a logical model, not a mandate for one table per
bullet or a complete XBRL warehouse before the first usable API.

| Responsibility | Stored records |
|---|---|
| Identity | issuer, security, source identifier, listing history, identity evidence |
| Evidence | filing, source status event, artifact revision, extraction run, fact observation |
| Fact selection | selected observation, statement basis, supersession/rejection reason |
| Shared market inputs | quote observation, FX observation, price history, provider status |
| Derived data | standard metric definition, financial snapshot, evaluated snapshot |
| Publication | immutable data release, release membership, active-release pointer |
| Operations | schedule occurrence, job, job item, checkpoint, bounded job events |
| Private research | user, workspace, definition revision, column layout, AI edit audit |
| Existing private features | tracking, notes, portfolios, trades, manual assets and cash |

Use PostgreSQL NUMERIC for exact source decimals and financial ledger amounts.
JSONB holds nested payloads and formula trees; typed fields cover query keys,
constraints, provenance links, and commonly filtered metrics. Store decimal values
in API contracts without accidental binary rounding; presentation may round them.

Keep distinct source observations even when they map to the same canonical metric.
A uniqueness constraint on canonical concept/period alone would discard conflicting
tags or duplicate-context evidence. Identify an extracted observation by artifact,
extraction revision, and source occurrence/context; resolve duplicates explicitly.

## Selecting the latest adjusted facts

The selector owns the default current answer, not React or the AI. It retains all
observations and records why one is selected. A selection key includes issuer,
concept, period start/end or instant, currency/unit, dimensions, consolidation
scope, accounting basis, and security/share basis where relevant.

For each new or changed filing:

1. Extract its current and comparative periods with original provenance.
2. Identify explicitly replaced statements, corrections, and non-reliance notices.
3. Compare only equivalent observations. A quarterly or segment value cannot
   replace annual consolidated data merely because it was filed later.
4. Prefer the latest valid authoritative observation for that basis. Keep validated
   checks for known scale/tag/context errors; timestamp ordering alone is insufficient.
5. Apply partial amendments only to their stated scope. Absent replacement facts
   do not delete still-valid earlier values.
6. Rebuild coherent statement groups when a restatement changes scope or basis.
   Preserve unchanged dependencies when compatible; otherwise mark a conflict.
7. Recompute dependent historical metrics, current ratios, and personal outputs.

Record reporting period, source publication/acceptance time, first observed time,
and selected-in-release identity. This preserves both latest-restated research and
the evidence known at an earlier date. A retained decision snapshot must not be
silently rewritten with hindsight; default live research uses the new selection.

A non-reliance notice can invalidate an input before replacement numbers exist.
Mark affected inputs and results unavailable/pending review rather than guessing.
Reviewed supplements remain separate attributed inputs; newer contradictory
evidence requires revalidation. Personal assumptions never overwrite this layer.

### Historical change history

P-09 requires a user-visible history, not only retained files. Reuse immutable
observations, artifact revisions, and release selections. Record a change linking
the previous and replacement observation or invalidation, its cause category,
source event, detection time, and activation release. Preserve the full chain.
Use a stable transition identity so retries cannot duplicate a history entry.

Publish the change record with the changed selection. Keep discovered changes
pending until validated; do not show an unaccepted candidate as the current value.
Separate a source revision from an extraction fix or a formula revision. An
unchanged number can still receive new provenance; do not call that a numerical
adjustment. Link a derived ratio's change to its input or definition revisions.

Expose history for a security, metric, and financial period through the API and
the figure's UI. Return before/after values, units and scope, comparable difference,
both source references, source/detection/activation times, and any filing-backed
explanation or adjustment components. Missing explanations remain unknown. Mark
unavailable earlier evidence and respect document access restrictions. These records
must survive pruning of transient browsing releases.

Acceptance cases are defined in
[shared data](../product/shared-data.md#historical-change-history).

## Standard metrics and personal calculations

Reuse the existing engine and its provenance/missing-data rules. Preserve strict
stored results and keep disclosed zero-assumption views separate. Basic shared
ratios have named definitions, units, period selectors, input IDs, and engine revision.

Do not turn the owner's Graham and Return Quality choices into unchangeable global
policy. Expose them as identifiable existing/default calculation sets. Users can
derive private definitions without changing the default or official inputs.

The mandatory [personal-calculation design](personal-calculations.md) defines the
dashboard AI, bounded formula language, validation, revision history, and execution.
P-10 also requires read-only company research and ad hoc calculations through the
agent. Store private workspace configuration in PostgreSQL JSONB with typed owner
and revision fields; provider credentials stay separate. User-funded versus
service-funded AI connections remain an investigation, not an assumed capability.
Its boundary must exist in the first schema and API design. The platform is not
feature-complete until the agreed first set of AI edits works end to end.

## Publication and client freshness

A release pins selected fact/snapshot revisions, quote and FX observations,
security identity, and standard metric engine revision. Unchanged records can be
referenced from a new release; there is no need to duplicate raw evidence hourly.

Build changed results privately, validate, and switch one active-release pointer
in a short transaction. The publisher compares the expected parent revision. If
another job has published meanwhile, rebase and revalidate the candidate; do not
overwrite newer data with an older candidate. Initial publication is serialized.

List, detail, facets, exports, and personal calculations carry the release ID.
Cursors bind release, query, sort, tie-breaker, and private workspace revision.
Requests for an expired release return an explicit restart response. Retention
for transient browse releases is bounded; saved decision evidence has separate
retention so pruning a browsing release cannot break a portfolio audit record.

The browser learns of new releases through lightweight polling initially, checks
again on focus/reconnect, and refreshes the entire current query coherently. It
does not fetch new pages against a different release or trigger source refreshes.
Historical views are labelled; default live views must not stay pinned indefinitely.

Publication consistency is not freshness. Also expose per-source checked-through
time, discovered-but-unprocessed changes, and per-company input age. A lightweight
current validity status can invalidate an older browse release when a correction
or withdrawal is known; it must not substitute newer numeric values into that
release. The UI then shows pending/unavailable state and refreshes.

The owner must approve freshness budgets and outage behavior before production.
No implementation can guarantee zero lag or know a correction before the source
discloses it. Never describe an old validated release as current merely because
the ingestion job failed to publish a new one.

## Durable jobs in one service

Store job kind, parameters, status, priority, due time, attempts, next retry time,
owner, lease expiry, ownership generation, heartbeat, checkpoint, and error summary.
Schedule occurrences have a unique key so leadership changes cannot enqueue the
same occurrence twice. Persist job items when batches may finish out of order;
a highest-seen document ID is not proof all earlier work completed.

Claim a job with a short PostgreSQL transaction using row locking. Release the
database lock before network or CPU work. Renew the lease while progressing.
Each result/checkpoint transaction checks the ownership generation. An expired
worker cannot commit after a replacement claims the job. Recover expired jobs
periodically while the API is running, not only at process startup.

Commit completed work and its checkpoint together. Use at-least-once execution
with idempotent writes; a crash between upload and checkpoint can repeat work.
Retries have bounded exponential backoff, jitter, deadlines, and visible failure.
Malformed documents have per-item outcomes so one failure does not erase a batch.

If using a session advisory lock for scheduler leadership, hold a dedicated direct
database session, monitor loss, stop scheduling on loss, and retry acquisition.
Do not assume a transaction-pooled connection holds a session lock reliably.
Unique schedule keys remain necessary even with leadership.
[PostgreSQL advisory locks](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS)

Bound source concurrency and enforce provider limits across current jobs and
backfills. Keep source clients, keys, and request budgets on the server. Drain jobs
on shutdown; resume from checkpoints after forced termination. HTTP request
background tasks are not the persistence mechanism for this job system.

## API and storage queries

Proposed endpoints:

```text
GET  /api/v1/capabilities
GET  /api/v1/data-status
GET  /api/v1/companies?release=...&workspace_revision=...&max_pe=...&cursor=...
GET  /api/v1/companies/{security_id}?release=...
GET  /api/v1/companies/{security_id}/evidence?release=...
GET  /api/v1/facets?release=...&workspace_revision=...
GET  /api/v1/me/workspaces/{id}
POST /api/v1/me/workspaces/{id}/ai-edits
GET  /api/v1/me/calculation-runs/{id}
```

Apply authentication and ownership checks to every private route and query.
Keep operational mutations administrative. React requests never start source
ingestion; user formula execution is a different, permitted operation.

Preserve current filter semantics, null ordering, multi-column sorting, counts,
and strict versus assumption views. Positive maximum-P/E filters must exclude
missing and non-positive values. Stable security IDs break sort ties.

Add indexes for measured queries, starting with release/security, common filters,
source document identity, source revision, job due/lease state, and owner/workspace.
Query shared rows directly first. Cache keys include all data/engine/formula/view
revisions plus authorization scope. Cache deletion cannot lose authoritative data.

## Compatibility and deployment

Frontend and backend ship together initially, but open tabs can lag. Keep build,
API contract, database schema, data release, metric engine, formula-language, and
workspace revisions distinct. See [compatibility](../product/compatibility.md).

Generate client types from the API contract and test supported old/new client
fixtures. The capability response declares API support, formula-language support,
available fields, and reload requirements. Breaking changes get an explicit
contract transition; unsupported clients must not render misinterpreted responses.

Use expand/migrate/contract database changes: add compatible schema, deploy code,
migrate/reconcile data, and remove obsolete schema after old readers are retired.
Run migrations once per deployment. Keep rollback-compatible code until the
transition completes. Recompute semantic engine changes into a new data release.

Use one PostgreSQL database, S3, the Python service, and a Next.js deployment
with server-rendering support. The Python backend remains one service; Next.js adds
a frontend server runtime or managed equivalent. Provider choice and cost are
separate deployment decisions.

P-14 requires local implementation and testing first. Follow the
[incremental rollout](implementation-rollout.md); do not provision Railway to
start development. Railway's PostgreSQL template is unmanaged: backups, upgrades,
and recovery remain our responsibility, unlike a fully managed database offering.
[Railway PostgreSQL](https://docs.railway.com/databases/postgresql)

The Next.js public site includes company pages, blogs/articles, educational guides,
and product/use-case pages for discovery around fundamentals and agent-assisted
financial research (P-12). Serve useful rendered HTML with metadata, canonical URLs,
internal links, and sitemap entries. Editorial content carries authorship, sources,
and update dates. Static generation can serve editorial pages; dynamic financial
pages follow the shared data freshness policy. Choose authoring/publishing tooling
separately; a new CMS or automatic publishing is not implied by this requirement.

Next.js server rendering of financial data reads the same versioned FastAPI
contract as browser interactions, not a separate database path. Public page caches
must be refreshed or invalidated on data publication within the agreed freshness
budget. Include the rendered data revision so client hydration stays consistent.
Keep private workspaces authenticated, outside public caches, and non-indexable.

Agent execution remains in Python; Next.js does not need to host another agent
loop. Browser SSE connections terminate at FastAPI through the deployment's routing;
any proxy must permit streaming without buffering and support the run's connection
lifetime. UI reconnection and agent-run recovery remain separate responsibilities.

Back up PostgreSQL with tested recovery and retain referenced S3 revisions.
Rehearse a consistent restore across both stores. Monitor source lag, pending
changes, failed jobs, lease expiry, publication age, invalidated evidence,
formula failures, and HTTP latency. Separate readiness from freshness.

## Migration and verification

1. Freeze the exact current UI payload and its evidence/price inputs. Inventory
   current SQLite records, cache paths, filters, calculation definitions, and
   private data. Record reuse: `store.py` owns persistence; `EvidenceLoader`,
   `normalize.py`, and `sync.py` own existing extraction/derivation; `App.jsx`
   and `sort.js` own current query/display behavior. Preserve their contracts.
2. Add PostgreSQL and S3 behind the existing boundaries. Migrate with identity,
   decimal, count, provenance, and content-hash reconciliation. Keep SQLite only
   for transition unless offline mode is explicitly requested.
3. Implement durable automatic discovery, source reconciliation, corrections,
   and versioned publication. Compare against the frozen inputs, not moving quotes.
4. Migrate the frontend from Vite to Next.js, reusing React components and existing
   calculations. Switch to API filtering with contract/parity tests and automatic
   revision refresh. Verify public rendered HTML and private-route/cache isolation.
   Preserve the existing calculated view during the transition.
5. Deliver authenticated workspaces and the agreed AI formula/column editing scope.
   Preserve existing owner research as named definitions and attributed private data.
6. Deploy after recovery/freshness tests; expand history through resumable backfill.

Each stage preserves P-02/P-04/P-07 and the existing financial evidence invariants.
The first usable shared-data milestone is not completion of mandatory P-06.

Required checks include the repository's full payload regression and filing audits
for affected engine/data changes, plus correction and non-reliance scenarios,
unchanged-resource deduplication, out-of-order job items, stale-owner rejection,
publication races, versioned pagination, automatic browser refresh, private formula
isolation, full-universe custom sorting, old-tab compatibility, and restore rehearsal.
Record missing data or credentials as unverified checks, never as passing results.

## Decisions still needed

The structure above does not require a daily reload from zero or separate worker
deployment. The remaining owner choices are freshness/outage policy, supported
source scope, the first personal function set, and the AI edit confirmation policy.
These are tracked in [product.md](../product.md), not silently chosen by code.
