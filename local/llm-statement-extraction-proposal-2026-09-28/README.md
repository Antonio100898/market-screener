# LLM statement extraction proposal

The proposed fallback can supply source-cited candidates without changing official data in shadow
mode. It reuses the canonical adapter and derivation path. It does not trust model output as a
published fact.

Prompt behavior is **UNVERIFIED**. No LLM was called.

## Product decisions preserved

- P-02: one verified observation can feed every client; no local or per-user official value.
- P-03 and P-17: source bytes and run artifacts go to content-addressed S3; Python/Alembic owns the
  PostgreSQL extraction and observation records.
- P-04: every new or changed filing creates a new extraction attempt. An old accepted candidate
  cannot silently stand in for a new filing.
- P-09: source bytes, candidates, checks, selections, and supersession remain immutable history.

## Observed failure

`inventory.md` records all 18 rows. The latest annual accession normally contributes only one DEI
cover share fact to Company Facts. The current path is:

1. `EvidenceLoader.load` builds the retained bundle.
2. `sync._derive_evidence` calls `normalize.build_snapshot`.
3. `build_snapshot` calls `_reject_foreign` before reading statement facts.
4. `_current_supported_foreign_annual` finds the latest 20-F or 40-F.
5. `_balance_currency_in_filing` finds no standard balance anchor in that accession.
6. `_reject_foreign` raises `UnsupportedFilerError`; the row remains `foreign`.

The filing bytes can still contain complete statements. The three frozen pilots prove three
different source shapes: a classified inline-IFRS statement, an unclassified bank statement, and a
40-F that incorporates audited statements from another official SEC accession.

## Correct flow

```text
retained official filing bytes + exact missing-field request
    -> LLM candidate JSON
    -> deterministic schema, citation, meaning, period, unit, scope, and reconciliation checks
    -> immutable extraction run and fact observation
    -> existing facts.canonical adapter shape
    -> existing _derive_evidence path
    -> later publication selection
```

Deterministic extraction always runs first. The fallback requests only fields still missing. A
model result never edits Company Facts, a snapshot, or a published selection.

## Input contract

One request contains:

- stable issuer identity and request ID;
- each official document's accession, document name, media type, SHA-256, and relationship to the
  annual filing;
- retained document bytes with table structure and HTML anchors or PDF pages preserved;
- an explicit requested-field list with canonical name, semantic definition, instant or duration,
  target period, and required consolidation scope;
- `candidate.schema.json` and the exact prompt in `prompt.md`.

The model does not choose which fields are needed and does not create canonical definitions.

## Candidate semantics

For `found`, `value` is the printed cell converted only to a signed decimal string. Thousands or
millions remain in `scale`; the deterministic verifier performs multiplication. The candidate
must identify the exact source label, unit/currency, period, accounting basis, scope, statement,
row, column, and value anchor or page.

`not_found` means the supplied documents were searched and did not report the requested fact.
`ambiguous` means at least two readings remain. Neither outcome can carry a usable value.

The output has no model-confidence field. Confidence is not evidence and cannot pass a check.

## Deterministic acceptance checks

All checks must pass:

1. JSON validates against `candidate.schema.json`; every requested field appears exactly once and
   no unrequested field appears.
2. Every accession, document, and SHA-256 matches the retained source manifest.
3. The anchor or page exists. The row, column, displayed value, sign, statement title, period text,
   and unit/scale evidence occur at that location.
4. The numeric text parses exactly. Parentheses, minus signs, dashes, decimal separators, and inline
   XBRL sign/scale attributes agree. The verifier, not the model, applies scale.
5. Instant and duration dates match the requested period and are not after the filing date.
6. Currency and accounting basis are explicit in the statement or machine-readable filing
   context. No convenience translation or note-table currency can set the primary basis.
7. Consolidation scope matches the request. Segment, parent-only, note, summary, and non-GAAP rows
   are rejected when consolidated primary-statement evidence is required.
8. The source label satisfies the request's approved canonical definition. The model cannot expand
   that definition.
9. Duplicate or contradictory cells return `ambiguous`. A later source can supersede an earlier
   one only through the existing selection rules.
10. A statement group reconciles within the source rounding tolerance. Assets must equal
    liabilities plus equity. Classified current totals must not exceed their matching totals.
11. The candidate is a supplement only: if deterministic extraction already supplies an
    equivalent fact, the candidate cannot replace it.
12. Cross-accession evidence is accepted only when the annual filing explicitly incorporates the
    official exhibit and both source artifacts are retained.

A passing check creates an accepted observation candidate. Publication remains a separate action.
During shadow mode, no candidate reaches canonical derivation or the dashboard.

## Human checks during shadow mode

A reviewer compares every result with a frozen gold answer and confirms:

- the citation opens the exact statement cell;
- the canonical definition matches the row's accounting meaning;
- period, currency, scale, sign, accounting basis, and scope are correct;
- `not_found` and `ambiguous` outcomes did not overlook a primary statement;
- the deterministic verifier rejected every planted note, segment, summary, and arithmetic trap.

The reviewer records accept/reject and reason. Human approval cannot rescue a failed deterministic
check.

## Immutable run metadata

Retain these records before implementation can publish anything:

- run ID, issuer ID, request ID, start/end time, and lifecycle state;
- source artifact hashes and the exact assembled provider-request artifact hash;
- prompt hash and prompt revision;
- provider, model ID, provider-reported model revision when available, and all model settings;
- exact response artifact hash and parsed-candidate artifact hash;
- latency, input/output tokens, and cost;
- verifier revision, each check and result, and verifier-report artifact hash;
- reviewer, review time, decision, reason, and superseded run or observation IDs.

`evidence_artifact` and `store_evidence` can retain all bytes. PostgreSQL still needs immutable
extraction-run and fact-observation records. Do not overload `company_snapshot` with this state.

## Source-view boundary

Some primary HTML documents are too large for an unknown provider's text limit. The fallback
module still accepts the retained raw bytes. If a provider requires a compact source view, create a
deterministic, lossless table view that preserves source order, headings, anchors, row/column text,
and a mapping back to the raw hash. Retain that exact view and send only that recorded artifact.
Provider file upload or retrieval that cannot expose the exact model input is unsuitable for this
audit contract.

## Scope and evidence gaps

- No prompt run, trace, output, cost, or latency evidence exists. See `ab-plan.md`.
- No provider or model has been selected. P-15 still forbids choosing an OpenAI model for the
  product agent; model selection remains a separate owner decision.
- The inspected pilot files are local only and must not be committed as duplicate source data.
  Implementation must register the retained bytes through `store_evidence`, verify S3 readback,
  and use the manifest hashes before any model call.
- Extraction-run and fact-observation tables are not implemented.
- This prompt handles financial statement fields. Security class, ADS ratio, and shareholder-right
  evidence need their own field contract and deterministic verifier; mixing them into this prompt
  would weaken both checks.

## Recommended owner decision

Approve the first implementation as `SUPPLEMENT` only: it may fill a deterministically missing
field, but it may not correct or override a deterministic value. Corrections stay in the stronger
reviewed-data flow.
