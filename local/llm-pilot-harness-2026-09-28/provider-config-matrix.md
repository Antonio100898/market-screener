# Provider configuration matrix

Checked against current official documentation on 2026-09-28. These are the exact redacted request
bodies emitted by `pilot.py`. No provider request has been made, so access, latency, billed tokens,
schema compilation, and model behavior remain **UNVERIFIED**.

All four configurations are `CONFIG_READY`: every included option is documented for the named
endpoint and model. Options without model-specific proof are omitted. A connectivity error blocks
that provider; the runner must not guess another field or retry a possibly charged request.

| Provider | Endpoint and model | Structured output | Thinking and sampling | Storage | Success path | Failure path | Context fit |
|---|---|---|---|---|---|---|---|
| OpenAI | `POST https://api.openai.com/v1/responses`; `gpt-6-luna` | `text.format={type:json_schema,name:statement_candidate_v1,strict:true,schema:...}` | `reasoning.effort=none`; temperature, top-p, and seed omitted | `store=false` | Require `status=completed`; scan `output[*].content[*]` for `type=output_text`, then read `.text` | Block on HTTP error, `status=incomplete/failed`, `error`, `incomplete_details`, `type=refusal`, absent text, or canonical-schema failure | 1,050,000; largest upper estimate 785,506 input + 8,192 output |
| Fireworks | `POST https://api.fireworks.ai/inference/v1/chat/completions`; `accounts/fireworks/models/glm-5p3-flash` | `response_format={type:json_schema,json_schema:{name,schema}}` | Reasoning option omitted; JSON-schema mode disables reasoning output. Temperature, top-p, top-k, and seed omitted | Chat Completions has zero data retention by default; no `store` field sent | Require HTTP 2xx and `choices[0].finish_reason=stop`; read `choices[0].message.content` | Block on HTTP error, non-`stop` finish, absent content, or canonical-schema failure | 1,040,000; largest upper estimate 785,493 input + 8,192 output |
| Google | `POST https://generativelanguage.googleapis.com/v1/interactions`; `gemini-3.5-flash-lite` | `response_format={type:text,mime_type:application/json,schema:...}` | `generation_config.thinking_level=minimal`; `thinking_summaries=none`; temperature, top-p, top-k, and seed omitted | `store=false` | Require `status=completed`; scan `steps[*]` for `type=model_output`, then `content[*][type=text].text` | Block on HTTP error, `failed/incomplete/cancelled/requires_action`, non-empty `errors`, absent text, or canonical-schema failure | 1,048,576; largest upper estimate 785,482 input + 8,192 output |
| Anthropic | `POST https://api.anthropic.com/v1/messages`; `claude-sonnet-5` | `output_config.format={type:json_schema,schema:...}` | `thinking.type=disabled`; `output_config.effort=low`; temperature, top-p, and top-k omitted | No per-request storage flag. Standard API retention applies unless the organization has ZDR | Require HTTP 2xx and `stop_reason=end_turn`; scan `content[*][type=text].text` | Block on HTTP error, `max_tokens/refusal`, absent text, or canonical-schema failure | 1,000,000; largest upper estimate 785,459 input + 8,192 output |

## Exact request fields

### OpenAI

`model`, `input`, `reasoning`, `max_output_tokens=8192`, `text`, `store=false`, and
`truncation=disabled`. GPT-6 Luna documents the model ID, 1.05M context, 128K maximum output,
Structured Outputs, and `none` reasoning. Responses documents `store=false`, `text.format`,
completed/incomplete status, and explicit refusal handling.

- [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)
- [Responses request](https://developers.openai.com/api/reference/responses/create)
- [Structured Outputs and refusal handling](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Responses storage](https://developers.openai.com/api/docs/guides/migrate-to-responses)

### Fireworks

`model`, `messages`, `response_format`, `max_tokens=8192`, `stream=false`, `n=1`, and
`context_length_exceeded_behavior=error`. The last field prevents Fireworks' documented default
output truncation from silently reducing the response budget. No sampling or reasoning field is
sent. The structured-output guide says `response_format` disables reasoning output and documents
the response content path.

- [GLM 5.3 Flash model ID, context, and price](https://fireworks.ai/models/fireworks/glm-5p3-flash)
- [Chat Completions request and response](https://docs.fireworks.ai/api-reference/post-chatcompletions)
- [Structured Outputs](https://docs.fireworks.ai/structured-responses/structured-response-formatting)
- [Zero data retention](https://docs.fireworks.ai/guides/security_compliance/data_handling)

### Google

`model`, `system_instruction`, `input`, `response_format`, `generation_config`, `store=false`, and
`stream=false`. The model documents a 1,048,576-token input limit, 65,536-token output limit,
Structured Outputs, and thinking. The thinking guide specifically documents `minimal` for this
model. Interactions documents all request fields, response steps, status values, and stateless
storage.

- [Gemini 3.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
- [Interactions API reference](https://ai.google.dev/api/interactions-api-v1)
- [Structured Outputs](https://ai.google.dev/gemini-api/docs/structured-output)
- [Thinking levels](https://ai.google.dev/gemini-api/docs/thinking)
- [Stateless storage](https://ai.google.dev/gemini-api/docs/zdr)

### Anthropic

`model`, `max_tokens=8192`, `system`, `messages`, `thinking={type:disabled}`, and `output_config`
with `effort=low` and the JSON schema. Sonnet 5 rejects non-default sampling values, so none are
sent. It supports a 1M context at standard price and up to 128K output tokens.

- [Claude Sonnet 5 API behavior](https://platform.claude.com/docs/en/models/sonnet-5/whats-new-sonnet-5)
- [Messages API](https://platform.claude.com/docs/en/api/typescript/messages)
- [Structured Outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- [Effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- [API retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention)

## Schema form

Every provider receives the same reduced schema. It contains only `type`, `properties`, `required`,
`additionalProperties`, `items`, `anyOf`, and `enum`.

- Internal references are inlined.
- `oneOf` becomes `anyOf`.
- `const` becomes a one-value `enum`.
- `allOf`, patterns, and string, numeric, and array bounds are removed.
- `html_anchor` and `page` are both required and nullable.

This grammar preserves the three outcome shapes. It is intentionally broader for lexical and
locator constraints. Every response must also pass the untouched canonical schema; this second
check restores the removed constraints before any value can be accepted.
