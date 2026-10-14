"""Retain actual accepted source checks without rewriting historical releases."""
from alembic import op
import sqlalchemy as sa

revision = "20261004_0017"
down_revision = "20261004_0016"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.add_column("release_company_source", sa.Column("activation_source_item_id", sa.BigInteger(), nullable=True))
    op.add_column("release_company_source", sa.Column("accepted_occurrence_id", sa.BigInteger(), nullable=True))
    op.create_check_constraint("ck_release_source_witness_pair", "release_company_source",
        "(activation_source_item_id IS NULL) = (accepted_occurrence_id IS NULL)")
    op.create_foreign_key("fk_release_source_item_binding", "release_company_source", "source_observation",
        ["source_observation_id", "activation_source_item_id"], ["source_observation_id", "source_item_id"])
    op.create_foreign_key("fk_release_source_accepted_check", "release_company_source", "source_check_occurrence",
        ["accepted_occurrence_id", "activation_source_item_id", "source_observation_id"],
        ["occurrence_id", "source_item_id", "source_observation_id"])


def downgrade():
    raise RuntimeError("Activation witnesses are immutable history; use a forward migration")
