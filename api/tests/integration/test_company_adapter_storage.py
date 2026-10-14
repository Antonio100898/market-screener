"""Real isolated publication lineage mechanics; synthetic custody bytes, no financial proof."""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import issuer, source_item
from screener.publication import PublicationRepository, StalePublicationSource
from screener.shared_companies import SharedCompanyRepository, SnapshotArtifact
from screener.source_observations import SourceItemIdentity, SourceObservationRepository
from screener.storage_config import StorageSettings
from test_publication_storage import storage, _claim, NOW, pytestmark


def test_adapter_migration_and_bootstrap_require_actual_current_raw_lineage(storage):
    engine, raw = storage
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261013_0022"
    settings = StorageSettings.from_env()
    objects = ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket)
    generated = store_evidence(b'{"fixture":"generated adapter metadata"}', "application/json",
        objects, SqlArtifactRepository(engine))
    snapshot = SharedCompanyRepository(engine, clock=lambda: NOW).store_and_select(
        issuer_source="COMPANY_ADAPTER", issuer_identifier="IFRS-ADIDAS-AG",
        security_identifier="company-adapter:IFRS-ADIDAS-AG:primary-ordinary", ticker="ADS.DE",
        exchange_code="XETRA", quote_currency="EUR", security_title="PRIMARY_ORDINARY_SHARE",
        source_accession="adidas-fixture", security_basis="PRIMARY_ORDINARY_SHARE", engine_revision=194,
        canonical_payload=dict(ticker="ADS.DE", cik="IFRS-ADIDAS-AG"),
        artifacts=[SnapshotArtifact(raw.content_sha256, "raw_filing"),
                   SnapshotArtifact(generated.content_sha256, "canonical_company_adapter_facts")])
    lease = _claim(engine, "adapter-fixture")
    publications = PublicationRepository(engine, clock=lambda: NOW)
    with pytest.raises(ValueError, match="filing"):
        publications.bootstrap(lease, request_key="adapter-unobserved")
    observations = SourceObservationRepository(engine, clock=lambda: NOW)
    item = SourceItemIdentity("COMPANY_ADAPTER", "filing", "adidas-fixture", "IFRS-ADIDAS-AG")
    recorded = observations.record(lease, item=item, item_outcome_key="adapter-present",
        state="present", metadata={"kind": "isolated custody fixture; no downloaded-source claim"},
        source_url="https://www.adidas-group.com/isolated-fixture", artifact_sha256=raw.content_sha256)
    assert publications.bootstrap(lease, request_key="adapter-observed").member_count == 1
    observations.record(lease, item=item, item_outcome_key="adapter-withdrawn", state="removed",
        metadata={"reason": "isolated withdrawal fixture"},
        source_url="https://www.adidas-group.com/isolated-fixture")
    with engine.begin() as connection:
        with pytest.raises(StalePublicationSource):
            publications._validate_sources(connection, (recorded.observation.source_observation_id,),
                                          snapshot.security_id, snapshot.snapshot_id)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(issuer.insert().values(source_system="COMPANY_ADAPTER", source_identifier="OTHER"))
    for identifier in (None, "OTHER"):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(source_item.insert().values(source_system="COMPANY_ADAPTER", item_kind="filing",
                    source_key="unsupported", issuer_source_identifier=identifier))
