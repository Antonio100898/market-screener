"""Add generic source items and immutable observations.

Revision ID: 20260929_0006
Revises: 20260929_0005
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260929_0006"
down_revision: Union[str, Sequence[str], None] = "20260929_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_item",
        sa.Column("source_item_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("issuer_source_identifier", sa.Text(), nullable=True),
        sa.Column("parent_source_item_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_system IN ('SEC', 'EDINET')",
            name="ck_source_item_source_system",
        ),
        sa.CheckConstraint(
            "item_kind IN ('inventory', 'aggregate', 'filing', 'resource')",
            name="ck_source_item_kind",
        ),
        sa.CheckConstraint(
            "length(trim(source_key)) > 0",
            name="ck_source_item_source_key",
        ),
        sa.CheckConstraint(
            "issuer_source_identifier IS NULL "
            "OR length(trim(issuer_source_identifier)) > 0",
            name="ck_source_item_issuer_source_identifier",
        ),
        sa.CheckConstraint(
            "parent_source_item_id IS NULL "
            "OR parent_source_item_id <> source_item_id",
            name="ck_source_item_parent_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["parent_source_item_id", "source_system"],
            ["source_item.source_item_id", "source_item.source_system"],
            name="fk_source_item_parent",
        ),
        sa.PrimaryKeyConstraint("source_item_id", name="pk_source_item"),
        sa.UniqueConstraint(
            "source_item_id",
            "source_system",
            name="uq_source_item_id_system",
        ),
        sa.UniqueConstraint(
            "source_system",
            "item_kind",
            "source_key",
            name="uq_source_item_identity",
        ),
    )
    op.create_table(
        "source_observation",
        sa.Column(
            "source_observation_id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        sa.Column("source_item_id", sa.BigInteger(), nullable=False),
        sa.Column("observation_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("canonical_metadata", postgresql.JSONB(), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detecting_job_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "observation_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_observation_sha256",
        ),
        sa.CheckConstraint(
            "state IN ('present', 'pending', 'unavailable', 'removed')",
            name="ck_source_observation_state",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(canonical_metadata) = 'object'",
            name="ck_source_observation_metadata_object",
        ),
        sa.CheckConstraint(
            "(state = 'present' AND artifact_sha256 IS NOT NULL) "
            "OR (state <> 'present' AND artifact_sha256 IS NULL)",
            name="ck_source_observation_state_artifact",
        ),
        sa.CheckConstraint(
            "length(trim(source_url)) > 0",
            name="ck_source_observation_source_url",
        ),
        sa.CheckConstraint(
            "etag IS NULL OR length(trim(etag)) > 0",
            name="ck_source_observation_etag",
        ),
        sa.CheckConstraint(
            "last_modified IS NULL OR length(trim(last_modified)) > 0",
            name="ck_source_observation_last_modified",
        ),
        sa.ForeignKeyConstraint(
            ["source_item_id"],
            ["source_item.source_item_id"],
            name="fk_source_observation_item",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_sha256"],
            ["evidence_artifact.content_sha256"],
            name="fk_source_observation_artifact",
        ),
        sa.ForeignKeyConstraint(
            ["detecting_job_id"],
            ["durable_job.job_id"],
            name="fk_source_observation_job",
        ),
        sa.PrimaryKeyConstraint(
            "source_observation_id",
            name="pk_source_observation",
        ),
        sa.UniqueConstraint(
            "source_item_id",
            "observation_sha256",
            name="uq_source_observation_identity",
        ),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Source observation history is retained evidence; use a forward migration"
    )
