# Signup, database access, and agent model

Researched 2026-09-25. Recommendations, not approved vendor choices. No accounts,
dependencies, migrations, or model calls were created by this research.
Preserves P-03, P-06/P-07/P-10, P-12 and P-14: Python owns shared/private data,
users stay isolated, and local testing precedes deployment.

## Authentication and signup

Recommend Clerk for the first release: use its Next.js signup/signin components
and verify requests in FastAPI using its Python SDK. This avoids building password
storage, verification, and recovery ourselves. Clerk lists a free Hobby plan up to
50,000 monthly retained users, a billing measure distinct from total registrations.
Paid features and growth need a separate budget check.
[Pricing](https://clerk.com/pricing),
[official Python SDK](https://github.com/clerk/clerk-sdk-python).

Proposed signup methods: Google plus verified email code. Owner chooses methods
and whether the initial release is invitation-only or open signup. Password signup
is not required unless requested. Production social credentials and email delivery
must be tested, not inferred from development-mode success.

Clerk is hosted even when our app runs on localhost. Local-first is not necessarily
offline-only: owner approval is needed for this external identity dependency.
If self-hosted identity is required, consider Better Auth in Next.js with its
JWT/JWKS integration for FastAPI. That adds auth storage, email operations, and
cross-runtime session handling; it is not the recommended minimal first choice.
[Better Auth JWT integration](https://better-auth.com/docs/plugins/jwt).

### Identity and permissions contract

- FastAPI verifies token signature, issuer, expiry, expected audience where used,
  and authorized frontend origin. Never trust an owner ID submitted by a client.
- Map the verified issuer/subject to a stable internal PostgreSQL user ID. Email
  is not an ownership key. Create the default workspace idempotently at first
  authenticated use; a delayed webhook must not prevent first login.
- Scope every private query, AI run, event stream, cache, and tool to that user.
  Public company pages remain separate from authenticated personal data.
- Use authenticated fetch streaming for SSE if bearer headers are required;
  native EventSource does not supply arbitrary authorization headers. Never put
  bearer credentials in stream URLs. Refresh credentials before reconnecting.
- Validate webhook signatures and tolerate duplicate/out-of-order events. Handle
  account disable/deletion locally, revoke access, and stop new work. Long-running
  runs must recheck account status before private writes. Define a bounded session
  revocation policy; offline token verification alone does not give instant logout.
- Owner must approve private-data retention/deletion and account-linking policy.
  Linking identities must require proof, not a matching email address alone.
- Rate-limit signup and AI usage; cap concurrent runs and spend per account.
  Public signup must not expose an unlimited service-funded model key.

Acceptance: real signup → verification → signin → new workspace → logout/relogin;
duplicate first requests create one workspace; expired/wrong-origin tokens fail;
two users cannot access each other's workspace/run/stream even with guessed IDs;
disable/deletion blocks access; webhook replay is harmless. Test local and deployed
cookie/origin behavior. No development identity bypass can be enabled in production.

## PostgreSQL and Prisma

Prisma is a database access/migration tool, not PostgreSQL itself. Its official
client targets TypeScript/Node.js. It fits a TypeScript-owned backend; introducing
it here would give Next.js a second route into data already owned by FastAPI.
[Prisma TypeScript](https://www.prisma.io/typescript).

**Accepted in P-17:** use SQLAlchemy with Alembic migrations on Python and a
pinned PostgreSQL driver. SQLAlchemy Core can express explicit queries
and batch writes without requiring an object model for every record. Use one
migration owner; do not let Prisma and Alembic both manage the application schema.
[SQLAlchemy](https://docs.sqlalchemy.org/en/20/intro.html),
[Alembic](https://alembic.sqlalchemy.org/en/latest/).

The pinned dependencies, first migration, and verified evidence-object boundary
now exist. Extend the current persistence owners rather than copy existing
financial rules. Next.js receives generated
API types and calls FastAPI; it does not need a PostgreSQL client for type safety.
Keep exact decimals, JSONB validation, transactions, provenance, and tenant checks.
Verify migrations from empty and existing databases, bulk-import throughput,
rollback-compatible releases, and full-universe financial parity.

Prisma is not part of this architecture. Moving financial APIs and jobs to
TypeScript would require a new owner decision and a separate architecture change.

## Lowest-cost reliable agent

Owner correction (P-15): exclude OpenAI models. Replace the previous OpenAI/Gemini
shortlist with GLM, DeepSeek, Qwen, and hosted Llama. Recommendation: test Qwen3.8
Flash and GLM-5.3-Flash first, then compare the other candidates where affordable
hosted access is verified. This is an evaluation order, not a quality ranking.

Shortlist, standard paid text rates in USD per million tokens; no batch discounts:

| Candidate | Input | Output | Proposed evaluation role |
|---|---:|---:|---|
| Qwen3.8 Flash | $0.113 | $0.382 | Global deployment scope, Frankfurt endpoint listing |
| GLM-5.3-Flash | $0.15 | $0.50 | Current GLM low-cost candidate |
| GLM-4.7-FlashX | $0.07 | $0.40 | Lower-price challenger |
| DeepSeek | Not verified | Not verified | Verify current model/endpoint and pricing before testing |
| Llama 3.3 70B on Groq | Quote required | Quote required | Groq currently lists enterprise access, not a public rate |

[Alibaba pricing](https://www.alibabacloud.com/help/en/model-studio/model-pricing),
[Z.ai pricing](https://docs.z.ai/guides/overview/pricing),
[Groq model catalog](https://console.groq.com/docs/models).
This is a practical shortlist, not an exhaustive market ranking. Confirm account
access, tool/structured-output/streaming compatibility, lifecycle, and current rates
before adoption. DeepSeek's official pricing page could not be retrieved during
this check; do not reuse older prices. Z.ai also lists free Flash models, worth a
development comparison but not evidence of production capacity or reliability.
Other Llama hosts may have public rates; Groq's quote is not a model-wide price.

Use a hosted API initially; open weights do not require renting a GPU. Verify
processing region, retention/training policy, and commercial terms before sending
private research. An endpoint in Frankfurt with Global processing scope is not
a promise that all processing remains in the EU. Compatibility with an OpenAI
API wire format does not mean using an OpenAI model or sending data to OpenAI.

Illustration only: 20,000 uncached input tokens plus 3,000 billable output tokens
totalled across all calls would cost about $0.003406 on Qwen3.8 Flash, $0.0045
on GLM-5.3-Flash, or $0.0026 on GLM-4.7-FlashX. Ten thousand such runs would be
$34.06, $45, or $26 in text tokens. Real token counts differ by model. Repeated context,
reasoning, retries, search/tool fees, cache writes, long-context rates, and other
services change the bill. This is not a forecast of real research-run cost.

### Selection gate before choosing the production default

Create a small held-out set of real product tasks: finding a ticker, explaining
an adjustment with evidence, handling absent facts, creating/editing/removing a
formula, dependency-aware deletion, Japanese disclosures, ambiguous requests,
and hostile instructions embedded in filings. Repeat cases to expose instability.

Use the same available tools, task inputs, and budgets across candidates. Measure
correct completion, evidence accuracy, invalid edits, latency, total billed tokens,
tool fees, and retries. Security/ownership is enforced by the server regardless of
model performance; any observed unauthorized mutation blocks release.
Choose the lowest cost per correctly completed task meeting owner-approved quality
and latency targets. No benchmark has run, and no provider credentials are assumed.

Keep arithmetic in deterministic tools; retrieve relevant facts instead of putting
the whole company universe in context. Bound steps, output, retries, and per-user
spend. Store model identity and observed cost with each run. Begin with one provider;
add a stronger fallback only if failed-task evidence justifies its cost. A budget
exhaustion returns an explicit incomplete result, not invented financial answers.

## Decisions and order

1. Before storage implementation: approve Python-owned access/migrations or request
   a different boundary; record it before adding database dependencies.
2. Before private workspace integration: approve hosted Clerk, signup methods,
   open/invite-only access, and account lifecycle rules.
3. Before live agent tests: authorize a provider and small evaluation budget.
   Choose service-funded versus user-funded access separately from the model name.

No Railway account is needed for these local phases. Hosted auth and live model
tests do need their own provider configuration when those increments begin.
