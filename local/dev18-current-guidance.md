# Developer 18 — current guidance

Guidance revision: 2
Updated: 2026-09-28
Owner: the reviewing session
Developer session: fresh background session
State: **ACCEPTED — Review 2; stop**

Read this file and `local/dev18-review-map.md`. Do not commit, stash, switch branches, edit the
approved prompt, production code, plans, or owner decisions. Never print, copy, hash, or persist an
API key.

## Paths you own

- `local/llm-pilot-harness-2026-09-28/pilot.py`
- `local/llm-pilot-harness-2026-09-28/README.md`
- `local/llm-pilot-harness-2026-09-28/.gitignore`
- `local/llm-pilot-harness-2026-09-28/provider-config-matrix.md`
- `local/llm-pilot-harness-2026-09-28/live/**`
- Developer update in `local/dev18-review-map.md`

Read-only inputs include `.env`, generated dry-run artifacts, raw frozen inputs, the approved
prompt/schema/proposal, provider config matrix, and cost report.

## Required skills and owner decision

Read `~/.agents/skills/_shared/code-work.md`. Invoke `applying-feature`, `ponytail`,
`llm-behaviour`, `editing-system-prompts`, and `no-scaffolding-leaks`.

Owner decision, verbatim: "Approve the four-provider connectivity screen with a hard maximum spend
of USD 16. Run 48 standard calls one at a time across six frozen cases, baseline and approved
prompt, and four models. Stop a provider on a configuration error. Do not automatically retry a
call whose charge is uncertain. The screen cannot publish data."

## Current increment — Review 1 record corrections

**Correct shape from zero.** The runner persists the exact redacted request before each call,
sends one request through the documented provider transport, immediately persists raw response,
provider request ID, status, usage, latency, and cost, then validates JSON against the untouched
canonical schema and resolves every citation against the frozen source map. It never updates shared
facts, snapshots, or the dashboard.

The live screen is complete. Do not call any provider again. Correct only these review gaps:

1. `turns.md` starts with “Structured mode exposed no reasoning text,” but Fireworks returned and
   retained reasoning. Make this sentence data-driven and add a self-test that pins the visible-
   reasoning case.
2. Update `provider-config-matrix.md`: the earlier Fireworks statement that structured output
   disables reasoning is disproved for this model. Record the observed response and the resulting
   configuration limit without proposing a prompt or retry.
3. Do not commit duplicate request text. Add ignore rules for per-run `request.body` and
   `request.json`; they are deterministically reproducible from the accepted harness and their
   hashes remain in the ledger/meta files. Keep exact provider response bodies, parsed outputs,
   redacted headers, result files, ledger, summaries, and turn tables as evidence.
4. Rerun py_compile, self-test, live-artifact secret scan, ledger/summary total reconciliation, and
   `git diff --check`. Confirm no network call occurred during correction.

Original implementation requirements, retained for context:

1. Extend the accepted harness with the minimum HTTP transport and response adapters. Use Python
   standard library only unless an installed dependency is already required. Read keys from process
   environment. Redact credential-bearing headers from every artifact and exception.
2. Pin exactly the configurations in `provider-config-matrix.md`. Do not invent a fallback field.
   Before sending, locally re-run schema/config/context checks. A provider whose config cannot be
   constructed exactly is blocked before spend.
3. Add `live-screen --cap-usd 16`. It requires an explicit live flag, checks all four variables,
   and writes a run ledger before the first request. Refuse a cap above the owner-approved amount.
4. Order calls smallest case first. Run one call at a time and alternate baseline/candidate within
   each provider/case. Persist request before send. Persist the exact body and response headers/body
   after receipt. Never resend a request lacking a definitive provider result.
5. Stop only the affected provider on HTTP 400/401/402/403/404/422, model/config/schema refusal, or
   a response shape that contradicts the documented config. Stop the whole run before the next call
   if projected or observed total cost can exceed USD 16. No automatic retry of any uncertain
   charged failure. A clearly uncharged pre-send failure may be corrected only in code and reviewed
   before resuming.
6. For every response: record provider/model/request ID, exact timestamps, latency, finish/status,
   token usage, calculated cost, parsed JSON, canonical-schema result, source-ID resolution,
   evidence-quote match, and requested-field coverage. Model text has no visible reasoning in
   structured mode; record that fact rather than inferring a belief.
7. Score against frozen gold values/locations where available and the three negative neighbours.
   `found` must match printed value, unit/currency, period, scope, and cited source. `not_found` and
   `ambiguous` must match the case expectation. A schema-valid wrong value is a failed extraction.
8. Read each response as it lands. Stop an affected provider if the first turn shows a blocking
   systematic problem that makes later calls useless; record the first divergence. Do not edit the
   approved prompt during the run.
9. Tests before live: response adapters with frozen synthetic provider bodies; redaction; cap;
   pre-send ledger; canonical and citation rejection; crash/restart refuses uncertain duplicate;
   dry-run still makes zero network calls. Run self-test independently before the live command.
10. After completion, write `live/README.md`, `live/ledger.jsonl`, per-run redacted request/response
    artifacts, turn tables, provider totals, exact cost, and limitations. Behavior remains a
    one-sample connectivity observation, not a verified model ranking.

## Return protocol

Update only Developer update in `local/dev18-review-map.md`. Report exact changes, preflight tests,
every provider result, accepted/failed fields, cost, first divergences, stopped providers, and
whether another session touched owned files. Never mark accepted.
