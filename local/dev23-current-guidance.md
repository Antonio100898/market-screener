# Developer 23 — current guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session
State: **ACTIVE — engine-187 bounded activation and UI gate**

Read this file and `local/dev23-review-map.md`. Verification/runtime-data increment only. Do not
edit production code, tests, plans, owner decisions, prompts, or provider credentials. Do not
commit, stash, or switch branches.

## Paths you own

- `local/raw-sec-engine187-e2e-2026-09-28/**`
- Developer update in `local/dev23-review-map.md`

Runtime mutation authorized only through the named production commands:

- current SQLite and SEC cache under `~/.cache/graham-screener/`
- generated `api/screener/static/dashboard.json`

## Current increment

**Goal.** Activate the exact 18 accepted SEC statement sources through the bounded production job,
then prove engine 187 against the preserved UI payload. Expected additions are the 13 direct `ok`
rows plus CNI. AHNRF, BRBI, NXAT, and CIB must remain excluded for their recorded cover/ratio gaps.

1. Read `~/.agents/skills/_shared/code-work.md`; invoke `applying-feature` and `ponytail` for scope.
   This increment fixes no defect. Any code problem is returned with first divergence.
2. Record disk/RAM before each heavy stage. One heavy command at a time. Stop if free disk would
   fall below 1 GiB; do not delete unrelated data.
3. Preserve the current engine-185 dashboard outside the repo. Record SHA, size, engine, row count,
   and unique ticker/CIK counts. Keep it until all comparisons finish.
4. Run the bounded production acquisition for exactly these CIKs:
   `0001561861 0002020932 0001304409 0002025774 0001468642 0002058601 0002058897 0000016868
   0002039072 0001926293 0002039972 0002054507 0002000756 0002080845 0001841644 0002087398
   0002080073 0001875016`.
   Use `python -m screener.sync inline` with repeated `--cik`. Capture exact per-CIK states. Do not
   refetch Company Facts.
5. Verify all 18 active manifests, hashes, relationships, source/annual identities, and dirty state.
   Re-run acquisition once to prove idempotent `reused` state without dirtying snapshots.
6. Run full `make regress ARGS='--all --baseline <preserved>'` before replacing the UI payload.
   Explain every shared-row field movement; unexplained movement blocks.
7. Run `make derive`, `make export`, `make audit`, and `make audit-filings`. Capture raw logs.
8. Compare full payload identities and all shared fields. Confirm exact additions/removals, unique
   ticker/CIK, criteria/verdicts, financials, market cap, valuation/yields, historical ratios,
   profiles/alignment, notes, source accessions, and provenance. Quote/time/FX movements must be
   named, never called zero regression.
9. Verify the 14 expected additions use current annual dates and exact statement provenance. CNI
   must show 6-K source plus 40-F annual relationship. Verify the four expected exclusions and
   exact errors. No unsupported OTC/alias row may enter.
10. Write evidence under `local/raw-sec-engine187-e2e-2026-09-28/`. Remove only the temporary
    baseline after all comparisons and record its identity. Stop services not needed. Return
    `READY_FOR_REVIEW` only if every mandatory gate passes.

## Return protocol

Update only Developer update in `local/dev23-review-map.md`. Report raw commands/counts, baseline
and new identities, exact delta, audit results, disk, limits, and whether another session touched
owned files. Never mark accepted.
