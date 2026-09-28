# Developer 20 — current guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev20-review-map.md`. Do not commit, stash, switch branches, call any
model/provider, or edit integration, storage, normalization, plans, owner decisions, or another
developer's paths.

## Paths you own

- `api/screener/sources/inline_xbrl.py`
- `api/tests/test_inline_xbrl.py`
- `local/raw-sec-inline-parser-2026-09-28/**`
- Developer update in `local/dev20-review-map.md`

Read-only evidence:

- `local/raw-sec-statement-recovery-2026-09-28/**`
- official files under `~/.cache/graham-screener/raw-sec-statement-recovery/`
- current normalization, DERA, EDINET, IFRS-workbook, evidence, and model contracts

## Rules

Read `~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature` and `ponytail`. Trace the
existing fact contract before editing and write the reuse decision. This parser extracts evidence;
it does not decide dashboard admission or publish facts.

## Current increment — generic parser contract

**Observed gap.** SEC Company Facts omits current statement facts for 18 annual filers, while the
official extracted Inline-XBRL instance contains standard concepts, contexts, units, source fact
IDs, scale/sign attributes, and presentation roles. Seventeen cases are direct current annual
filings; incorporated exhibits are a later increment.

**Correct shape from zero.** The SEC source layer reads retained immutable extracted-instance,
FilingSummary, presentation-linkbase, schema, and primary-document bytes. It emits a Company-Facts-
shaped supplement of standard taxonomy facts plus explicit source metadata. Evidence selection and
normalization remain existing owners.

1. Trace the exact Company Facts shape consumed by `EvidenceLoader` and `normalize`, DERA's
   reshaping, provenance construction, period/unit conventions, and duplicate handling. Record
   what can be reused and why a new source parser is still needed.
2. Implement a pure parser accepting explicit filing metadata and local retained paths. No network,
   cache path, company list, or ticker exception belongs inside it.
3. Parse contexts, issuer identifier, explicit/typed dimensions, units, standard namespace/concept,
   exact transformed value, decimals, start/end/instant, fact ID, primary document anchor,
   Inline-XBRL scale/sign, and presentation roles. Preserve the source accession/document and the
   annual relationship accession/form separately in emitted metadata.
4. Emit only standard `ifrs-full` and `us-gaap` facts. Keep dimensioned facts identifiable; do not
   silently flatten them. Never infer a value, currency, sign, period, or absent current subtotal.
5. Emit the existing `facts -> namespace -> concept -> units -> entries` shape with the narrowest
   additional underscored source fields required for later provenance. Do not create canonical
   concepts or a second normalizer.
6. Fail closed on missing instance/summary/presentation/primary bytes, source-hash mismatch, issuer
   mismatch, unresolved fact anchors, conflicting duplicate facts for the same concept/unit/context,
   or invalid numeric transforms. Exact duplicates may deduplicate deterministically.
7. Tests: synthetic IFRS and US-GAAP; negative sign and scale; instant/duration; dimension
   preservation; standard-only; primary roles; duplicate conflict; wrong issuer/hash/missing anchor;
   no current-subtotal invention; deterministic output.
8. Real evidence: parse the 17 direct filings from the accepted audit. Reproduce every selected
   anchor/value/unit/period in `inventory.json`, preserve all raw hashes, and record total standard
   fact counts. Do not include CNI yet. Write verifier/output under
   `local/raw-sec-inline-parser-2026-09-28/` without copying filing bytes into Git.
9. Checks: focused tests, full Python suite, real 17-case verifier, syntax, and `git diff --check`.
   Do not bump engine version or run derive/export because the parser is not integrated.

## Queued after review

Retained SEC fetch/storage and `EvidenceLoader` integration, then generic incorporated-exhibit
discovery and provenance for CNI.

## Return protocol

Update only Developer update in `local/dev20-review-map.md`. Report trace, exact changed files,
fact counts, checks with raw output, real evidence, reuse decision, known limits, and whether
another session touched owned files. Never mark accepted.
