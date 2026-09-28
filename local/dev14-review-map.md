# Developer 14 — review map

Developer state: **IDLE — accepted after Review 3**
Current item: **Later-SEC same-class ticker continuity**
Review state: **ACCEPTED — Review 3**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

State: **READY_FOR_REVIEW**

### Review 2 correction

- `set_cover` now reselects an already-proven later symbol from immutable observations when its
  `basis_accn` equals the annual cover being reread. It uses the newly parsed annual title and ratio
  with the retained later accession, filed date, exchange, and symbol.
- The selection is local and deterministic. A same-annual reread needs no submissions or later
  filing request and creates no dirty transition when evidence is unchanged.
- A genuinely newer annual accession has no matching retained `basis_accn`, so the old continuity
  is not selected. The new annual symbol becomes current and the snapshot is marked dirty until new
  continuity proof exists.
- Added the full restart boundary test: annual A -> later L/current NEW -> cached reread of A with
  SEC unavailable keeps NEW and leaves observations/dirty state unchanged; annual A2 invalidates L.

### Review 1 corrections

- `normalize._reject_foreign` now treats `basis_accn` as the annual class authority. The later
  accession remains the ticker-change evidence. Missing, empty, or wrong annual basis fails closed.
- Depositary restatement provenance now names the annual `basis_accn`, not the later filing.
- Later filing prose can no longer replace the annual depositary ratio. The annual ratio remains
  authoritative until a future parser binds a later ratio to the exact class.
- `_later_ticker_continuity` returns before reading SEC submissions when no supported prior annual
  common-equity class exists. Empty, debt, preferred, and untraded-underlying inputs cannot trigger
  later filing fetches.
- A shortened listing phrase is accepted only when the same filing also contains the exact full
  annual title and exactly one supported prior class matches. Missing full title, multiple matching
  classes, changed class, changed exchange, and OTC inputs fail closed.
- Added `build_snapshot` acceptance and rejection tests. This closes the gap where receipt selection
  succeeded but normalization still rejected BRNX and ZTG.

### Trace and reuse

- `cover_pages` retains annual and later filing bytes and calls the existing evidence owner.
- `evidence.continuity_security` decides identity. `normalize._reject_foreign` decides whether that
  identity is valid for the current annual statement. No caller duplicates either rule.
- Existing `receipt.accn` behavior remains unchanged for normal annual covers. Only a continuity
  receipt with `basis_accn` takes the new path.
- Immutable observations still preserve both accessions. No alias table, punctuation conversion,
  company exception, or LLM prompt was added.
- Reuse decision remains `local/foreign-ticker-continuity-2026-09-28/reuse-decision.md`.

### Checks

```text
PYTHONPATH=api api/.venv/bin/python -m pytest \
  api/tests/test_cover.py api/tests/test_evidence.py \
  api/tests/test_normalize_notes.py api/tests/test_sync.py -q
219 passed in 0.44s

PYTHONPATH=api api/.venv/bin/python -m pytest api/tests -q
797 passed, 6 skipped, 1 pre-existing Starlette deprecation warning in 3.36s

PYTHONPATH=api api/.venv/bin/python \
  local/foreign-ticker-continuity-2026-09-28/verify_real_continuity.py
BRNX: status ok, snapshot ticker BRNX, balance sheet 2025-12-31, verdict FAIL.
ZTG: status ok, snapshot ticker ZTG, balance sheet 2025-09-30, verdict FAIL.

git diff --check -- <owned paths>
exit 0, no output
```

### Real retained evidence

- BRNX: annual `0001213900-26-034046`, symbol BNRG, exact class `Ordinary Shares, no
  par value per share`; later F-3 `0001213900-26-103505`, filed 2026-09-25, explicitly lists the
  same class on Nasdaq as BRNX. Annual/later SHA-256 values are recorded in `real-evidence.json`.
- ZTG: annual `0001493152-26-002776`, symbol ZGM, exact class `Class A Ordinary Shares, par value
  US$0.001 per share`; later F-3 `0001493152-26-043703`, filed 2026-09-22, explicitly lists the same
  class on Nasdaq as ZTG.
- The real verifier now runs the production `_derive_evidence`/`build_snapshot` path. Both companies
  return status `ok` under their current tickers. Exact output and retained hashes are in
  `local/foreign-ticker-continuity-2026-09-28/real-evidence.json`.
- Restart safety is pinned without a network dependency: rereading the same annual accession keeps
  the retained later identity; changing the annual accession removes it from the current view.

### Known limits

- Strict exact-title proof intentionally leaves ticker changes unsupported when the later filing
  uses only a shortened class description and never repeats the exact annual title.
- Only 6-K, 8-K, 8-A12B, F-3, S-3, and their listed amendments are scanned. Missing or unreadable
  later bytes remain excluded.
- Full derive, export, regression, audit, and filing audit were not run, as required by this
  increment. Reviewer must run them after acceptance.
- Six PostgreSQL integration tests were skipped because their external test service was unavailable.
- The reviewer updated guidance and the reviewer-owned map sections. No other session was observed
  changing implementation-owned files. No commit was made.

## Reviewer response

### Review 1 — changes required

Storage, retained bytes, and strict ticker/exchange selection are directionally accepted. The
increment is not accepted because its real verifier stops at receipt selection. The unchanged
`normalize._reject_foreign` rejects the later F-3 accession instead of recognizing the retained
annual `basis_accn`, so BRNX and ZTG are not proved recoverable through `build_snapshot`. Also,
document-wide ratio parsing can attach unrelated prose, and rows without prior annual evidence
should not fetch later filings. Guidance revision 2 requires the owning normalization change,
negative pins, and real snapshot evidence.

### Review 2 — changes required

Review 1 corrections pass independent focused tests (`218 passed`) and the reviewer reproduced real
`ok` snapshots for BRNX and ZTG. One restart-safety gap remains: rereading annual cover A calls
`set_cover`, which removes the active later observation L before `_later_ticker_continuity` runs.
If SEC submissions is then unavailable, the retained proof is not reused and current identity
falls back to the old ticker. Revision 3 requires deterministic reuse of retained continuity for
the same annual basis and invalidation only when a genuinely newer annual basis appears.

### Review 3 — accepted

Accepted. Same-annual rereads now reselect retained later evidence without SEC access or a false
dirty transition; a new annual accession invalidates the old continuity. The reviewer independently
observed `219 passed` and reproduced real `ok` snapshots for BRNX and ZTG with the named annual and
later accessions. The ratio remains annual-backed, ambiguous classes and OTC aliases fail closed,
and no company exception was added.
