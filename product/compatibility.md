# Compatibility and release decisions

## Frontend choice

**P-12 — accepted:** Use Next.js for the frontend, with SEO and production use
as motivations. Next.js remains a React frontend. This is the target framework;
the current Vite application has not yet been migrated.

The public site includes company pages, blogs/articles, educational guides, and
product/use-case pages about financial analysis, fundamentals, and agent-assisted
research. The owner's goal is discovery by people and AI search tools, not only
access to the dashboard.

Proposed implementation: serve public content as meaningful rendered HTML with
stable URLs, titles, canonical links, and sitemap entries. Publish useful, sourced
content with clear authorship and update dates. Keep private
workspaces authenticated and excluded from indexing and public caches. Search
indexing controls are not access control. Search ranking is not guaranteed by
framework choice, and AI discovery or citation is not guaranteed either.

Content authoring, editorial review, and publishing tooling remain design choices;
this decision does not authorize automatic article generation or publication.
Verify rendered content, public links, metadata, and sitemap coverage for each
public page type, alongside private-route isolation.

FastAPI remains the proposed owner of financial APIs, automatic jobs, and agent
execution. Next.js handles page rendering and browser interaction. Keep financial
logic and agent execution out of duplicated frontend server implementations.

## Delivery order

**P-14 — accepted:** Build and test locally first: shared PostgreSQL data,
automatic jobs, API-driven screens without a runtime `dashboard.json` dependency,
Next.js, private rules, and AI. Test deployment only after these flows pass locally.
No Railway account or subscription is required to start. Local model API calls
may still require a provider account and incur token charges.

Local testing does not prove cloud networking, streaming, recovery, or operating
costs. Those require a later private hosted rehearsal before public launch.
The [incremental rollout](../docs/implementation-rollout.md) defines the gates.

## Compatibility scope

The owner asked how frontend and backend versions should remain compatible.
The following release mechanics remain technical proposals.

## Proposed contract

Deploy frontend and backend from the same release initially. Still negotiate the
API contract: an already-open browser can be older than the running server.
Equal release numbers alone cannot protect an old browser tab.

Keep these identities separate:

- Release/build ID: which application code is running.
- API contract major and capabilities: which request/response shapes are supported.
- Database migration revision: which storage schema exists; internal to deployment.
- Data revision: which shared official inputs a response uses.
- Metric engine revision: how standard derived values were calculated.
- Formula language and definition revision: how a user's formula is interpreted.
- Workspace revision: which personal layout and definitions the user selected.

An ordinary data refresh requires neither a frontend deployment nor an API version
change. A personal formula edit requires neither a deployment nor a global engine
change. A semantic change to a formula must not hide behind an unchanged revision.

## Proposed compatibility behavior

Additive changes preserve supported clients. Breaking API changes use a new major
contract or an explicit migration with a documented support window. Check old and
new clients against supported server contracts before release.

The browser checks server capabilities on load and after reconnect. An unsupported
client gets an explicit reload/upgrade response, not partial data interpreted with
the wrong schema. An unsupported saved formula gets an explicit migration/error
state; it must not silently acquire new meaning.

See [release mechanics](../docs/production-architecture.md#compatibility-and-deployment)
for the proposed implementation and tests.

