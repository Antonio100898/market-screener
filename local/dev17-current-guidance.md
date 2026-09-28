# Developer 17 — current guidance

Guidance revision: 1
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — Review 1; stop**

Read this file and `local/dev17-review-map.md`. Do not call any model. Do not commit, stash, switch
branches, or edit production code, tests, approved prompt text, plans, or owner decisions.

## Paths you own

- `local/llm-pilot-harness-2026-09-28/**`
- Developer update in `local/dev17-review-map.md`

Read-only inputs:

- `local/llm-statement-extraction-proposal-2026-09-28/**`
- retained SEC cache under `~/.cache/graham-screener/`
- `.env` only through environment variables; never print, copy, hash, or retain secrets

## Required skills and rules

Read `~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature`, `ponytail`,
`llm-behaviour`, `editing-system-prompts`, and `no-scaffolding-leaks`. The prompt is already
approved and must remain byte-for-byte unchanged. This increment makes no provider request.

## Current increment — audited pilot request harness

**Goal.** Assemble reproducible provider request artifacts for the frozen AERO, CIB, and CNI cases
and their negative neighbours, prove schema compatibility without remote calls, and calculate a
conservative spend bound. Model execution waits for owner approval.

**Correct shape from zero.** A deterministic source-view builder reads retained official bytes and
emits a lossless, ordered view with stable source IDs and a mapping to raw hashes. One request
builder combines that view, the approved prompt, requested-field definitions, and a provider-
compatible schema. The runner records exact requests before sending and exact responses after a
future call. Provider adapters change transport only; extraction meaning stays one contract.

1. Trace existing retained artifacts, approved prompt/schema, canonical field definitions, and
   available installed libraries. Record the reuse decision before editing.
2. Build deterministic source views for the three frozen cases and the three neighbour cases in
   `ab-plan.md`. Preserve statement title, accounting basis, currency/scale, periods, table order,
   row/column text, HTML anchors, document/accession/hash, and links between a 40-F and an
   incorporated exhibit. Every emitted source ID must resolve back to retained bytes.
3. Fail instead of truncating. Record raw bytes, view bytes, approximate tokens using a documented
   conservative formula, and the provider context limit. The same semantic view goes to every
   provider.
4. Assemble baseline and approved-candidate requests for OpenAI `gpt-6-luna`, Fireworks
   `accounts/fireworks/models/glm-5p3-flash`, Google `gemini-3.5-flash-lite`, and Anthropic
   `claude-sonnet-5`. Retain request artifacts with secrets removed. Do not alter prompt meaning.
5. Normalize JSON Schema only where a provider rejects unsupported syntax. Keep one canonical
   schema and record every mechanical transform. Add local validation proving each normalized
   schema still represents `found`, `not_found`, and `ambiguous` identically. If provider
   compatibility cannot be proved without a remote call, label it unverified.
6. Build a dry-run command that lists planned runs and never sends network traffic. The future live
   runner must alternate baseline/candidate, persist request before call, persist response and
   provider request ID after call, and stop on a blocking integration error. Do not implement
   retries that can duplicate charged calls.
7. Calculate two costs: a one-run-per-cell connectivity screen and the full five-runs-per-arm A/B.
   Use actual assembled character/byte sizes plus conservative provider-token assumptions and
   current prices in `local/llm-provider-research-2026-09-28.md`. State a maximum approval amount.
8. Tests: deterministic rebuild hashes; every source ID resolves; required evidence survives;
   negative neighbours omit prohibited evidence; schema semantic equivalence; dry run makes zero
   network calls; secret grep finds none; `git diff --check` on owned paths.
9. Return `READY_FOR_REVIEW` with raw checks, request/view hashes, run counts, cost bound, and one
   recommended spend question. Behavior remains **UNVERIFIED** because no model was called.

## Return protocol

Update only Developer update in `local/dev17-review-map.md`. Report exact files, trace, reuse,
checks, cost bound, limits, and whether another session touched owned files. Never mark accepted.
