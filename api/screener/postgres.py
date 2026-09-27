from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
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


def create_postgres_engine(settings: StorageSettings | None = None) -> Engine:
    return create_engine((settings or StorageSettings.from_env()).database_url)
