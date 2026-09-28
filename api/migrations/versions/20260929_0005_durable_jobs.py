"""Add durable schedule occurrences, jobs, and item outcomes.

Revision ID: 20260929_0005
Revises: 20260928_0004
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260929_0005"
down_revision: Union[str, Sequence[str], None] = "20260928_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "job_schedule_occurrence",
        sa.Column("occurrence_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("schedule_key", sa.Text(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(schedule_key) > 0",
            name="ck_job_schedule_occurrence_schedule_key",
        ),
        sa.PrimaryKeyConstraint(
            "occurrence_id",
            name="pk_job_schedule_occurrence",
        ),
        sa.UniqueConstraint(
            "schedule_key",
            "scheduled_for",
            name="uq_job_schedule_occurrence_identity",
        ),
    )
    op.create_table(
        "durable_job",
        sa.Column("job_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("occurrence_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ownership_generation", sa.BigInteger(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkpoint", postgresql.JSONB(), nullable=True),
        sa.Column("error_summary", sa.String(length=2000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(kind) > 0", name="ck_durable_job_kind"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'retry_wait', 'succeeded', 'failed')",
            name="ck_durable_job_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts",
            name="ck_durable_job_attempts",
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name="ck_durable_job_max_attempts",
        ),
        sa.CheckConstraint(
            "ownership_generation >= 0",
            name="ck_durable_job_ownership_generation",
        ),
        sa.CheckConstraint(
            "deadline_at IS NULL OR deadline_at > due_at",
            name="ck_durable_job_deadline",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND owner IS NOT NULL "
            "AND length(owner) > 0 AND lease_expires_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL AND finished_at IS NULL) "
            "OR (status <> 'running' AND owner IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_durable_job_active_ownership",
        ),
        sa.CheckConstraint(
            "(status = 'retry_wait' AND next_retry_at IS NOT NULL) "
            "OR (status <> 'retry_wait' AND next_retry_at IS NULL)",
            name="ck_durable_job_retry_time",
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed') AND finished_at IS NOT NULL) "
            "OR (status NOT IN ('succeeded', 'failed') AND finished_at IS NULL)",
            name="ck_durable_job_finished_time",
        ),
        sa.CheckConstraint(
            "error_summary IS NULL OR length(error_summary) <= 2000",
            name="ck_durable_job_error_summary",
        ),
        sa.ForeignKeyConstraint(
            ["occurrence_id"],
            ["job_schedule_occurrence.occurrence_id"],
            name="fk_durable_job_occurrence",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_durable_job"),
        sa.UniqueConstraint(
            "occurrence_id",
            name="uq_durable_job_occurrence",
        ),
    )
    op.create_index(
        "ix_durable_job_claim",
        "durable_job",
        ["status", "due_at", "next_retry_at", sa.text("priority DESC"), "job_id"],
        unique=False,
    )
    op.create_table(
        "durable_job_item",
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("item_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("outcome", postgresql.JSONB(), nullable=True),
        sa.Column("error_summary", sa.String(length=2000), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(item_key) > 0",
            name="ck_durable_job_item_key",
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name="ck_durable_job_item_status",
        ),
        sa.CheckConstraint(
            "attempts > 0",
            name="ck_durable_job_item_attempts",
        ),
        sa.CheckConstraint(
            "error_summary IS NULL OR length(error_summary) <= 2000",
            name="ck_durable_job_item_error_summary",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["durable_job.job_id"],
            name="fk_durable_job_item_job",
        ),
        sa.PrimaryKeyConstraint(
            "job_id",
            "item_key",
            name="pk_durable_job_item",
        ),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Durable job history is operational evidence; use a forward migration"
    )
