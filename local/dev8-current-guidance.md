# Foreign evidence investigation developer guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev8-review-map.md` before work. Update only the Developer update section
of the review map. Do not commit, stash, switch branches, or edit application code.

## Owned paths

- `local/foreign-coverage-investigation-2026-09-28/**`
- Developer update in `local/dev8-review-map.md`

Every application, test, product, and migration path is read-only. Return `BLOCKED:` if another
path is required.

## Standing rules

Read `AGENTS.md`, `api/AGENTS.md`, `product.md`, `product/shared-data.md`,
`local/foreign-coverage-plan-2026-09-28.md`, the foreign-related invariants in `AGENTS.md`,
`api/screener/evidence.py`, `api/screener/sources/cover.py`, the relevant foreign-normalization
functions, and `~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature` to frame the
desired support capability and `ponytail` after the flow is understood. This increment is read-only.

## Current increment — complete foreign evidence inventory

1. Reproduce every listed snapshot whose current status is `foreign` by loading retained evidence
   and calling the owning normalization path. Record ticker, CIK, company, exchange, current annual
   filing date/accession/basis, cover row, and exact exception.
2. Reconcile exactly to 237 rows and the known top-level groups: 192 missing exact cover title, 27
   unresolved depositary ratio, and 18 incoherent standard statement.
3. Subclassify all rows, not only samples. For cover failures distinguish empty/parser failure,
   symbol mismatch, wrong security class, stale/OTC mapping, no registered class, and missing raw
   cover evidence. For ratio failures distinguish ratio present in cover title, primary-document
   footnote, tagged DEI ratio facts, wrong-class pairing, and no filing-backed ratio. For statement
   failures inspect standard namespaces, anchors, currencies, and current annual completeness.
4. Use official SEC pages or documents when retained evidence cannot answer. Record accession and
   exact source location. Do not use secondary sites as authority. Respect SEC request limits.
5. Produce `local/foreign-coverage-investigation-2026-09-28/README.md` with exact counts, complete
   ticker lists by subclass, representative filing evidence, and a recommended milestone split.
   Put commands or supporting machine-readable output beside it when needed.
6. Every proposed fix names the owning function, why it is generic, expected recovered count, and
   the real-company plus neighbour checks that would prove it. Mark rows requiring a company-specific
   adapter or owner decision. Do not implement.

## Queued after review

The manager assigns one fresh implementation developer per accepted generic fix family. Product
decisions are taken to the owner one at a time before code.

## Return protocol

Set Developer state to `WORKING`, `READY_FOR_REVIEW`, or `BLOCKED_EXTERNAL`. Report the trace,
reuse decision, evidence sources, exact reconciled counts, report paths, unverified limits, skills
used, and concurrent changes. Never mark review accepted.
