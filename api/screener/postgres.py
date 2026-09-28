from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

from .storage_config import StorageSettings


metadata = MetaData()

evidence_artifact = Table(
    "evidence_artifact",
    metadata,
    Column("content_sha256", String(64), primary_key=True),
    Column("object_key", Text, nullable=False),
    Column("byte_size", BigInteger, nullable=False),
    Column("media_type", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    Column("verified_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("byte_size >= 0", name="ck_evidence_artifact_byte_size"),
    UniqueConstraint("object_key", name="uq_evidence_artifact_object_key"),
)

issuer = Table(
    "issuer",
    metadata,
    Column("issuer_id", BigInteger, Identity(), nullable=False),
    Column("source_system", Text, nullable=False),
    Column("source_identifier", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    CheckConstraint(
        "source_system IN ('SEC', 'EDINET')",
        name="ck_issuer_source_system",
    ),
    CheckConstraint(
        "length(source_identifier) > 0",
        name="ck_issuer_source_identifier",
    ),
    PrimaryKeyConstraint("issuer_id", name="pk_issuer"),
    UniqueConstraint(
        "source_system",
        "source_identifier",
        name="uq_issuer_source_identifier",
    ),
)

priced_security = Table(
    "priced_security",
    metadata,
    Column("security_id", BigInteger, Identity(), nullable=False),
    Column(
        "issuer_id",
        BigInteger,
        ForeignKey("issuer.issuer_id", name="fk_priced_security_issuer"),
        nullable=False,
    ),
    Column("security_identifier", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    CheckConstraint(
        "length(security_identifier) > 0",
        name="ck_priced_security_security_identifier",
    ),
    PrimaryKeyConstraint("security_id", name="pk_priced_security"),
    UniqueConstraint(
        "security_identifier",
        name="uq_priced_security_identifier",
    ),
)

company_snapshot = Table(
    "company_snapshot",
    metadata,
    Column("snapshot_id", BigInteger, Identity(), nullable=False),
    Column(
        "security_id",
        BigInteger,
        ForeignKey(
            "priced_security.security_id",
            name="fk_company_snapshot_security",
        ),
        nullable=False,
    ),
    Column("engine_revision", Integer, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("snapshot_sha256", String(64), nullable=False),
    Column("canonical_payload", JSONB, nullable=False),
    Column("ticker", Text, nullable=False),
    Column("exchange_code", Text),
    Column("quote_currency", String(3)),
    Column("security_title", Text),
    Column("source_accession", Text),
    Column("security_basis", Text, nullable=False),
    Column("receipt_ratio", Numeric, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    CheckConstraint(
        "engine_revision > 0",
        name="ck_company_snapshot_engine_revision",
    ),
    CheckConstraint(
        "length(payload_sha256) = 64",
        name="ck_company_snapshot_payload_sha256",
    ),
    CheckConstraint(
        "length(snapshot_sha256) = 64",
        name="ck_company_snapshot_snapshot_sha256",
    ),
    CheckConstraint("length(ticker) > 0", name="ck_company_snapshot_ticker"),
    CheckConstraint(
        "exchange_code IS NULL OR length(exchange_code) > 0",
        name="ck_company_snapshot_exchange_code",
    ),
    CheckConstraint(
        "quote_currency IS NULL OR "
        "(length(quote_currency) = 3 AND quote_currency = upper(quote_currency))",
        name="ck_company_snapshot_quote_currency",
    ),
    CheckConstraint(
        "security_title IS NULL OR length(security_title) > 0",
        name="ck_company_snapshot_security_title",
    ),
    CheckConstraint(
        "source_accession IS NULL OR length(source_accession) > 0",
        name="ck_company_snapshot_source_accession",
    ),
    CheckConstraint(
        "length(security_basis) > 0",
        name="ck_company_snapshot_security_basis",
    ),
    CheckConstraint(
        "receipt_ratio IS NULL OR receipt_ratio > 0",
        name="ck_company_snapshot_receipt_ratio",
    ),
    PrimaryKeyConstraint("snapshot_id", name="pk_company_snapshot"),
    UniqueConstraint(
        "security_id",
        "snapshot_sha256",
        name="uq_company_snapshot_identity",
    ),
    UniqueConstraint(
        "security_id",
        "snapshot_id",
        name="uq_company_snapshot_security_snapshot",
    ),
)

company_snapshot_artifact = Table(
    "company_snapshot_artifact",
    metadata,
    Column("snapshot_id", BigInteger, nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("role", Text, nullable=False),
    CheckConstraint(
        "length(role) > 0",
        name="ck_company_snapshot_artifact_role",
    ),
    ForeignKeyConstraint(
        ["snapshot_id"],
        ["company_snapshot.snapshot_id"],
        name="fk_company_snapshot_artifact_snapshot",
    ),
    ForeignKeyConstraint(
        ["content_sha256"],
        ["evidence_artifact.content_sha256"],
        name="fk_company_snapshot_artifact_evidence",
    ),
    PrimaryKeyConstraint(
        "snapshot_id",
        "content_sha256",
        "role",
        name="pk_company_snapshot_artifact",
    ),
)

current_company_snapshot = Table(
    "current_company_snapshot",
    metadata,
    Column("security_id", BigInteger, nullable=False),
    Column("snapshot_id", BigInteger, nullable=False),
    Column(
        "selected_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    ForeignKeyConstraint(
        ["security_id"],
        ["priced_security.security_id"],
        name="fk_current_company_snapshot_security",
    ),
    ForeignKeyConstraint(
        ["security_id", "snapshot_id"],
        ["company_snapshot.security_id", "company_snapshot.snapshot_id"],
        name="fk_current_company_snapshot_snapshot",
    ),
    PrimaryKeyConstraint("security_id", name="pk_current_company_snapshot"),
)


def create_postgres_engine(settings: StorageSettings | None = None) -> Engine:
    return create_engine((settings or StorageSettings.from_env()).database_url)
