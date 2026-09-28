# Developer 15 — current guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session per accepted increment
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev15-review-map.md`. Do not edit production code, tests, plans, owner
decisions, or another developer's files. Do not commit, stash, or switch branches.

## Paths you own

- `local/foreign-ticker-continuity-e2e-2026-09-28/**`
- Developer update in `local/dev15-review-map.md`

The local SQLite database, raw SEC cache, and generated `api/screener/static/dashboard.json` may be
updated only by the named production commands below. They are runtime artifacts, not owned source
files.

## Current increment — engine-185 full payload gate

**Goal.** Prove the accepted ticker-continuity change through the exact UI payload and primary
filing audits. Expected product change: BRNX and ZTG may enter under current tickers. Every other
change must be explained or returned as a regression.

1. Read `~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature` and `ponytail` for scope
   discipline. This is verification only; do not patch failures.
2. Record free disk/RAM. The repo has no `make local-headroom` target, so use `df -h .` and
   `vm_stat`. Run one heavy command at a time.
3. Preserve the current engine-184 `api/screener/static/dashboard.json` before overwriting it. Put
   the temporary copy outside the repo, record its SHA-256, byte size, row count, and unique
   ticker/CIK counts in the evidence README. Do not commit the 220 MB baseline.
4. Run `make regress ARGS='--all --baseline <preserved-file>'` and capture complete output. The
   baseline must remain available until all comparison work is complete.
5. Run `make derive`, then `make export`. Record engine, payload SHA-256, size, row count, unique
   ticker/CIK counts, and exact added/removed tickers against the baseline.
6. Compare all shared rows and report changed fields, including criteria, verdict, financials,
   market cap, valuation multiples, yields, historical ratios, profile/alignment results, notes,
   source accessions, and UI-derived records. Volatile quote/time fields may move but must be named.
7. Run `make audit` and `make audit-filings`. Network or source failures are reported exactly and
   block acceptance; do not call them passing.
8. Verify BRNX and ZTG in the new UI payload: ticker/CIK unique, current security evidence includes
   annual basis plus later accession, calculations use the same retained statement facts, and no
   unsupported OTC row entered.
9. Write all command outputs or concise exact extracts under
   `local/foreign-ticker-continuity-e2e-2026-09-28/`, with a README that explains every changed
   field. Remove the temporary baseline after recording evidence and only when no comparison still
   needs it. Do not delete any user data or cache.
10. If any source or code defect appears, return `BLOCKED:` with the first divergence. Do not edit
    source. Otherwise return `READY_FOR_REVIEW` with raw counts.

## Return protocol

Update only Developer update in `local/dev15-review-map.md`. Include commands, raw results,
baseline/new identities, exact payload delta, audits, limits, and whether another session touched
owned files. Never mark accepted.
