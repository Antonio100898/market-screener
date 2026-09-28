# Developer 14 — current guidance

Guidance revision: 3
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session per accepted increment
State: **ACCEPTED — Review 3; stop**

Read this file and `local/dev14-review-map.md`. Write only inside the owned paths. Do not commit,
stash, switch branches, or edit another path.

## Paths you own

- `api/screener/evidence.py`
- `api/screener/normalize.py`
- `api/screener/sources/cover.py`
- `api/screener/store.py`
- `api/screener/sync.py`
- `api/tests/test_cover.py`
- `api/tests/test_evidence.py`
- `api/tests/test_normalize_notes.py`
- `api/tests/test_sync.py`
- `local/foreign-ticker-continuity-2026-09-28/**`
- Developer update in `local/dev14-review-map.md`

`local/foreign-coverage-plan-2026-09-28.md`, owner decisions, reviewer response, importer,
PostgreSQL, API, and all other files are reviewer-owned.

## Rules

Read `~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature` and `ponytail` after tracing
the current flow. Reuse the current cover parser, retained-byte paths, cover storage, and evidence
selection unless the trace proves a limitation. Do not add a ticker alias table or company-specific
exception.

## Current increment — Review 2 correction

**Owner decision.** A later official SEC filing may prove that the same security class changed
ticker. Retain both filings. Keep one stable security identity. Activate the new ticker only when
issuer, class, and exchange are explicit. Do not infer punctuation or unsupported OTC aliases.

**Observed gap.** Twenty-four excluded rows have a retained annual cover class but no exact current
ticker match. Some mapped tickers are newer than the annual-cover ticker. `EvidenceLoader.identity`
can select only a symbol already stored from the annual cover. `_index_tickers` may move the company
row to the current SEC mapping before evidence can prove that the same class moved.

**Correct shape from zero.** The evidence layer owns security continuity. It reads an immutable
annual-cover observation plus an immutable later SEC filing observation. It emits the current
symbol only when the later filing explicitly pairs that symbol and exchange with the same class
title; normalization consumes this evidence and keeps its existing class, ratio, and currency
checks. No caller guesses aliases.

Review 1 corrections are accepted. Review 2 found one restart-safety blocker:

1. `cover_pages` calls `set_cover` for the same annual accession before it reselects later evidence.
   `set_cover` deletes the active continuity row and restores the old annual symbol. If the next
   submissions request fails, retained proof is ignored and the recovered current ticker is lost.
2. Make current selection reproducible from retained observations. Re-reading the same annual
   accession must preserve or reselect a later continuity observation whose `basis_accn` is that
   annual accession. No network call may be required to keep already-proven identity. A genuinely
   newer annual accession must invalidate the old continuity until fresh proof exists.
3. Add a storage/cover sync test: annual A -> later L/current NEW -> reread annual A while SEC
   submissions fails -> current remains NEW, observations unchanged, and no false dirty transition.
   Add the boundary: annual A2 replaces A -> old continuity does not survive.
4. Rerun the same focused and full Python suites and the BRNX/ZTG real verifier. Do not run the full
   export/regression/audit gates yet.

## Queued after review

The LLM shadow extractor is a separate increment. Do not edit or propose its prompt here.

## Return protocol

Update only the Developer update in `local/dev14-review-map.md`. Set the state to
`READY_FOR_REVIEW` or `BLOCKED_EXTERNAL`. Report the trace, exact changes, raw checks, real evidence,
reuse decision, known limits, and whether another session touched owned files. Never mark accepted.
