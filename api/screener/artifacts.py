from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from .object_store import ImmutableObjectStore, VerifiedObject
from .postgres import evidence_artifact


@dataclass(frozen=True)
class EvidenceArtifact:
    content_sha256: str
    object_key: str
    byte_size: int
    media_type: str
    created_at: datetime
    verified_at: datetime


class ArtifactRepository(Protocol):
    def add_verified(
        self, verified: VerifiedObject, media_type: str
    ) -> EvidenceArtifact: ...


class SqlArtifactRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def add_verified(
        self, verified: VerifiedObject, media_type: str
    ) -> EvidenceArtifact:
        values = {
            "content_sha256": verified.content_sha256,
            "object_key": verified.object_key,
            "byte_size": verified.byte_size,
            "media_type": media_type,
            "verified_at": verified.verified_at,
        }
        statement = insert(evidence_artifact).values(**values).on_conflict_do_nothing()
        with self.engine.begin() as connection:
            connection.execute(statement)
            row = connection.execute(
                select(evidence_artifact).where(
                    evidence_artifact.c.content_sha256 == verified.content_sha256
                )
            ).mappings().one()

        for field in ("object_key", "byte_size"):
            if row[field] != values[field]:
                raise RuntimeError("Stored artifact conflicts with its content address")
        return EvidenceArtifact(**row)


def store_evidence(
    data: bytes,
    media_type: str,
    object_store: ImmutableObjectStore,
    repository: ArtifactRepository,
) -> EvidenceArtifact:
    verified = object_store.put_verified(data, media_type)
    return repository.add_verified(verified, media_type)
