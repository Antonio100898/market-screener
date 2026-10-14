"""Admit typed declared history; native persistence and publication stay closed."""
from alembic import op


revision = "20261009_0021"
down_revision = "20261004_0020"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.drop_constraint("ck_exact_bundle_identity", "exact_figure_bundle", type_="check")
    op.create_check_constraint("ck_exact_bundle_identity", "exact_figure_bundle",
        "length(trim(request_key)) > 0 AND length(bundle_sha256) = 64 AND length(snapshot_sha256) = 64 "
        "AND engine_revision > 0 AND contract_revision IN (1,2,3) AND length(pipeline_run_key) > 0")


def downgrade():
    raise RuntimeError("Declared history is retained; use a forward migration")
