from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from .durable_jobs import DurableJobRepository, LeaseToken
from .postgres import source_item, source_observation


SourceSystem = Literal["SEC", "EDINET"]
SourceItemKind = Literal["inventory", "aggregate", "filing", "resource"]
ObservationState = Literal["present", "pending", "unavailable", "removed"]
_SOURCE_SYSTEMS = {"SEC", "EDINET"}
_ITEM_KINDS = {"inventory", "aggregate", "filing", "resource"}
_STATES = {"present", "pending", "unavailable", "removed"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CREDENTIAL_QUERY_KEYS = {
    "key",
    "apikey",
    "xapikey",
    "accesskey",
    "token",
    "accesstoken",
    "secret",
    "clientsecret",
    "password",
    "subscriptionkey",
    "ocpapimsubscriptionkey",
    "authorization",
    "credential",
    "credentials",
    "signature",
    "signedtoken",
}


class SourceItemIdentityConflict(RuntimeError):
    pass


class SourceObservationIdentityConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceItemIdentity:
    source_system: SourceSystem
    item_kind: SourceItemKind
    source_key: str
    issuer_source_identifier: str | None = None
    parent_source_item_id: int | None = None


@dataclass(frozen=True)
class SourceItemRecord:
    source_item_id: int
    source_system: SourceSystem
    item_kind: SourceItemKind
    source_key: str
    issuer_source_identifier: str | None
    parent_source_item_id: int | None
    created_at: datetime


@dataclass(frozen=True)
class SourceObservationRecord:
    source_observation_id: int
    source_item_id: int
    observation_sha256: str
    state: ObservationState
    canonical_metadata: dict[str, Any]
    artifact_sha256: str | None
    source_url: str
    etag: str | None
    last_modified: str | None
    source_published_at: datetime | None
    detected_at: datetime
    detecting_job_id: int


@dataclass(frozen=True)
class StoredSourceObservation:
    item: SourceItemRecord
    observation: SourceObservationRecord
    created: bool


class SourceObservationRepository:
    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.engine = engine
        self._jobs = DurableJobRepository(engine)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def record(
        self,
        lease: LeaseToken,
        *,
        item: SourceItemIdentity,
        item_outcome_key: str,
        state: ObservationState,
        metadata: Mapping[str, Any],
        source_url: str,
        artifact_sha256: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        source_published_at: datetime | None = None,
        detected_at: datetime | None = None,
    ) -> StoredSourceObservation:
        identity = _source_item_identity(item)
        outcome_key = _required_text("item outcome key", item_outcome_key)
        observation_state = _state(state)
        canonical_metadata = _json_object(metadata)
        artifact = _optional_sha256("artifact_sha256", artifact_sha256)
        if observation_state == "present" and artifact is None:
            raise ValueError("present observations require a verified artifact")
        if observation_state != "present" and artifact is not None:
            raise ValueError("non-present observations cannot claim an artifact")
        url = _canonical_url(source_url)
        canonical_etag = _optional_text("etag", etag)
        canonical_last_modified = _optional_text("last_modified", last_modified)
        published = (
            _utc_datetime("source_published_at", source_published_at)
            if source_published_at is not None
            else None
        )
        current = _utc_datetime("repository clock", self._clock())
        detected = _utc_datetime("detected_at", detected_at or current)
        digest = observation_sha256(
            state=observation_state,
            metadata=canonical_metadata,
            artifact_sha256=artifact,
            source_url=url,
            etag=canonical_etag,
            last_modified=canonical_last_modified,
            source_published_at=published,
        )
        values = {
            "observation_sha256": digest,
            "state": observation_state,
            "canonical_metadata": canonical_metadata,
            "artifact_sha256": artifact,
            "source_url": url,
            "etag": canonical_etag,
            "last_modified": canonical_last_modified,
            "source_published_at": published,
        }

        with self.engine.begin() as connection:
            self._jobs._lock_live_lease(connection, lease, current)
            item_row = self._store_item(connection, identity)
            statement = (
                insert(source_observation)
                .values(
                    source_item_id=item_row["source_item_id"],
                    **values,
                    detected_at=detected,
                    detecting_job_id=lease.job_id,
                )
                .on_conflict_do_nothing(
                    constraint="uq_source_observation_identity"
                )
                .returning(*source_observation.c)
            )
            observation_row = connection.execute(statement).mappings().one_or_none()
            created = observation_row is not None
            if observation_row is None:
                observation_row = connection.execute(
                    select(source_observation).where(
                        source_observation.c.source_item_id
                        == item_row["source_item_id"],
                        source_observation.c.observation_sha256 == digest,
                    )
                ).mappings().one()
                expected = {"source_item_id": item_row["source_item_id"], **values}
                if any(
                    observation_row[field] != value
                    for field, value in expected.items()
                ):
                    raise SourceObservationIdentityConflict(
                        "stored observation conflicts with its canonical identity"
                    )
            outcome = {
                "source_item_id": item_row["source_item_id"],
                "source_observation_id": observation_row["source_observation_id"],
                "observation_sha256": digest,
            }
            self._jobs._record_item_outcome_on_connection(
                connection,
                lease,
                item_key=outcome_key,
                status="succeeded",
                outcome=outcome,
                error_summary=None,
                now=current,
                lease_is_locked=True,
            )
        return StoredSourceObservation(
            item=_item_record(item_row),
            observation=_observation_record(observation_row),
            created=created,
        )

    def history(self, source_item_id: int) -> tuple[SourceObservationRecord, ...]:
        item_id = _positive_integer("source_item_id", source_item_id)
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(source_observation)
                .where(source_observation.c.source_item_id == item_id)
                .order_by(
                    source_observation.c.detected_at,
                    source_observation.c.source_observation_id,
                )
            ).mappings().all()
        return tuple(_observation_record(row) for row in rows)

    @staticmethod
    def _store_item(connection, identity: SourceItemIdentity) -> Mapping[str, Any]:
        if identity.parent_source_item_id is not None:
            parent_system = connection.scalar(
                select(source_item.c.source_system).where(
                    source_item.c.source_item_id == identity.parent_source_item_id
                )
            )
            if parent_system is not None and parent_system != identity.source_system:
                raise SourceItemIdentityConflict(
                    "parent source item must belong to the same source system"
                )
        values = {
            "source_system": identity.source_system,
            "item_kind": identity.item_kind,
            "source_key": identity.source_key,
            "issuer_source_identifier": identity.issuer_source_identifier,
            "parent_source_item_id": identity.parent_source_item_id,
        }
        row = connection.execute(
            insert(source_item)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_source_item_identity")
            .returning(*source_item.c)
        ).mappings().one_or_none()
        if row is None:
            row = connection.execute(
                select(source_item).where(
                    source_item.c.source_system == identity.source_system,
                    source_item.c.item_kind == identity.item_kind,
                    source_item.c.source_key == identity.source_key,
                )
            ).mappings().one()
            if any(row[field] != value for field, value in values.items()):
                raise SourceItemIdentityConflict(
                    "source item key already identifies different immutable content"
                )
        return row


def observation_sha256(
    *,
    state: ObservationState,
    metadata: Mapping[str, Any],
    source_url: str,
    artifact_sha256: str | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    source_published_at: datetime | None = None,
) -> str:
    observation_state = _state(state)
    artifact = _optional_sha256("artifact_sha256", artifact_sha256)
    if observation_state == "present" and artifact is None:
        raise ValueError("present observations require a verified artifact")
    if observation_state != "present" and artifact is not None:
        raise ValueError("non-present observations cannot claim an artifact")
    payload = {
        "state": observation_state,
        "metadata": _json_object(metadata),
        "artifact_sha256": artifact,
        "source_url": _canonical_url(source_url),
        "etag": _optional_text("etag", etag),
        "last_modified": _optional_text("last_modified", last_modified),
        "source_published_at": (
            _utc_datetime("source_published_at", source_published_at)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
            if source_published_at is not None
            else None
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _source_item_identity(value: SourceItemIdentity) -> SourceItemIdentity:
    if not isinstance(value, SourceItemIdentity):
        raise ValueError("item must be a SourceItemIdentity")
    if value.source_system not in _SOURCE_SYSTEMS:
        raise ValueError("source system must be SEC or EDINET")
    if value.item_kind not in _ITEM_KINDS:
        raise ValueError("item kind must be inventory, aggregate, filing, or resource")
    issuer = _optional_text(
        "issuer source identifier", value.issuer_source_identifier
    )
    if issuer is not None:
        if value.source_system == "SEC" and re.fullmatch(r"[0-9]{10}", issuer) is None:
            raise ValueError("SEC issuer source identifier must be a zero-padded CIK")
        if (
            value.source_system == "EDINET"
            and re.fullmatch(r"E[0-9]{5}", issuer) is None
        ):
            raise ValueError(
                "EDINET issuer source identifier must be E plus five digits"
            )
    return SourceItemIdentity(
        source_system=value.source_system,
        item_kind=value.item_kind,
        source_key=_required_text("source key", value.source_key),
        issuer_source_identifier=issuer,
        parent_source_item_id=(
            _positive_integer("parent source item id", value.parent_source_item_id)
            if value.parent_source_item_id is not None
            else None
        ),
    )


def _state(value: str) -> ObservationState:
    if value not in _STATES:
        raise ValueError("state must be present, pending, unavailable, or removed")
    return value  # type: ignore[return-value]


def _json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("metadata must be a JSON object")
    _validate_json(value)
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return json.loads(canonical)


def _validate_json(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("metadata must contain strict JSON values")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item)
        return
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("metadata JSON object keys must be text")
        for item in value.values():
            _validate_json(item)
        return
    raise ValueError("metadata must contain strict JSON values")


def _canonical_url(value: str) -> str:
    raw = _required_text("source URL", value)
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or parts.hostname is None:
        raise ValueError("source URL must be an absolute HTTP or HTTPS URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("source URL must not contain credentials")
    if parts.fragment:
        raise ValueError("source URL must not contain a fragment")
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("source URL has an invalid port") from error
    hostname = parts.hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    query = parse_qsl(parts.query, keep_blank_values=True)
    for key, _value in query:
        compact = re.sub(r"[^a-z0-9]", "", key.lower())
        if compact in _CREDENTIAL_QUERY_KEYS or compact.endswith(
            ("signature", "signedtoken")
        ):
            raise ValueError("source URL must not contain credential query keys")
    return urlunsplit((scheme, netloc, parts.path, urlencode(sorted(query)), ""))


def _required_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value.strip()


def _optional_text(name: str, value: str | None) -> str | None:
    return None if value is None else _required_text(name, value)


def _optional_sha256(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 hash")
    return value


def _utc_datetime(name: str, value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _positive_integer(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _item_record(row: Mapping[str, Any]) -> SourceItemRecord:
    return SourceItemRecord(
        **{field: row[field] for field in SourceItemRecord.__dataclass_fields__}
    )


def _observation_record(row: Mapping[str, Any]) -> SourceObservationRecord:
    return SourceObservationRecord(
        **{
            field: row[field]
            for field in SourceObservationRecord.__dataclass_fields__
        }
    )
