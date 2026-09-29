# SEC resource fetch and candidate staging review map

Developer state: **IDLE — accepted after Review 1**
Current item: **SEC resource-fetch boundary investigation**
Review state: **ACCEPTED — Review 1**

The investigation report belongs under `## Developer update`. The reviewer owns the header and
reviewer response.

## Developer update

**Result: split resource fetch from candidate staging.** The smallest safe next build is SEC root
resource fetch only. It needs no schema change and no owner decision. Candidate derivation follows
only after the resource graph is durable and replay-safe.

### Reuse and fetch map

- Discovery already gives the stable child identity and all five parameters: accession, CIK, form,
  archive filename, and filing observation ID (`api/screener/sec_ingestion.py:247-265`,
  `api/screener/sec_ingestion.py:439-457`). The handler must first load that observation and prove it
  is the latest `present` SEC filing with the same accession, CIK, form, and filename. Child JSON is
  not authority by itself.
- Reuse `EdgarClient.fetch()` for exact bytes and validators without the legacy JSON/cache write path
  (`api/screener/sources/edgar.py:79-88`). Reuse `store_evidence()` for content-addressed S3 plus the
  verified artifact row (`api/screener/artifacts.py:60-67`). Reuse `SourceObservationRepository.record()`
  for lease-guarded immutable states and job outcomes (`api/screener/source_observations.py:119-190`).
- The first, graph-defining resource set is exactly:
  1. complete submission text from `https://www.sec.gov/Archives/{filename}`;
  2. accession inventory `https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession-without-dashes}/index.json`;
  3. mutable submissions aggregate `https://data.sec.gov/submissions/CIK{cik}.json`;
  4. mutable Company Facts aggregate `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json`.
  Store each as its own `resource` or `aggregate` source item. The two accession resources are
  immutable identities. The two issuer aggregates can produce many observed hashes over time; SEC
  documents that they are mutable, and the architecture requires that distinction
  (`docs/production-architecture.md:52-64`).
- After those four are present, the accession inventory and submissions metadata define the bounded
  leaf set: the primary filing document; only listed `R1.htm` through `R3.htm` cover reports; and, when
  present, `FilingSummary.xml`, one extracted `*_htm.xml` instance, one issuer `.xsd`, and zero or one
  `*_pre.xml` presentation file. The accepted pure selectors are `_inline_documents()` and
  `_archive_base()` (`api/screener/sync.py:1804-1843`). For an annual wrapper that explicitly
  incorporates a 6-K, reuse `incorporated_annual_source()` and fetch that accession's index and same
  leaf roles (`api/screener/sources/inline_xbrl.py:146-207`).
- Cover parsing is generic: `cover.securities()` plus `cover.depositary_ratio()` reads the same primary
  or rendered bytes for any issuer (`api/screener/sources/cover.py:299-374`,
  `api/screener/sources/cover.py:460-483`). Inline parsing is also generic across 10-K, 20-F, and 40-F;
  its five roles are explicit (`api/screener/sources/inline_xbrl.py:24-26`,
  `api/screener/sources/inline_xbrl.py:210-268`). No company-specific extractor belongs here.
- The manifest is derived metadata, not a remote SEC resource. Build canonical manifest bytes from
  the observed artifact hashes and retain them as derived evidence. The existing manifest format and
  canonical encoder can be reused (`api/screener/sources/inline_xbrl.py:271-307`), but its local-path
  verifier cannot be the new storage boundary.

### State and retry contract

- `present`: exact response bytes reached S3, readback passed, and the artifact observation committed
  under the live lease. Content changes create a new observation; replay reuses the same hash.
- `pending`: a discovered filing's root or index-listed leaf returns 404, Company Facts/submissions do
  not yet name the accession, or the filing graph is incomplete. Record the reason and retry the job.
  Do not produce an empty candidate.
- `unavailable`: only a valid, complete index proves an optional resource does not exist or a bounded
  terminal source response proves it is unsupported. A 404 during dissemination is not this state.
- Network/429/5xx errors retry. Malformed bytes may be retained as the exact `present` source revision,
  but graph validation fails and the job stays retryable. No removal, checkpoint, candidate, or success
  is inferred from 404, partial JSON, or partial filing files. This preserves the pending rule in the
  architecture (`docs/production-architecture.md:84-87`).

### Candidate boundary

The current parser and calculation owners remain reusable, but `EvidenceLoader` cannot be called by
the new worker: it reads SQLite and local files (`api/screener/evidence.py:70-146`). The later staging
slice should add one byte/artifact-backed evidence assembler, then call the existing
`sync._derive_evidence` owner. LLM extraction is called only when the complete deterministic bundle
returns an explicit unsupported parsing gap. It is never called for missing, pending, or malformed
source delivery.

`company_snapshot` can hold a non-current candidate because selection is a separate table
(`api/screener/postgres.py:105-184`, `api/screener/postgres.py:214-236`). No publication schema is
needed in this increment. The smallest repository change is a lease-guarded
`SharedCompanyRepository.store_candidate(...)` that reuses issuer/security/snapshot/artifact inserts
but does not call `_select_current()`. The present `store_and_select()` always changes the current
pointer (`api/screener/shared_companies.py:130-169`, `api/screener/shared_companies.py:367-389`) and is
therefore unsafe for staging. Increment 5 still owns selection, releases, stale-parent protection, and
history.

### Build and checks

1. **3B3a — root fetch:** add `SecResourceFetchHandler` and strict parameter/parent validation in
   `api/screener/sec_ingestion.py`; add the narrow observation lookup/validation in
   `api/screener/source_observations.py`. Test exact URLs and roles, aggregate revision, 404 pending,
   transient retry, partial index, duplicate replay, stop, and expired lease in
   `api/tests/test_sec_ingestion.py`.
2. **3B3a real gate:** extend `api/tests/integration/test_sec_ingestion_storage.py` with fixture
   transport plus real PostgreSQL/S3. Prove byte-exact readback, revision history, restart/retry,
   duplicate delivery, and that an expired worker leaves at most a verified orphan object.
3. **3B3b — leaf fetch and non-current candidate:** reuse the pure cover/Inline-XBRL selectors, add
   the artifact-backed evidence assembler and `store_candidate`, then test known-answer 10-K, direct
   20-F, and incorporated 40-F fixtures. Prove no `current_company_snapshot` change, idempotent
   candidate hashes, exact artifact roles, and no candidate from pending evidence. Run the normal full
   Python suite and the real PostgreSQL/S3 gate. An engine/evidence change additionally triggers the
   full UI regression and filing audits.

No owner decision blocks 3B3a or non-current staging. The unresolved freshness/outage decision blocks
production timing and automatic publication only.

**READY_FOR_REVIEW.** No production code, database, cache, service, or external API was changed.

## Reviewer response

### Review 1 — accepted

Accepted. Root fetch and candidate derivation are separate gates. The next build retains and
validates only the filing root, accession inventory, submissions aggregate, and Company Facts
aggregate. A 404 during dissemination remains pending and retryable. Candidate work must use a
byte-backed assembler and a non-current snapshot write; `store_and_select` is forbidden because it
would publish before increment 5 owns selection and history.
