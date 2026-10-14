"""Real owner publication/query mechanics for source-unavailable identities."""
from datetime import timedelta

import pytest

from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.company_availability import (ARTIFACT_ROLE, LOCAL_KIND, load_assessment,
    REMOTE_KIND, local_observation_metadata, local_source_item, local_source_url, unavailable_row)
from screener.company_queries import CompanyQuery, CompanyQueryRepository, RestartQuery
from screener.complete_accounting import load_accounting
from screener.display_publication import (CAPTURE_FIELDS, DisplayCompanyInput,
    DisplayReleasePreparer, canonical_bytes, capture_fields)
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.publication import PublicationRepository, StalePublicationSource
from screener.shared_companies import SharedCompanyRepository, SnapshotArtifact
from screener.source_observations import SourceItemIdentity, SourceObservationRepository
from screener.storage_config import StorageSettings
from test_publication_storage import storage, _claim, NOW, pytestmark


def make_unavailable(engine, lease, objects, *, ticker, cik, reason="Retained source file is missing locally."):
    artifacts = SqlArtifactRepository(engine)
    assessment = load_assessment(dict(kind=LOCAL_KIND, issuer_source="SEC", issuer_identifier=cik,
        ticker=ticker, reason_code="LOCAL_INPUT_MISSING", reason=reason,
        assessed_at=NOW.isoformat(), input_artifact_sha256s=[], remote_observation_ids=[]))
    diagnostic = store_evidence(canonical_bytes(assessment.json_record()), "application/json", objects, artifacts)
    snapshot = SharedCompanyRepository(engine, clock=lambda: NOW).store_and_select(
        issuer_source="SEC", issuer_identifier=cik, security_identifier=f"sec:{cik}:primary-listed-security",
        ticker=ticker, security_title=None, source_accession=None, security_basis="UNASSESSED",
        engine_revision=194, canonical_payload=unavailable_row(assessment, diagnostic.content_sha256,
            name=ticker, exchange="NYSE"), artifacts=[SnapshotArtifact(diagnostic.content_sha256, ARTIFACT_ROLE)])
    record = SourceObservationRepository(engine, clock=lambda: NOW).record(lease,
        item=local_source_item(assessment), item_outcome_key=f"availability:{ticker}", state="present",
        metadata=local_observation_metadata(assessment, diagnostic.content_sha256),
        source_url=local_source_url(assessment), artifact_sha256=diagnostic.content_sha256)
    row = unavailable_row(assessment, diagnostic.content_sha256, name=ticker, exchange="NYSE")
    captures = {}
    for role in CAPTURE_FIELDS:
        fields = capture_fields(role, row)
        if any(value not in (None, [], {}) for value in fields.values()):
            captured = store_evidence(canonical_bytes(dict(role=role, row_fields=fields,
                provider="actual retained test identity", captured_at=NOW.isoformat(),
                input_artifacts=[diagnostic.content_sha256])), "application/json", objects, artifacts)
            captures[role] = dict(status="CAPTURED", artifact_sha256=captured.content_sha256)
        else:
            captures[role] = dict(status="ABSENT", reason="No reported financial or price inputs")
    company = DisplayCompanyInput(snapshot.security_id, snapshot.snapshot_id, row, captures,
                                 (record.observation.source_observation_id,))
    return snapshot, company, assessment, diagnostic


def test_complete_release_lists_missing_source_identities_and_restarts_after_reassessment(storage):
    engine, _ = storage
    lease = _claim(engine, "unavailable-preview")
    settings = StorageSettings.from_env()
    objects = ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket)
    a = make_unavailable(engine, lease, objects, ticker="ABT", cik="0000001800")
    b = make_unavailable(engine, lease, objects, ticker="SECOND", cik="0000000002")
    repo = PublicationRepository(engine, clock=lambda: NOW)
    parent = repo.bootstrap(lease, request_key="unavailable-bootstrap")
    bindings = [dict(security_id=value[0].security_id, snapshot_id=value[0].snapshot_id, exact_bundle_id=None) for value in (a, b)]
    accounting = load_accounting(canonical_bytes(dict(accounting_revision=1,
        expected_parent_release_id=parent.release_id, engine_identity_sha256="a" * 64,
        engine_identity_status="DECLARED_NOT_VERIFIED",
        universe=[dict(key=value[1].strict_row["ticker"], artifact_sha256s=[value[3].content_sha256]) for value in (a, b)],
        outcomes=[dict(input_key=value[1].strict_row["ticker"], status="SELECTED", selection=binding)
                  for value, binding in zip((a, b), bindings)],
        parent_members=bindings, selected_members=[dict(**binding, disposition="RETAINED") for binding in bindings],
        retirements=[])))
    preparer = DisplayReleasePreparer(engine, objects)
    prepared = preparer.prepare_complete([a[1], b[1]], accounting=accounting,
        capture_mode="caller_captured_enrichment", limitations=["Source-unavailable diagnostics; no financial observations"])
    release = repo.publish_complete_display(lease, request_key="unavailable-display",
        expected_parent_release_id=parent.release_id, prepared=prepared, object_store=objects)
    queries = CompanyQueryRepository(engine, cursor_secret=b"owned-real-query-cursor-contract-secret")
    first = queries.list(CompanyQuery(limit=1))
    assert first["matched"] == 2 and first["next_cursor"]
    second = queries.list(CompanyQuery(limit=1), cursor=first["next_cursor"])
    assert {row["ticker"] for row in first["rows"] + second["rows"]} == {"ABT", "SECOND"}
    for value in (a, b):
        detail = queries.detail(security_id=value[0].security_id, release_id=release.release_id)
        assert detail["row"]["data_availability"]["status"] == "UNAVAILABLE"
        assert detail["row"]["price"] is None and detail["row"]["shares"] is None
        assert all(item["value"] is None for item in detail["row"]["criteria"])
        assert detail["row"]["assumption_mode"]["status"] == "UNAVAILABLE"
    changed = a[2].model_copy(update={"reason": "A different captured local input assessment."})
    artifact = store_evidence(canonical_bytes(changed.json_record()), "application/json", objects, SqlArtifactRepository(engine))
    SourceObservationRepository(engine, clock=lambda: NOW + timedelta(seconds=1)).record(lease,
        item=local_source_item(changed), item_outcome_key="ABT-reassessment", state="present",
        metadata=local_observation_metadata(changed, artifact.content_sha256),
        source_url=local_source_url(changed), artifact_sha256=artifact.content_sha256,
        detected_at=NOW + timedelta(seconds=1))
    with pytest.raises(RestartQuery):
        queries.list(CompanyQuery(limit=1), cursor=first["next_cursor"])
    next_accounting = accounting.model_copy(update={"expected_parent_release_id": release.release_id})
    next_prepared = preparer.prepare_complete([a[1], b[1]], accounting=next_accounting,
        capture_mode="caller_captured_enrichment", limitations=["Captured diagnoses remain unassessed"])
    with pytest.raises(StalePublicationSource):
        repo.publish_complete_display(lease, request_key="stale-diagnosis",
            expected_parent_release_id=release.release_id, prepared=next_prepared, object_store=objects)


def test_remote_unavailable_snapshot_requires_the_captured_current_observation(storage):
    engine, _ = storage
    lease = _claim(engine, "remote-unavailable-preview")
    observations = SourceObservationRepository(engine, clock=lambda: NOW)
    item = SourceItemIdentity("SEC", "resource", "captured-unavailable-test", "0000001800")
    remote = observations.record(lease,
        item=item,
        item_outcome_key="captured-remote", state="unavailable", metadata={"reason": "Test captured provider failure"},
        source_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000001800.json")
    assessment = load_assessment(dict(kind=REMOTE_KIND, issuer_source="SEC", issuer_identifier="0000001800",
        ticker="ABT", reason_code="REMOTE_SOURCE_UNAVAILABLE", reason="Test captured provider failure",
        assessed_at=NOW.isoformat(), input_artifact_sha256s=[],
        remote_observation_ids=[remote.observation.source_observation_id]))
    settings = StorageSettings.from_env()
    objects = ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket)
    artifact = store_evidence(canonical_bytes(assessment.json_record()), "application/json", objects, SqlArtifactRepository(engine))
    snapshot = SharedCompanyRepository(engine, clock=lambda: NOW).store_and_select(
        issuer_source="SEC", issuer_identifier="0000001800", security_identifier="sec:0000001800:primary-listed-security",
        ticker="ABT", security_title=None, source_accession=None, security_basis="UNASSESSED",
        engine_revision=194, canonical_payload=unavailable_row(assessment, artifact.content_sha256),
        artifacts=[SnapshotArtifact(artifact.content_sha256, ARTIFACT_ROLE)])
    repo = PublicationRepository(engine, clock=lambda: NOW)
    release = repo.bootstrap(lease, request_key="captured-unavailable-bootstrap")
    assert release.member_count == 1
    observations.record(lease, item=item, item_outcome_key="now-removed", state="removed",
        metadata={"reason": "Test withdrawal is not an unavailable diagnosis"},
        source_url=remote.observation.source_url, detected_at=NOW + timedelta(seconds=1))
    with engine.begin() as connection:
        with pytest.raises(StalePublicationSource):
            repo._validate_sources(connection, (remote.observation.source_observation_id,),
                                   snapshot.security_id, snapshot.snapshot_id)
