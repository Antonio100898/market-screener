# SEC resource fetch and candidate staging investigation

Updated: 2026-09-29
State: **ACCEPTED — Review 1; stop**

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`, the “Incremental ingestion
and reconciliation”, “Selecting the latest adjusted facts”, and “S3 contract” sections of
`docs/production-architecture.md`, increments 4 and 5 of `docs/implementation-rollout.md`,
`local/durable-ingestion-plan-2026-09-29.md`, and the accepted SEC discovery/import code. Read and
follow the shared code-work, applying-feature, ponytail, and no-scaffolding-leaks skills.

Investigate the smallest correct SEC resource-fetch slice and the boundary to non-current candidate
derivation. Do not edit production code, tests, product decisions, or plans. Do not call SEC/model
APIs, start services, mutate databases/cache, commit, stash, or switch branch.

## Decisions preserved

P-02, P-03, P-04, P-05, P-09, P-17.

## File you may change

- Developer update in `local/dev40-review-map.md`

## Questions to settle

1. Map the accepted `sec-resource-fetch` child parameters to exact SEC filing, submissions,
   Company Facts, cover, Inline-XBRL, manifest, and retained-evidence paths. Name what can be reused
   without filesystem/SQLite writes.
2. Define the smallest fetch job that records pending/unavailable/present revisions and exact S3
   artifacts for domestic and foreign filers. Separate mutable aggregate resources from immutable
   accession resources.
3. Define retry behavior when an index is visible before files or Company Facts are ready. A 404 or
   partial filing must not become removal, success, or an empty financial result.
4. Decide whether candidate derivation can use the existing `company_snapshot` table without
   updating `current_company_snapshot`. If it needs a repository split or publication schema, name
   the smallest safe boundary and keep increment 5 ownership intact.
5. Show how the generic Inline-XBRL/cover/EvidenceLoader/derivation path serves 10-K, 20-F, and 40-F
   without company-specific extractors. Keep the LLM fallback only for proven deterministic gaps.
6. Return small build slices, exact files/interfaces, fixture and real PostgreSQL/S3 checks, and any
   owner decision that blocks code now. Do not choose production timing or outage UI behavior.

## Report

Update only `## Developer update` in `local/dev40-review-map.md`. Cite files and lines. End with
`READY_FOR_REVIEW`, `BLOCKED`, or `CONTINUE`, and confirm no production/database/external changes.
