import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url

from screener.postgres import create_postgres_engine, evidence_artifact
from screener.shared_companies import SharedCompanyRepository, SnapshotArtifact
from screener.storage_config import StorageSettings


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_STORAGE_INTEGRATION") != "1",
    reason="set RUN_STORAGE_INTEGRATION=1 with local PostgreSQL running",
)


@pytest.mark.parametrize(
    "starting_revision", ["base", "20260927_0002", "20260927_0003"]
)
def test_upgrade_evidence_revision_retry_and_restart(monkeypatch, starting_revision):
    settings = StorageSettings.from_env()
    database_name = f"ms_company_{uuid4().hex}"
    admin_engine = create_postgres_engine(settings).execution_options(
        isolation_level="AUTOCOMMIT"
    )
    database_url = make_url(settings.database_url).set(database=database_name)

    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    engine = None
    restarted_engine = None
    try:
        monkeypatch.setenv(
            "SCREENER_DATABASE_URL",
            database_url.render_as_string(hide_password=False),
        )
        alembic = Config("alembic.ini")
        command.upgrade(alembic, starting_revision)
        engine = create_engine(database_url)
        with engine.connect() as connection:
            expected_revision = (
                None if starting_revision == "base" else starting_revision
            )
            assert (
                MigrationContext.configure(connection).get_current_revision()
                == expected_revision
            )

        legacy = (
            _seed_0002_company(engine)
            if starting_revision == "20260927_0002"
            else _seed_0003_company(engine)
            if starting_revision == "20260927_0003"
            else None
        )
        engine.dispose()
        engine = None

        command.upgrade(alembic, "head")
        engine = create_engine(database_url)
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == (
                "20260928_0004"
            )
        repository = SharedCompanyRepository(engine)

        if legacy is not None:
            migrated = repository.current(legacy["security_id"])
            assert migrated.snapshot_id == legacy["snapshot_id"]
            assert migrated.source_accession == "legacy-cover-accession"
            assert migrated.receipt_ratio == Decimal("8")
            assert repository.artifacts(migrated.snapshot_id) == [
                SnapshotArtifact(legacy["digest"], "raw_filing")
            ]

        facts_hash = "a" * 64
        old_cover_hash = "b" * 64
        new_cover_hash = "c" * 64
        with engine.begin() as connection:
            for digest in (facts_hash, old_cover_hash, new_cover_hash):
                connection.execute(
                    evidence_artifact.insert().values(
                        content_sha256=digest,
                        object_key=f"raw/sha256/{digest}",
                        byte_size=7,
                        media_type="application/json",
                        verified_at=datetime.now(timezone.utc),
                    )
                )

        values = {
            "issuer_source": "SEC",
            "issuer_identifier": "0000044287",
            "security_identifier": "sec:0000044287:primary-listed-security",
            "ticker": "ABT",
            "exchange_code": "NYSE",
            "quote_currency": "USD",
            "security_title": "American Depositary Shares, each representing eight shares",
            "source_accession": "0001104659-25-018349",
            "security_basis": "PRIMARY_DEPOSITARY_RECEIPT",
            "receipt_ratio": Decimal("8"),
            "engine_revision": 181,
            "canonical_payload": {"ticker": "ABT", "assets": 100},
            "artifacts": (
                SnapshotArtifact(facts_hash, "facts"),
                SnapshotArtifact(old_cover_hash, "raw_filing"),
            ),
        }
        first = repository.store_and_select(**values)
        repeated = repository.store_and_select(**values)
        mapping_only = repository.store_and_select(
            **{
                **values,
                "security_identifier": "sec:0000044287:mapping-only",
                "security_title": None,
                "source_accession": None,
                "security_basis": "SEC_TICKER_MAPPING_ONLY",
                "receipt_ratio": None,
            }
        )
        mapping_only_retry = repository.store_and_select(
            **{
                **values,
                "security_identifier": "sec:0000044287:mapping-only",
                "security_title": None,
                "source_accession": None,
                "security_basis": "SEC_TICKER_MAPPING_ONLY",
                "receipt_ratio": None,
            }
        )
        later_cover = repository.store_and_select(
            **{
                **values,
                "ticker": "ABTX",
                "exchange_code": "NASDAQ",
                "security_title": (
                    "American Depositary Shares, each representing four shares"
                ),
                "source_accession": "0001104659-26-020000",
                "receipt_ratio": Decimal("4"),
            }
        )
        changed_evidence = repository.store_and_select(
            **{
                **values,
                "ticker": "ABTX",
                "exchange_code": "NASDAQ",
                "security_title": (
                    "American Depositary Shares, each representing four shares"
                ),
                "source_accession": "0001104659-26-020000",
                "receipt_ratio": Decimal("4"),
                "artifacts": (
                    SnapshotArtifact(facts_hash, "facts"),
                    SnapshotArtifact(new_cover_hash, "raw_filing"),
                ),
            }
        )

        assert first.snapshot_id == repeated.snapshot_id
        assert first.created is True
        assert repeated.created is False
        assert mapping_only.snapshot_id == mapping_only_retry.snapshot_id
        assert mapping_only.created is True
        assert mapping_only_retry.created is False
        assert repository.current(mapping_only.security_id).security_title is None
        assert repository.current(mapping_only.security_id).source_accession is None
        assert len(
            {first.snapshot_id, later_cover.snapshot_id, changed_evidence.snapshot_id}
        ) == 3
        assert len(repository.snapshots(first.security_id)) == 3
        engine.dispose()
        engine = None

        restarted_engine = create_engine(database_url)
        restarted = SharedCompanyRepository(restarted_engine)
        snapshots = restarted.snapshots(first.security_id)
        assert restarted.current(first.security_id).snapshot_id == (
            changed_evidence.snapshot_id
        )
        assert [snapshot.snapshot_id for snapshot in snapshots] == [
            first.snapshot_id,
            later_cover.snapshot_id,
            changed_evidence.snapshot_id,
        ]
        assert [snapshot.source_accession for snapshot in snapshots] == [
            "0001104659-25-018349",
            "0001104659-26-020000",
            "0001104659-26-020000",
        ]
        assert [snapshot.receipt_ratio for snapshot in snapshots] == [
            Decimal("8"),
            Decimal("4"),
            Decimal("4"),
        ]
        assert restarted.artifacts(first.snapshot_id) == [
            SnapshotArtifact(facts_hash, "facts"),
            SnapshotArtifact(old_cover_hash, "raw_filing"),
        ]
        assert restarted.artifacts(later_cover.snapshot_id) == [
            SnapshotArtifact(facts_hash, "facts"),
            SnapshotArtifact(old_cover_hash, "raw_filing"),
        ]
        assert restarted.artifacts(changed_evidence.snapshot_id) == [
            SnapshotArtifact(facts_hash, "facts"),
            SnapshotArtifact(new_cover_hash, "raw_filing"),
        ]
        assert {
            column["name"]
            for column in inspect(restarted_engine).get_columns("priced_security")
        } == {"security_id", "issuer_id", "security_identifier", "created_at"}
        snapshot_columns = {
            column["name"]: column
            for column in inspect(restarted_engine).get_columns("company_snapshot")
        }
        assert snapshot_columns["security_title"]["nullable"] is True
        assert snapshot_columns["source_accession"]["nullable"] is True
        with pytest.raises(IntegrityError):
            with restarted_engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE company_snapshot SET security_title = '' "
                        "WHERE snapshot_id = :snapshot_id"
                    ),
                    {"snapshot_id": mapping_only.snapshot_id},
                )
        with pytest.raises(IntegrityError):
            with restarted_engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE company_snapshot SET source_accession = '' "
                        "WHERE snapshot_id = :snapshot_id"
                    ),
                    {"snapshot_id": mapping_only.snapshot_id},
                )
    finally:
        if engine is not None:
            engine.dispose()
        if restarted_engine is not None:
            restarted_engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin_engine.dispose()


def _seed_0002_company(engine):
    digest = "d" * 64
    payload = {"legacy": True}
    canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_sha256 = hashlib.sha256(canonical_payload.encode()).hexdigest()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO evidence_artifact
                    (content_sha256, object_key, byte_size, media_type, verified_at)
                VALUES
                    (:digest, :object_key, 7, 'application/json', :verified_at)
                """
            ),
            {
                "digest": digest,
                "object_key": f"raw/sha256/{digest}",
                "verified_at": datetime.now(timezone.utc),
            },
        )
        issuer_id = connection.scalar(
            text(
                """
                INSERT INTO issuer (source_system, source_identifier)
                VALUES ('SEC', '0000000001')
                RETURNING issuer_id
                """
            )
        )
        security_id = connection.scalar(
            text(
                """
                INSERT INTO priced_security
                    (issuer_id, security_identifier, ticker, exchange_code,
                     quote_currency, security_title, source_accession,
                     security_basis, receipt_ratio)
                VALUES
                    (:issuer_id, 'sec:0000000001:legacy', 'LEGACY', 'NYSE',
                     'USD', 'Legacy American Depositary Shares',
                     'legacy-cover-accession', 'PRIMARY_DEPOSITARY_RECEIPT', 8)
                RETURNING security_id
                """
            ),
            {"issuer_id": issuer_id},
        )
        snapshot_id = connection.scalar(
            text(
                """
                INSERT INTO company_snapshot
                    (security_id, engine_revision, payload_sha256, canonical_payload)
                VALUES
                    (:security_id, 181, :payload_sha256,
                     CAST(:canonical_payload AS jsonb))
                RETURNING snapshot_id
                """
            ),
            {
                "security_id": security_id,
                "payload_sha256": payload_sha256,
                "canonical_payload": canonical_payload,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO company_snapshot_artifact
                    (snapshot_id, content_sha256, role)
                VALUES (:snapshot_id, :digest, 'raw_filing')
                """
            ),
            {"snapshot_id": snapshot_id, "digest": digest},
        )
        connection.execute(
            text(
                """
                INSERT INTO current_company_snapshot (security_id, snapshot_id)
                VALUES (:security_id, :snapshot_id)
                """
            ),
            {"security_id": security_id, "snapshot_id": snapshot_id},
        )
    return {
        "security_id": security_id,
        "snapshot_id": snapshot_id,
        "digest": digest,
    }


def _seed_0003_company(engine):
    digest = "e" * 64
    with engine.begin() as connection:
        connection.execute(
            evidence_artifact.insert().values(
                content_sha256=digest,
                object_key=f"raw/sha256/{digest}",
                byte_size=7,
                media_type="application/json",
                verified_at=datetime.now(timezone.utc),
            )
        )
    stored = SharedCompanyRepository(engine).store_and_select(
        issuer_source="SEC",
        issuer_identifier="0000000002",
        security_identifier="sec:0000000002:legacy",
        ticker="LEGACY3",
        exchange_code="NYSE",
        quote_currency="USD",
        security_title="Legacy American Depositary Shares",
        source_accession="legacy-cover-accession",
        security_basis="PRIMARY_DEPOSITARY_RECEIPT",
        receipt_ratio=Decimal("8"),
        engine_revision=181,
        canonical_payload={"legacy": True},
        artifacts=(SnapshotArtifact(digest, "raw_filing"),),
    )
    return {
        "security_id": stored.security_id,
        "snapshot_id": stored.snapshot_id,
        "digest": digest,
    }
