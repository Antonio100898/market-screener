# Developer 17 — review map

Developer state: **IDLE — accepted after Review 1**
Current item: **Audited pilot request harness**
Review state: **ACCEPTED — Review 1**

The developer updates only `## Developer update`. The reviewer owns the header and reviewer response.

## Developer update

`READY_FOR_REVIEW` — dry-run pilot harness complete. Model behavior remains **UNVERIFIED** because
no provider or model call was made.

Files:

- `local/llm-pilot-harness-2026-09-28/pilot.py`
- `local/llm-pilot-harness-2026-09-28/README.md`
- `local/llm-pilot-harness-2026-09-28/reuse-decision.md`
- `local/llm-pilot-harness-2026-09-28/provider-config-matrix.md`
- `local/llm-pilot-harness-2026-09-28/cost-report.md`
- `local/llm-pilot-harness-2026-09-28/checks.md`
- ignored local inputs and outputs under `raw/` and `artifacts/`

Trace and reuse: the harness reads the approved prompt, untouched canonical schema, frozen
manifest identities, and six-case A/B plan. It uses Python's standard library. It does not replace
`store_evidence` or create a second production persistence owner. The correct future flow remains
persist request before call, persist exact response and provider request ID after call, validate
against the canonical schema, then run deterministic financial checks.

Source views retain every table in order, relevant statement prose, links, HTML anchors, and
visible inline-XBRL attributes. Every emitted source ID resolves to one frozen raw hash and
character span. Raw/view sizes and hashes are in `checks.md`. The 48 exact redacted request hashes
are in `artifacts/run-plan.json`; their ordered set hash is
`85f81d99e4bade8f9c9c5f409fd67a9c8ca207496380e529ecf7f41cf69cf8e2`.

Provider configuration: OpenAI, Fireworks, Google, and Anthropic exact endpoints, model IDs,
request fields, common schema form, reasoning settings, output limits, retention behavior,
response paths, failures, context limits, and source-view fit are documented in
`provider-config-matrix.md`. All included options are supported by current official docs. Runtime
acceptance remains unverified.

Checks:

```text
python3 -m py_compile local/llm-pilot-harness-2026-09-28/pilot.py
PASS
python3 local/llm-pilot-harness-2026-09-28/pilot.py self-test
PASS raw_hashes source_maps evidence schema dry_run_no_network secret_scan deterministic_rebuild
git diff --check -- local/llm-pilot-harness-2026-09-28 local/dev17-review-map.md
PASS
```

Run and spend bounds: 48 calls for the connectivity screen, conservative ceiling $15.9431; 240
calls for the full five-runs-per-arm A/B, ceiling $79.7153. The proposed next approval is **up to
$16 for the connectivity screen only**. No retry is allowed after an uncertain charged failure.

Limits: no key, model, response, behavior, latency, exact token count, or bill has been observed.
Raw and generated artifacts are not yet stored through S3/PostgreSQL. Another session did not
touch the owned harness files during this increment.

## Reviewer response

### Review 1 — accepted

Accepted. The reviewer independently reproduced the deterministic build, source-map, evidence,
schema, provider-request, secret, zero-network, and rebuild checks. All raw hashes match the frozen
manifest. The six semantic cases fit every selected context window under the conservative bound.
The common transport schema preserves all three outcome shapes and every response remains subject
to the untouched canonical schema. Exact documented configs exist for each named endpoint/model;
provider-side schema compilation and model behavior correctly remain unverified until a live call.
No model was called and no secret entered an artifact.
