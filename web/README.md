# Frontend runtimes

Next.js is available alongside the existing Vite fallback. Both reuse the same React dashboard and proxy data requests to FastAPI.

## Next.js locally

Start the existing FastAPI service separately, then run from `web/`:

```sh
npm ci
npm run dev:next
```

Open http://127.0.0.1:3000 for the public introduction, or http://127.0.0.1:3000/dashboard for the existing screener and portfolio. The dashboard remains client-rendered and excluded from indexing. Its existing JSON read is preserved until the complete API migration is accepted.

For a production build and server:

```sh
npm run build:next
npm run start:next
```

`SCREENER_API_ORIGIN` selects FastAPI's origin; the default is http://127.0.0.1:8000. Set it before starting development. Set it before both building and starting production: Next records external rewrites in the build. It is a server variable, not a browser secret. Keep API credentials in the existing token flow.

Both Next scripts bind to localhost. `npm run dev:next -- --port 3001` selects another frontend port. Production needs a Next server; its output is not copied into FastAPI's static directory.

## Vite fallback

`npm run dev`, `npm run build`, and `npm run preview` retain their existing behavior. `make dev` still uses Vite. `make build` still creates FastAPI's static UI. They use the same `SCREENER_API_ORIGIN` and proxy route list.

`npm test` checks the existing financial/display helpers and both proxy configurations. No financial calculation owner moved to Next.js.

## Public page types

`/stocks` reads bounded identity-only API pages; `/stocks/ABT` renders a strict, release-pinned company snapshot through `/api/v1`. Missing evidence stays missing. Unsupported/unavailable APIs return 503; an absent ticker returns 404. No GET starts ingestion or derivation. Neither route forwards browser tokens or private workspace records.

Set `SCREENER_SITE_ORIGIN` to the local frontend URL before building and starting, for example `http://127.0.0.1:3001`; this controls canonical and sitemap URLs. Production must use its approved public origin after deployment approval.

`/blog`, `/guides`, and `/product` each include an authored local sample draft. Drafts and unverified company snapshots are noindex and excluded from the sitemap. The API currently reports source validity as NOT_ASSESSED. Company indexing awaits a verified publication-readiness contract; editorial indexing awaits publication approval. Robots rules do not replace access control on private routes.

## Bounded screening preview

`/dashboard/api` is a separate, noindex preview. It reads the FastAPI `/api/v1` query contract and never reads `dashboard.json`. Python supplies filtering, ordering, Return Quality, full matching counts, global facets and the S&P 500 reference. The browser requests at most 100 rows per page. List, facets, reference and company detail use one published release. A new release resets the current query; checks run on focus, reconnect, visibility and once per minute while visible. This is release discovery, not an approved source freshness target.

The preview shows unverified snapshot validity. It has no tracking or portfolio mutations, private filters, ingestion or derive controls. Those workflows remain in `/dashboard`. Company details reuse the existing disclosed default zero-assumption view and strict switch through pinned API detail requests. Private view persistence and figure revision history require their own backend contracts. Build, real API browser checks and financial release acceptance remain required before cutover.
