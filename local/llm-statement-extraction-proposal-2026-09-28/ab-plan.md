# Prompt A/B plan

Prompt behavior is **UNVERIFIED**. This plan starts only after approval and provider selection.

## Question

Does the proposed prompt improve exact, source-cited extraction without increasing invented,
derived, wrong-scope, or uncited values?

There is no existing prompt. The baseline is this neutral instruction:

```text
Extract the requested financial statement fields from the supplied official filing documents.
Return JSON matching the supplied schema.
```

The candidate arm uses `prompt.md`. Both arms receive the same schema, exact field definitions,
source manifest, and frozen document bytes.

## Frozen inputs

Use the three cases in `frozen/manifest.md` plus these neighbours:

1. AERO management-summary table versus the audited primary statement. Accept only the audited
   statement location.
2. CIB `AssetsCurrent` and `LiabilitiesCurrent`. The bank presents an unclassified balance sheet;
   both must be `not_found`, not derived from note rows.
3. CNI 40-F wrapper without Exhibit 99.2. The wrapper cites the statements but reports no statement
   values; every requested value must be `not_found`.

Before the first call, register every source through `store_evidence` and require the hashes in
`frozen/manifest.md`. Freeze one request artifact per input and store its SHA-256 with the result.

## Run protocol

- Same provider, model identifier, model settings, verified S3 source bytes, schema, and request in
  both arms.
- Five runs per arm per input minimum. Alternate baseline and candidate. Run one at a time.
- Retain the exact provider request and response, run and trace IDs, start/end times, token counts,
  latency, provider-reported model revision when available, and cost.
- Build one turn table per run: model text, call or response, returned data, and the belief the text
  shows. A run without visible reasoning is recorded as such; do not infer it.

Five runs per arm show direction only. They do not establish a stable defect rate.

## Scoring

Count a requested field as accepted only when every deterministic check in `README.md` passes.
Report per arm:

- accepted fields / requested fields;
- exact `found`, `not_found`, and `ambiguous` outcomes;
- schema failures;
- uncited or non-matching values;
- wrong period, unit, currency, scale, sign, basis, or scope;
- note, segment, summary, or arithmetic substitutions;
- latency, tokens, and cost.

Read every run before aggregates. The candidate is suitable for implementation only if its turns
show the evidence rule causing the better decision and the neighbour cases do not regress.
