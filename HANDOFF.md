# Market Screener handoff

## Start here

- Repository: `https://github.com/Antonio100898/market-screener.git`
- Base branch: `main`
- Verified commit: `8cd7dd04e1a23454d0d0254e1d9f6b3e531ce3b2`
- Read `AGENTS.md`, then `product.md` and the linked `product/*.md` files before work.
- Use an isolated branch or worktree. Keep `main` available as the working baseline.

## What exists

- The existing Python extraction, normalization, financial calculations, FastAPI,
  React/Vite dashboard, SQLite database, and `dashboard.json` flow still run.
- Return Quality follows P-11, P-13, and P-16. Full Python, web, payload regression,
  audit, and filing-audit gates passed at the verified commit.
- PostgreSQL 18, SQLAlchemy, Alembic, psycopg, and an S3-compatible SeaweedFS
  service are defined in `compose.yaml`.
- Alembic revision `20260925_0001` creates the first immutable
  `evidence_artifact` table.
- `api/screener/artifacts.py` writes content-addressed evidence to S3, verifies
  its size and SHA-256, then records it in PostgreSQL.
- Real PostgreSQL/S3 write, failure, restart, and retained-read checks passed on
  the Windows machine before handoff.

## What does not exist yet

- Company fundamentals have not been migrated into PostgreSQL.
- The retained filing universe has not been uploaded into S3.
- The dashboard still downloads `dashboard.json` and filters in the browser.
- FastAPI list/detail endpoints backed by PostgreSQL are not implemented.
- The frontend is still React/Vite; the approved Next.js migration has not begun.
- Authentication, private workspaces, editable formulas, and the product AI agent
  are designs only.

## Mac setup

```sh
git clone https://github.com/Antonio100898/market-screener.git
cd market-screener
git switch -c codex/company-import origin/main
cp .env.example .env
make install
docker compose up -d --wait postgres seaweedfs
cd api
RUN_STORAGE_INTEGRATION=1 .venv/bin/python -m pytest -q tests/integration/test_artifact_storage.py
cd ..
make test
```

Do not commit `.env`, databases, filing caches, generated payloads, or provider
credentials.

## Runtime data to transfer separately

The real baseline is not in Git. It is stored on the Windows machine at:

`C:\Users\amwor\.codex\baselines\market-screener\baseline-2026-09-25`

| File | Size | SHA-256 |
|---|---:|---|
| `screener.db` | 591,175,680 bytes | `5BA48CDFD4DC2C92D7A50C31D0F93964B0F295F4992315865BCBDBAD3C11E0CA` |
| `dashboard.json` | 211,011,167 bytes | `AA590429396E2940B19169731521EC76934C11F7D43C3B3FA578DA8F15E010D1` |
| `source-cache.tar.zst` | 3,875,989,746 bytes | `74EED3F7393A63FF1DB224C9608E3AE0ECC22CFBE23F00E67C6D9FECBB0F40DC` |

After verifying the hashes on the Mac:

- Put `screener.db` at `~/.cache/graham-screener/screener.db`.
- Put `dashboard.json` at `api/screener/static/dashboard.json`.
- Extract `source-cache.tar.zst` into `~/.cache/graham-screener/`.

The archive contains 27,089 retained source files. Keep the three transferred
files unchanged as the rollback and regression baseline.

## Next increment

Implement increment 3 from `docs/implementation-rollout.md`:

1. Import one SEC company and one supported Japanese company from retained
   evidence through S3 and PostgreSQL.
2. Preserve the existing canonical values and per-figure provenance.
3. Make repeated import idempotent and restartable.
4. Read the imported companies through FastAPI and compare them with the SQLite
   and preserved `dashboard.json` results.
5. Extend to the retained universe only after both company paths pass review.

This increment must preserve P-02, P-03, P-04, P-09, and P-17. Do not refetch
source documents to repair an import or calculation defect.

## Product decisions that remain open

These do not block the two-company import:

- freshness and outage behavior;
- first supported personal-formula functions;
- immediate AI edits with undo versus confirmation before apply;
- authentication provider;
- AI provider and user-funded versus service-funded access;
- hosted S3-compatible provider for deployment.

Ask the product owner before implementing behavior that depends on one of these
choices.

## Windows machine notes

- Local and remote `main` point to the verified commit above.
- The pre-merge Windows working state is preserved as Git stash
  `pre-main-update-2026-09-27`; inspect it before dropping it.
- Docker Desktop 4.60.1 fails while creating Windows Unix-socket reparse points.
  Two runtime directories were moved to timestamped `.stale-*` siblings; no
  Docker volume or project data was removed. This Windows-only failure does not
  change the Mac setup or accepted storage evidence.
