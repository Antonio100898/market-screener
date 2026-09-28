# Developer 22 — review map

Developer state: **IDLE — accepted after Review 2**
Current item: **Incorporated SEC exhibit recovery and dual provenance**
Review state: **ACCEPTED — Review 2**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

Implemented guidance revision 2. Not committed.

### Trace and reuse

- Product decisions preserved: P-02, P-03, P-04, P-09.
- Reused `sync.retain_inline_statements` -> retained manifest ->
  `inline_xbrl.parse_inline_xbrl` -> `EvidenceLoader` -> `normalize._fact` ->
  `sync._source` -> filing audit. No adapter or second extraction path.
- Reuse record:
  `local/raw-sec-incorporated-exhibit-2026-09-28/reuse-decision.md`.

### Changes

- Generic annual-document parser accepts exactly one audited-financial-statement
  SEC archive link plus explicit incorporation-by-reference language.
- Acquisition resolves that accession and primary document against the same
  issuer's official submissions. It rejects CIK, accession, document, Form 6-K,
  date, link, and ambiguity mismatches.
- Incorporated manifests retain and hash the annual index/wrapper plus the source
  index, primary, instance, FilingSummary, presentation, and schema. Publication
  waits for parsing and a coherent current annual balance.
- Company Facts entries carry the 40-F identity for annual selection. Exact 6-K
  accession/form/filed/document remain separate source fields. An exact existing
  6-K Company Facts value is rebound rather than duplicated.
- `Provenance` and payload sources expose exact source identity plus `annual_accn`,
  `annual_form`, and `annual_filed`; derived/component sources retain the same
  relationship. Filing audit opens the exact source accession.
- Direct manifests and payloads remain unchanged. Engine version: 187.
- No production CIK, ticker, company, accession, document, or exhibit special case.

### Review 1 corrections

- An audited-statement link now qualifies only when the Form 6-K incorporation
  clause is within 1,200 characters of that link in normalized document text.
  A distant unrelated audited link fails closed. Real CNI remains verified.
- A derived source now emits top-level annual relationship fields only when every
  provenance leaf has the same non-null identity. Mixed leaves keep their own
  component metadata but make no top-level annual claim.
- Added negative distant-link and mixed/uniform component tests.

### Verification

- Focused owned tests: 233 passed.
- Full Python suite: 839 passed, 6 skipped, one existing Starlette warning.
- Direct 17-case verifier: 17/17 payload records exactly equal to engine 186;
  13 derive `ok`, four unchanged cover-evidence failures.
- Real retained CNI verifier: `status=ok`, supplement `applied`, current annual
  `0001104659-26-010352` Form 40-F, exact source
  `0000016868-26-000011` Form 6-K document `cni-20251231.htm`.
- CNI balance reconciles: 58.555b assets = 36.987b liabilities + 21.568b common
  equity. No SEC bytes copied into the evidence directory.
- `compileall`, CNI verifier, and `git diff --check` passed.
- Evidence:
  `local/raw-sec-incorporated-exhibit-2026-09-28/README.md` and `results.json`.

### Limits

- Main cache was not activated. No derive/export/UI gate, model/provider call,
  store mutation, commit, or push.
- The existing four direct-statement cover-evidence failures remain out of scope.
- No other session edit to an owned file was observed. The unrelated modified
  `local/foreign-coverage-plan-2026-09-28.md` was not touched.

## Reviewer response

### Review 1 — changes required

The generic dual-accession manifest, source-vs-annual selection split, direct compatibility, and
real CNI result are directionally accepted. Two safety gaps block acceptance: relationship
discovery can pair a global incorporation sentence with an unrelated audited link elsewhere, and
top-level derived provenance can claim an annual relationship while silently ignoring unbound
component leaves. Guidance revision 2 requires local link-to-clause proof and all-leaves agreement.

### Review 2 — accepted

Accepted. Incorporation evidence now binds the selected SEC link to nearby Form 6-K incorporation
text; distant unrelated audited links fail closed. Top-level derived provenance emits an annual
identity only when every leaf carries the same non-null relationship. The reviewer independently
observed 233 focused passes and real CNI `ok` with reconciled balance and exact 40-F/6-K identities.
Direct 17 behavior remains unchanged apart from engine 187.
