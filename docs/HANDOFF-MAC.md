# Continue Market Screener on Mac

Checkpoint: 2026-10-14. Branch: `codex/whole-platform-plan`.

## Owner's goal

Run the product locally with all supported tickers, including the Japanese
companies, and the same metrics as the existing main dashboard. Fix source
coverage and normalization across different filers. Keep missing evidence
distinct from zero. Show older accounting history with labels; Sony's current
calculations use IFRS, while older US-GAAP history stays separate.

The owner chose to push the code and rebuild data on Mac. A transfer of the
Windows data directory is optional, not a prerequisite. No further Windows
export or service recovery should run as part of this handoff.

Read `AGENTS.md`, `product.md`, and the linked product documents before editing.
Affected decisions: P-02/P-03/P-04/P-09 shared official data and provenance;
P-12/P-14 Next.js and local-first delivery; P-16 Return Quality; P-17 Python
database ownership. This checkpoint changes none of those decisions.

## Start the next session

Fetch and switch to this branch without overwriting existing Mac edits:

```sh
git fetch origin
git switch --track origin/codex/whole-platform-plan
```

If the local branch already exists, switch to it and use `git pull --ff-only`.
If either command would conflict with local changes, preserve those changes
before proceeding. Do not reset or force checkout.

Open this repository in Codex and ask:

> Read docs/HANDOFF-MAC.md. Continue local product delivery, beginning with
> checking this Mac's existing data and tools, rebuilding supported company
> data through the existing normalization owners, and verifying the dashboard.

Install native Mac dependencies into a new environment; Windows `.venv`,
`node_modules`, database services and service binaries were not pushed:

```sh
make install
make test
```

Use the locked web dependencies (`npm ci` from `web/`) for repeatable later
installs. Confirm Python and Node versions against the installed dependencies.
Run one heavy data/build workflow at a time after checking available memory.

## Code carried by this branch

- Shared company storage, source observations, publication, query API,
  historical figure contracts, private workspaces and research APIs.
- Python/Alembic migrations through `20261013_0022`.
- Next.js public pages and the API dashboard. `/dashboard` now renders the
  existing `BrowserApiDashboard`; the Vite implementation remains available.
- EDINET raw-filing reconstruction through `edinet_mapper`, `EvidenceLoader`,
  importer and ingestion, feeding the same canonical calculation owners.
- JGAAP parent equity and IFRS cash CapEx handling, older accounting history,
  and source-backed borrowing-presence checks that prevent false zero debt.
- Importer handling of complete, identity-matched `pending_facts` payloads,
  preserving their pending warning; incomplete markers remain unavailable.

Reuse these owners. Do not introduce another financial calculation path or
manufacture missing debt, inventory, shares, cash flow or FCF.

## Observed checks and limits

Engine revision: `196` (`api/screener/store.py`). Before this handoff:

- Full Python run: 3,995 passed, 679 storage integration tests skipped, one
  dependency warning. These skips do not prove PostgreSQL/S3 integration.
- Web run: 272 passed. That run preceded the latest default dashboard route
  edit; rerun web checks and exercise the route on Mac.
- Windows full-universe regression: 6,676 tasks completed; 6,674 returned
  rows, two foreign exclusions, all 38 Japanese tickers and Sony present.
  It recorded 3,011 changed companies and 3,051,074 field differences.
  These differences are observed, not financially accepted.
- A later Windows export was interrupted by Windows Update after 1,318
  worker records. The count is not a count of committed snapshots.
- The full PostgreSQL/S3 publication, API/Next build and browser verification
  have not completed. This is a work checkpoint, not a release.

Handoff checks on 2026-10-14:

- Extracted the staged Git tree into a clean Windows directory without the
  retained runtime cache. Ran `python -B -m pytest -q -p no:cacheprovider` from
  its `api/`, with bytecode and plugin autoload disabled: 3,994 passed,
  680 skipped, one warning, 75.45 seconds. This includes the genuine Alpen
  inputs. The additional skipped test needs optional retained market captures.
- Ran `node --test --test-concurrency=1 'test/*.test.mjs'` from the working
  `web/`: 272 passed, zero failed/skipped, 16.86 seconds. This run includes the
  current default route source. It does not prove a Next build or browser flow.
- A concurrent web run in the extracted directory, using a junction to the
  existing Windows dependencies, reported a render failure and stalled with
  WebSocket port conflicts. Only that owned test process family was stopped.
  That attempt remains failed/incomplete; fresh-clone web execution on Mac
  is still required. The passing working-directory run does not erase it.
- Independent index review confirmed the selected code and six small test
  files, excluded runtime data and credentials, and checked that the only
  serialization changes were Git line endings and four removed blank EOF
  lines. `git diff --cached --check` passed. Native test memory peaks were
  not measured. No service, publication or full data gate was run here.

Windows records, retained locally: `local/mac-checkpoint-clean-final2-tree-2026-10-14.json`,
`local/mac-checkpoint-clean-final2-python-2026-10-14.log`,
`local/mac-checkpoint-web-serial-2026-10-14.log`,
`local/mac-checkpoint-clean-final2-web-2026-10-14.log`, and
`local/mac-checkpoint-stage-final-review-2026-10-14.json`.

## Data on the Mac

First inspect what the existing Mac clone already has. Preserve its current
UI `dashboard.json` before deriving/exporting anything. Record its hash,
row count and identities. Preserve its source cache and SQLite database.

Git does not contain the full dashboard, source cache, SQLite snapshots,
PostgreSQL/S3 contents, prices, credentials or the Windows verification logs.
New calculations save new local data only after their source inputs exist.
Rebuild through existing source adapters and normalization; current online
responses are new observations and cannot be called the old Windows baseline.

The Windows UI baseline had 6,676 unique tickers/CIKs, including 38 `.T`
tickers. Baseline engine 181, 211,235,677 bytes, SHA-256:
`b4acfbd3694d0d18ef1998b43346e167fe27e69db2a4aff9a8cbdeeda9e3302c`.
That payload is not in this commit. Exact Windows comparison requires those
bytes; absent that baseline, report the comparison as unavailable.

The EDINET key was not found locally. GitHub has an encrypted workflow secret;
it is not a plaintext key available to Mac. Do not promise that all older
Japanese filings can be fetched without credentials. The already approved
Alpen export succeeded:
https://github.com/Antonio100898/market-screener/actions/runs/38442625807
Only that single export was authorized. Further cloud exports need owner
approval. Small genuine Alpen test inputs are retained separately from the
full data cache. Alpen source coverage observed six earnings years and three
cash-flow/FCF years; ten-year coverage is not yet established for every company.

If exact Windows continuation becomes useful, its untouched interrupted
database is under `local/full-universe-retained-replay-owned-2026-10-13/` on
Windows. Copy `export.db`, `export.db-wal` and `export.db-shm` together before
any SQLite open, preserve an untouched backup, then recover a separate working
copy. Do not delete sidecars or replay the old execution grant. The source
union is `local/full-universe-retained-input-union-2026-10-13/cache` (13,918
files, about 14 GB). Neither directory is part of this Git handoff.

## Next work, in order

1. Inspect the Mac's tools, cache, baseline payload and credentials without
   printing secrets. Run the source/test checks; distinguish optional retained
   source checks from missing required test dependencies.
2. Rebuild supported company data using existing SEC/EDINET/company-specific
   adapters. Retain exact source documents, currency, rows and provenance.
   Investigate missing Japanese CF/FCF, debt, inventory and share inputs before
   calling them genuine gaps. Do not infer shares from net income/EPS or partial
   voting disclosures. Preserve prior accounting labels.
3. Run the mandatory full UI-payload gate: full-universe regression, the actual
   export/enrichment path, `make audit`, extraction/provenance `make audit-filings`,
   and all UI-derived values. Explain every changed field and verdict using
   source evidence. Missing baseline/cache/credentials means unverified.
4. Bring up fresh local PostgreSQL/S3 using `compose.yaml` if Docker is available
   and compatible with this Mac. Python owns migrations; inspect existing local
   volumes first, migrate through Alembic, and use existing ingestion/publication
   owners. Do not create tables opportunistically or switch main's readers.
5. Publish the full supported cohort and verify object readbacks, query/detail
   identity, pagination, filtering, ratios, source history and stable cursor keys.
   Configure local storage credentials and `SCREENER_CURSOR_SECRET` consistently
   before importing API query owners. Windows preview helper scripts contain
   Windows paths; they are not ready-to-run Mac launchers.
6. Start FastAPI and Next.js with the same `SCREENER_API_ORIGIN`. The documented
   commands are in `web/README.md`; its older JSON-dashboard descriptions
   predate the route change described above. Build, then test `/dashboard`, `/dashboard/api`,
   public stocks, Sony and Japanese details in the browser. The new dashboard
   needs published API data; starting Next alone does not populate it.

Product-agent model/provider, credential funding, source freshness target and
hosted deployment remain owner decisions. P-15 excludes an OpenAI product-agent
model. Do not choose those product directions during the data rebuild.

## Git authority

The owner authorized this checkpoint commit and branch push on 2026-10-14.
This does not authorize future commits/pushes, merges, cloud export runs or
deployment. Keep the main branch unchanged and preserve local data/evidence.
