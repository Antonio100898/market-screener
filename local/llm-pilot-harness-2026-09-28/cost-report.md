# Pilot cost bound

The first approved spend should be at most **$16** for the 48-call connectivity screen. Do not
approve the full A/B yet. Its conservative standard-call ceiling is **$80**.

| Provider | Connectivity calls | Connectivity ceiling | Full A/B calls | Full A/B ceiling |
|---|---:|---:|---:|---:|
| OpenAI GPT-6 Luna | 12 | $1.1235 | 60 | $5.6175 |
| Fireworks GLM 5.3 Flash | 12 | $0.8783 | 60 | $4.3914 |
| Gemini 3.5 Flash-Lite | 12 | $1.9040 | 60 | $9.5199 |
| Claude Sonnet 5 | 12 | $12.0373 | 60 | $60.1865 |
| **Total** | **48** | **$15.9431** | **240** | **$79.7153** |

The estimate uses each exact serialized request's UTF-8 bytes divided by three, rounded up. Three
bytes per token is below the usual four-byte approximation for English and financial text. It also
leaves room for Anthropic's documented denser Sonnet 5 tokenizer. Before live approval, use each
provider's token-count facility where available and stop if a measured request exceeds this bound.

Every run is costed as if it uses all 8,192 allowed output tokens. OpenAI's documented long-context
price applies whenever the input estimate exceeds 272,000 tokens. No cache discount, Batch
discount, free tier, or retry is assumed.

The connectivity screen is one baseline and one candidate call for each of six cases on each of
four providers. The full A/B is five alternating runs per arm for every case and provider. A
blocking integration error stops that provider. There is no automatic retry of a call that may
already have been charged.

Prices:

- [OpenAI pricing](https://developers.openai.com/api/docs/pricing)
- [Fireworks GLM 5.3 Flash](https://fireworks.ai/models/fireworks/glm-5p3-flash)
- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Claude Sonnet 5 pricing](https://platform.claude.com/docs/en/models/sonnet-5/whats-new-sonnet-5)
