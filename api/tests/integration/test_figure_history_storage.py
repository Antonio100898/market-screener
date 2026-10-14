import os
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from screener.durable_jobs import DurableJobRepository, LeaseLost
from screener.figure_history import FigureHistoryConflict, FigureHistoryRepository, ExactFigureBundle, XmlLocation
from screener.postgres import (company_snapshot, durable_job, exact_figure_bundle, figure_observation,
    figure_selection, figure_dependency, figure_bundle_coverage, figure_bundle_source, publication_candidate,
    source_item_current, source_check_occurrence, source_item, release_company)
from screener.source_observations import SourceObservationRepository, SourceItemIdentity
from screener.storage_config import StorageSettings
from tests.test_figure_history import example_bundle, derived_graph
from test_publication_storage import NOW, _claim, _company, _source, _setup, storage


pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1", reason="requires real local PostgreSQL/S3")


def bound_bundle(storage, *, value="100.0000"):
    engine, artifact = storage
    lease = _claim(engine, "history")
    company = _company(engine, artifact, lease=lease)
    source_id = _source(engine, artifact, lease)
    with engine.connect() as connection:
        snapshot_hash = connection.scalar(select(company_snapshot.c.snapshot_sha256).where(company_snapshot.c.snapshot_id == company.snapshot_id))
        pointer = connection.execute(select(source_item_current).where(source_item_current.c.source_observation_id == source_id)).mappings().one()
    bundle = example_bundle(security_id=company.security_id, snapshot_id=company.snapshot_id,
        snapshot_sha256=snapshot_hash, artifact_sha256=artifact.content_sha256, source_item_id=pointer["source_item_id"],
        source_observation_id=source_id, occurrence_id=pointer["occurrence_id"], value=value)
    return engine, lease, company, bundle


@pytest.mark.parametrize("value", ["9007199254740993.1234567890123456789", "1E-20000", "1E+200000", "-0.0000", "+0001.2300"])
def test_exact_values_replay_restart_and_immutable_storage(storage, value):
    engine, lease, _, bundle = bound_bundle(storage, value=value)
    repository = FigureHistoryRepository(engine, clock=lambda: NOW)
    identifier = repository.store(lease, request_key="exact", bundle=bundle)
    assert repository.load(identifier) == bundle
    assert repository.store(lease, request_key="exact", bundle=bundle) == identifier
    engine.dispose()
    assert FigureHistoryRepository(engine).load(identifier) == bundle
    process = subprocess.run([sys.executable, "-c", """
import sys
from sqlalchemy import create_engine
from screener.storage_config import StorageSettings
from screener.figure_history import FigureHistoryRepository
engine = create_engine(StorageSettings.from_env().database_url)
try:
    print(FigureHistoryRepository(engine).load(int(sys.argv[1])).model_dump_json())
finally:
    engine.dispose()
""", str(identifier)], capture_output=True, text=True, check=True, timeout=15)
    assert ExactFigureBundle.model_validate_json(process.stdout) == bundle
    with engine.connect() as connection:
        assert connection.scalar(select(figure_observation.c.value_decimal_text)) == value
        assert connection.scalar(select(figure_selection.c.value_decimal_text)) == value
    for statement in (figure_observation.delete(), update(figure_observation).values(value_decimal_text="0"),
        exact_figure_bundle.delete(), update(exact_figure_bundle).values(pipeline_run_key="changed"),
        figure_selection.delete(), figure_dependency.delete(), figure_bundle_coverage.delete(), figure_bundle_source.delete()):
        if statement.table is figure_dependency:
            continue  # No dependency row in the reported-only example.
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(statement)
    with pytest.raises(IntegrityError, match="sealed exact bundle"):
        with engine.begin() as connection:
            connection.execute(figure_bundle_coverage.insert().values(bundle_id=identifier, metric_family="extra", view="STRICT", record={}))


def test_replay_conflicts_and_caller_transaction_rollback(storage):
    engine, lease, _, bundle = bound_bundle(storage)
    repository = FigureHistoryRepository(engine, clock=lambda: NOW)
    identifier = repository.store(lease, request_key="one", bundle=bundle)
    with pytest.raises(FigureHistoryConflict, match="different exact bundle"):
        repository.store(lease, request_key="one", bundle=bundle.model_copy(update={"pipeline_run_key": "other"}))
    observed = bundle.observations[0].model_copy(update={"value_decimal_text": "101", "source_lexical_text": "101"})
    selected = bundle.selections[0].model_copy(update={"value_decimal_text": "101"})
    changed = ExactFigureBundle.model_validate(bundle.model_copy(update={"observations": (observed,), "selections": (selected,)}))
    with pytest.raises(FigureHistoryConflict, match="different observation"):
        repository.store(lease, request_key="two", bundle=changed)
    with pytest.raises(RuntimeError, match="caller rollback"):
        with engine.begin() as connection:
            repository.store_on_connection(connection, lease, request_key="rollback", bundle=bundle)
            raise RuntimeError("caller rollback")
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(exact_figure_bundle)) == 1
        assert connection.scalar(select(func.count()).select_from(figure_observation)) == 1
        assert connection.scalar(select(exact_figure_bundle.c.bundle_id)) == identifier


@pytest.mark.parametrize("field,value,error", [
    ("snapshot_id", 999999, "actual snapshot"), ("security_id", 999999, "actual snapshot"),
    ("snapshot_sha256", "c" * 64, "actual snapshot"), ("engine_revision", 188, "actual snapshot"),
])
def test_actual_snapshot_binding_rejects_mismatches(storage, field, value, error):
    engine, lease, _, bundle = bound_bundle(storage)
    with pytest.raises(ValueError, match=error):
        FigureHistoryRepository(engine, clock=lambda: NOW).store(lease, request_key="wrong", bundle=bundle.model_copy(update={field: value}))
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(exact_figure_bundle)) == 0


def test_artifact_security_and_accepted_occurrence_bindings(storage):
    engine, lease, _, bundle = bound_bundle(storage)
    repository = FigureHistoryRepository(engine, clock=lambda: NOW)
    observation = bundle.observations[0]
    with pytest.raises(ValueError, match="artifact/source"):
        repository.store(lease, request_key="artifact", bundle=bundle.model_copy(update={"observations": (
            observation.model_copy(update={"reference": observation.reference.model_copy(update={"artifact_sha256": "c" * 64})}),)}))
    with pytest.raises(ValueError, match="security basis"):
        repository.store(lease, request_key="security", bundle=bundle.model_copy(update={"selections": (
            bundle.selections[0].model_copy(update={"receipt_ratio_text": "2"}),)}))
    with pytest.raises(ValueError, match="occurrence"):
        repository.store(lease, request_key="witness", bundle=bundle.model_copy(update={"sources": (
            bundle.sources[0].model_copy(update={"occurrence_id": 99999}),)}))
    with pytest.raises(ValueError, match="captured issuer"):
        repository.store(lease, request_key="item", bundle=bundle.model_copy(update={"sources": (
            bundle.sources[0].model_copy(update={"source_item_id": 99999}),)}))


def test_dependency_graph_roundtrip_unselected_observation_and_catalog_reuse(storage):
    engine, lease, _, bundle = bound_bundle(storage)
    template = derived_graph()
    graph = ExactFigureBundle.model_validate(bundle.model_copy(update={"selections": template.selections,
        "dependencies": template.dependencies, "coverage": template.coverage}))
    extra = bundle.observations[0].model_copy(update={"key": "unselected", "reference": bundle.observations[0].reference.model_copy(
        update={"location": bundle.observations[0].reference.location.model_copy(update={"pointer": "/facts/us-gaap/Assets/units/USD/1"})})})
    graph = ExactFigureBundle.model_validate(graph.model_copy(update={"observations": (*graph.observations, extra)}))
    repository = FigureHistoryRepository(engine, clock=lambda: NOW)
    identifier = repository.store(lease, request_key="graph", bundle=graph)
    assert repository.load(identifier) == graph
    assert repository.store(lease, request_key="graph", bundle=graph) == identifier
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(figure_dependency.delete())


def test_a_b_a_keeps_content_identity_and_new_check_witness(storage):
    engine, lease, _, bundle = bound_bundle(storage)
    repository = FigureHistoryRepository(engine, clock=lambda: NOW)
    first = repository.store(lease, request_key="A1", bundle=bundle)
    source = SourceObservationRepository(engine, clock=lambda: NOW)
    item = SourceItemIdentity("SEC", "filing", "filing-1", "0000001800")
    source.record(lease, item=item, item_outcome_key="B", state="pending", metadata={"revision": 2},
        source_url="https://www.sec.gov/filing-1", detected_at=NOW + timedelta(seconds=1))
    source.record(lease, item=item, item_outcome_key="A3", state="present", metadata={"revision": 1},
        artifact_sha256=bundle.observations[0].reference.artifact_sha256,
        source_url="https://www.sec.gov/filing-1", detected_at=NOW + timedelta(seconds=2))
    with engine.connect() as connection:
        current = connection.execute(select(source_item_current).where(source_item_current.c.source_item_id == bundle.sources[0].source_item_id)).mappings().one()
    assert current["source_observation_id"] == bundle.sources[0].source_observation_id
    assert current["occurrence_id"] != bundle.sources[0].occurrence_id
    third_bundle = bundle.model_copy(update={"sources": (bundle.sources[0].model_copy(update={"occurrence_id": current["occurrence_id"]}),)})
    third = repository.store(lease, request_key="A3", bundle=third_bundle)
    assert first != third
    assert repository.load(first).sources != repository.load(third).sources
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(figure_observation)) == 1
        assert connection.scalar(select(func.count()).select_from(source_check_occurrence)) == 3


@pytest.mark.parametrize("lock_kind", ["job", "source", "snapshot", "observation"])
def test_real_lock_wait_expiry_rolls_back_all_exact_records(storage, lock_kind):
    engine, artifact = storage
    now = datetime.now(timezone.utc)
    jobs = DurableJobRepository(engine)
    jobs.enqueue_scheduled(schedule_key="real-history", scheduled_for=now, kind="history", parameters={}, due_at=now)
    claim = jobs.claim_next(owner="history", lease_duration=timedelta(seconds=1.7), allowed_kinds={"history"})
    assert claim is not None
    # Existing fixture writers use a fixed clock seam; supply the real claim time explicitly.
    from screener.shared_companies import SharedCompanyRepository, SnapshotArtifact
    company = SharedCompanyRepository(engine).store_candidate(claim.lease, issuer_source="SEC", issuer_identifier="1800",
        security_identifier="sec:0000001800:primary", ticker="ABT", source_accession="filing-1", security_title="Common Stock",
        security_basis="PRIMARY_COMMON_SHARE", engine_revision=187, canonical_payload={"value": 100},
        artifacts=(SnapshotArtifact(artifact.content_sha256, "raw_filing"),))
    stored = SourceObservationRepository(engine).record(claim.lease, item=SourceItemIdentity("SEC", "filing", "filing-1", "0000001800"),
        item_outcome_key="source", state="present", metadata={}, artifact_sha256=artifact.content_sha256,
        source_url="https://www.sec.gov/filing-1")
    with engine.connect() as connection:
        snapshot_hash = connection.scalar(select(company_snapshot.c.snapshot_sha256))
        pointer = connection.execute(select(source_item_current)).mappings().one()
    bundle = example_bundle(security_id=company.security_id, snapshot_id=company.snapshot_id, snapshot_sha256=snapshot_hash,
        artifact_sha256=artifact.content_sha256, source_item_id=pointer["source_item_id"],
        source_observation_id=stored.observation.source_observation_id, occurrence_id=pointer["occurrence_id"])
    repository = FigureHistoryRepository(engine)
    # For an observation conflict, an uncommitted catalog insert forces ON CONFLICT to wait.
    blocker = engine.connect()
    transaction = blocker.begin()
    if lock_kind == "job":
        blocker.execute(select(durable_job).where(durable_job.c.job_id == claim.lease.job_id).with_for_update()).all()
    elif lock_kind == "source":
        blocker.execute(select(source_item).where(source_item.c.source_item_id == pointer["source_item_id"]).with_for_update()).all()
    elif lock_kind == "snapshot":
        blocker.execute(select(company_snapshot).where(company_snapshot.c.snapshot_id == company.snapshot_id).with_for_update()).all()
    else:
        from screener.figure_history import _digest
        observed = bundle.observations[0]
        record = observed.model_dump(mode="json")
        record.pop("key")
        value = record.pop("value_decimal_text")
        blocker.execute(figure_observation.insert().values(issuer_id=company.issuer_id, artifact_sha256=artifact.content_sha256,
            extraction_revision=observed.reference.extraction_revision, location_sha256=_digest(observed.reference.location.model_dump(mode="json")),
            value_decimal_text=value, record=record))
    started = Event()
    def write():
        started.set()
        return repository.store(claim.lease, request_key="expired", bundle=bundle)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(write)
            assert started.wait(2)
            wait_deadline = monotonic() + 1
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
                    assert not future.done()
                    assert monotonic() < wait_deadline
                    sleep(.01)
            deadline = monotonic() + 4
            while datetime.now(timezone.utc) <= claim.job.lease_expires_at + timedelta(seconds=.12):
                assert not future.done()
                assert monotonic() < deadline
                sleep(.03)
            transaction.rollback()
            with pytest.raises(LeaseLost):
                future.result(timeout=4)
    finally:
        if transaction.is_active:
            transaction.rollback()
        blocker.close()
    with engine.connect() as connection:
        for table in (exact_figure_bundle, figure_observation, figure_selection, figure_bundle_source):
            assert connection.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.parametrize("starting", ["base", "20261004_0011", "20261004_0012"])
def test_empty_and_populated_0011_upgrade_preserves_legacy_and_single_head(monkeypatch, starting):
    settings = StorageSettings.from_env()
    base_url = make_url(settings.database_url)
    assert base_url.host == "127.0.0.1" and base_url.port == 55432
    name = "ms_history_upgrade_" + uuid4().hex
    assert name.startswith("ms_history_upgrade_") and name.isidentifier()
    admin = create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    url = base_url.set(database=name)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url)
    try:
        monkeypatch.setenv("SCREENER_DATABASE_URL", url.render_as_string(hide_password=False))
        config = Config("alembic.ini")
        command.upgrade(config, starting)
        preserved = None
        if starting == "20261004_0011":
            with engine.begin() as connection:
                issuer_id = connection.scalar(text("INSERT INTO issuer(source_system,source_identifier) VALUES ('SEC','1800') RETURNING issuer_id"))
                security_id = connection.scalar(text("INSERT INTO priced_security(issuer_id,security_identifier) VALUES (:issuer,'legacy') RETURNING security_id"), {"issuer": issuer_id})
                connection.execute(text("INSERT INTO company_snapshot(security_id,engine_revision,payload_sha256,snapshot_sha256,canonical_payload,ticker,security_basis) VALUES (:security,187,:hash,:hash,'{}','ABT','PRIMARY_COMMON_SHARE')"), {"security": security_id, "hash": "a" * 64})
        if starting == "20261004_0012":
            from screener.artifacts import SqlArtifactRepository, store_evidence
            from screener.object_store import ImmutableObjectStore, create_s3_client
            objects = ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket)
            artifact = store_evidence(b"forward migration exact-history fixture", "application/json", objects, SqlArtifactRepository(engine))
            _, lease, _, bundle = bound_bundle((engine, artifact), value="-0.0000")
            repository = FigureHistoryRepository(engine, clock=lambda: NOW)
            # Build the literal previous-schema fixture without calling newer runtime SQL.
            from screener.figure_history import _digest
            observed = bundle.observations[0]
            record = observed.model_dump(mode="json")
            record.pop("key")
            value = record.pop("value_decimal_text")
            with engine.begin() as connection:
                issuer_id = connection.scalar(text("SELECT issuer_id FROM priced_security WHERE security_id=:id"), {"id": bundle.security_id})
                observed_id = connection.scalar(figure_observation.insert().values(artifact_sha256=observed.reference.artifact_sha256,
                    issuer_id=issuer_id, extraction_revision=observed.reference.extraction_revision,
                    location_sha256=_digest(observed.reference.location.model_dump(mode="json")), value_decimal_text=value, record=record)
                    .returning(figure_observation.c.observation_id))
                identifier = connection.scalar(exact_figure_bundle.insert().values(request_key="before-forward-names",
                    bundle_sha256=_digest(bundle.model_dump(mode="json")), security_id=bundle.security_id, snapshot_id=bundle.snapshot_id,
                    snapshot_sha256=bundle.snapshot_sha256, engine_revision=bundle.engine_revision, pipeline_run_key=bundle.pipeline_run_key,
                    contract_revision=1, storing_job_id=lease.job_id, observation_bindings=[dict(key=observed.key,
                        artifact_sha256=observed.reference.artifact_sha256, extraction_revision=observed.reference.extraction_revision,
                        location_sha256=_digest(observed.reference.location.model_dump(mode="json")))])
                    .returning(exact_figure_bundle.c.bundle_id))
                node = bundle.selections[0]
                node_record = node.model_dump(mode="json")
                node_value = node_record.pop("value_decimal_text")
                connection.execute(figure_selection.insert().values(bundle_id=identifier, node_key=node.node_key,
                    metric_family=node.metric_family, metric_path=node.metric_path, view=node.view,
                    observation_id=observed_id, value_decimal_text=node_value, record=node_record))
                connection.execute(figure_bundle_coverage.insert().values(bundle_id=identifier,
                    metric_family=bundle.coverage[0].metric_family, view=bundle.coverage[0].view, record=bundle.coverage[0].model_dump(mode="json")))
                connection.execute(figure_bundle_source.insert().values(bundle_id=identifier, **bundle.sources[0].model_dump()))
                connection.execute(update(exact_figure_bundle).where(exact_figure_bundle.c.bundle_id == identifier).values(sealed_at=NOW))
            with engine.connect() as connection:
                previous_names = set(connection.scalars(text("SELECT conname FROM pg_constraint WHERE conrelid IN ('figure_observation'::regclass,'exact_figure_bundle'::regclass)")))
                assert {"figure_observation_pkey", "exact_figure_bundle_pkey", "exact_figure_bundle_request_key_key"} <= previous_names
                preserved = {table.name: connection.execute(select(*[c for c in table.c if c.name != "contract_details"])).mappings().all() for table in (
                    company_snapshot, exact_figure_bundle, figure_observation, figure_selection, figure_bundle_source)}
        command.upgrade(config, "head")
        command.check(config)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(config).get_current_head()
            assert connection.scalar(select(func.count()).select_from(company_snapshot)) == int(starting != "base")
            assert connection.scalar(select(func.count()).select_from(exact_figure_bundle)) == int(starting == "20261004_0012")
            names = set(connection.scalars(text("SELECT conname FROM pg_constraint WHERE conrelid IN ('figure_observation'::regclass,'exact_figure_bundle'::regclass)")))
            assert {"pk_figure_observation", "pk_exact_figure_bundle", "uq_exact_figure_bundle_request"} <= names
            if preserved is not None:
                for table in (company_snapshot, exact_figure_bundle, figure_observation, figure_selection, figure_bundle_source):
                    assert connection.execute(select(*[c for c in table.c if c.name != "contract_details"])).mappings().all() == preserved[table.name]
        if preserved is not None:
            assert repository.load(identifier) == bundle
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def test_legacy_publication_remains_unassessed_with_nullable_binding(storage):
    engine, _, lease, publication, first, original, _, candidate, _ = _setup(storage)
    assert candidate.exact_bundle_id is None
    second = publication.publish(lease, request_key="legacy-followup", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=first.release_id)
    with engine.connect() as connection:
        assert connection.scalar(select(release_company.c.exact_bundle_id).where(release_company.c.release_id == second.release_id)) is None
        assert connection.scalar(select(publication_candidate.c.exact_bundle_id)) is None


def test_concurrent_replay_creates_one_bundle_and_catalog(storage):
    engine, lease, _, bundle = bound_bundle(storage)
    other_lease = _claim(engine, "history-other")
    repository = FigureHistoryRepository(engine, clock=lambda: NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda token: repository.store(token, request_key="same", bundle=bundle), (lease, other_lease)))
    assert outcomes[0] == outcomes[1]
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(exact_figure_bundle)) == 1
        assert connection.scalar(select(func.count()).select_from(figure_observation)) == 1


def test_candidate_binding_requires_sealed_bundle_and_matching_snapshot(storage):
    engine, artifact, lease, publication, first, company, _, _, _ = _setup(storage)
    with engine.connect() as connection:
        snapshot_hash = connection.scalar(select(company_snapshot.c.snapshot_sha256).where(company_snapshot.c.snapshot_id == company.snapshot_id))
        pointer = connection.execute(select(source_item_current)).mappings().one()
    bundle = example_bundle(security_id=company.security_id, snapshot_id=company.snapshot_id,
        snapshot_sha256=snapshot_hash, artifact_sha256=artifact.content_sha256, source_item_id=pointer["source_item_id"],
        source_observation_id=pointer["source_observation_id"], occurrence_id=pointer["occurrence_id"])
    identifier = FigureHistoryRepository(engine, clock=lambda: NOW).store(lease, request_key="bound", bundle=bundle)
    values = dict(request_key="bound-candidate", security_id=company.security_id, snapshot_id=company.snapshot_id,
        exact_bundle_id=identifier, expected_parent_release_id=first.release_id, cause_type="source_change", registering_job_id=lease.job_id)
    with engine.begin() as connection:
        candidate = connection.scalar(publication_candidate.insert().values(**values).returning(publication_candidate.c.candidate_id))
        connection.execute(update(publication_candidate).where(publication_candidate.c.candidate_id == candidate).values(registered_at=NOW))
    with pytest.raises(IntegrityError, match="immutable"):
        with engine.begin() as connection:
            connection.execute(update(publication_candidate).where(publication_candidate.c.candidate_id == candidate).values(exact_bundle_id=None))
    other = _company(engine, artifact, value=200, lease=lease)
    with pytest.raises(IntegrityError, match="fk_publication_candidate_exact_bundle"):
        with engine.begin() as connection:
            connection.execute(publication_candidate.insert().values(**{**values, "request_key": "wrong-snapshot", "snapshot_id": other.snapshot_id}))
    with engine.begin() as connection:
        unsealed = connection.scalar(exact_figure_bundle.insert().values(request_key="unfinished", bundle_sha256="a" * 64,
            security_id=company.security_id, snapshot_id=company.snapshot_id, snapshot_sha256=snapshot_hash,
            engine_revision=187, pipeline_run_key="unfinished", contract_revision=1, observation_bindings=[], storing_job_id=lease.job_id).returning(exact_figure_bundle.c.bundle_id))
    with pytest.raises(IntegrityError, match="sealed exact bundle"):
        with engine.begin() as connection:
            connection.execute(publication_candidate.insert().values(**{**values, "request_key": "unfinished-candidate", "exact_bundle_id": unsealed}))


def test_one_bundle_load_batches_hundreds_of_observations_in_six_statements(storage):
    engine, lease, _, bundle = bound_bundle(storage)
    observations = tuple(bundle.observations[0].model_copy(update={"key": f"source-{i:04}",
        "reference": bundle.observations[0].reference.model_copy(update={"location":
            bundle.observations[0].reference.location.model_copy(update={"pointer": f"/facts/us-gaap/Assets/units/USD/{i}"})})}) for i in range(350))
    selection = bundle.selections[0].model_copy(update={"observation_key": observations[0].key})
    bundle = ExactFigureBundle.model_validate(bundle.model_copy(update={"observations": observations, "selections": (selection,)}))
    repository = FigureHistoryRepository(engine, clock=lambda: NOW)
    identifier = repository.store(lease, request_key="large", bundle=bundle)
    statements = []
    def count(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", count)
    try:
        assert repository.load(identifier) == bundle
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(statements) == 6
    assert sum("JOIN figure_observation" in sql for sql in statements) == 1


@pytest.mark.parametrize("identity", ["0000009999", "CIK1800"])
def test_native_sec_context_must_identify_actual_snapshot_issuer(storage, identity):
    engine, lease, _, bundle = bound_bundle(storage)
    scope = bundle.observations[0].scope.model_copy(update={"entity_identifier": identity})
    changed = bundle.model_copy(update={"observations": (bundle.observations[0].model_copy(update={"scope": scope}),),
        "selections": (bundle.selections[0].model_copy(update={"scope": scope}),)})
    with pytest.raises(ValueError, match="scope entity.*issuer"):
        FigureHistoryRepository(engine, clock=lambda: NOW).store(lease, request_key="wrong-context", bundle=changed)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(exact_figure_bundle)) == 0


def test_native_sec_cik_zero_padding_is_not_a_different_issuer(storage):
    engine, lease, _, bundle = bound_bundle(storage)
    scope = bundle.observations[0].scope.model_copy(update={"entity_identifier": "1800"})
    changed = bundle.model_copy(update={"observations": (bundle.observations[0].model_copy(update={"scope": scope}),),
        "selections": (bundle.selections[0].model_copy(update={"scope": scope}),)})
    repository = FigureHistoryRepository(engine, clock=lambda: NOW)
    identifier = repository.store(lease, request_key="unpadded-context", bundle=changed)
    assert repository.load(identifier).observations[0].scope.entity_identifier == "1800"


def test_unknown_entity_scheme_retained_without_inventing_full_issuer_mapping(storage):
    engine, lease, _, bundle = bound_bundle(storage)
    scope = bundle.observations[0].scope.model_copy(update={"entity_identifier": "retained-LEI",
        "entity_scheme": "http://standards.iso.org/iso/17442"})
    changed = bundle.model_copy(update={"observations": (bundle.observations[0].model_copy(update={"scope": scope}),),
        "selections": (bundle.selections[0].model_copy(update={"scope": scope}),)})
    repository = FigureHistoryRepository(engine, clock=lambda: NOW)
    with pytest.raises(ValueError, match="unknown scheme remains unassessed"):
        repository.store(lease, request_key="unknown-full", bundle=changed)
    partial = changed.model_copy(update={"coverage": (bundle.coverage[0].model_copy(update={"assessment": "PARTIAL",
        "reason": "entity-to-issuer correspondence unassessed"}),)})
    identifier = repository.store(lease, request_key="unknown-partial", bundle=partial)
    assert repository.load(identifier).observations[0].scope == scope


def test_native_edinet_context_preserves_filer_encoding_and_rejects_other_filer(storage):
    engine, artifact = storage
    lease = _claim(engine, "edinet-native")
    from screener.shared_companies import SharedCompanyRepository, SnapshotArtifact
    company = SharedCompanyRepository(engine, clock=lambda: NOW).store_candidate(lease, issuer_source="EDINET", issuer_identifier="E03475",
        security_identifier="edinet:E03475:primary", ticker="3028.T", source_accession="S100Z2YO", security_title="Common Stock",
        security_basis="PRIMARY_COMMON_SHARE", engine_revision=187, canonical_payload={"value": 100},
        artifacts=(SnapshotArtifact(artifact.content_sha256, "raw_filing"),))
    source = SourceObservationRepository(engine, clock=lambda: NOW).record(lease,
        item=SourceItemIdentity("EDINET", "filing", "S100Z2YO", "E03475"), item_outcome_key="source", state="present",
        metadata={}, artifact_sha256=artifact.content_sha256, source_url="https://disclosure2.edinet-fsa.go.jp/S100Z2YO")
    with engine.connect() as connection:
        snapshot_hash = connection.scalar(select(company_snapshot.c.snapshot_sha256))
        pointer = connection.execute(select(source_item_current)).mappings().one()
    template = example_bundle(security_id=company.security_id, snapshot_id=company.snapshot_id, snapshot_sha256=snapshot_hash,
        artifact_sha256=artifact.content_sha256, source_item_id=pointer["source_item_id"],
        source_observation_id=source.observation.source_observation_id, occurrence_id=pointer["occurrence_id"])
    scope = template.observations[0].scope.model_copy(update={"entity_identifier": "E03475-000",
        "entity_scheme": "http://disclosure.edinet-fsa.go.jp"})
    location = XmlLocation(member="PublicDoc/fixture.xbrl", element_ordinal=647, qname="{http://example.test/jppfs}Assets",
        context_ref="CurrentYearInstant", unit_ref="USD")
    reference = template.observations[0].reference.model_copy(update={"source_system": "EDINET", "issuer_source_identifier": "E03475",
        "source_document": "S100Z2YO", "location": location})
    bundle = template.model_copy(update={"observations": (template.observations[0].model_copy(update={"scope": scope,
        "qname": location.qname, "reference": reference}),), "selections": (template.selections[0].model_copy(update={"scope": scope}),)})
    repository = FigureHistoryRepository(engine, clock=lambda: NOW)
    identifier = repository.store(lease, request_key="native-edinet", bundle=bundle)
    assert repository.load(identifier).observations[0].scope.entity_identifier == "E03475-000"
    foreign = scope.model_copy(update={"entity_identifier": "E01991-000"})
    changed = bundle.model_copy(update={"observations": (bundle.observations[0].model_copy(update={"scope": foreign}),),
        "selections": (bundle.selections[0].model_copy(update={"scope": foreign}),)})
    with pytest.raises(ValueError, match="scope entity.*issuer"):
        repository.store(lease, request_key="foreign-edinet", bundle=changed)
