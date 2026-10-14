"""Index release-bound source recurrence pages without changing source truth."""
from alembic import op
import sqlalchemy as sa

revision = "20261004_0019"
down_revision = "20261004_0018"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_index("ix_source_check_item_occurrence", "source_check_occurrence",
                    ["source_item_id", sa.text("occurrence_id DESC")])


def downgrade():
    raise RuntimeError("Correct retained-history access with a forward migration")
