# Market Screener

Owner decisions: [product.md](product.md). Target design:
[production architecture](docs/production-architecture.md) and
[personal calculations](docs/personal-calculations.md).
The design describes planned work. The implementation below remains the current
local application, with Graham screening as its existing calculation set.

Evaluates US-listed SEC filers against Benjamin Graham's Enterprising Investor
criteria (The Intelligent Investor, ch. 15), computed from primary SEC XBRL
filings — never aggregator fundamentals — with provenance for every figure.
The universe includes domestic filers and foreign 20-F/40-F filers when their
current annual report supplies a coherent US-GAAP or standard IFRS balance sheet
in one identifiable currency and its cover resolves the exact common-equity
security (including any depositary ratio). For non-USD statements, explicit
current and fiscal-date FX rates put a USD-listed share price on the reporting
basis; missing FX leaves valuation unavailable rather than guessed.
Tokyo-listed companies can also enter from Japan's official EDINET annual XBRL
reports and complete corrections when their security code matches the current JPX domestic-share list.
Japanese quotes and statements remain in yen; fields without a verified filing
concept remain blank.

Six criteria are scored: P/E < 10, current ratio ≥ 1.5, debt ≤ 1.1× net current
assets, positive EPS in each of the last 5 years, a current dividend, and price
≤ 1.2× tangible book value. Graham's earnings-growth test is reported but never
scored — it measured against a fixed 1966 base no modern year can honestly replace.

```
api/   Python: sources → normalisation → screens → FastAPI, plus the local store
web/   React SPA: one fetch of dashboard.json, all filtering client-side
```

## Setup

```sh
make install
export SEC_USER_AGENT="Your Name you@example.com"   # SEC requires a contact
```

## Data

From the dashboard toolbar or the command line — same jobs either way:

| Command | What it does |
|---|---|
| `make bulk` | first full load: SEC's 1.4 GB archive, every US filer |
| `make metadata` | sector, exchange, filer size from SEC's submissions archive |
| `make daily` | refetch only companies that filed since the last run |
| `make events` | material 8-K items — restatements, delisting notices, auditor changes |
| `make cover` | resolve the exact traded class and any depositary-share ratio from annual covers |
| `make quotes` | refresh every eligible quote and atomically rebuild the shared dashboard |
| `make export` | current RTH/pre/post quotes + 5y weekly closes, rebuild `dashboard.json` |
| `make derive` | recompute dashboard-eligible snapshots after an engine change — no refetching |
| `make derive-all` | recompute every cached snapshot, including deferred filers |

For Japanese annual filings, run `cd api && .venv/bin/python -m screener.sync edinet-import --from YYYY-MM-DD --to YYYY-MM-DD`.
The command asks for the EDINET API key without echoing or saving it. Add
`--edinet-code 6752` to limit the import to one Tokyo-listed company. Run
`make export` afterward to update Research.

GitHub Actions can run the same import from **Actions → EDINET sync**. Configure
the repository Actions secret `EDINET_API_KEY`; scheduled runs then import the
latest seven-day window on Japanese business days. Manual runs accept an exact
date range and optional four-digit security code. Each successful run preserves
the database and filing cache for the next run and publishes the rebuilt
`dashboard.json` as a 30-day workflow artifact.

Raw filings are cached as files; derived snapshots live in SQLite
(`~/.cache/graham-screener/screener.db`). Missing data is never treated as zero in
reported figures, history, or Graham verdicts. Company detail opens with its
disclosed zero-assumption view and can switch back to strict filing values. Return
Quality uses the same convention through a separate, labelled discovery overlay;
every substituted field is listed and the overlay never changes a Graham grade.

## Run

```sh
make dev     # API on :8000 + Vite on :5173
make test    # pytest + node --test
make share   # ngrok tunnel with a write-protecting token, usable from a phone
```

While the API server is running, it automatically runs `make quotes` when the
shared dashboard is at least one hour old. Research and Portfolio both read that
same atomic snapshot; there is no portfolio-only price overlay.

Key API routes: `GET /dashboard.json`, `GET /screen/enterprising/{ticker}`,
`GET /fundamentals/{ticker}` (audit trail: every figure with tag, form, accession).
