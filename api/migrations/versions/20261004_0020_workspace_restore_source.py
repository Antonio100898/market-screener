"""Retain the actual source revision of an explicit private presentation restore."""
from alembic import op
import sqlalchemy as sa

revision = "20261004_0020"
down_revision = "20261004_0019"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.add_column("research_workspace_revision", sa.Column("restored_from_revision", sa.BigInteger(), nullable=True))
    op.create_foreign_key("fk_research_workspace_revision_restore", "research_workspace_revision",
                          "research_workspace_revision", ["workspace_id", "restored_from_revision"],
                          ["workspace_id", "edit_revision"])
    op.create_check_constraint("ck_research_workspace_revision_restore", "research_workspace_revision",
                               "restored_from_revision IS NULL OR (restored_from_revision > 0 AND restored_from_revision < edit_revision)")


def downgrade():
    raise RuntimeError("Private workspace history is retained; use a forward migration")
