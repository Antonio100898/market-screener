# Developer 20 — review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Generic SEC Inline-XBRL supplement parser**
Review state: **ACCEPTED — Review 1**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

READY_FOR_REVIEW

### Product decisions

- P-02: one source parser emits one shared official fact bundle.
- P-03: the parser reads explicit retained paths and verifies every retained hash.
- P-04: it emits source facts only; current-fact selection remains in normalization.
- P-09: it preserves source and annual relationship identities separately.

### Trace and correct owner shape

`SEC retained files -> inline_xbrl.parse_inline_xbrl -> Company-Facts-shaped supplement ->
EvidenceLoader (later increment) -> normalize -> sync._derive_evidence`.

The source layer owns byte verification and extraction. `EvidenceLoader` owns assembly.
`normalize.py` owns IFRS mapping, annual fact selection, statement admission, duplicate-period
policy, and provenance. `sync.py` owns derivation. This increment does not edit those owners.

### Reuse decision

Reused `dera.harvest`'s output shape and dimension string, and the existing normalization contract.
A new parser is required because Company Facts omits these current facts and DERA does not read the
retained Inline-XBRL instance, anchors, or presentation roles. Full note:
`local/raw-sec-inline-parser-2026-09-28/reuse-decision.md`.

### Changed files

- `api/screener/sources/inline_xbrl.py`: pure standard IFRS/US-GAAP parser with explicit metadata,
  five retained paths, required hashes, context/unit/dimension parsing, source-anchor validation,
  statement roles, exact transformed values, deterministic precision-aware duplicate handling, and
  separate source/annual metadata.
- `api/tests/test_inline_xbrl.py`: 14 focused tests for IFRS, US-GAAP, sign/scale, instant/duration,
  explicit/typed dimensions, standard-only behavior, roles, compatible/conflicting duplicates,
  issuer/hash/anchor/file failures, invalid transforms, no subtotal invention, and determinism.
- `local/raw-sec-inline-parser-2026-09-28/{README.md,reuse-decision.md,results.json,
  verify_parser.py}`: real 17-filing evidence and reproducible verifier. No filing bytes are in Git.
- `local/dev20-review-map.md`: this return only.

### Real evidence

Command:

`PYTHONPATH=api python3 local/raw-sec-inline-parser-2026-09-28/verify_parser.py`

Raw output:

`{"all_selected_anchors_reproduced": true, "all_source_hashes_preserved": true,
"companies": 17, "selected_anchors": 124, "standard_facts": 18775}`

All 124 accepted value/unit/period/anchor records were reproduced. Parser output retained every
verified file hash. Per-company counts are in `results.json`.

### Checks

- Focused: `PYTHONPATH=api api/.venv/bin/pytest -q api/tests/test_inline_xbrl.py`
  -> `14 passed in 0.07s`.
- Full Python: `PYTHONPATH=api api/.venv/bin/pytest -q api/tests`
  -> `811 passed, 6 skipped, 1 warning in 4.81s`. The warning is the existing FastAPI/httpx
  deprecation warning.
- Syntax: `python3 -m py_compile api/screener/sources/inline_xbrl.py
  api/tests/test_inline_xbrl.py local/raw-sec-inline-parser-2026-09-28/verify_parser.py`
  -> exit 0.
- `git diff --no-index --check /dev/null <each owned untracked file>` loop
  -> `untracked whitespace check: PASS`.

### Known limits

- CNI incorporated-exhibit discovery is intentionally excluded and remains the queued increment.
- No evidence integration, storage, normalization, engine change, derive/export, provider call, or
  commit was made.
- The repository has no `make local-headroom` target. Before the full suite, manual checks showed
  no throttled memory pages, load averages `7.06 4.96 3.95`, and 4.5 GiB free disk.

No other session was observed touching the owned files.

## Reviewer response

### Review 1 — accepted

Accepted. The parser is pure, hash-bound, standard-taxonomy only, preserves dimensions and source
metadata, and fails closed on issuer, anchor, hash, numeric, and material duplicate conflicts. The
reviewer independently observed 14 focused passes and reproduced all 124 accepted anchors across
17 retained filings, totaling 18,775 facts. Integration, incorporated exhibits, and engine output
remain deliberately outside this increment.
