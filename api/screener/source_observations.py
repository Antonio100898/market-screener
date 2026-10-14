from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Literal, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from .durable_jobs import DurableJobRepository, LeaseToken, _SOURCE_RECEIPT_PREFIX
from .postgres import source_check_occurrence, source_item, source_observation
from .source_validity import SourceDeliveryRecord, current_observation_id, current_observations, load_delivery, record_check


SourceSystem = Literal["SEC", "EDINET", "COMPANY_ADAPTER"]
SourceItemKind = Literal["inventory", "aggregate", "filing", "resource"]
ObservationState = Literal["present", "pending", "unavailable", "removed"]
_SOURCE_SYSTEMS = {"SEC", "EDINET", "COMPANY_ADAPTER"}
_ITEM_KINDS = {"inventory", "aggregate", "filing", "resource"}
_STATES = {"present", "pending", "unavailable", "removed"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEC_QUARTER = re.compile(r"^[0-9]{4}Q[1-4]$")
_SEC_FILING_COMPARISON_FIELDS = (
    "form",
    "company_name",
    "cik",
    "filing_date",
    "archive_filename",
    "accession",
    "quarter",
)
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


class StaleSourceRevision(RuntimeError):
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
    delivery: SourceDeliveryRecord | None = None


@dataclass(frozen=True)
class SourceAcquisitionReceipt:
    producer_job_id: int
    request_identity_sha256: str
    receipt_key: str
    source_item: SourceItemIdentity
    observation_sha256: str
    delivery: SourceDeliveryRecord

    def to_outcome(self, *, _raw_work_budget=None) -> dict[str, Any]:
        item = (asdict(self.source_item) if _raw_work_budget is None else
                {name: getattr(self.source_item, name) for name in SourceItemIdentity.__dataclass_fields__})
        delivery = (asdict(self.delivery) if _raw_work_budget is None else
                    {name: getattr(self.delivery, name) for name in SourceDeliveryRecord.__dataclass_fields__})
        value = dict(receipt_revision=1, producer_job_id=self.producer_job_id,
            request_identity_sha256=self.request_identity_sha256, receipt_key=self.receipt_key,
            source_item=item, observation_sha256=self.observation_sha256,
            delivery={**delivery, "checked_at":
                _utc_datetime("source acquisition checked_at", self.delivery.checked_at).isoformat()})
        if _raw_work_budget is None:
            self.from_outcome(value)
        else:
            self.from_outcome(value, _raw_work_budget=_raw_work_budget)
        return value

    @classmethod
    def from_outcome(cls, value: Mapping[str, Any], *, _raw_work_budget=None) -> SourceAcquisitionReceipt:
        encoded_size = None if _raw_work_budget is None else _raw_work_budget.receipt_codec(value)
        if _raw_work_budget is not None and (type(encoded_size) is not int or not 0 <= encoded_size <= 8192):
            raise ValueError("source acquisition receipt reservation must be an exact byte size")
        fields = {"receipt_revision", "producer_job_id", "request_identity_sha256", "receipt_key",
                  "source_item", "observation_sha256", "delivery"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("source acquisition receipt has invalid fields")
        if type(value["receipt_revision"]) is not int or value["receipt_revision"] != 1:
            raise ValueError("source acquisition receipt revision must be integer 1")
        job = _receipt_positive_integer("producer job ID", value["producer_job_id"])
        key = _receipt_text("receipt key", value["receipt_key"], 512)
        request = _receipt_sha256("request identity SHA-256", value["request_identity_sha256"])
        digest = _receipt_sha256("observation SHA-256", value["observation_sha256"])
        item = value["source_item"]
        if not isinstance(item, dict) or set(item) != set(SourceItemIdentity.__dataclass_fields__):
            raise ValueError("source acquisition receipt has invalid source item fields")
        if any(not isinstance(item[name], str) for name in ("source_system", "item_kind", "source_key")):
            raise ValueError("source acquisition receipt source identity must use text")
        identity = _source_item_identity(SourceItemIdentity(**item))
        if asdict(identity) != item:
            raise ValueError("source acquisition receipt item is not canonical")
        _receipt_text("source key", identity.source_key, 1024)
        if identity.parent_source_item_id is not None:
            _receipt_positive_integer("parent source item ID", identity.parent_source_item_id)
        delivery = value["delivery"]
        if not isinstance(delivery, dict) or set(delivery) != set(SourceDeliveryRecord.__dataclass_fields__):
            raise ValueError("source acquisition receipt has invalid delivery fields")
        for name in ("occurrence_id", "source_item_id", "source_observation_id", "job_id", "ownership_generation"):
            _receipt_positive_integer(name, delivery[name])
        _receipt_text("source outcome key", delivery["item_key"], 512)
        if delivery["item_key"].startswith(_SOURCE_RECEIPT_PREFIX) or delivery["job_id"] != job:
            raise ValueError("source acquisition receipt delivery belongs to another job or namespace")
        checked_text = delivery["checked_at"]
        if not isinstance(checked_text, str):
            raise ValueError("source acquisition checked_at must be canonical UTC text")
        try:
            checked = _utc_datetime("source acquisition checked_at", datetime.fromisoformat(checked_text))
        except (TypeError, ValueError) as error:
            raise ValueError("source acquisition checked_at must be canonical UTC text") from error
        if checked.isoformat() != checked_text:
            raise ValueError("source acquisition checked_at must be canonical UTC text")
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if encoded_size is not None and len(encoded) != encoded_size:
            raise ValueError("source acquisition receipt encoded size changed after reservation")
        if len(encoded) > 8192:
            raise ValueError("source acquisition receipt exceeds 8192 bytes")
        return cls(job, request, key, identity, digest,
                   SourceDeliveryRecord(**{**delivery, "checked_at": checked}))


def _receipt_text(name: str, value: str, maximum: int) -> str:
    if (not isinstance(value, str) or not value or "\x00" in value
            or value != value.strip() or len(value.encode("utf-8")) > maximum):
        raise ValueError(f"{name} must be canonical nonempty text within {maximum} bytes")
    return value


def _receipt_sha256(name: str, value: str) -> str:
    if value is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 hash")
    return _optional_sha256(name, value)


def _receipt_positive_integer(name: str, value: int) -> int:
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise ValueError(f"{name} must be a positive PostgreSQL bigint")
    return value


def _identity_from_record(item: SourceItemRecord) -> SourceItemIdentity:
    return SourceItemIdentity(*(getattr(item, name) for name in SourceItemIdentity.__dataclass_fields__))


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
        return self._record(
            lease, item=item, item_outcome_key=item_outcome_key, state=state,
            metadata=metadata, source_url=source_url, artifact_sha256=artifact_sha256,
            etag=etag, last_modified=last_modified, source_published_at=source_published_at,
            detected_at=detected_at,
        )

    def record_with_acquisition_receipt(
        self,
        lease: LeaseToken,
        *,
        receipt_key: str,
        request_identity_sha256: str,
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
        _raw_work_budget=None,
    ) -> StoredSourceObservation:
        key = _receipt_text("receipt key", receipt_key, 512)
        request = _receipt_sha256("request identity SHA-256", request_identity_sha256)
        return self._record(
            lease, item=item, item_outcome_key=item_outcome_key, state=state,
            metadata=metadata, source_url=source_url, artifact_sha256=artifact_sha256,
            etag=etag, last_modified=last_modified, source_published_at=source_published_at,
            detected_at=detected_at,
            receipt_key=key, request_identity_sha256=request,
            **({} if _raw_work_budget is None else dict(_raw_work_budget=_raw_work_budget)),
        )

    def _record(
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
        receipt_key: str | None = None,
        request_identity_sha256: str | None = None,
        _raw_work_budget=None,
    ) -> StoredSourceObservation:
        identity = _source_item_identity(item)
        outcome_key = _required_text("item outcome key", item_outcome_key)
        observation_state = _state(state)
        from .company_availability import LOCAL_KIND, admit_local_source_url
        # Preflight the new typed local body before the existing JSON copier allocates it.
        if ((isinstance(metadata, Mapping) and metadata.get("kind") == LOCAL_KIND)
                or (isinstance(source_url, str) and source_url.startswith("urn:"))):
            if admit_local_source_url(source_url, item=item, state=observation_state,
                    metadata=metadata, artifact=artifact_sha256, etag=etag, last_modified=last_modified,
                    published_at=source_published_at) is None:
                raise ValueError("URN observations require a typed internal assessment")
        canonical_metadata = _json_object(metadata)
        artifact = _optional_sha256("artifact_sha256", artifact_sha256)
        if observation_state == "present" and artifact is None:
            raise ValueError("present observations require a verified artifact")
        if observation_state != "present" and artifact is not None:
            raise ValueError("non-present observations cannot claim an artifact")
        local_url = admit_local_source_url(source_url, item=item, state=observation_state,
            metadata=canonical_metadata, artifact=artifact, etag=etag, last_modified=last_modified,
            published_at=source_published_at)
        if local_url is not None and receipt_key is not None:
            raise ValueError("internal assessments cannot claim remote acquisition receipts")
        url = local_url if local_url is not None else _canonical_url(source_url)
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

        if outcome_key.startswith(_SOURCE_RECEIPT_PREFIX):
            raise ValueError("source outcome key uses the reserved receipt namespace")
        if receipt_key is not None:
            _receipt_text("source outcome key", outcome_key, 512)
            _receipt_text("source key", identity.source_key, 1024)
            _receipt_positive_integer("producer job ID", lease.job_id)
            _receipt_positive_integer("ownership generation", lease.generation)
            if identity.parent_source_item_id is not None:
                _receipt_positive_integer("parent source item ID", identity.parent_source_item_id)
        budget_args = {} if _raw_work_budget is None else dict(_raw_work_budget=_raw_work_budget)
        with self.engine.begin() as connection:
            self._jobs._lock_live_lease(connection, lease, self._clock, **budget_args)
            if receipt_key is not None:
                existing = self._acquisition_receipt_on_connection(
                    connection, producer_job_id=lease.job_id, receipt_key=receipt_key,
                    request_identity_sha256=request_identity_sha256,
                    **budget_args,
                )
                if existing is not None:
                    if (identity != _identity_from_record(existing.item)
                            or digest != existing.observation.observation_sha256
                            or outcome_key != existing.delivery.item_key):
                        raise SourceObservationIdentityConflict("source acquisition request conflicts with receipt")
                    self._jobs._lock_live_lease(connection, lease, self._clock, **budget_args)
                    return existing
            item_row = self._store_item(connection, identity)
            observation_row, created, delivery = self._record_on_connection(
                connection,
                lease,
                item_row=item_row,
                outcome_key=outcome_key,
                digest=digest,
                values=values,
                detected=detected,
                now=current,
                **budget_args,
            )
            if receipt_key is not None:
                receipt = SourceAcquisitionReceipt(
                    producer_job_id=lease.job_id,
                    request_identity_sha256=request_identity_sha256, receipt_key=receipt_key,
                    source_item=identity, observation_sha256=digest, delivery=delivery,
                )
                self._jobs._record_source_receipt_on_connection(
                    connection, lease, item_key=_SOURCE_RECEIPT_PREFIX + receipt_key,
                    outcome=receipt.to_outcome(), now=self._clock,
                    **budget_args,
                )
        return StoredSourceObservation(
            item=_item_record(item_row),
            observation=_observation_record(observation_row),
            created=created,
            delivery=delivery,
        )

    def acquisition_receipt(
        self, *, producer_job_id: int, receipt_key: str, request_identity_sha256: str, _raw_work_budget=None,
    ) -> StoredSourceObservation | None:
        job = _receipt_positive_integer("producer job ID", producer_job_id)
        key = _receipt_text("receipt key", receipt_key, 512)
        request = _receipt_sha256("request identity SHA-256", request_identity_sha256)
        budget_arguments = {} if _raw_work_budget is None else dict(_raw_work_budget=_raw_work_budget)
        with self.engine.connect() as connection:
            return self._acquisition_receipt_on_connection(
                connection, producer_job_id=job, receipt_key=key,
                request_identity_sha256=request, **budget_arguments,
            )

    def _acquisition_receipt_on_connection(
        self, connection, *, producer_job_id, receipt_key, request_identity_sha256, _raw_work_budget=None,
    ) -> StoredSourceObservation | None:
        durable = self._jobs._item_outcome_on_connection(
            connection, producer_job_id, _SOURCE_RECEIPT_PREFIX + receipt_key,
            **({} if _raw_work_budget is None else dict(_raw_work_budget=_raw_work_budget)),
        )
        if durable is None:
            return None
        if (durable["status"] != "succeeded" or durable["error_summary"] is not None
                or type(durable["attempts"]) is not int or durable["attempts"] != 1):
            raise ValueError("source acquisition receipt has invalid durable status")
        if _raw_work_budget is None:
            receipt = SourceAcquisitionReceipt.from_outcome(durable["outcome"])
        else:
            receipt = SourceAcquisitionReceipt.from_outcome(durable["outcome"], _raw_work_budget=_raw_work_budget)
        if (receipt.producer_job_id != producer_job_id or receipt.receipt_key != receipt_key
                or receipt.request_identity_sha256 != request_identity_sha256):
            raise SourceObservationIdentityConflict("source acquisition receipt request identity differs")
        statement = select(source_observation, source_item).join(
            source_item, source_item.c.source_item_id == source_observation.c.source_item_id,
        ).where(
            source_observation.c.source_item_id == receipt.delivery.source_item_id,
            source_observation.c.source_observation_id == receipt.delivery.source_observation_id,
        )
        row = self._jobs._read_jsonb_row(connection, statement, (source_observation.c.source_observation_id,),
            (source_observation.c.canonical_metadata,), _raw_work_budget)
        if row is None:
            raise ValueError("source acquisition receipt observation is absent")
        item, observation = _item_record(row), _observation_record(row)
        if _raw_work_budget is not None:
            _raw_work_budget.receipt_observation(observation)
        if (_identity_from_record(item) != receipt.source_item
                or observation.observation_sha256 != receipt.observation_sha256
                or observation_sha256(
                    state=observation.state, metadata=observation.canonical_metadata,
                    artifact_sha256=observation.artifact_sha256, source_url=observation.source_url,
                    etag=observation.etag, last_modified=observation.last_modified,
                    source_published_at=observation.source_published_at,
                ) != receipt.observation_sha256):
            raise SourceObservationIdentityConflict("source acquisition receipt source identity differs")
        actual = load_delivery(connection, receipt.delivery.occurrence_id,
            source_item_id=item.source_item_id, source_observation_id=observation.source_observation_id)
        if actual != receipt.delivery:
            raise SourceObservationIdentityConflict("source acquisition receipt original delivery differs")
        return StoredSourceObservation(item, observation, False, actual)

    def record_edinet_list(
        self,
        lease: LeaseToken,
        *,
        index_date: str,
        process_at: datetime,
        item_outcome_key: str,
        artifact_sha256: str,
        source_url: str,
        etag: str | None = None,
        last_modified: str | None = None,
        detected_at: datetime | None = None,
    ) -> StoredSourceObservation:
        process = _utc_datetime("EDINET process time", process_at)
        current = _utc_datetime("repository clock", self._clock())
        detected = _utc_datetime("detected_at", detected_at or current)
        identity = _source_item_identity(
            SourceItemIdentity("EDINET", "aggregate", f"documents/{index_date}")
        )
        metadata = _json_object({
            "index_date": index_date,
            "process_at": process.isoformat(),
        })
        artifact = _optional_sha256("artifact_sha256", artifact_sha256)
        assert artifact is not None
        url = _canonical_url(source_url)
        canonical_etag = _optional_text("etag", etag)
        canonical_last_modified = _optional_text("last_modified", last_modified)
        digest = observation_sha256(
            state="present",
            metadata=metadata,
            artifact_sha256=artifact,
            source_url=url,
            etag=canonical_etag,
            last_modified=canonical_last_modified,
            source_published_at=process,
        )
        values = {
            "observation_sha256": digest,
            "state": "present",
            "canonical_metadata": metadata,
            "artifact_sha256": artifact,
            "source_url": url,
            "etag": canonical_etag,
            "last_modified": canonical_last_modified,
            "source_published_at": process,
        }
        with self.engine.begin() as connection:
            self._jobs._lock_live_lease(connection, lease, self._clock)
            item_row = self._store_item(connection, identity)
            connection.execute(
                select(source_item.c.source_item_id)
                .where(source_item.c.source_item_id == item_row["source_item_id"])
                .with_for_update()
            ).scalar_one()
            latest = connection.execute(
                select(source_observation)
                .where(source_observation.c.source_observation_id == current_observation_id(connection, item_row["source_item_id"]))
                .with_for_update()
            ).mappings().one_or_none()
            latest_process = latest["source_published_at"] if latest is not None else None
            if latest_process is not None and latest_process > process:
                raise StaleSourceRevision("EDINET list revision is older than retained state")
            if (
                latest is not None
                and latest_process == process
                and latest["artifact_sha256"] != artifact
            ):
                raise StaleSourceRevision(
                    "EDINET list changed without a newer process time"
                )
            observation_row, created, delivery = self._record_on_connection(
                connection,
                lease,
                item_row=item_row,
                outcome_key=_required_text("item outcome key", item_outcome_key),
                digest=digest,
                values=values,
                detected=detected,
                now=current,
            )
        return StoredSourceObservation(
            item=_item_record(item_row),
            observation=_observation_record(observation_row),
            created=created,
            delivery=delivery,
        )

    def latest_sec_financial_filings(
        self,
        quarter: str,
    ) -> tuple[SourceObservationRecord, ...]:
        partition = _sec_quarter(quarter)
        candidate_items = (
            select(source_observation.c.source_item_id)
            .join(
                source_item,
                source_item.c.source_item_id
                == source_observation.c.source_item_id,
            )
            .where(
                source_item.c.source_system == "SEC",
                source_item.c.item_kind == "filing",
                source_observation.c.canonical_metadata["quarter"].astext
                == partition,
            )
            .distinct()
        )
        current = current_observations()
        latest = select(*current.c).where(current.c.source_item_id.in_(candidate_items)).subquery()
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(latest)
                .where(latest.c.canonical_metadata["quarter"].astext == partition)
                .order_by(latest.c.source_item_id)
            ).mappings().all()
        return tuple(_observation_record(row) for row in rows)

    def latest_sec_filing(
        self,
        observation_id: int, *, _raw_work_budget=None,
    ) -> StoredSourceObservation:
        return self._latest_filing("SEC", observation_id,
            **({} if _raw_work_budget is None else dict(_raw_work_budget=_raw_work_budget)))

    def latest_edinet_filing(
        self,
        observation_id: int,
    ) -> StoredSourceObservation:
        return self._latest_filing("EDINET", observation_id)

    def sec_filing(self, observation_id: int, *, _raw_work_budget=None) -> StoredSourceObservation:
        """Read the cited immutable SEC filing without current-source admission."""
        return self._filing("SEC", observation_id, require_latest=False,
            **({} if _raw_work_budget is None else dict(_raw_work_budget=_raw_work_budget)))

    def _latest_filing(
        self,
        source_system: SourceSystem,
        observation_id: int, *, _raw_work_budget=None,
    ) -> StoredSourceObservation:
        return self._filing(source_system, observation_id, require_latest=True,
            **({} if _raw_work_budget is None else dict(_raw_work_budget=_raw_work_budget)))

    def _filing(
        self,
        source_system: SourceSystem,
        observation_id: int,
        *,
        require_latest: bool,
        _raw_work_budget=None,
    ) -> StoredSourceObservation:
        cited_id = _positive_integer("observation id", observation_id)
        with self.engine.connect() as connection:
            statement = (
                select(source_observation, source_item)
                .join(
                    source_item,
                    source_item.c.source_item_id
                    == source_observation.c.source_item_id,
                )
                .where(source_observation.c.source_observation_id == cited_id)
            )
            row = self._jobs._read_jsonb_row(connection, statement, (source_observation.c.source_observation_id,),
                (source_observation.c.canonical_metadata,), _raw_work_budget)
            if row is None:
                raise ValueError(
                    f"cited {source_system} filing observation does not exist"
                )
            if (
                row["source_system"] != source_system
                or row["item_kind"] != "filing"
            ):
                raise ValueError(
                    f"cited observation is not an {source_system} filing"
                )
            if require_latest and current_observation_id(connection, row["source_item_id"]) != cited_id:
                raise ValueError(
                    f"cited {source_system} filing observation is not latest"
                )
        return StoredSourceObservation(
            item=_item_record(row),
            observation=_observation_record(row),
            created=False,
        )

    def by_sha256(self, observation_digest: str, *, _raw_work_budget=None) -> StoredSourceObservation:
        digest = _optional_sha256("observation SHA-256", observation_digest)
        assert digest is not None
        with self.engine.connect() as connection:
            return self.by_sha256_on_connection(connection, digest,
                **({} if _raw_work_budget is None else dict(_raw_work_budget=_raw_work_budget)))

    @staticmethod
    def by_sha256_on_connection(connection, observation_digest: str, *, _raw_work_budget=None) -> StoredSourceObservation:
        digest = _optional_sha256("observation SHA-256", observation_digest)
        assert digest is not None
        statement = (
            select(source_observation, source_item)
            .join(source_item, source_item.c.source_item_id == source_observation.c.source_item_id)
            .where(source_observation.c.observation_sha256 == digest)
            .limit(2)
        )
        if _raw_work_budget is None:
            rows = connection.execute(statement).mappings().all()
        else:
            row = DurableJobRepository._read_jsonb_row(connection, statement,
                (source_observation.c.source_observation_id,), (source_observation.c.canonical_metadata,),
                _raw_work_budget, maximum_rows=2)
            rows = [] if row is None else [row]
        if len(rows) != 1:
            raise ValueError("source observation SHA-256 is missing or ambiguous")
        return StoredSourceObservation(
            item=_item_record(rows[0]),
            observation=_observation_record(rows[0]),
            created=False,
        )

    def latest_item(
        self,
        source_system: SourceSystem,
        item_kind: SourceItemKind,
        source_key: str,
    ) -> StoredSourceObservation | None:
        identity = _source_item_identity(
            SourceItemIdentity(source_system, item_kind, source_key)
        )
        system = identity.source_system
        kind = identity.item_kind
        key = identity.source_key
        current = current_observations()
        with self.engine.connect() as connection:
            row = connection.execute(
                select(current, source_item)
                .join(
                    source_item,
                    source_item.c.source_item_id
                    == current.c.source_item_id,
                )
                .where(
                    source_item.c.source_system == system,
                    source_item.c.item_kind == kind,
                    source_item.c.source_key == key,
                )
                .limit(1)
            ).mappings().one_or_none()
        if row is None:
            return None
        return StoredSourceObservation(
            item=_item_record(row),
            observation=_observation_record(row),
            created=False,
        )

    def latest_edinet_filings(
        self, index_date: str
    ) -> tuple[StoredSourceObservation, ...]:
        try:
            date.fromisoformat(index_date)
        except (TypeError, ValueError) as error:
            raise ValueError("EDINET index date must be an ISO date") from error
        current = current_observations()
        latest = (
            select(*current.c)
            .join(
                source_item,
                source_item.c.source_item_id == current.c.source_item_id,
            )
            .where(
                source_item.c.source_system == "EDINET",
                source_item.c.item_kind == "filing",
                current.c.canonical_metadata["index_date"].astext
                == index_date,
            )
            .subquery()
        )
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(latest, source_item)
                .join(
                    source_item,
                    source_item.c.source_item_id == latest.c.source_item_id,
                )
                .order_by(latest.c.source_item_id)
            ).mappings().all()
        return tuple(
            StoredSourceObservation(
                item=_item_record(row),
                observation=_observation_record(row),
                created=False,
            )
            for row in rows
        )

    def latest_edinet_event(
        self, document_id: str, event_types: tuple[str, ...] | None = None
    ) -> StoredSourceObservation | None:
        key = _required_text("EDINET document id", document_id)
        current = current_observations()
        event_filter = (
            current.c.canonical_metadata["event"].astext.in_(event_types)
            if event_types is not None else
            current.c.canonical_metadata["event"].astext.is_not(None)
        )
        with self.engine.connect() as connection:
            row = connection.execute(
                select(current, source_item)
                .join(source_item, source_item.c.source_item_id == current.c.source_item_id)
                .where(
                    source_item.c.source_system == "EDINET",
                    source_item.c.item_kind == "resource",
                    event_filter,
                    or_(
                        (
                            current.c.canonical_metadata["event"].astext != "withdrawal"
                        ) & (
                            current.c.canonical_metadata["row"]["docID"].astext == key
                        ),
                        (
                            current.c.canonical_metadata["event"].astext == "withdrawal"
                        ) & (
                            current.c.canonical_metadata["row"]["parentDocID"].astext == key
                        ),
                    ),
                )
                .order_by(
                    current.c.source_published_at.desc().nullslast(),
                    current.c.source_observation_id.desc(),
                )
                .limit(1)
            ).mappings().one_or_none()
        return (
            StoredSourceObservation(
                item=_item_record(row), observation=_observation_record(row), created=False
            )
            if row is not None else None
        )

    def record_witnessed_sec_removal(
        self,
        lease: LeaseToken,
        *,
        prior_observation_id: int,
        item_outcome_key: str,
        quarter: str,
        inventory_source_item_id: int,
        inventory_observation_id: int,
        inventory_observation_sha256: str,
        inventory_artifact_sha256: str,
        detected_at: datetime | None = None,
    ) -> StoredSourceObservation:
        prior_id = _positive_integer("prior observation id", prior_observation_id)
        outcome_key = _required_text("item outcome key", item_outcome_key)
        partition = _sec_quarter(quarter)
        inventory_item_id = _positive_integer(
            "inventory source item id", inventory_source_item_id
        )
        inventory_id = _positive_integer(
            "inventory observation id", inventory_observation_id
        )
        inventory_digest = _optional_sha256(
            "inventory observation SHA-256", inventory_observation_sha256
        )
        inventory_artifact = _optional_sha256(
            "inventory artifact SHA-256", inventory_artifact_sha256
        )
        assert inventory_digest is not None and inventory_artifact is not None
        current = _utc_datetime("repository clock", self._clock())
        detected = _utc_datetime("detected_at", detected_at or current)

        with self.engine.begin() as connection:
            self._jobs._lock_live_lease(connection, lease, self._clock)
            inventory = connection.execute(
                select(source_observation, source_item)
                .join(
                    source_item,
                    source_item.c.source_item_id
                    == source_observation.c.source_item_id,
                )
                .where(
                    source_observation.c.source_observation_id == inventory_id
                )
            ).mappings().one_or_none()
            if not _valid_sec_quarterly_inventory(
                inventory,
                partition=partition,
                item_id=inventory_item_id,
                observation_sha256=inventory_digest,
                artifact_sha256=inventory_artifact,
            ):
                raise ValueError("SEC quarterly inventory witness is invalid")

            prior = connection.execute(
                select(source_observation, source_item)
                .join(
                    source_item,
                    source_item.c.source_item_id
                    == source_observation.c.source_item_id,
                )
                .where(source_observation.c.source_observation_id == prior_id)
            ).mappings().one_or_none()
            if not _valid_prior_sec_filing(prior, partition):
                raise ValueError("prior SEC filing observation is invalid")
            assert prior is not None and inventory is not None
            metadata = {
                field: prior["canonical_metadata"][field]
                for field in _SEC_FILING_COMPARISON_FIELDS
            }
            metadata.update(
                {
                    "reason": "absent_from_rebuilt_quarter",
                    "inventory_source_item_id": inventory_item_id,
                    "inventory_observation_id": inventory_id,
                    "inventory_observation_sha256": inventory_digest,
                    "inventory_artifact_sha256": inventory_artifact,
                }
            )
            digest = observation_sha256(
                state="removed",
                metadata=metadata,
                source_url=inventory["source_url"],
            )
            connection.execute(
                select(source_item.c.source_item_id)
                .where(source_item.c.source_item_id == prior["source_item_id"])
                .with_for_update()
            ).scalar_one()
            latest_prior_id = current_observation_id(connection, prior["source_item_id"])
            if latest_prior_id != prior_id:
                retained_delivery = connection.execute(
                    select(source_check_occurrence.c.occurrence_id)
                    .join(
                        source_observation,
                        (source_observation.c.source_observation_id
                         == source_check_occurrence.c.source_observation_id)
                        & (source_observation.c.source_item_id
                           == source_check_occurrence.c.source_item_id),
                    )
                    .where(
                        source_check_occurrence.c.source_item_id == prior["source_item_id"],
                        source_check_occurrence.c.job_id == lease.job_id,
                        source_check_occurrence.c.ownership_generation == lease.generation,
                        source_check_occurrence.c.item_key == outcome_key,
                        source_observation.c.observation_sha256 == digest,
                        source_observation.c.state == "removed",
                    )
                ).scalar_one_or_none()
                if retained_delivery is None:
                    raise ValueError("prior SEC filing observation is not latest")
            values = {
                "observation_sha256": digest,
                "state": "removed",
                "canonical_metadata": metadata,
                "artifact_sha256": None,
                "source_url": inventory["source_url"],
                "etag": None,
                "last_modified": None,
                "source_published_at": None,
            }
            item_row = {field: prior[field] for field in source_item.c.keys()}
            observation_row, created, delivery = self._record_on_connection(
                connection,
                lease,
                item_row=item_row,
                outcome_key=outcome_key,
                digest=digest,
                values=values,
                detected=detected,
                now=current,
            )
        return StoredSourceObservation(
            item=_item_record(item_row),
            observation=_observation_record(observation_row),
            created=created,
            delivery=delivery,
        )

    def delivery(self, occurrence_id: int, *, source_item_id: int, source_observation_id: int) -> SourceDeliveryRecord:
        _positive_integer("occurrence_id", occurrence_id)
        _positive_integer("source_item_id", source_item_id)
        _positive_integer("source_observation_id", source_observation_id)
        with self.engine.connect() as connection:
            return load_delivery(connection, occurrence_id, source_item_id=source_item_id,
                                 source_observation_id=source_observation_id)

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

    def _record_on_connection(
        self,
        connection,
        lease: LeaseToken,
        *,
        item_row: Mapping[str, Any],
        outcome_key: str,
        digest: str,
        values: Mapping[str, Any],
        detected: datetime,
        now: datetime,
        _raw_work_budget=None,
    ) -> tuple[Mapping[str, Any], bool, SourceDeliveryRecord]:
        connection.execute(select(source_item.c.source_item_id).where(
            source_item.c.source_item_id == item_row["source_item_id"]).with_for_update()).scalar_one()
        budget_args = {} if _raw_work_budget is None else dict(_raw_work_budget=_raw_work_budget)
        self._jobs._lock_live_lease(connection, lease, self._clock, **budget_args)
        statement = (
            insert(source_observation)
            .values(
                source_item_id=item_row["source_item_id"],
                **values,
                detected_at=detected,
                detecting_job_id=lease.job_id,
            )
            .on_conflict_do_nothing(constraint="uq_source_observation_identity")
        )
        if _raw_work_budget is None:
            observation_row = connection.execute(statement.returning(*source_observation.c)).mappings().one_or_none()
        else:
            _raw_work_budget.sql_row_preflight((source_observation.c.source_observation_id,), 0)
            identity = connection.execute(statement.returning(source_observation.c.source_observation_id)).scalar_one_or_none()
            observation_row = None if identity is None else self._jobs._read_jsonb_row(connection,
                select(source_observation).where(source_observation.c.source_observation_id == identity),
                (source_observation.c.source_observation_id,), (source_observation.c.canonical_metadata,),
                _raw_work_budget, for_update=True)
            if identity is not None and observation_row is None:
                raise ValueError("written observation disappeared within its transaction")
        created = observation_row is not None
        if observation_row is None:
            lookup = (
                select(source_observation).where(
                    source_observation.c.source_item_id
                    == item_row["source_item_id"],
                    source_observation.c.observation_sha256 == digest,
                )
            )
            if _raw_work_budget is None:
                observation_row = connection.execute(lookup).mappings().one()
            else:
                observation_row = self._jobs._read_jsonb_row(connection, lookup,
                    (source_observation.c.source_observation_id,), (source_observation.c.canonical_metadata,), _raw_work_budget)
                if observation_row is None:
                    raise ValueError("source observation conflict row is absent")
            expected = {"source_item_id": item_row["source_item_id"], **values}
            if any(observation_row[field] != value for field, value in expected.items()):
                raise SourceObservationIdentityConflict(
                    "stored observation conflicts with its canonical identity"
                )
        delivery = record_check(connection, lease, source_item_id=item_row["source_item_id"],
                     source_observation_id=observation_row["source_observation_id"], item_key=outcome_key, checked_at=detected)
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
            now=self._clock,
            lease_is_locked=True,
            **budget_args,
        )
        return observation_row, created, delivery

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
    from .company_availability import (
        LOCAL_KIND, admit_local_source_url, load_assessment, local_source_item,
    )
    local_url = None
    if isinstance(metadata, Mapping) and metadata.get("kind") == LOCAL_KIND:
        assessment = load_assessment(metadata.get("assessment"))
        local_url = admit_local_source_url(source_url,
            item=local_source_item(assessment), state=observation_state, metadata=metadata,
            artifact=artifact, etag=etag, last_modified=last_modified,
            published_at=source_published_at)
    payload = {
        "state": observation_state,
        "metadata": _json_object(metadata),
        "artifact_sha256": artifact,
        "source_url": local_url if local_url is not None else _canonical_url(source_url),
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
        raise ValueError("source system must be SEC, EDINET, or COMPANY_ADAPTER")
    if value.item_kind not in _ITEM_KINDS:
        raise ValueError("item kind must be inventory, aggregate, filing, or resource")
    issuer = _optional_text(
        "issuer source identifier", value.issuer_source_identifier
    )
    if value.source_system == "COMPANY_ADAPTER" and issuer != "IFRS-ADIDAS-AG":
        raise ValueError("company adapter must identify the explicit Adidas issuer")
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


def _sec_quarter(value: str) -> str:
    if not isinstance(value, str) or _SEC_QUARTER.fullmatch(value) is None:
        raise ValueError("quarter must use YYYYQ1 through YYYYQ4")
    return value


def _valid_sec_quarterly_inventory(
    row: Mapping[str, Any] | None,
    *,
    partition: str,
    item_id: int,
    observation_sha256: str,
    artifact_sha256: str,
) -> bool:
    return bool(
        row is not None
        and row["source_item_id"] == item_id
        and row["source_system"] == "SEC"
        and row["item_kind"] == "inventory"
        and row["source_key"] == f"quarterly-index/{partition}"
        and row["state"] == "present"
        and row["observation_sha256"] == observation_sha256
        and row["artifact_sha256"] == artifact_sha256
        and row["canonical_metadata"].get("quarter") == partition
    )


def _valid_prior_sec_filing(
    row: Mapping[str, Any] | None,
    partition: str,
) -> bool:
    if (
        row is None
        or row["source_system"] != "SEC"
        or row["item_kind"] != "filing"
        or row["state"] != "present"
    ):
        return False
    metadata = row["canonical_metadata"]
    return bool(
        metadata.get("quarter") == partition
        and all(field in metadata for field in _SEC_FILING_COMPARISON_FIELDS)
        and metadata["accession"] == row["source_key"]
    )


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
