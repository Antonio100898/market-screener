# EDINET archive fetch and non-current candidate

State: **ACCEPTED — independent review passed**

## Result

4B handles the `edinet-resource-fetch` job created by 4A. It rechecks the cited filing is the
latest present revision, requires a current JPX listing, fetches the exact type-1 archive, retains
it in S3, records a parented source observation, validates every ZIP member, maps through the
existing EDINET adapter, merges the current retained history, derives through the existing engine,
and stores one lease-guarded candidate.

A reusable archive checkpoint is written only after the whole ZIP and XBRL mapping pass. A process
restart reads those verified bytes from S3 without another EDINET request. A malformed 200 response
is retained as an observed revision but is not checkpointed; a retry can fetch corrected bytes.
Amendments, numeric and alphanumeric JPX codes, source replay, and expired workers are covered.

The candidate does not write `current_company_snapshot`. Publication, scheduling, freshness/outage
presentation, and selection remain outside 4B. If several same-issuer candidates accumulate before
publication, increment 5 must rebuild or rebase from all retained source observations before it
selects one; 4B never exposes those candidates as current.

This preserves P-02, P-03, P-04, P-05, P-09, and P-17. No schema, migration, engine version,
calculation rule, API, or UI contract changed.

## Proof

- Focused EDINET, importer, observation, and snapshot tests: **108 passed**.
- Real PostgreSQL/S3 and runtime tests: **27 passed**; the EDINET subset is **7 passed**. Exact S3
  readback, real mapper and default derivation, persisted restart without refetch, immutable replay,
  current-pointer preservation, and lease expiry before observation and candidate commits pass.
- Normal suite: **1,001 passed, 56 skipped**; web: **113 passed**.
- Full regression recomputed the **6,995-company** baseline. It reported 76 unrelated current-input
  changes: six mutable ticker-map updates, four cover/peer changes, and stored historical-price
  adjustments. No criterion or verdict moved, and 4B cannot enter that payload because it does not
  select a candidate.
- The production export path completed with **7,001 unique rows**. Its extra/current-input changes
  were not accepted into this task; the pre-change UI payload was restored byte-for-byte at
  `358b03211d87e3db8ffca57d9244d6244295225cac6eb3bd250196890d3014a5`.
- Audit: **279 source values** and **3,411 arithmetic checks**, zero wrong. Filing audit:
  **378 rendered-statement checks**, zero wrong.
- Alembic remains `20260929_0006 (head)`.

Three independent reviews found no remaining blocker after corrections for corrupt-archive
checkpointing, JPX eligibility, alphanumeric security codes, restart proof, real mapping/derivation,
and stale-lease boundaries.

No external EDINET request, publication, commit, push, stash, or branch switch occurred. The export
and filing audit used their normal live market/source inputs. PostgreSQL and S3 ran only for the
local storage gates.
