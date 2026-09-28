# Live connectivity screen

Calls with definitive results: 19 of 48. Calculated standard cost: USD 5.08612760.
The screen writes shadow artifacts only. It cannot publish company data.

| Provider | Calls | Completed | Accepted | Failed | Unscored | Cost USD |
|---|---:|---:|---:|---:|---:|---:|
| openai | 3 | 2 | 14 | 0 | 7 | 0.0079258 |
| fireworks | 8 | 7 | 14 | 23 | 7 | 0.40490380 |
| google | 1 | 0 | 0 | 0 | 7 | 0 |
| anthropic | 7 | 6 | 8 | 22 | 7 | 4.673298 |

## First divergence or stop

- openai: `screen--openai--cni-exhibit--baseline--1` — HTTP 429: TPM limit 200000, requested 222435.
- fireworks: `screen--fireworks--cni-exhibit--baseline--1` — anchor mismatch, source did not resolve, evidence quote did not match.
- google: `screen--google--cni-wrapper-neighbour--baseline--1` — HTTP 503: model temporarily at high demand.
- anthropic: `screen--anthropic--cni-wrapper-neighbour--candidate--1` — canonical schema failed: $.results[0]: expected one oneOf branch, got 0.

## Provider stops

- google: `screen--google--cni-wrapper-neighbour--baseline--1` — HTTP 503: model temporarily at high demand. No retry.
- openai: `screen--openai--cni-exhibit--baseline--1` — HTTP 429: TPM limit 200000, requested 222435. No retry.
- fireworks: `screen--fireworks--aero-primary--candidate--1` — Fireworks finish_reason=length. No retry.
- anthropic: `screen--anthropic--aero-primary--baseline--1` — HTTP 400: credit balance too low. No retry.

This is one sample per arm and case. It proves connectivity only, not a stable model ranking.
Fireworks unexpectedly returned reasoning text despite the structured-output setting; it is retained in the raw response. Other providers exposed no reasoning text.
