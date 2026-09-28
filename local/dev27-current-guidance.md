# Developer 27 — current guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — Review 1; stop**

Read `product.md`, `product/shared-data.md`, this file, and `local/dev27-review-map.md`. This is a
read-only investigation. Do not edit production code/tests/product decisions, mutate PostgreSQL/S3/
SQLite/cache, call provider models, import EDINET, commit, stash, or switch branches.

## Affected decisions

- P-02: one shared official result for every client.
- P-03: automatic jobs retain source documents in S3; PostgreSQL owns fundamentals/derived fields.
- P-04: latest adjusted official numbers become the default.
- P-09: revisions and evidence remain inspectable.
- P-17: Python/Alembic owns the schema and FastAPI boundary.

## Paths you own

- `local/engine187-persistence-audit-2026-09-28/**`
- Developer update in `local/dev27-review-map.md`

## Current increment — read-only contract audit

**Goal.** Identify the smallest generic changes needed for engine-187 SEC statement artifacts and
all retained supported EDINET companies to pass verified object storage, immutable PostgreSQL
snapshots, selection, restart, and FastAPI parity.

1. Read `~/.agents/skills/_shared/code-work.md`; invoke `applying-feature` and `ponytail` only for
   design discipline. Trace before proposing code.
2. Trace `CompanyImporter` for SEC and EDINET: every retained artifact role, S3 write/readback,
   evidence link, snapshot identity/hash, current selection, bulk retry, and API reader.
3. Trace engine-187 Inline-XBRL current manifests and accession directories. For direct and
   incorporated cases, list every byte that P-03/P-09 require in S3 and which are currently omitted.
4. Inventory every retained supported EDINET company in SQLite/cache. Record ticker, EDINET entity,
   document IDs, source archives, currency, security basis, snapshot status/engine, dashboard
   presence, and current importer result. Do not fetch new EDINET filings.
5. Inspect current PostgreSQL/S3 state only if services are already available; otherwise use existing
   accepted evidence and report runtime state as unavailable. Do not start services in this audit.
6. Reconcile current engine-187 dashboard eligibility against importer selection. Separate stale
   engine snapshots, missing artifacts, identity failures, source-policy failures, and code defects.
7. State the correctly-built-from-zero owner shape. Prefer extending current artifact enumeration
   and importer over a second source-specific persistence path. No ticker/company exceptions.
8. Propose disjoint build milestones with exact files and acceptance checks: SEC artifact retention,
   EDINET retained-universe import, full reconciliation/API restart. Name migration needs, if any.
9. Evidence under `local/engine187-persistence-audit-2026-09-28/`: README, exact inventory JSON/CSV,
   reproducible read-only script, reuse decision, and source/hash counts. Checks: unique identities,
   totals reconcile, no mutation, deterministic rerun, syntax, diff check.

## Return protocol

Update only Developer update in `local/dev27-review-map.md`. Report exact gaps/counts, current
Japanese companies, source roles, reuse decision, proposed increments, checks, limits, and any
concurrent edits. Never mark accepted.
