# LLM provider research for SEC statement extraction

Checked 2026-09-28. Prices are per million input/output tokens. The example cost assumes 100,000
input tokens and 5,000 output tokens. It excludes retries and provider-specific taxes.

## Finding

No public benchmark proves one current model is best for this exact contract. The economical choice
is the model with the lowest cost per **deterministically verified extraction**, not the lowest token
price. Run the approved prompt against the frozen AERO, CIB, and CNI cases before selecting a
production provider.

## Shortlist

1. **OpenAI `gpt-6-luna` — best theoretical first-pass value.** 1.05M context, Structured Outputs,
   Batch, $0.10/$0.50. Example: $0.0125 standard or $0.00625 Batch. The public model ID has no dated
   snapshot, so request and response hashes remain essential.
   [Model](https://developers.openai.com/api/docs/models/gpt-6-luna) ·
   [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
2. **Fireworks `accounts/fireworks/models/glm-5p3-flash` — best match to the proposed schema.**
   1.04M context, $0.15/$0.50, example $0.0175. Fireworks documents support for JSON Schema
   2020-12 features used here: `$defs`, `$ref`, `oneOf`, `anyOf`, `allOf`, patterns, and nulls.
   [Model and price](https://fireworks.ai/models/fireworks/glm-5p3-flash) ·
   [Structured outputs](https://docs.fireworks.ai/structured-responses/structured-response-formatting)
3. **Google `gemini-3.5-flash-lite` — best document-specialized Batch candidate.** Google describes
   it as optimized for document parsing and simple data extraction. It has 1.048M context and
   Structured Outputs. $0.30/$2.50 standard; Batch is half price. Example: $0.0425 standard or
   $0.02125 Batch.
   [Model](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite) ·
   [Pricing](https://ai.google.dev/gemini-api/docs/pricing) ·
   [Structured outputs](https://ai.google.dev/gemini-api/docs/structured-output)
4. **Together `deepseek-ai/DeepSeek-V4-Flash-0731` — cheapest dated open-model candidate.** 1.048M
   context, Structured Outputs, $0.14/$0.28, example $0.0154. The dated name helps audits, but the
   provider does not publicly guarantee immutable weights under that ID.
   [Catalog and price](https://docs.together.ai/docs/serverless/models) ·
   [Structured outputs](https://docs.together.ai/docs/inference/chat/structured-outputs)
5. **Anthropic `claude-sonnet-5` — quality ceiling, not first-pass value.** 1M context, Structured
   Outputs, $2/$10, example $0.25 standard or about $0.125 Batch. Use only as a comparison or retry
   when cheap models fail deterministic verification.
   [Model](https://platform.claude.com/docs/en/models/sonnet-5/whats-new-sonnet-5) ·
   [Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)

Mistral `mistral-small-2603` is inexpensive and has a stronger stated lifecycle guarantee, but its
256K context is a worse fit until the deterministic source-view reducer is proven.

## Recommendation

Run the frozen pilot with four models:

- `gpt-6-luna`
- Fireworks `glm-5p3-flash`
- `gemini-3.5-flash-lite`
- `claude-sonnet-5` as the expensive control

Use five alternating baseline/candidate runs per frozen case and model. Score exact value, source
location, unit, currency, scale, period, scope, correct abstention, schema acceptance, latency, and
actual billed cost. Choose the cheapest model whose candidates pass every deterministic check.

Likely production cascade, subject to the pilot: Luna or GLM first; a different-provider retry only
after verification fails; human review after a second failure. Never average answers or accept a
majority vote.

## Why the pilot is mandatory

[ExtractBench](https://www.alphaxiv.org/abs/2602.12247v2) found that schema-valid output did not
guarantee correct extraction, and every tested frontier model failed its very broad 369-field SEC
schema. Its useful design lesson is to keep each request narrow. This project already follows that
shape: ask only for deterministically missing fields, retain exact citations, and verify every value.

## Implementation notes

- Normalize the approved candidate schema per provider before a call. Provider schema subsets
  differ; schema acceptance is a gate, not a retry condition.
- Prefer Batch for the shadow pilot when turnaround up to 24 hours is acceptable.
- Send a deterministic source view when raw HTML exceeds the model limit. Retain the view and its
  mapping to raw SEC hashes. Never silently truncate.
- Public SEC filings contain no private customer data, but still record each provider's retention
  mode and exact request artifact.
