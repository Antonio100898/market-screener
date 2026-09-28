from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from .postgres import (
    company_snapshot,
    company_snapshot_artifact,
    current_company_snapshot,
    issuer,
    priced_security,
)


class IdentityConflict(ValueError):
    pass


class AmbiguousCurrentTicker(RuntimeError):
    pass


@dataclass(frozen=True)
class SnapshotArtifact:
    content_sha256: str
    role: str


@dataclass(frozen=True)
class StoredCompany:
    issuer_id: int
    security_id: int
    snapshot_id: int
    payload_sha256: str
    engine_revision: int
    created: bool = True


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


class SharedCompanyRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

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
        snapshot_sha256 = _snapshot_sha256(
            revision,
            payload_sha256,
            snapshot_metadata,
            artifact_values,
        )

        with self.engine.begin() as connection:
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
            self._select_current(
                connection,
                security_row["security_id"],
                snapshot_row["snapshot_id"],
            )

        return StoredCompany(
            issuer_id=issuer_row["issuer_id"],
            security_id=security_row["security_id"],
            snapshot_id=snapshot_row["snapshot_id"],
            payload_sha256=snapshot_row["payload_sha256"],
            engine_revision=snapshot_row["engine_revision"],
            created=created,
        )

    def current(self, security_id: int) -> StoredSnapshot | None:
        statement = (
            select(company_snapshot)
            .join(
                current_company_snapshot,
                current_company_snapshot.c.snapshot_id
                == company_snapshot.c.snapshot_id,
            )
            .where(current_company_snapshot.c.security_id == security_id)
        )
        with self.engine.connect() as connection:
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

    def current_by_ticker(self, ticker: str) -> SelectedCompany | None:
        wanted = _required_text("ticker", ticker)
        statement = (
            select(
                company_snapshot,
                issuer.c.source_system.label("issuer_source"),
                issuer.c.source_identifier.label("issuer_identifier"),
                priced_security.c.security_identifier,
            )
            .join(
                current_company_snapshot,
                current_company_snapshot.c.snapshot_id
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
    def _put_snapshot(connection: Connection, values: dict[str, Any]):
        inserted = connection.execute(
            insert(company_snapshot)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(company_snapshot.c.snapshot_id)
        ).scalar_one_or_none()
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
    else:
        raise ValueError("issuer must be a SEC CIK or EDINET code")
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
) -> str:
    security = dict(snapshot_metadata)
    security["receipt_ratio"] = _decimal_identity(
        snapshot_metadata["receipt_ratio"]
    )
    identity = {
        "engine_revision": engine_revision,
        "payload_sha256": payload_sha256,
        "security": security,
        "artifacts": list(artifacts),
    }
    return hashlib.sha256(_canonical_payload(identity).encode()).hexdigest()


def _decimal_identity(value: Decimal | None) -> str | None:
    return format(value.normalize(), "f") if value is not None else None


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
