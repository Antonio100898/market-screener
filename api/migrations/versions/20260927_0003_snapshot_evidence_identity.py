"""Bind security evidence and artifact sets to immutable snapshots.

Revision ID: 20260927_0003
Revises: 20260927_0002
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping, Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260927_0003"
down_revision: Union[str, Sequence[str], None] = "20260927_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "company_snapshot",
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "company_snapshot",
        sa.Column("ticker", sa.Text(), nullable=True),
    )
    op.add_column(
        "company_snapshot",
        sa.Column("exchange_code", sa.Text(), nullable=True),
    )
    op.add_column(
        "company_snapshot",
        sa.Column("quote_currency", sa.String(length=3), nullable=True),
    )
    op.add_column(
        "company_snapshot",
        sa.Column("security_title", sa.Text(), nullable=True),
    )
    op.add_column(
        "company_snapshot",
        sa.Column("source_accession", sa.Text(), nullable=True),
    )
    op.add_column(
        "company_snapshot",
        sa.Column("security_basis", sa.Text(), nullable=True),
    )
    op.add_column(
        "company_snapshot",
        sa.Column("receipt_ratio", sa.Numeric(), nullable=True),
    )

    connection = op.get_bind()
    snapshots = connection.execute(
        sa.text(
            """
            SELECT snapshot.snapshot_id,
                   snapshot.engine_revision,
                   snapshot.payload_sha256,
                   security.ticker,
                   security.exchange_code,
                   security.quote_currency,
                   security.security_title,
                   security.source_accession,
                   security.security_basis,
                   security.receipt_ratio
            FROM company_snapshot AS snapshot
            JOIN priced_security AS security
              ON security.security_id = snapshot.security_id
            ORDER BY snapshot.snapshot_id
            """
        )
    ).mappings()
    for snapshot in snapshots:
        artifacts = [
            {
                "content_sha256": row.content_sha256,
                "role": row.role,
            }
            for row in connection.execute(
                sa.text(
                    """
                    SELECT content_sha256, role
                    FROM company_snapshot_artifact
                    WHERE snapshot_id = :snapshot_id
                    ORDER BY content_sha256, role
                    """
                ),
                {"snapshot_id": snapshot["snapshot_id"]},
            )
        ]
        metadata = {
            field: snapshot[field]
            for field in (
                "ticker",
                "exchange_code",
                "quote_currency",
                "security_title",
                "source_accession",
                "security_basis",
                "receipt_ratio",
            )
        }
        connection.execute(
            sa.text(
                """
                UPDATE company_snapshot
                SET snapshot_sha256 = :snapshot_sha256,
                    ticker = :ticker,
                    exchange_code = :exchange_code,
                    quote_currency = :quote_currency,
                    security_title = :security_title,
                    source_accession = :source_accession,
                    security_basis = :security_basis,
                    receipt_ratio = :receipt_ratio
                WHERE snapshot_id = :snapshot_id
                """
            ),
            {
                "snapshot_id": snapshot["snapshot_id"],
                "snapshot_sha256": _snapshot_sha256(
                    snapshot["engine_revision"],
                    snapshot["payload_sha256"],
                    metadata,
                    artifacts,
                ),
                **metadata,
            },
        )

    for column in (
        "snapshot_sha256",
        "ticker",
        "security_title",
        "source_accession",
        "security_basis",
    ):
        op.alter_column("company_snapshot", column, nullable=False)

    op.create_check_constraint(
        "ck_company_snapshot_snapshot_sha256",
        "company_snapshot",
        "length(snapshot_sha256) = 64",
    )
    op.create_check_constraint(
        "ck_company_snapshot_ticker",
        "company_snapshot",
        "length(ticker) > 0",
    )
    op.create_check_constraint(
        "ck_company_snapshot_exchange_code",
        "company_snapshot",
        "exchange_code IS NULL OR length(exchange_code) > 0",
    )
    op.create_check_constraint(
        "ck_company_snapshot_quote_currency",
        "company_snapshot",
        "quote_currency IS NULL OR "
        "(length(quote_currency) = 3 AND quote_currency = upper(quote_currency))",
    )
    op.create_check_constraint(
        "ck_company_snapshot_security_title",
        "company_snapshot",
        "length(security_title) > 0",
    )
    op.create_check_constraint(
        "ck_company_snapshot_source_accession",
        "company_snapshot",
        "length(source_accession) > 0",
    )
    op.create_check_constraint(
        "ck_company_snapshot_security_basis",
        "company_snapshot",
        "length(security_basis) > 0",
    )
    op.create_check_constraint(
        "ck_company_snapshot_receipt_ratio",
        "company_snapshot",
        "receipt_ratio IS NULL OR receipt_ratio > 0",
    )
    op.drop_constraint(
        "uq_company_snapshot_identity",
        "company_snapshot",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_company_snapshot_identity",
        "company_snapshot",
        ["security_id", "snapshot_sha256"],
    )

    op.drop_constraint(
        "uq_priced_security_issuer_identifier",
        "priced_security",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_priced_security_identifier",
        "priced_security",
        ["security_identifier"],
    )
    for constraint in (
        "ck_priced_security_ticker",
        "ck_priced_security_exchange_code",
        "ck_priced_security_quote_currency",
        "ck_priced_security_security_title",
        "ck_priced_security_source_accession",
        "ck_priced_security_security_basis",
        "ck_priced_security_receipt_ratio",
    ):
        op.drop_constraint(constraint, "priced_security", type_="check")
    for column in (
        "ticker",
        "exchange_code",
        "quote_currency",
        "security_title",
        "source_accession",
        "security_basis",
        "receipt_ratio",
    ):
        op.drop_column("priced_security", column)


def downgrade() -> None:
    raise RuntimeError("Snapshot evidence history is immutable; use a forward migration")


def _snapshot_sha256(
    engine_revision: int,
    payload_sha256: str,
    metadata: Mapping[str, Any],
    artifacts: list[dict[str, str]],
) -> str:
    security = dict(metadata)
    security["receipt_ratio"] = _decimal_identity(metadata["receipt_ratio"])
    identity = {
        "engine_revision": engine_revision,
        "payload_sha256": payload_sha256,
        "security": security,
        "artifacts": artifacts,
    }
    canonical = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _decimal_identity(value: Decimal | None) -> str | None:
    return format(value.normalize(), "f") if value is not None else None
