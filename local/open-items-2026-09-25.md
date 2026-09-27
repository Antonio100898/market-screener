# Open items

- Increments 1A and 1B are accepted: code/storage boundaries are verified, the
  real baseline is preserved, and engine 181 passed the full release gates.
- P-17 settled database ownership. Its first PostgreSQL/Alembic and immutable
  object-storage slice is implemented, runtime-verified, and accepted.

- Added `docs/auth-database-agent-choices.md`: hosted Clerk and low-cost model
  evaluation remain proposals. Python SQLAlchemy/Alembic is accepted in P-17.

- Increment 2 is accepted; increments 3–11 remain queued. See `docs/implementation-rollout.md`.
- Docker Desktop is blocked by a locked local `dockerInference` runtime entry.
  Increment 2's earlier real storage checks passed; repair Docker before the next
  storage integration run.
- Owner choices pending: freshness/outage policy, formula functions, AI edit confirmation, AI provider/funding, authentication, and hosted object-store choice.
- Later data migration, live AI tests, restore drills, deployment, and measured
  hosted costs remain unverified.
