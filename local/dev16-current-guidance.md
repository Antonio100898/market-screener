# Developer 16 — current guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — proposal ready for owner; stop**

Read this file and `local/dev16-review-map.md`. This increment is investigation and proposal only.
Do not edit production code, tests, prompts used at runtime, plans, or owner decisions. Do not call
an LLM. Do not commit, stash, or switch branches.

## Paths you own

- `local/llm-statement-extraction-proposal-2026-09-28/**`
- Developer update in `local/dev16-review-map.md`

## Required skills and rules

Read `~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature`, `ponytail`,
`llm-behaviour`, `editing-system-prompts`, and `no-scaffolding-leaks`. The exact prompt is a
proposal for owner approval. Do not install it, test it against a model, or claim it works.

## Current increment — exact contract and prompt proposal

**Owner decision.** Deterministic extraction runs first. When it cannot produce a coherent standard
statement, an LLM may emit source-cited candidates. Store the filing, source location, model,
prompt revision, response, and checks. Deterministic verification owns publication. Start in shadow
mode; model-only output cannot change the dashboard.

**Correct shape from zero.** One fallback module receives retained official filing bytes plus an
explicit list of missing canonical fields. It emits a strict candidate object. A deterministic
verifier proves every value against cited source text/table structure, units, periods, currency,
scope, and accounting reconciliation. Immutable storage records the attempt. Only a later, separate
publication decision may feed the existing canonical adapter contract.

1. Trace the current Company Facts failure, canonical adapter contract, normalization readers,
   provenance requirements, retained artifact storage, and PostgreSQL immutable observation model.
   Record the reuse decision with symbols and paths.
2. Inspect the 18 incomplete-company rows and retained official files. Choose 3 frozen pilot cases
   that cover different statement layouts. Record exact accessions/hashes and which required fields
   deterministic extraction lacks. Do not fetch from non-official sources.
3. Define the minimum candidate JSON schema. Include value as an exact decimal string, canonical
   field, source label, unit/currency, period start/end, instant/duration, consolidation scope,
   exact page/table/row or HTML anchor, short verbatim evidence, source accession/document hash,
   and explicit `not_found`/`ambiguous` outcomes. Never allow guessed zero, inferred currency,
   arithmetic-only values, or an uncited value.
4. Define deterministic acceptance checks and immutable run metadata. State which checks can be
   automatic and which require human approval during shadow mode. Reuse the existing canonical
   adapter and artifact storage contracts; do not design a second normalization engine.
5. Write the exact proposed system/developer prompt in its own file. It must teach evidence-based
   extraction for a class of statements, not name today's companies. It must describe the reader's
   task, source boundary, output schema, ambiguity behavior, and self-check. No build/profile/tier
   narration. Keep it minimal.
6. Define the A/B plan required after owner approval: same provider/model/settings, three frozen
   cases plus neighbours, 5 runs per arm minimum, alternating arms, full recorded requests and
   outputs, turn tables, exact acceptance counts, cost/time. Because no baseline prompt exists,
   propose a neutral minimal baseline and explain what decision the candidate is meant to improve.
7. Identify decisions still needed before implementation. Ask only one recommended owner question
   in the return. Provider/model choice is a separate decision unless it changes the proposed text.
8. Return `READY_FOR_REVIEW` with proposal paths and evidence gaps. No model run means prompt
   behavior is **UNVERIFIED**.

## Return protocol

Update only Developer update in `local/dev16-review-map.md`. Report trace, exact proposal files,
frozen cases, reuse decision, checks performed on retained inputs, known limits, and one owner
decision. Never mark accepted.
