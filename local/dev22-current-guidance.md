# Developer 22 — current guidance

Guidance revision: 2
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — Review 2; stop**

Read this file and `local/dev22-review-map.md`. Do not commit, stash, switch branches, call any
model/provider, edit importer/PostgreSQL/API/UI/plans/owner decisions, mutate the main store, or
run full derive/export.

## Paths you own

- `api/screener/sources/inline_xbrl.py`
- `api/screener/models.py`
- `api/screener/normalize.py`
- `api/screener/sync.py`
- `api/screener/store.py`
- `api/tests/test_inline_xbrl.py`
- `api/tests/test_normalize_notes.py`
- `api/tests/test_sync.py`
- `api/tests/test_audit.py`
- `local/raw-sec-incorporated-exhibit-2026-09-28/**`
- Developer update in `local/dev22-review-map.md`

Read-only evidence/cache:

- accepted raw SEC audit/parser/integration records
- `~/.cache/graham-screener/raw-sec-statement-recovery/`

## Rules

Read `~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature` and `ponytail`. Reuse the
direct acquisition/parser/merge path. No ticker, company name, accession, document name, or exhibit
number may be hardcoded in production.

## Current increment — Review 1 corrections

**Observed gap.** A current 40-F may explicitly incorporate audited statements filed in another SEC
accession. The statement fact must participate in current-annual selection while provenance still
opens the exact source accession/document. A single accession field cannot express both roles.

**Correct shape from zero.** Acquisition proves an explicit annual-document relationship to one
same-issuer SEC-filed exhibit, retains both annual wrapper and source filing bytes, and emits a
manifest with separate annual and source identities. Entry fields carry annual selection identity;
per-figure provenance carries the exact source plus the annual relationship.

Review 1 accepts the dual-identity shape and real CNI result, but found two provenance gaps:

1. `incorporated_annual_source` independently finds a global incorporation sentence and an audited-
   statement link anywhere in the document. It can join unrelated sections. Bind the selected SEC
   link to incorporation-by-reference/Form 6-K text in the same local clause or bounded surrounding
   text. Add a negative test with the two facts far apart and an unrelated audited link. Keep the
   real CNI proof passing.
2. `sync._source.annual_identity` ignores component leaves that lack annual-relationship metadata.
   A mixed derived fact can therefore claim one annual relationship even though only one component
   has it. Emit top-level annual fields only when every provenance leaf has the same non-null annual
   identity. Each component still serializes its own relationship. Add mixed and uniform component
   tests.
3. Rerun the same focused/full/direct-17/CNI checks, compile, and diff check. No network, model,
   main-store, derive/export, or prompt action.

Original implementation requirements, retained for context:

1. Trace source selection, annual filing admission, `_fact`, `Provenance`, `_source`, audit source
   lookup, serialization tests, and direct-manifest compatibility. Record reuse decision.
2. Discover relationships from the retained current 40-F primary document. Require explicit
   incorporation-by-reference language and an SEC archive URL encoding the same CIK, source
   accession, and document. Resolve source form/filed metadata from official submissions. Reject
   external issuers, non-SEC URLs, missing accession/document, later source filing, ambiguity, or
   unlinked exhibits.
3. Fetch/retain the annual wrapper relationship artifact and the source accession's index, primary,
   extracted instance, FilingSummary, presentation, and schema bytes atomically. Publish one
   `incorporated_annual_exhibit` manifest only after all hashes, relationship evidence, parsing, and
   balance coherence verify.
4. Generalize manifest verification for direct and incorporated relationships. Paths/URLs must
   follow the recorded accession for each role. No absolute path or company exception.
5. For every supplement fact, set normal Company Facts `accn/form/filed` to the current annual
   relationship so existing selection works. Preserve actual source accession/form/filed/document/
   fact anchor separately. Direct filings remain byte-for-byte equivalent at the payload boundary.
6. Extend `Provenance` only as needed to serialize both roles. The normal `accn/form/filed` in the
   UI source must open the exact source filing. Add annual relationship fields only when source and
   annual differ. Derived/component provenance must preserve them.
7. Engine bump for the added current source. No direct-company output may change except newly
   disclosed source metadata if it is intentionally required; pin this with tests.
8. Tests: direct compatibility; valid generic incorporation; CIK/accession/document/form/date/link
   mismatch; ambiguous links; missing relationship bytes; source-vs-annual selection; provenance
   serialization; audit reads source accession; no hardcoded CNI identifiers.
9. Real evidence: build CNI relationship from accepted retained bytes in a temporary manifest,
   derive through `EvidenceLoader` and `_derive_evidence`, and show `ok`, current annual 40-F basis,
   exact 6-K source accession/document on figures, reconciled balance, and no copied SEC bytes.
10. Run focused tests, full Python suite, direct 17-case verifier, CNI verifier, syntax, and
    `git diff --check`. Do not activate main cache or run UI gates yet.

## Queued after review

Bounded main-cache activation for all 18, engine derive/export/regression/audits, then PostgreSQL/S3
artifact import. Reassess whether any current filing remains for LLM fallback.

## Return protocol

Update only Developer update in `local/dev22-review-map.md`. Report trace, exact changes, engine,
tests, direct compatibility, CNI source/annual evidence, known limits, and whether another session
touched owned files. Never mark accepted.
