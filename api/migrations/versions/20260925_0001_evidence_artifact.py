"""Create immutable evidence artifacts.

Revision ID: 20260925_0001
Revises:
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260925_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evidence_artifact",
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("byte_size >= 0", name="ck_evidence_artifact_byte_size"),
        sa.PrimaryKeyConstraint("content_sha256"),
        sa.UniqueConstraint("object_key", name="uq_evidence_artifact_object_key"),
    )


def downgrade() -> None:
    raise RuntimeError("Evidence is immutable; roll back with a forward migration")
