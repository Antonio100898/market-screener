from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, create_engine, event, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from screener.postgres import (
    company_snapshot,
    company_snapshot_artifact,
    current_company_snapshot,
    evidence_artifact,
    issuer,
    metadata,
    priced_security,
)
from screener.shared_companies import (
    IdentityConflict,
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
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys = ON")

    metadata.create_all(engine)
    yield SharedCompanyRepository(engine)
    engine.dispose()


def _company(**overrides):
    values = {
        "issuer_source": "SEC",
        "issuer_identifier": "44287",
        "security_identifier": "sec:0000044287:primary-common",
        "ticker": "ABT",
        "exchange_code": "NYSE",
        "quote_currency": "usd",
        "security_title": "Common Stock, without par value",
        "source_accession": "0001104659-26-018349",
        "security_basis": "PRIMARY_COMMON_SHARE",
        "engine_revision": 181,
        "canonical_payload": {"ticker": "ABT", "assets": 100},
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


def _count(repository, table):
    with repository.engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(table))


def test_stable_issuer_and_security_identity_rejects_conflicts(repository):
    first = repository.store_and_select(**_company())
    repeated = repository.store_and_select(**_company())

    assert first.issuer_id == repeated.issuer_id
    assert first.security_id == repeated.security_id
    assert _count(repository, issuer) == 1
    assert _count(repository, priced_security) == 1

    with pytest.raises(IdentityConflict, match="priced security"):
        repository.store_and_select(
            **_company(
                issuer_identifier="12345",
                ticker="OTHER",
            )
        )
    assert _count(repository, issuer) == 1
    assert _count(repository, priced_security) == 1


def test_edinet_code_has_a_separate_stable_issuer_identity(repository):
    stored = repository.store_and_select(
        **_company(
            issuer_source="edinet",
            issuer_identifier="e01772",
            security_identifier="edinet:E01772:primary-ordinary",
            ticker="6752.t",
            exchange_code="TSE",
            quote_currency="jpy",
            security_title="Common stock",
            source_accession="S100W4QX",
            security_basis="PRIMARY_ORDINARY_SHARE",
            canonical_payload={"ticker": "6752.T"},
        )
    )

    with repository.engine.connect() as connection:
        row = connection.execute(
            select(issuer).where(issuer.c.issuer_id == stored.issuer_id)
        ).mappings().one()
    assert (row["source_system"], row["source_identifier"]) == (
        "EDINET",
        "E01772",
    )


def test_snapshot_write_is_canonical_and_idempotent(repository):
    first = repository.store_and_select(
        **_company(canonical_payload={"assets": 100, "ticker": "ABT"})
    )
    repeated = repository.store_and_select(
        **_company(canonical_payload={"ticker": "ABT", "assets": 100})
    )

    assert first.snapshot_id == repeated.snapshot_id
    assert first.created is True
    assert repeated.created is False
    assert first.payload_sha256 == repeated.payload_sha256
    assert _count(repository, company_snapshot) == 1
    assert repository.current(first.security_id).canonical_payload == {
        "assets": 100,
        "ticker": "ABT",
    }


def test_snapshot_accepts_absent_cover_evidence_but_not_empty_values(repository):
    stored = repository.store_and_select(
        **_company(
            security_title=None,
            source_accession=None,
            security_basis="SEC_TICKER_MAPPING_ONLY",
        )
    )

    snapshot = repository.current(stored.security_id)
    assert snapshot.security_title is None
    assert snapshot.source_accession is None
    assert snapshot.receipt_ratio is None

    with pytest.raises(ValueError, match="must not be empty"):
        repository.store_and_select(**_company(security_title=""))
    with pytest.raises(ValueError, match="must not be empty"):
        repository.store_and_select(**_company(source_accession=" "))


def test_later_cover_and_ratio_create_a_new_snapshot_for_the_same_security(
    repository,
):
    first = repository.store_and_select(
        **_company(
            security_title="American Depositary Shares, each representing eight shares",
            source_accession="0001104659-25-018349",
            security_basis="PRIMARY_DEPOSITARY_RECEIPT",
            receipt_ratio=Decimal("8"),
        )
    )
    second = repository.store_and_select(
        **_company(
            ticker="ABTX",
            exchange_code="NASDAQ",
            security_title="American Depositary Shares, each representing four shares",
            source_accession="0001104659-26-020000",
            security_basis="PRIMARY_DEPOSITARY_RECEIPT",
            receipt_ratio=Decimal("4"),
        )
    )

    snapshots = repository.snapshots(first.security_id)
    assert second.security_id == first.security_id
    assert second.snapshot_id != first.snapshot_id
    assert _count(repository, priced_security) == 1
    assert [snapshot.source_accession for snapshot in snapshots] == [
        "0001104659-25-018349",
        "0001104659-26-020000",
    ]
    assert [snapshot.receipt_ratio for snapshot in snapshots] == [
        Decimal("8.0000000000"),
        Decimal("4.0000000000"),
    ]
    assert [snapshot.ticker for snapshot in snapshots] == ["ABT", "ABTX"]


def test_artifact_links_are_many_per_snapshot_and_deduplicated(repository):
    facts_hash = "a" * 64
    filing_hash = "b" * 64
    _add_artifact(repository, facts_hash)
    _add_artifact(repository, filing_hash)
    first = repository.store_and_select(
        **_company(
            artifacts=(
                SnapshotArtifact(facts_hash, "facts"),
                SnapshotArtifact(facts_hash, "facts"),
                SnapshotArtifact(facts_hash, "dimensioned_facts"),
            )
        )
    )
    repeated = repository.store_and_select(
        **_company(
            artifacts=(
                SnapshotArtifact(facts_hash, "dimensioned_facts"),
                SnapshotArtifact(facts_hash, "facts"),
            )
        )
    )
    changed = repository.store_and_select(
        **_company(
            artifacts=(
                SnapshotArtifact(facts_hash, "dimensioned_facts"),
                SnapshotArtifact(facts_hash, "facts"),
                SnapshotArtifact(filing_hash, "raw_filing"),
            )
        )
    )

    assert repeated.snapshot_id == first.snapshot_id
    assert changed.snapshot_id != first.snapshot_id
    assert _count(repository, company_snapshot_artifact) == 5
    assert repository.artifacts(first.snapshot_id) == [
        SnapshotArtifact(facts_hash, "dimensioned_facts"),
        SnapshotArtifact(facts_hash, "facts"),
    ]
    assert repository.artifacts(changed.snapshot_id) == [
        SnapshotArtifact(facts_hash, "dimensioned_facts"),
        SnapshotArtifact(facts_hash, "facts"),
        SnapshotArtifact(filing_hash, "raw_filing"),
    ]


def test_new_selection_keeps_the_prior_snapshot(repository):
    first = repository.store_and_select(**_company())
    second = repository.store_and_select(
        **_company(canonical_payload={"ticker": "ABT", "assets": 105})
    )

    assert second.snapshot_id != first.snapshot_id
    assert repository.current(first.security_id).snapshot_id == second.snapshot_id
    assert [row.snapshot_id for row in repository.snapshots(first.security_id)] == [
        first.snapshot_id,
        second.snapshot_id,
    ]
    assert _count(repository, current_company_snapshot) == 1
    assert _count(repository, company_snapshot) == 2


def test_receipt_ratio_requires_an_exact_positive_value(repository):
    with pytest.raises(TypeError, match="binary floating point"):
        repository.store_and_select(**_company(receipt_ratio=2.5))
    with pytest.raises(ValueError, match="exact positive"):
        repository.store_and_select(**_company(receipt_ratio=Decimal("0")))

    stored = repository.store_and_select(
        **_company(
            security_title="American Depositary Shares",
            security_basis="PRIMARY_DEPOSITARY_RECEIPT",
            receipt_ratio=Decimal("0.125"),
        )
    )
    assert repository.current(stored.security_id).receipt_ratio == Decimal(
        "0.1250000000"
    )
