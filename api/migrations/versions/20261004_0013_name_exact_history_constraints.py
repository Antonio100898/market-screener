"""Name the retained exact-history constraints without rewriting data."""
from alembic import op

revision = "20261004_0013"
down_revision = "20261004_0012"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.execute("ALTER TABLE figure_observation RENAME CONSTRAINT figure_observation_pkey TO pk_figure_observation")
    op.execute("ALTER TABLE exact_figure_bundle RENAME CONSTRAINT exact_figure_bundle_pkey TO pk_exact_figure_bundle")
    op.execute("ALTER TABLE exact_figure_bundle RENAME CONSTRAINT exact_figure_bundle_request_key_key TO uq_exact_figure_bundle_request")


def downgrade():
    raise RuntimeError("Exact history is retained; use a forward migration")
