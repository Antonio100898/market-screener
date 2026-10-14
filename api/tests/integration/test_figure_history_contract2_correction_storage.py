"""Forward0015 preserves rows and refuses pre-existing duplicate endpoints."""
import os
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, OperationalError, IntegrityError

from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.figure_history import FigureHistoryRepository
from screener.postgres import exact_figure_bundle, figure_selection
from screener.storage_config import StorageSettings
from test_figure_history_contract2_storage import objects
from test_figure_history_storage import bound_bundle
from test_publication_storage import NOW

pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1", reason="requires isolated local PostgreSQL/S3")


@pytest.fixture
def previous_schema(monkeypatch):
    base = make_url(StorageSettings.from_env().database_url)
    assert base.host == "127.0.0.1" and base.port == 55432
    name = "ms_contract2_correction_" + uuid4().hex
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name)
    engine = create_engine(url)
    monkeypatch.setenv("SCREENER_DATABASE_URL", url.render_as_string(hide_password=False))
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "20261004_0014")
        artifact = store_evidence(b"publication source bytes: 100", "application/json", objects(), SqlArtifactRepository(engine))
        _, lease, _, bundle = bound_bundle((engine, artifact), value="-0.0000")
        repository = FigureHistoryRepository(engine, clock=lambda: NOW)
        identifier = repository.store(lease, request_key="original-before0015", bundle=bundle)
        yield engine, config, repository, identifier, bundle
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def state(engine):
    with engine.connect() as connection:
        return {"version": connection.scalar(text("SELECT version_num FROM alembic_version")),
            "bundles": connection.execute(select(exact_figure_bundle).order_by(exact_figure_bundle.c.bundle_id)).mappings().all(),
            "nodes": connection.execute(select(figure_selection).order_by(figure_selection.c.bundle_id, figure_selection.c.node_key)).mappings().all(),
            "index": connection.scalar(text("SELECT indexdef FROM pg_indexes WHERE indexname='uq_figure_selection_output'"))}


def old_schema_duplicate(engine, original_id):
    # Insert the previously admitted shape before sealing, without disabling any immutable guards.
    with engine.begin() as connection:
        parent = dict(connection.execute(select(exact_figure_bundle).where(exact_figure_bundle.c.bundle_id == original_id)).mappings().one())
        parent.pop("bundle_id")
        parent.update(request_key="retained-duplicate-before0015", sealed_at=None)
        identifier = connection.scalar(exact_figure_bundle.insert().values(**parent).returning(exact_figure_bundle.c.bundle_id))
        node = dict(connection.execute(select(figure_selection).where(figure_selection.c.bundle_id == original_id)).mappings().one())
        node["bundle_id"] = identifier
        connection.execute(figure_selection.insert().values(**node))
        node["node_key"] = "same-actual-period-other-origin"
        node["record"] = {**node["record"], "period": {**node["record"]["period"], "origin": "INFERRED"}}
        connection.execute(figure_selection.insert().values(**node))
        connection.execute(update(exact_figure_bundle).where(exact_figure_bundle.c.bundle_id == identifier).values(sealed_at=NOW))


def test_valid_populated_upgrade_preserves_exact_rows_and_enforces_origin_independent_index(previous_schema):
    engine, config, repository, identifier, bundle = previous_schema
    before = state(engine)
    command.upgrade(config, "20261004_0015")
    after = state(engine)
    assert after["version"] == "20261004_0015"
    assert after["bundles"] == before["bundles"] and after["nodes"] == before["nodes"]
    assert repository.load(identifier) == bundle
    assert "'kind'" in after["index"] and "'start'" in after["index"] and "'end'" in after["index"]
    with pytest.raises(IntegrityError):
        old_schema_duplicate(engine, identifier)
    assert state(engine) == after


def test_duplicate_populated_upgrade_rejects_without_deletion_or_relabel(previous_schema):
    engine, config, _, identifier, _ = previous_schema
    old_schema_duplicate(engine, identifier)
    before = state(engine)
    for attempt in range(2):
        with pytest.raises(DBAPIError, match="Duplicate exact OUTPUT actual periods prevent0015"):
            command.upgrade(config, "20261004_0015")
        assert state(engine) == before
    assert len(before["nodes"]) == 3 and before["version"] == "20261004_0014"


def test_actual_0015_lock_timeout_rolls_back_then_retry_preserves_rows(previous_schema):
    engine, config, repository, identifier, bundle = previous_schema
    before = state(engine)
    with engine.connect() as blocker:
        tx = blocker.begin()
        blocker.execute(text("LOCK TABLE figure_selection IN ROW EXCLUSIVE MODE"))
        start = monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(command.upgrade, config, "20261004_0015")
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                deadline = monotonic() + 2
                while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
                    assert not future.done() and monotonic() < deadline
                    sleep(.01)
            with pytest.raises(OperationalError, match="lock timeout"):
                future.result(timeout=8)
        elapsed = monotonic() - start
        assert 4.5 < elapsed < 8
        tx.rollback()
    assert state(engine) == before
    command.upgrade(config, "20261004_0015")
    after = state(engine)
    assert after["version"] == "20261004_0015" and after["nodes"] == before["nodes"] and after["bundles"] == before["bundles"]
    assert repository.load(identifier) == bundle
    print(f"Actual0015 Lock wait {elapsed:.3f}s; all0014 rows/index/version preserved; retry/index/row checks passed.")
