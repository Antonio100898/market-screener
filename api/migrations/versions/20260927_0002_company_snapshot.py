"""Store immutable company snapshots and their current selection.

Revision ID: 20260927_0002
Revises: 20260925_0001
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260927_0002"
down_revision: Union[str, Sequence[str], None] = "20260925_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "issuer",
        sa.Column("issuer_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("source_identifier", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_system IN ('SEC', 'EDINET')",
            name="ck_issuer_source_system",
        ),
        sa.CheckConstraint(
            "length(source_identifier) > 0",
            name="ck_issuer_source_identifier",
        ),
        sa.PrimaryKeyConstraint("issuer_id", name="pk_issuer"),
        sa.UniqueConstraint(
            "source_system",
            "source_identifier",
            name="uq_issuer_source_identifier",
        ),
    )
    op.create_table(
        "priced_security",
        sa.Column("security_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("issuer_id", sa.BigInteger(), nullable=False),
        sa.Column("security_identifier", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("exchange_code", sa.Text(), nullable=True),
        sa.Column("quote_currency", sa.String(length=3), nullable=True),
        sa.Column("security_title", sa.Text(), nullable=False),
        sa.Column("source_accession", sa.Text(), nullable=False),
        sa.Column("security_basis", sa.Text(), nullable=False),
        sa.Column("receipt_ratio", sa.Numeric(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(ticker) > 0",
            name="ck_priced_security_ticker",
        ),
        sa.CheckConstraint(
            "length(security_identifier) > 0",
            name="ck_priced_security_security_identifier",
        ),
        sa.CheckConstraint(
            "exchange_code IS NULL OR length(exchange_code) > 0",
            name="ck_priced_security_exchange_code",
        ),
        sa.CheckConstraint(
            "quote_currency IS NULL OR "
            "(length(quote_currency) = 3 AND quote_currency = upper(quote_currency))",
            name="ck_priced_security_quote_currency",
        ),
        sa.CheckConstraint(
            "length(security_title) > 0",
            name="ck_priced_security_security_title",
        ),
        sa.CheckConstraint(
            "length(source_accession) > 0",
            name="ck_priced_security_source_accession",
        ),
        sa.CheckConstraint(
            "length(security_basis) > 0",
            name="ck_priced_security_security_basis",
        ),
        sa.CheckConstraint(
            "receipt_ratio IS NULL OR receipt_ratio > 0",
            name="ck_priced_security_receipt_ratio",
        ),
        sa.ForeignKeyConstraint(
            ["issuer_id"],
            ["issuer.issuer_id"],
            name="fk_priced_security_issuer",
        ),
        sa.PrimaryKeyConstraint("security_id", name="pk_priced_security"),
        sa.UniqueConstraint(
            "issuer_id",
            "security_identifier",
            name="uq_priced_security_issuer_identifier",
        ),
    )
    op.create_table(
        "company_snapshot",
        sa.Column("snapshot_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("engine_revision", sa.Integer(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("canonical_payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "engine_revision > 0",
            name="ck_company_snapshot_engine_revision",
        ),
        sa.CheckConstraint(
            "length(payload_sha256) = 64",
            name="ck_company_snapshot_payload_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["priced_security.security_id"],
            name="fk_company_snapshot_security",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name="pk_company_snapshot"),
        sa.UniqueConstraint(
            "security_id",
            "engine_revision",
            "payload_sha256",
            name="uq_company_snapshot_identity",
        ),
        sa.UniqueConstraint(
            "security_id",
            "snapshot_id",
            name="uq_company_snapshot_security_snapshot",
        ),
    )
    op.create_table(
        "company_snapshot_artifact",
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(role) > 0",
            name="ck_company_snapshot_artifact_role",
        ),
        sa.ForeignKeyConstraint(
            ["content_sha256"],
            ["evidence_artifact.content_sha256"],
            name="fk_company_snapshot_artifact_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["company_snapshot.snapshot_id"],
            name="fk_company_snapshot_artifact_snapshot",
        ),
        sa.PrimaryKeyConstraint(
            "snapshot_id",
            "content_sha256",
            "role",
            name="pk_company_snapshot_artifact",
        ),
    )
    op.create_table(
        "current_company_snapshot",
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "selected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["priced_security.security_id"],
            name="fk_current_company_snapshot_security",
        ),
        sa.ForeignKeyConstraint(
            ["security_id", "snapshot_id"],
            ["company_snapshot.security_id", "company_snapshot.snapshot_id"],
            name="fk_current_company_snapshot_snapshot",
        ),
        sa.PrimaryKeyConstraint(
            "security_id",
            name="pk_current_company_snapshot",
        ),
    )


def downgrade() -> None:
    raise RuntimeError("Company snapshot history is immutable; use a forward migration")
