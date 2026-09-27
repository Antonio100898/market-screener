# Market Screener delivery plan

Canonical goal, requirements, ownership, queued increments, and checks:
[implementation rollout](../docs/implementation-rollout.md).

Implementation is active in the dedicated worktree on branch
`codex/market-screener-platform`. The source application starts at committed `main`
`697ef24750ea5a40f545073498168dc33070ef43`. The owner's finished changes were
integrated only in this worktree. Increments 1A, 1B, and 2 are accepted;
increments 3–11 remain queued. The owner authorized committing and pushing the accepted work to
`codex/market-screener-platform`. Product decisions P-01 through P-17 apply.

Reuse: existing source/normalization/derivation and React behavior stay; persistence
and data delivery change. No financial engine rewrite. Increment 2 is accepted;
increments 3–11 remain queued. Detailed boundaries and verification live in the
canonical plan, not duplicate schedules here.

## Increment register

| Increment | State | Evidence |
|---|---|---|
| 1A. Inventory committed code and storage boundaries | accepted | `local/worktree-bootstrap-2026-09-25/`; 77 citations independently verified |
| 1B. Preserve runtime data, integrate finished changes, and run full baseline | accepted | `local/dev3-review-map.md`, `local/baseline-2026-09-25/`, and verified external backups |
| 2. Local storage and evidence contract | accepted | `local/dev2-review-map.md`; real restart/read and failure checks passed |
| 3–11 | queued | `docs/implementation-rollout.md` |
