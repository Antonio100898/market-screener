import os
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from screener import api
from screener.postgres import evidence_artifact, metadata
from screener.shared_companies import (
    AmbiguousCurrentTicker,
    SharedCompanyRepository,
    SnapshotArtifact,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_for_sqlite(_type, _compiler, **_kwargs):
    return "INTEGER"


@pytest.fixture
def repository():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys = ON")

    metadata.create_all(engine)
    yield SharedCompanyRepository(engine)
    engine.dispose()


def _company(**overrides):
    values = {
        "issuer_source": "SEC",
        "issuer_identifier": "1104659",
        "security_identifier": "sec:0001104659:primary-listed-security",
        "ticker": "NTES",
        "exchange_code": "NASDAQ",
        "quote_currency": "USD",
        "security_title": (
            "American Depositary Shares, each representing five ordinary shares"
        ),
        "source_accession": "0001104659-26-043468",
        "security_basis": "PRIMARY_DEPOSITARY_RECEIPT",
        "receipt_ratio": Decimal("5"),
        "engine_revision": 181,
        "canonical_payload": {"ticker": "NTES", "ttm_eps": 6.72},
    }
    values.update(overrides)
    return values


def _add_artifact(repository, digest):
    with repository.engine.begin() as connection:
        connection.execute(
            evidence_artifact.insert().values(
                content_sha256=digest,
                object_key=f"raw/sha256/{digest}",
                byte_size=1,
                media_type="application/json",
                verified_at=datetime.now(timezone.utc),
            )
        )


def test_current_ticker_lookup_is_case_insensitive_and_returns_exact_evidence(
    repository,
):
    facts_hash = "a" * 64
    cover_hash = "b" * 64
    _add_artifact(repository, facts_hash)
    _add_artifact(repository, cover_hash)
    stored = repository.store_and_select(
        **_company(
            artifacts=(
                SnapshotArtifact(facts_hash, "official_api_facts"),
                SnapshotArtifact(cover_hash, "structured_cover_identity"),
            )
        )
    )

    selected = repository.current_by_ticker("ntes")

    assert selected.snapshot.snapshot_id == stored.snapshot_id
    assert selected.issuer_source == "SEC"
    assert selected.issuer_identifier == "0001104659"
    assert selected.security_identifier == (
        "sec:0001104659:primary-listed-security"
    )
    assert selected.snapshot.source_accession == "0001104659-26-043468"
    assert selected.snapshot.receipt_ratio == Decimal("5")
    assert selected.artifacts == (
        SnapshotArtifact(facts_hash, "official_api_facts"),
        SnapshotArtifact(cover_hash, "structured_cover_identity"),
    )
    assert repository.current_by_ticker("MISSING") is None


def test_duplicate_current_ticker_is_an_explicit_integrity_error(repository):
    repository.store_and_select(**_company())
    repository.store_and_select(
        **_company(
            issuer_identifier="1800",
            security_identifier="sec:0000001800:primary-listed-security",
            canonical_payload={"ticker": "NTES", "ttm_eps": 3.57},
        )
    )

    with pytest.raises(AmbiguousCurrentTicker, match="more than one security"):
        repository.current_by_ticker("NtEs")


def test_current_ticker_lookup_does_not_return_historical_tickers(repository):
    first = repository.store_and_select(**_company())
    second = repository.store_and_select(
        **_company(
            ticker="NTES.NEW",
            canonical_payload={"ticker": "NTES.NEW", "ttm_eps": 6.72},
        )
    )

    assert second.security_id == first.security_id
    assert repository.current_by_ticker("NTES") is None
    assert repository.current_by_ticker("ntes.new").snapshot.snapshot_id == (
        second.snapshot_id
    )


def test_company_endpoint_serializes_revision_ratio_and_artifacts(
    repository,
    monkeypatch,
):
    facts_hash = "a" * 64
    _add_artifact(repository, facts_hash)
    repository.store_and_select(
        **_company(
            receipt_ratio=Decimal("5.0000000000"),
            artifacts=(SnapshotArtifact(facts_hash, "official_api_facts"),),
        )
    )
    monkeypatch.setattr(api, "_shared_company_repository", lambda: repository)

    response = TestClient(api.app).get("/companies/ntes")

    assert response.status_code == 200
    body = response.json()
    assert body["canonical_payload"] == {"ticker": "NTES", "ttm_eps": 6.72}
    assert body["revision"]["engine_revision"] == 181
    assert len(body["revision"]["payload_sha256"]) == 64
    assert len(body["revision"]["snapshot_sha256"]) == 64
    assert body["issuer"] == {
        "source_system": "SEC",
        "source_identifier": "0001104659",
    }
    assert body["security"]["security_identifier"] == (
        "sec:0001104659:primary-listed-security"
    )
    assert body["security"]["receipt_ratio"] == "5"
    assert body["artifacts"] == [
        {"role": "official_api_facts", "content_sha256": facts_hash}
    ]


def test_company_endpoint_returns_404_without_old_runtime_fallback(monkeypatch):
    class MissingRepository:
        def current_by_ticker(self, ticker):
            return None

    def forbidden(*_args, **_kwargs):
        raise AssertionError("old runtime path must not be called")

    monkeypatch.setattr(api, "_shared_company_repository", MissingRepository)
    monkeypatch.setattr(api.store, "connect", forbidden)
    monkeypatch.setattr(api, "_snapshot_for", forbidden)
    monkeypatch.setattr(api.sync, "_derive_evidence", forbidden)

    response = TestClient(api.app).get("/companies/MISSING")

    assert response.status_code == 404
    assert response.json()["detail"] == "current company MISSING not found"


def test_company_endpoint_reports_ambiguous_current_ticker(monkeypatch):
    class AmbiguousRepository:
        def current_by_ticker(self, ticker):
            raise AmbiguousCurrentTicker(
                f"current ticker {ticker.upper()} selects more than one security"
            )

    monkeypatch.setattr(api, "_shared_company_repository", AmbiguousRepository)

    response = TestClient(api.app).get("/companies/dupe")

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "current ticker DUPE selects more than one security"
    )


def test_postgres_failure_stays_visible_without_fallback(monkeypatch):
    class UnavailableRepository:
        def current_by_ticker(self, ticker):
            raise OperationalError(
                "SELECT current company",
                {},
                RuntimeError("PostgreSQL unavailable"),
            )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("old runtime path must not be called")

    monkeypatch.setattr(api, "_shared_company_repository", UnavailableRepository)
    monkeypatch.setattr(api.store, "connect", forbidden)
    monkeypatch.setattr(api, "_snapshot_for", forbidden)
    response = TestClient(api.app, raise_server_exceptions=False).get(
        "/companies/NTES"
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "PostgreSQL company store unavailable"


def test_importing_api_does_not_validate_or_connect_to_postgres():
    environment = os.environ.copy()
    environment["SCREENER_DATABASE_URL"] = "not-a-postgres-url"

    result = subprocess.run(
        [sys.executable, "-c", "import screener.api"],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
