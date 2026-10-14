"""Forward0016 changes only candidate expected-parent nullability."""
import os
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from uuid import uuid4
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.postgres import (company_snapshot, current_company_snapshot, data_release, publication_candidate,
    publication_candidate_source, release_company)
from screener.storage_config import StorageSettings
from test_figure_history_contract2_storage import objects
from test_publication_storage import _claim, _company, _source, NOW

pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1", reason="requires isolated PostgreSQL/S3")


@pytest.fixture
def previous_head(monkeypatch):
    base = make_url(StorageSettings.from_env().database_url)
    assert base.host == "127.0.0.1" and base.port == 55432
    name = "ms_first_migration_" + uuid4().hex
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection: connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(base.set(database=name))
    monkeypatch.setenv("SCREENER_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "20261004_0015")
        yield engine, config
    finally:
        engine.dispose()
        with admin.connect() as connection: connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def state(engine):
    with engine.connect() as connection:
        return {"version": connection.scalar(text("SELECT version_num FROM alembic_version")),
            "nullable": connection.scalar(text("SELECT is_nullable FROM information_schema.columns WHERE table_name='publication_candidate' AND column_name='expected_parent_release_id'")),
            "constraints": connection.execute(text("SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='publication_candidate'::regclass ORDER BY conname")).all(),
            "tables": {table.name: connection.execute(select(*(c for c in table.c if c.name != "figure_history_revision")).order_by(*table.primary_key)).mappings().all() for table in
                (company_snapshot, current_company_snapshot, data_release, publication_candidate, publication_candidate_source, release_company)}}


def populate(engine):
    artifact = store_evidence(b"publication source bytes: 100", "application/json", objects(), SqlArtifactRepository(engine))
    lease = _claim(engine, "publisher")
    original = _company(engine, artifact)
    # This is deliberately0015 data; use its literal columns rather than today's writer.
    with engine.begin() as connection:
        initial = connection.scalar(text("INSERT INTO data_release(request_key,publishing_job_id) VALUES('initial',:job) RETURNING release_id"), {"job": lease.job_id})
        values = dict(release=initial, security=original.security_id, snapshot=original.snapshot_id, now=NOW)
        connection.execute(text("INSERT INTO release_company(release_id,security_id,snapshot_id) VALUES(:release,:security,:snapshot)"), values)
        connection.execute(text("INSERT INTO release_company_lineage(release_id,security_id,assessment) VALUES(:release,:security,'NOT_ASSESSED')"), values)
        connection.execute(text("UPDATE data_release SET published_at=:now WHERE release_id=:release"), values)
        connection.execute(text("INSERT INTO active_data_release(singleton_id) VALUES(1) ON CONFLICT DO NOTHING"))
        connection.execute(text("SELECT release_id FROM active_data_release WHERE singleton_id=1 FOR UPDATE"))
        connection.execute(text("UPDATE active_data_release SET release_id=:release WHERE singleton_id=1"), values)
    source = _source(engine, artifact, lease)
    snapshot = _company(engine, artifact, value=90, lease=lease)
    with engine.begin() as connection:
        values = dict(security=snapshot.security_id, snapshot=snapshot.snapshot_id, parent=initial, job=lease.job_id, source=source, now=NOW)
        candidate = connection.scalar(text("INSERT INTO publication_candidate(request_key,security_id,snapshot_id,expected_parent_release_id,cause_type,registering_job_id) VALUES('candidate',:security,:snapshot,:parent,'source_change',:job) RETURNING candidate_id"), values)
        connection.execute(text("INSERT INTO publication_candidate_source(candidate_id,source_observation_id) VALUES(:candidate,:source)"), dict(candidate=candidate, source=source))
        connection.execute(text("UPDATE publication_candidate SET registered_at=:now WHERE candidate_id=:candidate"), dict(candidate=candidate, now=NOW))
    return engine, artifact, lease, None, SimpleNamespace(release_id=initial), original, snapshot, SimpleNamespace(candidate_id=candidate), source


def assert_only_parent_nullability_changed(before, after):
    # PostgreSQL18 exposes existing NOT NULL constraints in pg_constraint.
    expected = [row for row in before["constraints"] if row[1] != "NOT NULL expected_parent_release_id"]
    assert len(before["constraints"]) - len(expected) == 1
    assert after["constraints"] == expected and after["tables"] == before["tables"]


@pytest.mark.parametrize("populated", [False, True])
def test_empty_and_populated_0015_upgrade_preserves_rows_and_constraints(previous_head, populated):
    engine, config = previous_head
    if populated: populate(engine)
    before = state(engine)
    assert before["nullable"] == "NO"
    command.upgrade(config, "20261004_0016")
    after = state(engine)
    assert after["version"] == "20261004_0016" and after["nullable"] == "YES"
    assert_only_parent_nullability_changed(before, after)


def test_actual_0016_lock_timeout_preserves_populated_rows_then_retry_parity(previous_head):
    engine, config = previous_head
    populate(engine)
    before = state(engine)
    with engine.connect() as blocker:
        tx = blocker.begin()
        blocker.execute(text("SELECT count(*) FROM publication_candidate"))
        start = monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(command.upgrade, config, "20261004_0016")
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                deadline = monotonic() + 2
                while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
                    assert not future.done() and monotonic() < deadline
                    sleep(.01)
            with pytest.raises(OperationalError, match="lock timeout"): future.result(timeout=8)
        elapsed = monotonic() - start
        assert 4.5 < elapsed < 8
        tx.rollback()
    assert state(engine) == before
    command.upgrade(config, "20261004_0016")
    after = state(engine)
    assert after["version"] == "20261004_0016" and after["nullable"] == "YES"
    assert_only_parent_nullability_changed(before, after)
    print(f"Actual0016 Lock timeout {elapsed:.3f}s; populated0015 rows/FKs/nullability/version intact; retry/0016 row and constraint preservation passed; actual-head parity is checked by0018.")
