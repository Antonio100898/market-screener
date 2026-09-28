# Developer 19 — current guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev19-review-map.md`. This is read-only investigation. Do not call any
model, edit production code/tests/prompts/plans/owner decisions, commit, stash, or switch branches.

## Paths you own

- `local/raw-sec-statement-recovery-2026-09-28/**`
- Developer update in `local/dev19-review-map.md`

## Rules

Read `~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature` and `ponytail`. Reuse the
retained SEC bytes, accepted frozen manifests, current normalization/canonical-adapter contracts,
and exact Company Facts failure inventory. No provider key may be read.

## Current increment — classify deterministic recovery before LLM fallback

**Observed evidence.** The live screen showed that AERO's retained primary 20-F contains exact
inline-XBRL anchors, standard IFRS concepts, contexts, units, scale, signs, and values for every
requested field even though SEC Company Facts omits them. The model was reading machine evidence
that code may be able to read directly.

**Correct shape from zero.** Deterministic structured extraction owns every fact that an official
filing exposes with machine-readable concept/context/unit/period data. An LLM sees a filing only
after that path cannot produce the required canonical field. The LLM must never replace a parser
for facts already explicit in inline XBRL.

1. Reproduce the 18 `incomplete_company_facts` rows from the accepted inventory. Record current
   accession, form, primary document, incorporated official exhibits, and retained/local source
   availability. Fetch missing bytes only from SEC and record exact hashes.
2. For each company, inspect raw inline-XBRL facts, contexts, units, scale/sign, statement table
   placement, accounting basis, consolidation scope, and incorporation relationships. Do not infer
   a fact from prose or arithmetic.
3. Classify each row:
   - generic inline-XBRL recovery using standard US-GAAP/IFRS concepts;
   - generic incorporated-exhibit recovery with explicit SEC linkage;
   - extension-tag mapping needing an approved canonical definition;
   - unstructured statement requiring LLM candidate extraction;
   - still unsupported because source/identity/currency/scope is missing.
4. For every deterministically recoverable row, list the minimum Graham-required anchors available,
   exact concept names, periods, units/currency, value anchors, and whether statements reconcile.
   Check the accounting equation and classified-current bounds where applicable.
5. Trace existing owners: raw cover/primary retention, `ifrs_workbook`/EDINET canonical adapters,
   normalization's IFRS translation and provenance, artifact storage, and importer. Record the
   smallest generic reuse path. No company-specific exception.
6. Quantify what remains for LLM after deterministic recovery. Recommend the next implementation
   increment and which frozen LLM cases still represent real fallback needs.
7. Evidence goes under `local/raw-sec-statement-recovery-2026-09-28/`: complete CSV/JSON inventory,
   README, reproducible read-only verifier, hashes, and reuse decision. Do not duplicate large SEC
   documents in Git.
8. Checks: every one of 18 appears once; source hashes and cited anchors resolve; category totals
   equal 18; reconciliation evidence is explicit; verifier is deterministic; `git diff --check`.

## Return protocol

Update only Developer update in `local/dev19-review-map.md`. Report the trace, exact counts/tickers,
reusable owners, checks, evidence gaps, and one recommended next increment. Never mark accepted.
