"""Isolated additive21 preservation and old runtime compatibility controls."""
import os
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError

from screener import automatic_ingestion
from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.figure_history import FigureHistoryRepository
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import exact_figure_bundle, figure_selection
from screener.storage_config import StorageSettings
from tests.test_figure_history import validate_changed
from test_figure_history_storage import bound_bundle
from test_publication_storage import NOW, storage
from test_automatic_ingestion_storage import RetainedTransport, fixture_responses, plan, pointers, wait


pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1",
                              reason="requires root-assigned isolated PostgreSQL/S3")


@pytest.fixture
def previous_schema(monkeypatch):
    base = make_url(StorageSettings.from_env().database_url)
    assert base.host == "127.0.0.1" and base.port == 55432
    name = "ms_native3_migration_" + uuid4().hex
    assert name.startswith("ms_native3_migration_") and name.isidentifier()
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name)
    engine = create_engine(url)
    monkeypatch.setenv("SCREENER_DATABASE_URL", url.render_as_string(hide_password=False))
    config = Config("alembic.ini")
    client = create_s3_client()
    try:
        command.upgrade(config, "20261004_0020")
        objects = ImmutableObjectStore(client, StorageSettings.from_env().s3_bucket)
        artifact = store_evidence(b"publication source bytes: 100", "application/json", objects, SqlArtifactRepository(engine))
        _, lease, _, bundle = bound_bundle((engine, artifact), value="-0.0000")
        repository = FigureHistoryRepository(engine, clock=lambda: NOW)
        identifiers = [repository.store(lease, request_key=f"old{revision}",
            bundle=validate_changed(bundle, contract_revision=revision)) for revision in (1, 2)]
        yield engine, config, repository, identifiers
    finally:
        client.close()
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def database_state(engine):
    with engine.connect() as connection:
        return dict(revision=connection.scalar(text("SELECT version_num FROM alembic_version")),
            bundles=connection.execute(select(exact_figure_bundle).order_by(exact_figure_bundle.c.bundle_id)).mappings().all(),
            nodes=connection.execute(select(figure_selection).order_by(figure_selection.c.bundle_id, figure_selection.c.node_key)).mappings().all(),
            constraint=connection.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_exact_bundle_identity'")))


def test_additive21_preserves_literal_old_rows_and_metadata_parity(previous_schema):
    engine, config, repository, identifiers = previous_schema
    before = database_state(engine)
    loaded = [repository.load(identifier).model_dump(mode="json") for identifier in identifiers]
    command.upgrade(config, "20261009_0021")
    command.check(config)
    after = database_state(engine)
    assert after["revision"] == "20261009_0021"
    assert after["bundles"] == before["bundles"] and after["nodes"] == before["nodes"]
    assert [repository.load(identifier).model_dump(mode="json") for identifier in identifiers] == loaded
    assert "3" in after["constraint"]
    with engine.connect() as connection:
        template = dict(connection.execute(select(exact_figure_bundle).where(exact_figure_bundle.c.bundle_id == identifiers[0])).mappings().one())
    for field in ("bundle_id", "created_at", "sealed_at"):
        template.pop(field)
    with pytest.raises(IntegrityError, match="ck_exact_bundle_identity"):
        with engine.begin() as connection:
            connection.execute(exact_figure_bundle.insert().values(**{**template, "request_key": "unsupported4", "contract_revision": 4}))
    assert database_state(engine) == after


def test_additive21_real_lock_timeout_rolls_back_then_retry(previous_schema):
    engine, config, _, _ = previous_schema
    before = database_state(engine)
    with engine.connect() as blocker:
        transaction = blocker.begin()
        blocker.execute(text("LOCK TABLE exact_figure_bundle IN ROW EXCLUSIVE MODE"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(command.upgrade, config, "20261009_0021")
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                deadline = monotonic() + 3
                while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
                    assert not future.done() and monotonic() < deadline
                    sleep(.01)
            with pytest.raises(OperationalError, match="lock timeout"):
                future.result(timeout=8)
        transaction.rollback()
    assert database_state(engine) == before
    command.upgrade(config, "20261009_0021")
    after = database_state(engine)
    assert after["revision"] == "20261009_0021" and after["bundles"] == before["bundles"] and after["nodes"] == before["nodes"]


def test_actual_head21_keeps_old_capture2_pair2_runtime_and_exact_recovery(storage, tmp_path):
    engine = storage[0]
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261009_0021"
    current, _ = plan(tmp_path, kind="sec-recent-discovery-v2", key="old-head21")
    edgar = RetainedTransport(fixture_responses())
    client = create_s3_client()
    before = pointers(engine)
    assembly = automatic_ingestion.start_automatic_ingestion(current, engine=engine, s3=client, edgar=edgar)
    try:
        rows = wait(engine, lambda values: len(values) == 3 and all(row["status"] == "succeeded" for row in values))
        resource = next(row for row in rows if row["kind"] == "sec-resource-fetch-v2")
        stage = next(row for row in rows if row["kind"] == "sec-candidate-stage")
        assert resource["checkpoint"]["capture_revision"] == 2
        assert stage["checkpoint"]["checkpoint_revision"] == 2
        graph = FigureHistoryRepository(engine).load(stage["checkpoint"]["exact_bundle_id"])
        assert graph.contract_revision == 2 and graph.snapshot_id == stage["checkpoint"]["snapshot_id"]
        sleep(1.1)
        assert len(wait(engine, lambda values: len(values) == 3)) == 3 and pointers(engine) == before
    finally:
        automatic_ingestion.stop_automatic_ingestion(assembly)
    again = automatic_ingestion.start_automatic_ingestion(current, engine=engine, s3=client, edgar=edgar)
    try:
        assert len(wait(engine, lambda values: len(values) == 3 and all(row["status"] == "succeeded" for row in values))) == 3
        assert pointers(engine) == before
    finally:
        automatic_ingestion.stop_automatic_ingestion(again)
        client.close()


def test_head21_allowlist_does_not_admit_unknown_or_default_schema_before_scheduling(storage):
    engine = storage[0]
    calls = []
    client = type("Bucket", (), {"head_bucket": lambda self, **kwargs: calls.append(kwargs)})()
    for revision in ("20260929_0006", "unsupported_native_schema"):
        with engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num=:revision"), dict(revision=revision))
        try:
            with pytest.raises(ValueError, match="supported migrated schema"):
                automatic_ingestion.preflight(engine, client, "synthetic-bucket")
            assert not calls
        finally:
            with engine.begin() as connection:
                connection.execute(text("UPDATE alembic_version SET version_num='20261009_0021'"))
