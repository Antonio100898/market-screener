# Audited LLM extraction pilot harness

The harness builds 48 redacted provider request artifacts for six frozen SEC cases and four
providers. Live calls require an explicit flag and a spend cap no greater than USD 16.

```sh
python3 local/llm-pilot-harness-2026-09-28/pilot.py build
python3 local/llm-pilot-harness-2026-09-28/pilot.py self-test
python3 local/llm-pilot-harness-2026-09-28/pilot.py dry-run
```

Run the approved connectivity screen only after exporting all four provider keys:

```sh
python3 local/llm-pilot-harness-2026-09-28/pilot.py live-screen --live --cap-usd 16
```

The live runner uses Python's standard HTTP client. It writes the redacted request and ledger entry
before sending. It writes the response, request ID, usage, latency, calculated cost, schema result,
source checks, and field score immediately after each response. A provider stops on a configuration
or response-shape error. A request with an uncertain transport result is never retried.

`raw/` and `artifacts/` are ignored. Raw files are exact SEC copies whose hashes must match the
approved frozen manifest. Generated request artifacts contain public filing text but are not source
code and must not be committed as duplicate filing data.

## Cases

- AERO primary: seven classified IFRS statement fields.
- CIB primary: five unclassified-bank statement fields.
- CNI exhibit: seven fields from Exhibit 99.2 linked by the 40-F wrapper.
- AERO summary neighbour: one narrow request that must prefer the audited statement over summaries.
- CIB unclassified neighbour: current assets and current liabilities must be `not_found`.
- CNI wrapper neighbour: the wrapper is supplied without Exhibit 99.2, so values must be
  `not_found`.

Every provider receives the same semantic source view and common reduced schema. Only transport
fields differ. Each visible source ID maps to one frozen raw document hash and character span.
Tables are retained in full and in order. Relevant statement headings, accounting basis,
currency/scale, periods, links, and inline-XBRL fact attributes are retained. Style, scripts,
hidden facts, and unrelated prose are omitted deterministically; the builder never truncates a
view to fit a provider.

## Product decisions

P-02, P-03, P-04, P-09, and P-17 remain unchanged. This shadow harness creates candidate requests
only. It cannot update shared data, snapshots, or the dashboard.

## Limits

- A new checkout has no model behavior evidence until `live-screen` completes.
- The live screen is one sample per arm and case. It does not establish a stable ranking.
- Raw filing bytes and run artifacts are not yet registered through S3/PostgreSQL.
- The screen never updates shared facts, snapshots, or the dashboard.

See [provider configuration](provider-config-matrix.md), [cost bound](cost-report.md), and
[reuse decision](reuse-decision.md).
