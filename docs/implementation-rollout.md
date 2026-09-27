# Local-first implementation and Railway rehearsal

Date: 2026-09-25. Status: active local build. Increments 1A, 1B, and 2 are
accepted and included on `codex/market-screener-platform`; increments 3–11 are
queued.

## Goal and boundaries

Build and prove the complete product locally, then test the same application on
Railway before public launch. No Railway account or payment is needed now (P-14).
Jev, agent VMs, Redis, MongoDB, and a separate worker deployment are out of scope.
They are not prerequisites for the requested features.

Preserve P-01 through P-13: shared evidence and corrections, automatic updates,
private AI-editable research, existing financial semantics, and Next.js public
pages. This plan does not settle the open product choices in `product.md`.
No commits, pushes, account purchases, or public publication without owner approval.

## Smallest local setup

Signup/authentication, Prisma versus Python database ownership, and low-cost model
selection now have an explicit [decision brief](auth-database-agent-choices.md).
Vendor choices remain recommendations until approved.
P-15 excludes OpenAI models for the product agent; evaluate GLM, DeepSeek, Qwen,
and hosted Llama without adding GPU infrastructure by default.

- PostgreSQL with a persistent local volume: official facts, releases, jobs,
  users, workspaces, and agent-run state.
- A local S3-compatible test endpoint with persistent storage: captured evidence.
  Select and pin its implementation during setup; test the actual hosted storage
  separately before deployment. An emulator does not prove AWS compatibility.
- One Python application: FastAPI, automatic scheduling, ingestion, calculations,
  and bounded agent execution. Start with one Uvicorn process. Blocking parsing
  must not run on its async request loop; bound background execution and memory.
- Next.js: public pages and the private dashboard, both using the Python API.
- A real model API during AI acceptance tests; deterministic substitutes for
  repeatable failure tests. No GPU or local model hosting is required.

Run storage through Docker Compose and application development processes locally.
Use the same database major version, migrations, object keys, and API contracts
in the later deployment. Keep secrets outside Git and browser bundles. Bind local
data services to localhost; do not open them to the public internet.

Observed here: PostgreSQL 18.6 and SeaweedFS 4.47 passed real migration,
write/read, failure, restart, and clean-shutdown checks in increment 2. Docker
Desktop later became unavailable because Windows locked its local
`dockerInference` runtime entry; this is a machine issue, not an application test
failure. The preserved SQLite baseline is 591,175,680 bytes, the UI payload is
211,011,167 bytes, and the retained source cache contains 27,089 files. Full
details and hashes are in `local/baseline-2026-09-25/README.md`.

## Ownership and reuse

Existing sources feed normalization and screening, SQLite stores results, export
adds prices, and React loads the entire JSON then filters it. Preserve the existing
extractors, evidence loader, normalization, price application, and financial tests.
Extend the persistence boundary in `api/screener/store.py`; do not duplicate the
financial engine. Reuse React components and established filter/sort behavior.

Target: jobs alone publish shared data into PostgreSQL from retained objects.
FastAPI owns query semantics and private calculation evaluation. Next.js renders
API results. Validated agent tools read shared facts and edit only owned workspaces.
PostgreSQL JSONB holds configuration, not executable arbitrary Python or SQL.

## Testable requirements

1. Two clients receive identical shared values for a given data release; a private
   formula never changes those inputs. Check two browser sessions and API responses.
2. Scheduled jobs run without UI activity, survive restarts, and handle amendments,
   changed source responses, later comparative adjustments, and withdrawals.
   Check replay, crash recovery, stale job ownership, and source-outage cases.
3. Figure history retains old/new values, sources, dates, and known reasons;
   extraction fixes are not labelled company corrections. Check revision chains.
4. List, detail, filters, counts, sorting, and pagination use the API; no runtime
   dashboard JSON fetch remains. Compare full-universe results with frozen inputs.
5. Private workspaces survive restart and concurrent edits; AI research and edits
   work with real model calls, ownership checks, deterministic arithmetic, and undo.
   Check two users, unsupported formulas, hostile input, and interrupted streams.
6. Public Next.js company/editorial pages contain rendered content and metadata;
   private pages and data never leak through rendering, indexing, or public caches.
7. A restored database plus retained objects reproduces accepted outputs. Old
   clients cannot silently misread a new contract. Check restore and upgrade drills.

## Ordered increments

Current states are recorded in `local/market-screener-plan-2026-09-25.md`. Each
increment has one implementation owner for the listed boundary, followed by
independent review. Split any item exceeding a day's bounded
return before starting it. Proceed only after its check and real evidence pass.
The old working application remains available until the replacement passes parity.

### 1. Preserve a trustworthy baseline

State: accepted. The consistent SQLite backup, exact UI payload, and retained
source cache were hashed and verified. Engine 181 passed the full 6,676-company
regression, export, audit, and filing audit.

Owner: migration/testing; paths: `local/`, existing test entry points.
Coordinate with current UI edits before freezing a baseline. Inventory payload,
database, prices/FX, source cache, private notes/portfolios, and calculation settings.
Use SQLite's consistent backup mechanism, not a live file copy. Save hashes and
input versions, and prove the backup opens. Do not overwrite the current payload.
Run the existing baseline suite; record pre-existing failures separately.
Done: reproducible baseline manifest and usable backup, with missing evidence named.

### 2. Start local storage and define the contract

State: accepted. PostgreSQL, Alembic, and immutable verified objects passed real
failure, restart, read, migration, and clean-shutdown checks.

Owner: persistence; paths: local service configuration, `api/` storage and tests.
Check Docker readiness, RAM, and disk. Add repeatable startup, pinned dependencies,
ignored secret configuration, database migrations, and the object-storage client.
Design identity, artifact, observation, release, job, and owner keys before import;
implement only the schema needed by the next vertical slice.
P-17 settles database ownership: SQLAlchemy/Alembic in Python, with no second
Prisma-owned application schema in Next.js.
Done: database write/restart/read and object upload/hash/read/restart tests pass;
failed upload cannot create a usable evidence record. No old data is deleted.

### 3. Migrate one real company, then the retained universe

Owner: migration; paths: `api/` persistence/import and tests.
First prove one SEC company and one supported Japanese company through retained
evidence, PostgreSQL, derivation, and API response. Then run a restartable bulk import.
Reconcile identities, currencies, decimals, provenance, source bytes, and counts.
Keep historical gaps explicit; import cannot reconstruct unseen earlier revisions.
Done: repeated import is idempotent; all changed financial fields are explained by
evidence. Preserve SQLite and baseline for rollback; do not create two live writers.

### 4. Make ingestion durable and automatic

Owner: job lifecycle; paths: `api/` jobs, source adapters, and tests.
Implement schedules, persisted work/checkpoints, leases, retry limits, and bounded
source concurrency. Prioritize current updates over historical backfill.
Done: with all browsers closed, a due job publishes new data; forced restart resumes
without lost work; duplicate scheduling and expired owners cannot corrupt results.
Then add incremental discovery and source reconciliation as a separate checked slice.

### 5. Publish corrections and expose history

Owner: facts/publication; paths: `api/` selection, history, and tests.
Preserve source revisions, select compatible authoritative observations, derive
dependent values, and atomically publish releases with stale-publisher protection.
Done: amendment, later comparative restatement, withdrawal, partial amendment,
and parser-fix cases give correct latest results and honest old/new history.
Add the API history read path; its UI follows in increment 7.

### 6. Replace the dashboard file with query APIs

Owner: query contract; paths: `api/` API/query code and contract fixtures.
Implement list/detail/facets/status/history against PostgreSQL with release-bound
pagination and explicit null behavior. Preserve P/E/P/E3 maximum filter semantics,
financial grades, and existing Return Quality definitions P-11/P-13.
Done: every eligible company, count, ordering, filter result, and derived value
matches the frozen baseline or has an approved, evidence-backed difference.

### 7. Move the UI to Next.js and the API

Owner: frontend; paths: `web/` and frontend checks.
First preserve current React behavior in Next.js; then switch data reads to the
new API. Verify each slice independently. Add revision refresh and figure history.
Keep JSON only as a regression artifact, not a runtime dependency.
Done: real browser network checks show no dashboard JSON request; list/detail remain
coherent across a publication; old tabs reload safely when incompatible.
Add public company and editorial page types with metadata, sitemap and authored
sample content. Verify HTML without JavaScript; do not publish articles automatically.

### 8. Add private ownership, then personal formulas

Owner: private research; paths: `api/` auth/workspace/calculation and `web/` workspace UI.
Choose authentication and define ownership before saving private data. A local-only
test identity may support early slices but cannot pass the production-auth gate.
First deliver signup, verification, signin/logout, account lifecycle and API token
verification as a separately reviewed increment. Proposed provider: Clerk; hosted
identity during local development requires owner approval. Follow the decision
brief's two-user, webhook-replay, revocation, and authenticated-stream checks.
Migrate existing personal records only after confirming their owner.
Deliver workspace persistence/concurrency checks first, then the owner-approved
bounded formula functions, revision history, column edits, and undo.
Done: two users cannot read or edit one another's data; private formulas update
from corrected shared inputs and sort/filter over the entire eligible universe.

### 9. Add the research agent and streaming

Owner: agent lifecycle; paths: `api/` agent/tools/run storage and `web/` chat.
Proposed library: LangGraph inside Python, with PostgreSQL checkpoints. Application
code owns durable scheduling/recovery, cancellation, step/time/token budgets, and
tool permissions. A graph checkpoint alone does not resume an abandoned run.
Start with read-only research, then validated workspace edits under the chosen
confirmation policy. Use real provider calls for acceptance; never expose keys.
Evaluate the decision brief's low-cost model shortlist on real research and edit
tasks before choosing a default. Record cost per correct completed task, including
retry/tool costs; token price alone is not the acceptance criterion.
Done: the owner researches a ticker and creates/edits/removes a calculation/column;
inputs and evidence are visible. Replayed tools cannot duplicate edits. SSE
disconnect/reconnect resumes persisted events; API restart and cancellation are
tested independently. No agent writes shared fundamentals.

### 10. Prove the complete local flow

Owner: release verification; paths: checks and deployment configuration.
Run source discovery → retained artifact → facts/correction → release → API →
Next.js → private calculation → real AI edit. Test restore, source failures,
provider failures, authorization, expired releases, and old/new clients.
Measure RAM, CPU, disk growth, and normal API latency while jobs and 1/5/10 agent
runs execute; these are test points, not promised capacity. Keep loads bounded.
Done: requirements 1–7 have recorded evidence, no unexplained regressions, and
freshness/outage behavior is owner-approved. Package the tested applications.

### 11. Rehearse Railway privately, then launch

Owner: deployment; paths: deploy configuration and operational documentation.
Only now create accounts/provision resources using the checklist below. Test real
storage behavior, HTTPS/auth cookies, cross-origin policy, long SSE connections,
restart recovery, backups/restore, scheduled ingestion, and measured billing.
Run several automatic refresh cycles, including failure/recovery, before launch.
Public launch requires owner approval; a successful deploy is not a launch gate.

Historical backfill can start locally after durable ingestion passes, in bounded
batches. It must not delay current updates. Report ten-year coverage and gaps;
never describe an incomplete import as complete history.

## Financial regression gate and rollback

For every affected engine/extraction/evidence/pricing/profile/serialization slice:
preserve the exact pre-change UI payload, run `make regress` for the full universe,
rebuild through the actual UI-consumed path, run `make audit`, and run
`make audit-filings` where provenance/extraction changes. Also run relevant tests
and `make test`. On Windows use Git Bash/WSL or verified command equivalents.
Do not overwrite the baseline before comparison. Keep this gate working during
the API migration; compare new query outputs to the same frozen evidence.

Missing credentials, caches, prices, or baselines mean unverified, not passing.
Include row/security uniqueness, financial ratios, verdicts, source IDs, notes,
and UI-derived values. Roll back via retained data releases and compatible code;
schema changes use expand/migrate/contract, not destructive down-migrations.

## Railway setup later: minimum private rehearsal

Three services: Next.js, Python, and PostgreSQL with a persistent volume. One
private evidence bucket. No separate worker, cron service, Redis, or agent VM.
Keep the Python scheduler awake. Internal database traffic stays private.
Railway's PostgreSQL template is **unmanaged**: we own upgrades, backup setup,
monitoring, and restore testing. It is not equivalent to RDS management.
[Railway PostgreSQL](https://docs.railway.com/databases/postgresql)

P-03 says S3. AWS S3 satisfies it directly. Railway offers S3-compatible buckets,
which could keep everything with one vendor, but substituting those for AWS S3
requires owner confirmation and compatibility tests. Local emulation changes
neither the production decision nor the requirement to retain source revisions.

Prices checked 2026-09-25: Hobby has a $5 monthly minimum including $5 usage,
with a 5 GB volume limit. Pro has a $20 minimum including $20 usage and higher
limits. These are floors, not the app's total bill. Railway buckets cost
$0.015/GB-month and service egress costs $0.05/GB.
[Railway pricing](https://railway.com/pricing)

Planning allowance: roughly $25–50/month for a small always-on deployment, not
a measured quote. Confirm from local resource measurements and the private pilot.
AI tokens, domain, taxes, external data providers, AWS S3 if selected, extra backup
copies, and heavy backfill are additional. Local hosting costs $0 in cloud fees;
real AI/provider calls may still cost money. Do not subscribe before readiness.

Bucket downloads and API operations have no separate Railway charge, but uploads
from a Railway service count as that service's outbound traffic. Stored backups
and retained revisions also consume storage. [Bucket billing](https://docs.railway.com/storage-buckets/billing)

Railway closes active HTTP requests after 15 minutes, or after five minutes with
no transferred data. Design SSE with heartbeats and reconnect/replay; an AI run
must outlive its connection. Verify a run spanning multiple connections in the
actual deployment. [Network limits](https://docs.railway.com/networking/public-networking/specs-and-limits)

### Owner account checklist, deferred until increment 11

1. Create a Railway account, secure sign-in, and create a private test project.
2. Approve billing after the measured sizing check. Hobby may fit a private pilot;
   upgrade if actual database/index/WAL needs exceed its limits. Recheck prices.
3. Authorize only the `market-screener` GitHub repository when connecting it.
   Deploy an explicitly approved code revision, not unrelated uncommitted changes.
4. Choose AWS S3 or approve Railway-compatible storage. Keep bucket access private.
5. Enter credentials through the platform secret settings, never chat or Git.
6. Enable backups and billing alerts, run a restore, and test before adding a
   public domain. A hard spending stop can cause downtime; choose it deliberately.

## What the owner needs now

No hosting account. First engineering action: increment 1, then verify Docker
Desktop/WSL readiness for local storage. No installers or services were started by
this planning work. Before real AI tests, choose the provider and authorize a small
test budget; a consumer chat subscription is not assumed to cover API calls.

Ask remaining product questions just before their dependent increment: freshness
and outage behavior before automatic publication; formula function set and edit
confirmation before personal AI edits; provider funding and authentication before
multi-user AI. None needs to block the baseline inventory.
