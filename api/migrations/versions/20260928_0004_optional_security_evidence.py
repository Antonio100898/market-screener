"""Allow snapshots backed only by the official SEC ticker mapping.

Revision ID: 20260928_0004
Revises: 20260927_0003
"""
from typing import Sequence, Union

from alembic import op


revision: str = "20260928_0004"
down_revision: Union[str, Sequence[str], None] = "20260927_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_company_snapshot_security_title",
        "company_snapshot",
        type_="check",
    )
    op.drop_constraint(
        "ck_company_snapshot_source_accession",
        "company_snapshot",
        type_="check",
    )
    op.alter_column("company_snapshot", "security_title", nullable=True)
    op.alter_column("company_snapshot", "source_accession", nullable=True)
    op.create_check_constraint(
        "ck_company_snapshot_security_title",
        "company_snapshot",
        "security_title IS NULL OR length(security_title) > 0",
    )
    op.create_check_constraint(
        "ck_company_snapshot_source_accession",
        "company_snapshot",
        "source_accession IS NULL OR length(source_accession) > 0",
    )


def downgrade() -> None:
    raise RuntimeError("Snapshot evidence history is immutable; use a forward migration")
