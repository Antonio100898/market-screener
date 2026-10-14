from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import Text, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from .durable_jobs import DurableJobRepository, LeaseToken
from .publication import PublicationRequired, lock_active_release, release_selection, require_release
from .postgres import (
    company_snapshot,
    company_snapshot_artifact,
    current_company_snapshot,
    evidence_artifact,
    issuer,
    priced_security,
)


class IdentityConflict(ValueError):
    pass


class AmbiguousCurrentTicker(RuntimeError):
    pass


class PairReferenceCapacity(ValueError):
    """A reference projection exceeded its declared count or byte ceiling."""
    def __init__(self, phase, required, limit):
        super().__init__(f"{phase} exceeds reference capacity")
        self.phase, self.required, self.limit = phase, required, limit


def _reference_id(name, value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError(f"{name} requires an exact positive signed64 integer")
    return value


def _reference_sha(value):
    if type(value) is not str or len(value) != 64 or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("reference digest must be lowercase SHA256")
    return value


def _reference_text(value):
    if value is None:
        return None
    if type(value) is not str or len(value) > 4096 or "\0" in value or len(value.encode("utf-8")) > 4096:
        raise ValueError("reference text exceeds4KiB or has invalid type")
    return value


def _reference_lengths(columns):
    return [func.coalesce(func.octet_length(cast(column, Text)), 0) for column in columns]


def _reference_frontier(phase, sizes, *, count=1):
    if sizes["max_field"] > 4096:
        raise PairReferenceCapacity(phase + "_field", sizes["max_field"], 4096)
    # Six bytes per UTF8 byte covers JSON escaping; fixed scalar/field overhead is reserved.
    reserved = sizes["total_bytes"] * 6 + count * 1024 + 4096
    if reserved > 1024 * 1024:
        raise PairReferenceCapacity(phase + "_references", reserved, 1024 * 1024)


@dataclass(frozen=True)
class SnapshotArtifact:
    content_sha256: str
    role: str


@dataclass(frozen=True)
class RegisteredIdentity:
    issuer_id: int
    security_id: int


@dataclass(frozen=True)
class StoredCompany:
    issuer_id: int
    security_id: int
    snapshot_id: int
    payload_sha256: str
    engine_revision: int
    created: bool = True
    snapshot_sha256: str | None = None


@dataclass(frozen=True)
class StoredSnapshot:
    snapshot_id: int
    security_id: int
    engine_revision: int
    payload_sha256: str
    snapshot_sha256: str
    canonical_payload: dict[str, Any]
    ticker: str
    exchange_code: str | None
    quote_currency: str | None
    security_title: str | None
    source_accession: str | None
    security_basis: str
    receipt_ratio: Decimal | None
    created_at: datetime


@dataclass(frozen=True)
class SelectedCompany:
    issuer_source: str
    issuer_identifier: str
    security_identifier: str
    snapshot: StoredSnapshot
    artifacts: tuple[SnapshotArtifact, ...]
    release_id: int | None = None


class SharedCompanyRepository:
    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.engine = engine
        self._jobs = DurableJobRepository(engine)
        self._clock = clock

    def register_identity(
        self,
        lease: LeaseToken,
        *,
        issuer_source: str,
        issuer_identifier: str,
        security_identifier: str,
    ) -> RegisteredIdentity:
        """Register a leased identity relation before a financial snapshot exists."""
        if not isinstance(lease, LeaseToken):
            raise TypeError("identity registration requires a lease token")
        _reference_id("lease job", lease.job_id)
        _reference_id("lease generation", lease.generation)
        for name, value in (("lease owner", lease.owner), ("issuer source", issuer_source),
                            ("issuer identifier", issuer_identifier), ("security identifier", security_identifier)):
            _reference_text(value)
            if value is None:
                raise ValueError(f"{name} requires text")
            _required_text(name, value)
        issuer_values = _issuer_values(issuer_source, issuer_identifier)
        security = _required_text("security identifier", security_identifier)
        with self.engine.begin() as connection:
            self._jobs._lock_live_lease(connection, lease, self._clock)
            issuer_row = self._put_issuer(connection, issuer_values)
            security_row = self._put_security(connection, {
                "issuer_id": issuer_row["issuer_id"], "security_identifier": security,
            })
            self._jobs._lock_live_lease(connection, lease, self._clock)
            result = RegisteredIdentity(issuer_row["issuer_id"], security_row["security_id"])
        return result

    def store_and_select(
        self,
        *,
        issuer_source: str,
        issuer_identifier: str,
        security_identifier: str,
        ticker: str,
        security_title: str | None,
        source_accession: str | None,
        security_basis: str,
        engine_revision: int,
        canonical_payload: Mapping[str, Any],
        exchange_code: str | None = None,
        quote_currency: str | None = None,
        receipt_ratio: Decimal | str | int | None = None,
        artifacts: Iterable[SnapshotArtifact] = (),
    ) -> StoredCompany:
        return self._store(
            issuer_source=issuer_source,
            issuer_identifier=issuer_identifier,
            security_identifier=security_identifier,
            ticker=ticker,
            security_title=security_title,
            source_accession=source_accession,
            security_basis=security_basis,
            engine_revision=engine_revision,
            canonical_payload=canonical_payload,
            exchange_code=exchange_code,
            quote_currency=quote_currency,
            receipt_ratio=receipt_ratio,
            artifacts=artifacts,
            select_current=True,
        )

    def store_candidate(
        self,
        lease: LeaseToken,
        *,
        issuer_source: str,
        issuer_identifier: str,
        security_identifier: str,
        ticker: str,
        security_title: str | None,
        source_accession: str | None,
        security_basis: str,
        engine_revision: int,
        canonical_payload: Mapping[str, Any],
        exchange_code: str | None = None,
        quote_currency: str | None = None,
        receipt_ratio: Decimal | str | int | None = None,
        artifacts: Iterable[SnapshotArtifact] = (),
    ) -> StoredCompany:
        return self._store(
            issuer_source=issuer_source,
            issuer_identifier=issuer_identifier,
            security_identifier=security_identifier,
            ticker=ticker,
            security_title=security_title,
            source_accession=source_accession,
            security_basis=security_basis,
            engine_revision=engine_revision,
            canonical_payload=canonical_payload,
            exchange_code=exchange_code,
            quote_currency=quote_currency,
            receipt_ratio=receipt_ratio,
            artifacts=artifacts,
            select_current=False,
            lease=lease,
        )

    def store_candidate_on_connection(
        self,
        connection: Connection,
        lease: LeaseToken,
        *,
        issuer_source: str,
        issuer_identifier: str,
        security_identifier: str,
        ticker: str,
        security_title: str | None,
        source_accession: str | None,
        security_basis: str,
        engine_revision: int,
        canonical_payload: Mapping[str, Any],
        exchange_code: str | None = None,
        quote_currency: str | None = None,
        receipt_ratio: Decimal | str | int | None = None,
        artifacts: Iterable[SnapshotArtifact] = (),
        _raw_work_budget=None,
    ) -> StoredCompany:
        """Stage an immutable snapshot inside the caller's live transaction."""
        if (not isinstance(connection, Connection) or connection.closed
                or connection.engine is not self.engine or not connection.in_transaction()
                or not connection.get_transaction().is_active):
            raise ValueError("candidate requires a live transaction from this repository engine")
        if not isinstance(lease, LeaseToken):
            raise TypeError("candidate requires a lease token")
        if getattr(connection.connection.dbapi_connection, "autocommit", None) is not False:
            raise ValueError("candidate requires DBAPI autocommit disabled")
        return self._store(
            issuer_source=issuer_source,
            issuer_identifier=issuer_identifier,
            security_identifier=security_identifier,
            ticker=ticker,
            security_title=security_title,
            source_accession=source_accession,
            security_basis=security_basis,
            engine_revision=engine_revision,
            canonical_payload=canonical_payload,
            exchange_code=exchange_code,
            quote_currency=quote_currency,
            receipt_ratio=receipt_ratio,
            artifacts=artifacts,
            select_current=False,
            lease=lease,
            connection=connection, _raw_work_budget=_raw_work_budget,
        )

    def _store(
        self,
        *,
        issuer_source: str,
        issuer_identifier: str,
        security_identifier: str,
        ticker: str,
        security_title: str | None,
        source_accession: str | None,
        security_basis: str,
        engine_revision: int,
        canonical_payload: Mapping[str, Any],
        exchange_code: str | None,
        quote_currency: str | None,
        receipt_ratio: Decimal | str | int | None,
        artifacts: Iterable[SnapshotArtifact],
        select_current: bool,
        lease: LeaseToken | None = None,
        connection: Connection | None = None,
        _raw_work_budget=None,
    ) -> StoredCompany:
        issuer_values = _issuer_values(issuer_source, issuer_identifier)
        security_values = {
            "security_identifier": _required_text(
                "security identifier", security_identifier
            )
        }
        snapshot_metadata = _snapshot_metadata_values(
            ticker=ticker,
            exchange_code=exchange_code,
            quote_currency=quote_currency,
            security_title=security_title,
            source_accession=source_accession,
            security_basis=security_basis,
            receipt_ratio=receipt_ratio,
        )
        revision = _engine_revision(engine_revision)
        if _raw_work_budget is not None:
            from .primary_input_transport import (_retained_json_dump_reserve, _retained_driver_reserve)
            size = _retained_json_dump_reserve(canonical_payload, _raw_work_budget, sha=True)
            _raw_work_budget.json_decode(size)
            _retained_driver_reserve(canonical_payload, _raw_work_budget)
        payload = _canonical_payload(canonical_payload)
        payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
        payload_value = json.loads(payload)
        artifact_pairs = set()
        for artifact in artifacts:
            values = _artifact_values(artifact)
            artifact_pairs.add((values["content_sha256"], values["role"]))
        artifact_values = tuple(
            {"content_sha256": content_sha256, "role": role}
            for content_sha256, role in sorted(artifact_pairs)
        )
        from .company_availability import validate_snapshot
        unavailable = validate_snapshot(payload_value, artifact_values,
            issuer_source=issuer_values["source_system"], issuer_identifier=issuer_values["source_identifier"],
            ticker=snapshot_metadata["ticker"])
        if issuer_values["source_system"] == "COMPANY_ADAPTER":
            if (security_values["security_identifier"] != "company-adapter:IFRS-ADIDAS-AG:primary-ordinary"
                    or snapshot_metadata["ticker"] != "ADS.DE" or snapshot_metadata["receipt_ratio"] is not None):
                raise ValueError("company adapter must identify its exact configured ordinary security")
            if unavailable is None:
                roles = {artifact["role"] for artifact in artifact_values}
                if (snapshot_metadata["security_basis"] != "PRIMARY_ORDINARY_SHARE"
                        or snapshot_metadata["quote_currency"] != "EUR"
                        or not {"canonical_company_adapter_facts", "raw_filing"} <= roles):
                    raise ValueError("company adapter requires explicit security and retained workbook evidence")
            elif snapshot_metadata["security_basis"] != "UNASSESSED" or snapshot_metadata["quote_currency"] is not None:
                raise ValueError("unavailable adapter cannot claim admitted security or quote evidence")
        snapshot_sha256 = _snapshot_sha256(
            revision,
            payload_sha256,
            snapshot_metadata,
            artifact_values,
            _raw_work_budget=_raw_work_budget,
        )

        with self.engine.begin() if connection is None else nullcontext(connection) as connection:
            if select_current and lock_active_release(connection) is not None:
                raise PublicationRequired("published company selections must change through a data release")
            if lease is not None:
                self._jobs._lock_live_lease(connection, lease, self._clock,
                    **({} if _raw_work_budget is None else {"_raw_work_budget": _raw_work_budget}))
            issuer_row = self._put_issuer(connection, issuer_values)
            security_row = self._put_security(
                connection,
                {**security_values, "issuer_id": issuer_row["issuer_id"]},
            )
            snapshot_row, created = self._put_snapshot(
                connection,
                {
                    "security_id": security_row["security_id"],
                    "engine_revision": revision,
                    "payload_sha256": payload_sha256,
                    "snapshot_sha256": snapshot_sha256,
                    "canonical_payload": payload_value,
                    **snapshot_metadata,
                },
                _raw_work_budget=_raw_work_budget,
            )
            self._link_artifacts(
                connection,
                snapshot_row["snapshot_id"],
                artifact_values,
            )
            self._verify_artifacts(
                connection,
                snapshot_row["snapshot_id"],
                artifact_values,
            )
            if select_current:
                self._select_current(
                    connection,
                    security_row["security_id"],
                    snapshot_row["snapshot_id"],
                )

            if lease is not None:
                self._jobs._lock_live_lease(connection, lease, self._clock,
                    **({} if _raw_work_budget is None else {"_raw_work_budget": _raw_work_budget}))

        return StoredCompany(
            issuer_id=issuer_row["issuer_id"],
            security_id=security_row["security_id"],
            snapshot_id=snapshot_row["snapshot_id"],
            payload_sha256=snapshot_row["payload_sha256"],
            engine_revision=snapshot_row["engine_revision"],
            created=created,
            snapshot_sha256=snapshot_row["snapshot_sha256"],
        )

    def snapshot(self, snapshot_id: int) -> SelectedCompany | None:
        """Read one immutable staged or published snapshot without current selection."""
        if type(snapshot_id) is not int or snapshot_id <= 0:
            raise ValueError("snapshot_id must be a positive integer")
        with self.engine.connect() as connection:
            row = connection.execute(select(
                company_snapshot,
                issuer.c.source_system.label("issuer_source"),
                issuer.c.source_identifier.label("issuer_identifier"),
                priced_security.c.security_identifier,
            ).join(priced_security, priced_security.c.security_id == company_snapshot.c.security_id)
             .join(issuer, issuer.c.issuer_id == priced_security.c.issuer_id)
             .where(company_snapshot.c.snapshot_id == snapshot_id)).mappings().one_or_none()
            if row is None:
                return None
            return SelectedCompany(
                issuer_source=row["issuer_source"], issuer_identifier=row["issuer_identifier"],
                security_identifier=row["security_identifier"],
                snapshot=StoredSnapshot(**{name: row[name] for name in StoredSnapshot.__dataclass_fields__}),
                artifacts=tuple(self._artifacts(connection, snapshot_id)),
            )

    @staticmethod
    def snapshot_reference_on_connection(connection, snapshot_id, *, _raw_work_budget=None):
        """Read bounded complete metadata without transferring canonical payload bytes."""
        _reference_id("snapshot", snapshot_id)
        metadata_names = ("ticker", "exchange_code", "quote_currency", "security_title",
                          "source_accession", "security_basis", "receipt_ratio")
        fields = {name: company_snapshot.c[name] for name in metadata_names}
        fields["receipt_ratio"] = cast(company_snapshot.c.receipt_ratio, Text)
        fields.update(issuer_source=issuer.c.source_system, issuer_identifier=issuer.c.source_identifier,
                      security_identifier=priced_security.c.security_identifier)
        joined = company_snapshot.join(priced_security).join(issuer)
        lengths = _reference_lengths(fields.values())
        where = company_snapshot.c.snapshot_id == snapshot_id
        sizes = connection.execute(select(func.greatest(*lengths).label("max_field"),
            sum(lengths).label("total_bytes")).select_from(joined).where(where)).mappings().one_or_none()
        if sizes is None:
            raise ValueError("paired snapshot is absent")
        _reference_frontier("snapshot_header", sizes)
        links = company_snapshot_artifact.outerjoin(evidence_artifact,
            evidence_artifact.c.content_sha256 == company_snapshot_artifact.c.content_sha256)
        link_fields = dict(content_sha256=company_snapshot_artifact.c.content_sha256,
            role=company_snapshot_artifact.c.role, registration_sha=evidence_artifact.c.content_sha256,
            object_key=evidence_artifact.c.object_key, media_type=evidence_artifact.c.media_type,
            byte_size=evidence_artifact.c.byte_size)
        variable_links = {name: column for name, column in link_fields.items() if name != "byte_size"}
        link_lengths = _reference_lengths(variable_links.values())
        link_where = company_snapshot_artifact.c.snapshot_id == snapshot_id
        link_sizes = connection.execute(select(func.count().label("count"),
            func.coalesce(func.max(func.greatest(*link_lengths)), 0).label("max_field"),
            func.coalesce(func.sum(sum(link_lengths)), 0).label("total_bytes"))
            .select_from(links).where(link_where)).mappings().one()
        if link_sizes["count"] > 64:
            raise PairReferenceCapacity("snapshot_roles", link_sizes["count"], 64)
        combined = dict(max_field=max(sizes["max_field"], link_sizes["max_field"]),
                        total_bytes=sizes["total_bytes"] + link_sizes["total_bytes"])
        _reference_frontier("snapshot", combined, count=link_sizes["count"] + 1)
        if _raw_work_budget is not None:
            _raw_work_budget.reserve(reads=combined["total_bytes"], work=3 * combined["total_bytes"])
        header = dict(connection.execute(select(company_snapshot.c.snapshot_id, company_snapshot.c.security_id,
            company_snapshot.c.engine_revision, company_snapshot.c.payload_sha256, company_snapshot.c.snapshot_sha256,
            *(column.label(name) for name, column in fields.items()))
            .select_from(joined).where(where)).mappings().one())
        rows = [dict(row) for row in connection.execute(select(*(column.label(name) for name, column in link_fields.items()))
            .select_from(links).where(link_where).order_by(company_snapshot_artifact.c.content_sha256,
                company_snapshot_artifact.c.role).limit(65)).mappings()]
        actual_header = [header[name] for name in fields]
        actual_link = [row[name] for row in rows for name in variable_links]
        for value in actual_header + actual_link:
            _reference_text(value)
        actual_bytes = lambda values: sum(len(value.encode("utf-8")) for value in values if value is not None)
        if (len(rows) != link_sizes["count"] or len(rows) > 64
                or actual_bytes(actual_header) != sizes["total_bytes"]
                or actual_bytes(actual_link) != link_sizes["total_bytes"]):
            raise ValueError("snapshot references changed during bounded projection")
        for name in ("snapshot_id", "security_id", "engine_revision"):
            _reference_id(name, header[name])
        _reference_sha(header["payload_sha256"]); _reference_sha(header["snapshot_sha256"])
        if (header["receipt_ratio"] is not None
                and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", header["receipt_ratio"]) is None):
            raise ValueError("receipt ratio must use bounded SQL numeric text")
        metadata = _snapshot_metadata_values(**{name: header[name] for name in metadata_names})
        if any(metadata[name] != header[name] for name in metadata_names if name != "receipt_ratio"):
            raise ValueError("snapshot metadata is not stored canonically")
        header["receipt_ratio"] = _decimal_identity(metadata["receipt_ratio"], _raw_work_budget=_raw_work_budget)
        returned_texts = [value for value in [*(header[name] for name in fields), *actual_link] if value is not None]
        returned_characters = max((len(value) for value in returned_texts), default=0)
        if returned_characters > 4096:
            raise PairReferenceCapacity("snapshot_returned_field", returned_characters, 4096)
        returned_lengths = [len(value.encode("utf-8")) for value in returned_texts]
        _reference_frontier("snapshot_returned", dict(max_field=max(returned_lengths, default=0),
            total_bytes=sum(returned_lengths)), count=len(rows) + 1)
        artifacts = []
        for row in rows:
            _reference_sha(row["content_sha256"])
            if (row["registration_sha"] != row["content_sha256"]
                    or row["object_key"] != "raw/sha256/" + row["content_sha256"]
                    or type(row["byte_size"]) is not int or not 0 <= row["byte_size"] < 2**63):
                raise ValueError("snapshot has missing or invalid registered artifact metadata")
            artifact = _artifact_values(SnapshotArtifact(row["content_sha256"], row["role"]))
            if artifact["role"] != row["role"]:
                raise ValueError("snapshot artifact role is not canonical")
            artifacts.append(artifact)
        if _snapshot_sha256(header["engine_revision"], header["payload_sha256"], metadata, artifacts, _raw_work_budget=_raw_work_budget) != header["snapshot_sha256"]:
            raise ValueError("snapshot reference digest differs from recorded metadata")
        return dict(header=header, artifacts=rows)

    def current(self, security_id: int, *, release_id: int | None = None) -> StoredSnapshot | None:
        selection = release_selection(release_id)
        statement = (
            select(company_snapshot)
            .join(
                selection,
                selection.c.snapshot_id
                == company_snapshot.c.snapshot_id,
            )
            .where(selection.c.security_id == security_id)
        )
        with self.engine.connect() as connection:
            require_release(connection, release_id)
            row = connection.execute(statement).mappings().one_or_none()
        return StoredSnapshot(**row) if row else None

    def snapshots(self, security_id: int) -> list[StoredSnapshot]:
        statement = (
            select(company_snapshot)
            .where(company_snapshot.c.security_id == security_id)
            .order_by(company_snapshot.c.snapshot_id)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [StoredSnapshot(**row) for row in rows]

    def current_by_ticker(self, ticker: str, *, release_id: int | None = None) -> SelectedCompany | None:
        wanted = _required_text("ticker", ticker)
        selection = release_selection(release_id)
        statement = (
            select(
                company_snapshot,
                issuer.c.source_system.label("issuer_source"),
                issuer.c.source_identifier.label("issuer_identifier"),
                priced_security.c.security_identifier,
                selection.c.release_id,
            )
            .join(
                selection,
                selection.c.snapshot_id
                == company_snapshot.c.snapshot_id,
            )
            .join(
                priced_security,
                priced_security.c.security_id == company_snapshot.c.security_id,
            )
            .join(issuer, issuer.c.issuer_id == priced_security.c.issuer_id)
            .where(func.lower(company_snapshot.c.ticker) == wanted.lower())
            .order_by(company_snapshot.c.snapshot_id)
            .limit(2)
        )
        with self.engine.connect() as connection:
            require_release(connection, release_id)
            rows = connection.execute(statement).mappings().all()
            if len(rows) > 1:
                raise AmbiguousCurrentTicker(
                    f"current ticker {wanted.upper()} selects more than one security"
                )
            if not rows:
                return None
            row = rows[0]
            artifacts = tuple(self._artifacts(connection, row["snapshot_id"]))

        snapshot_fields = StoredSnapshot.__dataclass_fields__
        return SelectedCompany(
            issuer_source=row["issuer_source"],
            issuer_identifier=row["issuer_identifier"],
            security_identifier=row["security_identifier"],
            snapshot=StoredSnapshot(
                **{name: row[name] for name in snapshot_fields}
            ),
            artifacts=artifacts,
            release_id=row["release_id"],
        )

    def artifacts(self, snapshot_id: int) -> list[SnapshotArtifact]:
        with self.engine.connect() as connection:
            return self._artifacts(connection, snapshot_id)

    @staticmethod
    def _artifacts(
        connection: Connection,
        snapshot_id: int,
    ) -> list[SnapshotArtifact]:
        statement = (
            select(
                company_snapshot_artifact.c.content_sha256,
                company_snapshot_artifact.c.role,
            )
            .where(company_snapshot_artifact.c.snapshot_id == snapshot_id)
            .order_by(
                company_snapshot_artifact.c.content_sha256,
                company_snapshot_artifact.c.role,
            )
        )
        rows = connection.execute(statement).mappings().all()
        return [SnapshotArtifact(**row) for row in rows]

    @staticmethod
    def _put_issuer(connection: Connection, values: dict[str, Any]):
        connection.execute(insert(issuer).values(**values).on_conflict_do_nothing())
        return connection.execute(
            select(issuer).where(
                issuer.c.source_system == values["source_system"],
                issuer.c.source_identifier == values["source_identifier"],
            )
        ).mappings().one()

    @staticmethod
    def _put_security(connection: Connection, values: dict[str, Any]):
        connection.execute(
            insert(priced_security).values(**values).on_conflict_do_nothing()
        )
        row = connection.execute(
            select(priced_security).where(
                priced_security.c.security_identifier
                == values["security_identifier"],
            )
        ).mappings().one()
        _verify_identity(
            "priced security",
            row,
            values,
            (
                "issuer_id",
                "security_identifier",
            ),
        )
        return row

    @staticmethod
    def _put_snapshot(connection: Connection, values: dict[str, Any], *, _raw_work_budget=None):
        inserted = connection.execute(
            insert(company_snapshot)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(company_snapshot.c.snapshot_id)
        ).scalar_one_or_none()
        if _raw_work_budget is not None:
            sizes = connection.execute(select(func.coalesce(func.sum(
                func.octet_length(cast(func.to_jsonb(company_snapshot.table_valued()), Text))), 0).label("bytes"))
                .where(company_snapshot.c.security_id == values["security_id"],
                       company_snapshot.c.snapshot_sha256 == values["snapshot_sha256"])).mappings().one()
            from .sec_ingestion import _size
            size = _size(sizes["bytes"])
            _raw_work_budget.reserve(reads=size, work=3 * size)
        row = connection.execute(
            select(company_snapshot).where(
                company_snapshot.c.security_id == values["security_id"],
                company_snapshot.c.snapshot_sha256 == values["snapshot_sha256"],
            )
        ).mappings().one()
        _verify_identity(
            "company snapshot",
            row,
            values,
            (
                "security_id",
                "engine_revision",
                "payload_sha256",
                "snapshot_sha256",
                "canonical_payload",
                "ticker",
                "exchange_code",
                "quote_currency",
                "security_title",
                "source_accession",
                "security_basis",
                "receipt_ratio",
            ),
        )
        return row, inserted is not None

    @staticmethod
    def _link_artifacts(
        connection: Connection,
        snapshot_id: int,
        artifacts: Iterable[dict[str, str]],
    ) -> None:
        for artifact in artifacts:
            connection.execute(
                insert(company_snapshot_artifact)
                .values(snapshot_id=snapshot_id, **artifact)
                .on_conflict_do_nothing()
            )

    @staticmethod
    def _verify_artifacts(
        connection: Connection,
        snapshot_id: int,
        artifacts: Iterable[dict[str, str]],
    ) -> None:
        stored = set(
            connection.execute(
                select(
                    company_snapshot_artifact.c.content_sha256,
                    company_snapshot_artifact.c.role,
                ).where(company_snapshot_artifact.c.snapshot_id == snapshot_id)
            ).all()
        )
        expected = {
            (artifact["content_sha256"], artifact["role"])
            for artifact in artifacts
        }
        if stored != expected:
            raise IdentityConflict(
                "Stored company snapshot conflicts with its artifact set"
            )

    @staticmethod
    def _select_current(
        connection: Connection,
        security_id: int,
        snapshot_id: int,
    ) -> None:
        statement = insert(current_company_snapshot).values(
            security_id=security_id,
            snapshot_id=snapshot_id,
        )
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[current_company_snapshot.c.security_id],
                set_={
                    "snapshot_id": statement.excluded.snapshot_id,
                    "selected_at": func.now(),
                },
                where=(
                    current_company_snapshot.c.snapshot_id
                    != statement.excluded.snapshot_id
                ),
            )
        )


def _issuer_values(source: str, identifier: str) -> dict[str, str]:
    source = source.strip().upper()
    identifier = identifier.strip().upper()
    if source == "SEC" and identifier.isdigit() and len(identifier) <= 10:
        identifier = identifier.zfill(10)
    elif source == "EDINET" and re.fullmatch(r"E\d{5}", identifier):
        pass
    elif source == "COMPANY_ADAPTER" and identifier == "IFRS-ADIDAS-AG":
        pass
    else:
        raise ValueError("issuer must be a SEC CIK, EDINET code, or explicit Adidas adapter")
    return {"source_system": source, "source_identifier": identifier}


def _snapshot_metadata_values(
    *,
    ticker: str,
    exchange_code: str | None,
    quote_currency: str | None,
    security_title: str | None,
    source_accession: str | None,
    security_basis: str,
    receipt_ratio: Decimal | str | int | None,
) -> dict[str, Any]:
    values = {
        "ticker": _required_text("ticker", ticker).upper(),
        "exchange_code": _optional_text(exchange_code),
        "quote_currency": _optional_text(quote_currency),
        "security_title": _optional_text(security_title),
        "source_accession": _optional_text(source_accession),
        "security_basis": _required_text("security basis", security_basis),
        "receipt_ratio": _receipt_ratio(receipt_ratio),
    }
    if values["quote_currency"] is not None:
        values["quote_currency"] = values["quote_currency"].upper()
        if not re.fullmatch(r"[A-Z]{3}", values["quote_currency"]):
            raise ValueError("quote currency must be a three-letter ISO code")
    return values


def _snapshot_sha256(
    engine_revision: int,
    payload_sha256: str,
    snapshot_metadata: Mapping[str, Any],
    artifacts: Iterable[Mapping[str, str]],
    *, _raw_work_budget=None,
) -> str:
    security = dict(snapshot_metadata)
    security["receipt_ratio"] = _decimal_identity(
        snapshot_metadata["receipt_ratio"], _raw_work_budget=_raw_work_budget
    )
    identity = {
        "engine_revision": engine_revision,
        "payload_sha256": payload_sha256,
        "security": security,
        "artifacts": list(artifacts),
    }
    if _raw_work_budget is not None:
        from .primary_input_transport import _retained_json_dump_reserve
        _retained_json_dump_reserve(identity, _raw_work_budget, sha=True)
    return hashlib.sha256(_canonical_payload(identity).encode()).hexdigest()


def _decimal_identity(value: Decimal | None, *, _raw_work_budget=None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    if _raw_work_budget is not None:
        digits = normalized.as_tuple()
        exponent, count = digits.exponent, len(digits.digits)
        from .sec_ingestion import _size
        if type(exponent) is not int:
            raise ValueError("finite Decimal identity required for raw-work sizing")
        size = digits.sign + (count + exponent if exponent >= 0 else max(1, count + exponent) + 1 - exponent)
        _raw_work_budget.reserve(work=_size(size))
    return format(normalized, "f")


def _receipt_ratio(value: Decimal | str | int | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float):
        raise TypeError("receipt ratio must not pass through binary floating point")
    try:
        ratio = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("receipt ratio must be an exact positive number") from exc
    if not ratio.is_finite() or ratio <= 0:
        raise ValueError("receipt ratio must be an exact positive number")
    return ratio


def _engine_revision(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("engine revision must be a positive integer")
    return value


def _canonical_payload(payload: Mapping[str, Any]) -> str:
    if not isinstance(payload, Mapping):
        raise TypeError("canonical payload must be a mapping")
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _artifact_values(artifact: SnapshotArtifact) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", artifact.content_sha256):
        raise ValueError("artifact SHA-256 must be 64 lowercase hexadecimal characters")
    return {
        "content_sha256": artifact.content_sha256,
        "role": _required_text("artifact role", artifact.role),
    }


def _required_text(name: str, value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return _required_text("optional identity field", value)


def _verify_identity(
    name: str,
    stored: Mapping[str, Any],
    incoming: Mapping[str, Any],
    fields: Iterable[str],
) -> None:
    if any(stored[field] != incoming[field] for field in fields):
        raise IdentityConflict(f"Stored {name} conflicts with the supplied identity")
