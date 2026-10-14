from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
import hashlib
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

    def get(self, content_sha256: str) -> EvidenceArtifact: ...


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

    def get(self, content_sha256: str) -> EvidenceArtifact:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(evidence_artifact).where(
                    evidence_artifact.c.content_sha256 == content_sha256
                )
            ).mappings().one_or_none()
        if row is None:
            raise ValueError("verified evidence artifact does not exist")
        return EvidenceArtifact(**row)


def store_evidence(
    data: bytes,
    media_type: str,
    object_store: ImmutableObjectStore,
    repository: ArtifactRepository,
) -> EvidenceArtifact:
    verified = object_store.put_verified(data, media_type)
    return repository.add_verified(verified, media_type)


def read_registered_artifact(
    content_sha256: str,
    object_store: ImmutableObjectStore,
    repository: ArtifactRepository,
    *,
    maximum: int | None = None,
    label: str = "registered",
    verified_payloads: dict[str, bytes] | None = None,
) -> tuple[EvidenceArtifact, bytes]:
    """Read only registered, verified content-addressed bytes."""
    if type(content_sha256) is not str or not re.fullmatch(r"[a-f0-9]{64}", content_sha256):
        raise ValueError(f"invalid {label} artifact identity")
    artifact = repository.get(content_sha256)
    if (artifact.content_sha256 != content_sha256
            or artifact.object_key != f"raw/sha256/{content_sha256}"
            or type(artifact.byte_size) is not int or artifact.byte_size < 0
            or maximum is not None and artifact.byte_size > maximum):
        raise ValueError(f"invalid {label} artifact metadata")
    if verified_payloads is not None and content_sha256 in verified_payloads:
        payload = verified_payloads[content_sha256]
        if (type(payload) is not bytes or len(payload) != artifact.byte_size
                or hashlib.sha256(payload).hexdigest() != content_sha256):
            raise ValueError(f"invalid {label} cached artifact bytes")
    else:
        payload = object_store.read_verified(artifact.object_key, content_sha256, artifact.byte_size)
        if verified_payloads is not None:
            verified_payloads[content_sha256] = payload
    return artifact, payload
