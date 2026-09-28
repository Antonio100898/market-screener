# Reuse decision

Affected product decisions: P-02, P-03, P-04, P-09, and P-17.

## Existing owners

- `local/llm-statement-extraction-proposal-2026-09-28/prompt.md` owns the approved extraction
  instructions. The harness reads its system-message section without editing it.
- `local/llm-statement-extraction-proposal-2026-09-28/candidate.schema.json` owns the canonical
  candidate contract. Every future response must pass this untouched schema after provider output.
- `local/llm-statement-extraction-proposal-2026-09-28/frozen/manifest.md` owns the accepted SEC
  document identities and hashes.
- `local/llm-statement-extraction-proposal-2026-09-28/ab-plan.md` owns the six frozen cases and the
  alternating baseline/candidate protocol.
- `api/screener/artifacts.py::store_evidence` owns durable content-addressed evidence. The dry-run
  harness does not duplicate that production owner.

## Searches and gap

`rg` found no existing source-view builder, provider request builder, extraction-run recorder, or
pilot runner. Installed project dependencies do not include provider SDKs, an HTML parser package,
or a JSON Schema validator.

## Chosen approach

Use one standard-library script under this directory. It verifies the frozen raw hashes, converts
visible filing content into an ordered source view with raw offsets, builds one common reduced
provider schema, emits redacted request artifacts, and prints the run plan. Provider-specific code
changes transport fields only. The dry-run command contains no HTTP client or provider call.

The source view keeps visible text, table/row/cell order, links, HTML IDs, and visible inline-XBRL
fact attributes. Each emitted source ID maps to a raw document hash and character span. It omits
style, script, and hidden inline-XBRL content because those are not visible filing evidence.

The provider schema is a mechanical common denominator: references are inlined, `oneOf` becomes
`anyOf`, `const` becomes a one-value `enum`, unsupported bounds and patterns are removed, and both
nullable locator fields are required. The untouched canonical schema remains the final validator,
so a provider's broader grammar cannot publish an invalid candidate.
