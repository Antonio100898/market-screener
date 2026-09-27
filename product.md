# Market Screener product decisions

This is the index of decisions made by the product owner. Read it before working
on this repository. Detailed decisions live in `product/`.

## Authority

These decisions take priority over architecture proposals, implementation plans,
and existing behavior when deciding the product's intended outcome. They do not
claim the target features are implemented. Explicit new owner decisions can
change them; update the affected document and retain the decision history.

Before changing an affected area, name its decision IDs, check for conflicts,
and choose evidence that verifies the decisions still hold. A conflicting task
requires an explicit owner decision. Do not quietly weaken a requirement to fit
the current code. Recommendations and unanswered questions are not approved
product decisions.

## Accepted decisions

| ID | Owner decision | Details |
|---|---|---|
| P-01 | The product and repository are named Market Screener / `market-screener`. Graham is one calculation set. | This document |
| P-02 | All clients share the same official company data. | [Shared data](product/shared-data.md) |
| P-03 | Automatic jobs download source documents to S3. PostgreSQL stores fundamentals and shared derived fields. | [Shared data](product/shared-data.md) |
| P-04 | Default research must use the latest adjusted official numbers. Previously loaded data must not silently stay outdated. | [Shared data](product/shared-data.md) |
| P-05 | Refresh is automatic and does not depend on React or user activity. | [Shared data](product/shared-data.md) |
| P-06 | Each user can ask a dashboard AI to create, edit, and remove their calculations, rules, and columns. This is mandatory. | [Personal research](product/personal-research.md) |
| P-07 | Personal calculations and UI choices are separate from shared official data. | [Personal research](product/personal-research.md) |
| P-08 | Product decisions govern all repository work and must be checked when affected. | This document and `AGENTS.md` |
| P-09 | Track historical data revisions so users can see which number changed, its previous and new values, and the source of the adjustment. | [Shared data](product/shared-data.md#historical-change-history) |
| P-10 | Each user has a persistent private workspace. Its agent can inspect supported tickers' shared fundamentals, run calculations, and research with the user, while editing only private workspace data. | [Personal research](product/personal-research.md) |
| P-11 | Return Quality includes the ten-year median annual FCF/revenue. Higher positive margins raise the score. P-16 supersedes P-11's original equal-weight rule with the current percentile weights. | [Personal research](product/personal-research.md#return-quality) |
| P-12 | Use Next.js for the frontend, including public company pages, blogs, guides, and product pages so people and AI search tools can discover the financial research platform. | [Frontend and compatibility](product/compatibility.md#frontend-choice) |
| P-13 | Divide Return Quality by 1 + the ten-year median annual Total CapEx/OCF; higher capital spending relative to OCF reduces the score. | [Personal research](product/personal-research.md#return-quality) |
| P-17 | Python owns the application PostgreSQL schema and migrations through SQLAlchemy and Alembic. Next.js accesses application data through FastAPI, not Prisma or direct database queries. | [Shared data](product/shared-data.md#database-ownership) |

**P-14 — local-first delivery:** Build and test the new data, API, Next.js,
workspace, and AI flows locally before testing a hosted deployment. Railway
accounts and paid infrastructure are not prerequisites for development.
See [delivery order](product/compatibility.md#delivery-order).

**P-16 — always-available Return Quality:** Every ticker has a numeric 0–100
score. Negative returns lower it. Use available inputs and mark missing data.
Missing OCF or FCF makes the score 0. Apply known debt and CapEx penalties.
Investigate extraction bugs before treating gaps as genuine. P-16 supersedes
P-11's earlier equal-weight wording while retaining FCF/revenue as an input.
See [Return Quality](product/personal-research.md#return-quality).

## Questions requiring an owner decision

**P-15 — agent model choice:** Do not use an OpenAI model for the product agent.
Evaluate alternatives such as GLM, DeepSeek, Qwen, and Llama. No specific model
or hosting provider is selected yet. See [personal research](product/personal-research.md).

These are genuine product choices, not implied answers:

- How quickly must new source information reach users? What may remain visible
  during an outage or while a correction is being processed?
- Which sources and kinds of disclosure count as supported official data beyond
  the existing SEC/EDINET coverage? Earnings releases can precede statements.
- Which initial personal functions are sufficient? The capability is mandatory;
  its exact first function set is not yet approved.
- Should clear AI edit requests apply immediately with undo, or always require a
  preview confirmation? Ambiguous requests must be clarified either way.
- Should users connect their own AI provider credentials, use a service-funded
  provider, or have both options? Verify supported connection methods and billing
  before promising existing account/subscription access.

Technical recommendations and proposed defaults are in
[the architecture](docs/production-architecture.md),
[the personal-calculation design](docs/personal-calculations.md), and
[compatibility requirements](product/compatibility.md). They remain proposals
unless explicitly identified above as accepted owner decisions.

## Decision history

- 2026-09-25: Owner approved SQLAlchemy and Alembic as the single application
  database owner in Python and declined Prisma for this architecture (P-17).

- 2026-09-25: Owner required a score for every ticker and set Return Quality to
  0 when OCF or FCF is unavailable, even if other return inputs exist (P-16).
  This supersedes the positive-only harmonic mean and missing-score rules.
- 2026-09-25: Owner capped the Return Quality debt/equity reduction at 50% (P-16).
- 2026-09-25: Owner chose local development and end-to-end testing before Railway
  deployment testing (P-14). Hosting setup is deferred, not required to start.

- 2026-09-25: Owner approved dividing Return Quality by `1 + median(Total
  CapEx / OCF)` over the latest ten fiscal years (P-13). A 50% median reduces
  a score of 30 to 20.
- 2026-09-25: Owner expanded P-12 beyond company pages to public blogs and pages
  for discovery around financial analysis, fundamentals, and agent-assisted research.
- 2026-09-25: Owner selected Next.js for the frontend with SEO and production use
  as motivations (P-12). The current Vite implementation has not yet been migrated.
- 2026-09-25: Owner requested the ten-year median annual FCF/revenue in Return
  Quality and originally confirmed equal weight with ROE, ROIC, and RONTA
  (P-11). P-16 later superseded that weighting with the current percentile
  weights while retaining the FCF/revenue input.
- 2026-09-25: Owner clarified the shared fundamentals/private workspace boundary
  and required the workspace agent to inspect company data, calculate, and help
  with research (P-10). JSON workspace storage and AI provider/account options
  are design proposals to evaluate.
- 2026-09-25: Owner required a visible history of edits and adjustments to historical
  data (P-09), preserving the previous numbers and how they changed.
- 2026-09-25: Owner requested the Market Screener name, shared S3/PostgreSQL data,
  automatic refresh, latest adjusted numbers, personal AI-editable research, and
  this product decision hierarchy. Replaced the earlier architecture proposal.

The earlier ten-year evidence objective remains a coverage goal, subject to
verified source availability. It is separate from the age of an acceptable
balance-sheet fact and from the refresh schedule.
