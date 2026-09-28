# Market Screener delivery plan

Canonical goal, requirements, ownership, queued increments, and checks:
[implementation rollout](../docs/implementation-rollout.md).

Implementation continues on branch `codex/company-import`. Increments 1A, 1B, 2, and 3 are
accepted. Increment 4 is active; increments 5–11 remain queued. The owner authorized local commits.
Push and merge remain unauthorized. Product decisions P-01 through P-17 apply.

Reuse: existing source/normalization/derivation and React behavior stay; persistence
and data delivery change. No financial engine rewrite. Detailed boundaries and verification live
in the canonical plan, not duplicate schedules here.

## Increment register

| Increment | State | Evidence |
|---|---|---|
| 1A. Inventory committed code and storage boundaries | accepted | `local/worktree-bootstrap-2026-09-25/`; 77 citations independently verified |
| 1B. Preserve runtime data, integrate finished changes, and run full baseline | accepted | `local/dev3-review-map.md`, `local/baseline-2026-09-25/`, and verified external backups |
| 2. Local storage and evidence contract | accepted | `local/dev2-review-map.md`; real restart/read and failure checks passed |
| 3. Migrate one real company, then the retained universe | accepted | `local/company-import-plan-2026-09-27.md`; full engine-187 persistence and API restart gate passed |
| 4. Make ingestion durable and automatic | active | `local/durable-ingestion-plan-2026-09-29.md` |
| 5–11 | queued | `docs/implementation-rollout.md` |
