"""Real storage over synthetic financial/source fixtures; no native authority."""
import os
import hashlib
from dataclasses import replace

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from screener import figure_history as owner
from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.durable_jobs import LeaseLost
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import (active_data_release, data_release, exact_figure_bundle,
    figure_selection, publication_candidate, release_candidate, release_company, source_observation)
from screener.publication import PublicationRepository
from screener.source_observations import SourceItemIdentity, SourceObservationRepository
from screener.storage_config import StorageSettings
from tests.test_figure_history import example_bundle, validate_changed
from tests.test_figure_history_contract2 import document_bundle
from tests.test_figure_history_contract3 import declared_bundle, native_bundle
from test_figure_history_storage import bound_bundle
from test_publication_storage import NOW, _company, _source, storage


pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1",
                              reason="requires root-assigned isolated PostgreSQL/S3")


@pytest.fixture
def objects():
    settings = StorageSettings.from_env()
    client = create_s3_client(settings)
    try:
        yield ImmutableObjectStore(client, settings.s3_bucket)
    finally:
        client.close()


def state(engine):
    with engine.connect() as connection:
        return {table.name: connection.scalar(select(func.count()).select_from(table))
                for table in (exact_figure_bundle, data_release, release_company, release_candidate, publication_candidate)} | {
            "active": connection.scalar(select(active_data_release.c.release_id))}


def prepared(storage, objects, *, documents=True):
    engine, lease, company, original = bound_bundle(storage)
    with engine.connect() as connection:
        url = connection.scalar(select(source_observation.c.source_url).where(
            source_observation.c.source_observation_id == original.sources[0].source_observation_id))
    observed = original.observations[0].model_copy(update=dict(reference=
        original.observations[0].reference.model_copy(update=dict(source_document=url))))
    original = validate_changed(original, observations=(observed,))
    if documents:
        original = document_bundle(original, body=b"publication source bytes: 100")
    bundle = declared_bundle(original, document=documents)
    repository = owner.FigureHistoryRepository(engine, clock=lambda: NOW)
    token = repository.prepare_documents(bundle, objects)
    return engine, lease, company, bundle, repository, token


def seed_release(engine, lease, members, *, key="seeded", parent=None, candidates=(), active=False):
    with engine.begin() as connection:
        identifier = connection.scalar(data_release.insert().values(request_key=key,
            publishing_job_id=lease.job_id, parent_release_id=parent, figure_history_revision=1).returning(data_release.c.release_id))
        connection.execute(release_company.insert(), [dict(release_id=identifier, **member) for member in members])
        if candidates:
            connection.execute(release_candidate.insert(), [dict(release_id=identifier, candidate_id=value) for value in candidates])
        connection.execute(update(data_release).where(data_release.c.release_id == identifier).values(published_at=NOW))
        if active:
            connection.execute(update(active_data_release).where(active_data_release.c.singleton_id == 1).values(release_id=identifier))
    return identifier


def member(bundle, identifier):
    return dict(security_id=bundle.security_id, snapshot_id=bundle.snapshot_id, exact_bundle_id=identifier)


def test_declared3_bounded_storage_roundtrip_exact_replay_and_no_activation(storage, objects):
    engine, lease, _, bundle, repository, token = prepared(storage, objects)
    before = state(engine)
    identifier = repository.store(lease, request_key="declared3", bundle=bundle, prepared_documents=token)
    assert repository.load(identifier) == bundle
    assert repository.store(lease, request_key="declared3", bundle=bundle, prepared_documents=token) == identifier
    restored = owner.FigureHistoryRepository(engine, clock=lambda: NOW).load(identifier)
    assert restored.selections[0].value_decimal_text is None
    assert all(w.association_status == "DECLARED_NOT_VERIFIED" for w in restored.selections[0].source_witnesses)
    after = state(engine)
    assert after == {**before, "exact_figure_bundle": before["exact_figure_bundle"] + 1}
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(update(figure_selection).where(figure_selection.c.bundle_id == identifier).values(record={}))


@pytest.mark.parametrize("fault", ["legacy-proof", "limits", "source-url", "replay-change"])
def test_proof_source_and_replay_conflicts_leave_owned_storage_unchanged(storage, objects, fault):
    engine, lease, _, bundle, repository, token = prepared(storage, objects)
    if fault == "legacy-proof":
        old = validate_changed(bundle, contract_revision=2, selections=(bundle.selections[0].model_copy(update=dict(source_witnesses=())),))
        token = repository.prepare_documents(old, objects)
    elif fault == "limits":
        token = replace(token, limits_sha256="a" * 64)
    elif fault == "source-url":
        observed = bundle.observations[0].model_copy(update=dict(reference=bundle.observations[0].reference.model_copy(update=dict(source_document="wrong-document"))))
        bundle = validate_changed(bundle, observations=(observed,))
    else:
        repository.store(lease, request_key="conflict", bundle=bundle, prepared_documents=token)
        bundle = validate_changed(bundle, selections=(bundle.selections[0].model_copy(update=dict(reason="changed-declaration")),))
    before = state(engine)
    with pytest.raises((ValueError, owner.FigureHistoryConflict)):
        repository.store(lease, request_key="conflict", bundle=bundle, prepared_documents=token)
    assert state(engine) == before


@pytest.mark.parametrize("wrapper", ["store", "store_on_connection"])
def test_native_unlinked_or_replayed_records_stay_closed_before_sql_writes(storage, wrapper):
    engine, lease, _, original = bound_bundle(storage)
    template = native_bundle()
    bundle = validate_changed(template, security_id=original.security_id, snapshot_id=original.snapshot_id,
        snapshot_sha256=original.snapshot_sha256, sources=original.sources, engine_revision=original.engine_revision)
    repository = owner.FigureHistoryRepository(engine, clock=lambda: NOW)
    before = state(engine)
    for attempt in range(2):
        with pytest.raises(ValueError, match="native persistence is closed"):
            if wrapper == "store":
                repository.store(lease, request_key="native-closed", bundle=bundle)
            else:
                with engine.begin() as connection:
                    repository.store_on_connection(connection, lease, request_key="native-closed", bundle=bundle)
        assert state(engine) == before


def test_bounded_document_checks_whole_registered_object_before_small_span_read(storage, objects):
    engine, lease, _, bundle, repository, _ = prepared(storage, objects)
    body = b"x" * (8 * 1024 * 1024 + 1)
    artifact = store_evidence(body, "text/plain", objects, SqlArtifactRepository(engine))
    document = bundle.document_evidence[0].model_copy(update=dict(reference=bundle.document_evidence[0].reference.model_copy(update=dict(
        artifact_sha256=artifact.content_sha256, location=owner.DocumentSpan(start_byte=0, end_byte=1,
            span_sha256=hashlib.sha256(b"x").hexdigest())))))
    class RefuseRead:
        def read_verified(self, *args):
            raise AssertionError("oversize metadata reached object reader")
    before = state(engine)
    with pytest.raises(ValueError, match="body exceeds"):
        repository.prepare_document_records((document,), RefuseRead(), contract_revision=3)
    assert state(engine) == before


@pytest.mark.parametrize("revision", [1, 2])
def test_seeded_legacy_historical_replay_remains_valid_after_source_changes(storage, revision):
    engine, lease, _, bundle = bound_bundle(storage)
    bundle = validate_changed(bundle, contract_revision=revision)
    identifier = owner.FigureHistoryRepository(engine, clock=lambda: NOW).store(lease, request_key="legacy", bundle=bundle)
    release = seed_release(engine, lease, [member(bundle, identifier)], key="legacy-replay")
    source = SourceObservationRepository(engine, clock=lambda: NOW)
    source.record(lease, item=SourceItemIdentity("SEC", "filing", "filing-1", "0000001800"),
        item_outcome_key="later-unavailable", state="pending", metadata={"revision": 2},
        source_url="https://www.sec.gov/filing-1")
    before = state(engine)
    actual = PublicationRepository(engine, clock=lambda: NOW).bootstrap(lease, request_key="legacy-replay")
    assert actual.release_id == release and state(engine) == before


@pytest.mark.parametrize("caller", ["bootstrap", "publish"])
def test_actual_seeded_contract3_existing_release_replay_refuses_before_writes(storage, objects, caller):
    engine, lease, _, bundle, history, token = prepared(storage, objects)
    identifier = history.store(lease, request_key="declared-replay", bundle=bundle, prepared_documents=token)
    candidates = ()
    if caller == "publish":
        with engine.begin() as connection:
            candidate = connection.scalar(publication_candidate.insert().values(request_key="seeded-candidate3",
                security_id=bundle.security_id, snapshot_id=bundle.snapshot_id, expected_parent_release_id=None,
                cause_type="source_change", registering_job_id=lease.job_id, exact_bundle_id=identifier).returning(publication_candidate.c.candidate_id))
            connection.execute(update(publication_candidate).where(publication_candidate.c.candidate_id == candidate).values(registered_at=NOW))
        candidates = (candidate,)
    seed_release(engine, lease, [member(bundle, identifier)], key="declared-replay", candidates=candidates)
    publisher = PublicationRepository(engine, clock=lambda: NOW)
    before = state(engine)
    with pytest.raises(ValueError, match="not publication qualified"):
        if caller == "bootstrap":
            publisher.bootstrap(lease, request_key="declared-replay")
        else:
            publisher.publish(lease, request_key="declared-replay", candidate_ids=candidates, expected_parent_release_id=None)
    assert state(engine) == before


def test_changed_legacy_member_cannot_publish_with_unchanged_retained3_parent(storage, objects):
    engine, lease, _, bundle, history, token = prepared(storage, objects)
    identifier = history.store(lease, request_key="declared-parent", bundle=bundle, prepared_documents=token)
    parent = seed_release(engine, lease, [member(bundle, identifier)], key="declared-parent", active=True)
    artifact = store_evidence(b'{"cik":1900,"facts":{"us-gaap":{"Assets":{"units":{"USD":[{"val":100.0000,"end":"2025-12-31"}]}}}}}',
        "application/json", objects, SqlArtifactRepository(engine))
    company = _company(engine, artifact, ticker="NEW", identifier="1900", lease=lease)
    source_id = _source(engine, artifact, lease, identifier="1900", key="new-filing")
    with engine.connect() as connection:
        from screener.postgres import source_item_current
        pointer = connection.execute(select(source_item_current).where(source_item_current.c.source_observation_id == source_id)).mappings().one()
    current = example_bundle(security_id=company.security_id, snapshot_id=company.snapshot_id,
        snapshot_sha256=company.snapshot_sha256, artifact_sha256=artifact.content_sha256,
        source_item_id=pointer["source_item_id"], source_observation_id=source_id, occurrence_id=pointer["occurrence_id"])
    scope = current.observations[0].scope.model_copy(update=dict(entity_identifier="0000001900"))
    observed = current.observations[0].model_copy(update=dict(scope=scope, reference=current.observations[0].reference.model_copy(update=dict(issuer_source_identifier="0000001900"))))
    current = validate_changed(current, contract_revision=2, observations=(observed,), selections=(current.selections[0].model_copy(update=dict(scope=scope)),))
    changed = history.store(lease, request_key="changed-legacy2", bundle=current)
    publisher = PublicationRepository(engine, clock=lambda: NOW)
    candidate = publisher.register_candidate(lease, request_key="changed-legacy2", snapshot_id=company.snapshot_id,
        expected_parent_release_id=parent, source_observation_ids=(source_id,), cause_type="source_change",
        exact_bundle_id=changed, expected_exact_run_key=current.pipeline_run_key)
    before = state(engine)
    with pytest.raises(ValueError, match="not publication qualified"):
        publisher.publish(lease, request_key="retained3-refusal", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=parent)
    assert state(engine) == before


def test_declared3_caller_rollback_and_expired_lease_preserve_atomicity(storage, objects):
    engine, lease, _, bundle, history, token = prepared(storage, objects)
    before = state(engine)
    with pytest.raises(RuntimeError, match="fixture rollback"):
        with engine.begin() as connection:
            history.store_on_connection(connection, lease, request_key="rollback3", bundle=bundle, prepared_documents=token)
            raise RuntimeError("fixture rollback")
    assert state(engine) == before
    from datetime import timedelta
    expired = owner.FigureHistoryRepository(engine, clock=lambda: NOW + timedelta(minutes=10))
    expired_token = expired.prepare_documents(bundle, objects)
    with pytest.raises(LeaseLost):
        expired.store(lease, request_key="expired3", bundle=bundle, prepared_documents=expired_token)
    assert state(engine) == before
