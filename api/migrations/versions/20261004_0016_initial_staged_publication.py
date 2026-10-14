"""Allow explicitly empty expected official state without a synthetic parent."""
from alembic import op
import sqlalchemy as sa

revision = "20261004_0016"
down_revision = "20261004_0015"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.alter_column("publication_candidate", "expected_parent_release_id", existing_type=sa.BigInteger(), nullable=True)


def downgrade():
    raise RuntimeError("Initial publication history is retained; use a forward migration")
