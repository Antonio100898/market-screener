# Developer 18 — review map

Developer state: **IDLE — accepted after Review 2**
Current item: **Four-provider connectivity screen**
Review state: **ACCEPTED — Review 2**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

READY_FOR_REVIEW

### Review 1 correction return

- `turns.md` now derives its reasoning-visibility sentence from the retained responses. It states
  that Fireworks returned visible reasoning and that the other provider responses did not.
- The self-test pins Fireworks `reasoning_content`, its exact character count, the zero-reasoning
  cases for the other adapters, and the generated report sentence.
- `provider-config-matrix.md` now records that all eight Fireworks responses returned visible
  reasoning. It records the stopped AERO candidate's 8,192 reasoning tokens, empty answer, and
  `finish_reason=length`; JSON-schema mode did not reserve the output budget for JSON.
- `.gitignore` excludes each reproducible `request.body` and `request.json`. Request hashes remain
  in `request.meta.json` and `ledger.jsonl`. Exact response bodies, parsed output, redacted headers,
  results, summaries, and turn tables remain included. The unignored live evidence is about 704 KB.
- No provider command ran. The evidence remains 19 response bodies and USD `5.08612760`; the last
  ledger event is still the recorded Fireworks cost correction.

Observed correction checks:

```text
python3 -m py_compile local/llm-pilot-harness-2026-09-28/pilot.py
PASS

python3 local/llm-pilot-harness-2026-09-28/pilot.py self-test
PASS raw_hashes source_maps evidence schema live_protocol dry_run_no_network secret_scan deterministic_rebuild

live secret scan + summary/ledger/response/reasoning reconciliation
PASS secrets summary ledger responses reasoning-report no-network-record-change

ignored duplicate request artifact check + git diff --check
PASS duplicate request bodies ignored; diff clean
```

Implemented the approved live connectivity screen in
`local/llm-pilot-harness-2026-09-28/pilot.py` and documented its command in the adjacent
`README.md`. The runner uses the existing frozen requests and schema owners. It adds only standard
library HTTP transport, redacted pre-send artifacts, an append-only ledger, four response adapters,
the USD 16 cap, restart refusal, canonical/source/quote checks, frozen-gold scoring, per-run reports,
and cost accounting. It cannot call any publication or shared-data path.

Observed preflight:

```text
python3 -m py_compile local/llm-pilot-harness-2026-09-28/pilot.py
PASS

python3 local/llm-pilot-harness-2026-09-28/pilot.py self-test
PASS raw_hashes source_maps evidence schema live_protocol dry_run_no_network secret_scan deterministic_rebuild

live artifact scan against all four environment key values
PASS live artifacts contain no API key values

git diff --check -- owned code/docs/review map
PASS
```

The live screen received 19 definitive responses before every provider reached its required stop.
No request was retried. Total calculated standard cost was USD `5.08612760`, below the approved
USD 16 cap.

| Provider | Calls | Completed | Accepted | Failed | Unscored | Cost USD |
|---|---:|---:|---:|---:|---:|---:|
| OpenAI | 3 | 2 | 14 | 0 | 7 | 0.0079258 |
| Fireworks | 8 | 7 | 14 | 23 | 7 | 0.40490380 |
| Google | 1 | 0 | 0 | 0 | 7 | 0 |
| Anthropic | 7 | 6 | 8 | 22 | 7 | 4.673298 |

Provider stops:

- OpenAI: wrapper baseline and candidate were both 7/7. The next request stopped on HTTP 429:
  organization TPM limit 200,000, requested 222,435. This was not a balance error.
- Google: first request stopped on HTTP 503 because the model was temporarily at high demand.
- Fireworks: wrapper baseline and candidate were both 7/7. CNI and AERO values were numerically
  right but failed exact anchors and/or evidence quotes. The AERO candidate then consumed 8,192
  reasoning tokens, returned empty content with `finish_reason=length`, and stopped the provider.
  Fireworks returned visible `reasoning_content` despite the documented structured-output behavior.
- Anthropic: wrapper baseline was 7/7. Four responses failed the untouched canonical schema because
  required strings or checked locations were empty; the AERO one-field candidate was 1/1. The next
  request stopped on HTTP 400 because the credit balance was too low.

The first divergences, full per-run table, turn excerpts, request/response bytes, request IDs,
usage, latency, costs, and field-level reasons are under
`local/llm-pilot-harness-2026-09-28/live/`. The exact overview is `live/README.md`.

Reuse decision: kept the approved prompt, canonical schema, frozen source views, requests, and
provider configs unchanged. Extended their existing single-file harness; no new SDK, dependency,
publication owner, or extraction contract was added.

Skills used: `managed-delivery` fixed the return and stop gates; `applying-feature` kept the full
request-to-evidence path connected; `ponytail` selected the standard library and one runner;
`llm-behaviour` required per-response evidence and first-divergence records;
`editing-system-prompts` and `no-scaffolding-leaks` kept the approved prompt untouched;
`plain-writing` kept human reports direct.

No other session changed the owned paths during this increment. I did not commit.

## Reviewer response

### Review 1 — changes required

The live protocol, stops, cap, redaction, per-call evidence, and USD 5.08612760 total are accepted.
No retry or publication occurred. Before final acceptance, correct the false turn-table introduction,
record that Fireworks' actual reasoning behavior disproved the documented assumption, and exclude
the 27 MB of deterministically reproducible duplicate request bodies from Git while retaining their
hashes and all exact provider response evidence. No provider call is authorized for this correction.

### Review 2 — accepted

Accepted. The reviewer independently reproduced pycompile and the full harness self-test. The
turn-table statement now follows retained response evidence, Fireworks' observed reasoning behavior
supersedes the earlier documented assumption, and duplicate request bodies are ignored while exact
hashes and provider responses remain. No correction-time provider call occurred. The accepted live
result is 19 definitive attempts, no retries, no publication, and USD 5.08612760 calculated cost.
