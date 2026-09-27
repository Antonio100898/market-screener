# Owner decisions

1. Develop and test the complete replacement locally before hosted testing; no Railway account needed to start. Recorded as P-14.
2. Jev is outside this rollout's scope.
3. Exclude OpenAI models for the product agent; evaluate GLM/DeepSeek/Qwen/Llama or other alternatives (P-15). No specific provider selected.
4. All Market Screener implementation work will use a dedicated worktree. Keep the current checkout for the owner's other session. Wait until that work finishes before creating the baseline or worktree.
5. Work may start immediately in `codex/market-screener-platform` from committed main while the owner's other session continues. Merge or rebase its finished work later, then repeat the affected baseline gates before migration acceptance.
6. Use SQLAlchemy and Alembic in Python as the single application PostgreSQL schema and migration owner. Do not add Prisma or direct Next.js database access (P-17).
