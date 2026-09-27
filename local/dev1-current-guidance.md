# Developer 1 — current guidance

Guidance revision: 1
Updated: 2026-09-25
Owner: reviewing session
State: **ACCEPTED — inventory committed code and storage boundaries**

Read `AGENTS.md`, `product.md`, all linked product documents,
`docs/implementation-rollout.md`, and `local/dev1-review-map.md` before work.
The reviewer owns this file. Do not edit it.

## Paths you own

- `local/worktree-bootstrap-2026-09-25/**`
- The `## Developer update` section of `local/dev1-review-map.md`

Do not edit application code, product documents, source data, caches, or any other
`local/` file. Never commit, stash, checkout, reset, delete, install dependencies,
start services, or run network commands. Return `BLOCKED:` if another path is needed.

## Current increment — inventory committed code and storage boundaries

**Goal.** Establish the exact committed starting point and trace the existing owners
that local PostgreSQL and object storage must extend. This lets the next increment
reuse current financial behavior rather than create a second engine.

**Correct shape from zero.** Source adapters and retained evidence feed normalization;
`api/screener/store.py` owns persistence; derivation/export owns published rows;
FastAPI serves current contracts; React consumes `dashboard.json`. PostgreSQL and
object storage must enter behind these owners. This increment records the actual
callers, schemas, state transitions and tests before any implementation.

1. Record worktree path, branch, HEAD, remotes, clean/dirty state, Python/Node
   versions, dependency manifests, and available test commands. Do not record secrets.
2. Trace every current read/write path through source discovery, retained files,
   SQLite, normalization, derivation, price/FX application, dashboard export,
   FastAPI delivery and React loading. Name files, symbols and callers.
3. Inventory SQLite tables/owners from schema code without opening the shared runtime
   database. Identify transactions, uniqueness constraints, engine-version behavior,
   atomic publication, and private-data tables.
4. Inventory existing storage abstraction points and tests. State what can be extended,
   what is SQLite-specific, and the smallest coherent first PostgreSQL/object slice.
   Search for Docker, migration, PostgreSQL, S3 and configuration code before proposing
   a new file or dependency.
5. Write `README.md`, `code-state.json`, `flow.md`, and `reuse-decision.md` under the
   owned artifact directory. Validate JSON parses and every cited file/symbol exists.
6. Record machine headroom only as observed evidence. Do not start databases or full
   suites while free memory is unsafe. Mark runtime data, tests and service behavior
   `UNVERIFIED`; they belong to later checked increments.

## Return protocol

Update only `## Developer update` in `local/dev1-review-map.md`. Set state to
`READY_FOR_REVIEW` or `BLOCKED_EXTERNAL`. Include artifacts, exact validation commands,
results, reuse decision, gaps, and any concurrent changes observed. Send the same
concise return to the reviewer. Never approve your own work.
