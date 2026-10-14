"""Actual snapshot/history/checkpoint transactions; financial inputs are synthetic."""
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from time import monotonic, sleep

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import DBAPIError

from screener.durable_jobs import DurableJobRepository, LeaseLost, LeaseToken
from screener.artifacts import SqlArtifactRepository
from screener.figure_history import FigureHistoryRepository
from screener.job_runtime import JobContext
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import durable_job
from screener.shared_companies import SharedCompanyRepository
from screener.storage_config import StorageSettings
from test_shared_candidate_transaction_storage import prepare, bundle_for, counts
from test_publication_storage import NOW, storage

pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1", reason="requires isolated PostgreSQL/S3")


def checkpoint(engine, lease):
    with engine.connect() as connection:
        return connection.scalar(select(durable_job.c.checkpoint).where(durable_job.c.job_id == lease.job_id))


def pair(connection, engine, artifact, source, values, lease, *, clock=lambda: NOW):
    stored = SharedCompanyRepository(engine, clock=clock).store_candidate_on_connection(connection, lease, **values)
    bundle = bundle_for(stored, artifact, source)
    identifier = FigureHistoryRepository(engine, clock=clock).store_on_connection(
        connection, lease, request_key="checkpoint-pair", bundle=bundle)
    recovery = dict(fixture="synthetic-storage-proof", snapshot_id=stored.snapshot_id,
        snapshot_sha256=stored.snapshot_sha256, bundle_id=identifier, security_id=stored.security_id,
        engine_revision=stored.engine_revision, artifact_sha256=artifact.content_sha256,
        source_observation_id=source["source_observation_id"], pipeline_run_key=bundle.pipeline_run_key)
    return stored, identifier, bundle, recovery


def test_commit_invisibility_pair_retry_old_wrapper_and_fresh_process(storage):
    engine, artifact, lease, source, values = prepare(storage)
    jobs = DurableJobRepository(engine)
    old = jobs.save_checkpoint(lease, {"prior": True}, now=NOW)
    with engine.begin() as connection:
        stored, identifier, bundle, recovery = pair(connection, engine, artifact, source, values, lease)
        provisional = jobs.save_checkpoint_on_connection(connection, lease, recovery, now=NOW)
        assert provisional.checkpoint == recovery and provisional.job_id == old.job_id
        assert connection.in_transaction()
        assert checkpoint(engine, lease) == {"prior": True}
        assert all(value == 0 for value in counts(engine).values())
    assert checkpoint(engine, lease) == recovery
    with engine.begin() as connection:
        repeated, repeated_id, same_bundle, same_recovery = pair(connection, engine, artifact, source, values, lease)
        assert repeated.snapshot_id == stored.snapshot_id and repeated.created is False
        assert repeated_id == identifier and same_bundle == bundle and same_recovery == recovery
        assert jobs.save_checkpoint_on_connection(connection, lease, same_recovery, now=NOW).checkpoint == recovery
    assert jobs.save_checkpoint(lease, recovery, now=NOW).checkpoint == recovery
    engine.dispose()
    fresh = create_engine(engine.url)
    try:
        assert checkpoint(fresh, lease) == recovery
        assert FigureHistoryRepository(fresh).load(identifier) == bundle
        assert SharedCompanyRepository(fresh).snapshot(stored.snapshot_id).snapshot.snapshot_sha256 == stored.snapshot_sha256
        registered = SqlArtifactRepository(fresh).get(artifact.content_sha256)
        settings = StorageSettings.from_env()
        objects = ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket)
        assert objects.read_verified(registered.object_key, registered.content_sha256, registered.byte_size) == b'publication source bytes: 100'
        command.current(Config("alembic.ini"))
        command.heads(Config("alembic.ini"))
        command.check(Config("alembic.ini"))
    finally:
        fresh.dispose()
    result = subprocess.run([sys.executable, "-c", """
import json,sys
from sqlalchemy import create_engine,select
from screener.storage_config import StorageSettings
from screener.postgres import durable_job
from screener.shared_companies import SharedCompanyRepository
from screener.figure_history import FigureHistoryRepository
engine=create_engine(StorageSettings.from_env().database_url)
try:
    with engine.connect() as c:
        recovery=c.scalar(select(durable_job.c.checkpoint).where(durable_job.c.job_id==int(sys.argv[1])))
    snapshot=SharedCompanyRepository(engine).snapshot(recovery['snapshot_id'])
    bundle=FigureHistoryRepository(engine).load(recovery['bundle_id'])
    print(json.dumps(dict(checkpoint=recovery,snapshot_sha256=snapshot.snapshot.snapshot_sha256,bundle=bundle.model_dump(mode='json'))))
finally:engine.dispose()
""", str(lease.job_id)], capture_output=True, text=True, check=True, timeout=15)
    assert json.loads(result.stdout) == dict(checkpoint=recovery, snapshot_sha256=stored.snapshot_sha256,
                                           bundle=bundle.model_dump(mode="json"))
    assert counts(engine)["company_snapshot"] == counts(engine)["exact_figure_bundle"] == 1
    assert counts(engine)["current_company_snapshot"] == 0


@pytest.mark.parametrize("rollback", ["outer", "savepoint"])
def test_complete_pair_checkpoint_rollback_and_context_stays_immutable(storage, rollback):
    engine, artifact, lease, source, values = prepare(storage)
    jobs = DurableJobRepository(engine)
    initial = jobs.save_checkpoint(lease, {"prior": True}, now=NOW)
    context = JobContext(initial, threading.Event(), jobs, lease, lambda: NOW)
    def write(connection):
        stored, identifier, _, recovery = pair(connection, engine, artifact, source, values, lease)
        provisional = context.save_checkpoint_on_connection(connection, recovery)
        assert provisional.checkpoint == recovery and context.job.checkpoint == {"prior": True}
        assert connection.scalar(select(durable_job.c.checkpoint).where(durable_job.c.job_id == lease.job_id)) == recovery
        raise RuntimeError("after all three writes")
    if rollback == "outer":
        with pytest.raises(RuntimeError, match="all three"):
            with engine.begin() as connection:
                write(connection)
    else:
        with engine.begin() as connection:
            with pytest.raises(RuntimeError, match="all three"):
                with connection.begin_nested():
                    write(connection)
            assert connection.in_transaction()
    assert context.job is initial and context.job.checkpoint == {"prior": True}
    assert checkpoint(engine, lease) == {"prior": True}
    assert all(value == 0 for value in counts(engine).values())
    with engine.begin() as connection:
        stored, identifier, bundle, recovery = pair(connection, engine, artifact, source, values, lease)
        jobs.save_checkpoint_on_connection(connection, lease, recovery, now=NOW)
    assert stored.created and checkpoint(engine, lease) == recovery
    assert FigureHistoryRepository(engine).load(identifier) == bundle


@pytest.mark.parametrize("fault", ["json", "graph", "checkpoint_sql", "generation", "final_fence"])
def test_late_failure_rolls_back_all_three(storage, fault):
    engine, artifact, lease, source, values = prepare(storage)
    jobs = DurableJobRepository(engine)
    jobs.save_checkpoint(lease, {"prior": True}, now=NOW)
    expired = False
    def clock():
        return NOW + timedelta(minutes=6) if expired else NOW
    def after_update(connection, cursor, statement, parameters, context, executemany):
        nonlocal expired
        if statement.startswith("UPDATE durable_job SET checkpoint"):
            expired = True
    def fail_checkpoint_sql(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE durable_job SET checkpoint"):
            connection.exec_driver_sql("SELECT 1/0")
    with pytest.raises((ValueError, LeaseLost, DBAPIError)):
        with engine.begin() as connection:
            if fault == "graph":
                stored = SharedCompanyRepository(engine, clock=clock).store_candidate_on_connection(connection, lease, **values)
                bundle = bundle_for(stored, artifact, source)
                malformed = bundle.model_copy(update={"snapshot_sha256": "c" * 64})
                FigureHistoryRepository(engine, clock=clock).store_on_connection(connection, lease, request_key="checkpoint-pair", bundle=malformed)
            else:
                _, _, _, recovery = pair(connection, engine, artifact, source, values, lease, clock=clock)
                if fault == "json":
                    recovery["bad"] = float("nan")
                if fault == "final_fence":
                    event.listen(connection, "after_cursor_execute", after_update)
                if fault == "checkpoint_sql":
                    event.listen(connection, "before_cursor_execute", fail_checkpoint_sql)
                token = LeaseToken(lease.job_id, lease.owner, lease.generation + 1) if fault == "generation" else lease
                jobs.save_checkpoint_on_connection(connection, token, recovery, now=clock)
    assert checkpoint(engine, lease) == {"prior": True}
    assert all(value == 0 for value in counts(engine).values())
    if fault == "final_fence":
        assert expired, "must reach actual checkpoint UPDATE before final lease rejection"


@pytest.mark.parametrize("configuration", ["connection", "engine"])
def test_real_autocommit_rejected_before_checkpoint_write(storage, configuration):
    engine, _, lease, _, _ = prepare(storage)
    DurableJobRepository(engine).save_checkpoint(lease, {"prior": True}, now=NOW)
    owner_engine = create_engine(engine.url, isolation_level="AUTOCOMMIT") if configuration == "engine" else engine
    try:
        with owner_engine.connect() as connection:
            if configuration == "connection":
                connection = connection.execution_options(isolation_level="AUTOCOMMIT")
            with connection.begin():
                assert connection.in_transaction() and connection.get_transaction().is_active
                assert connection.connection.dbapi_connection.autocommit is True
                with pytest.raises(ValueError, match="autocommit"):
                    DurableJobRepository(owner_engine).save_checkpoint_on_connection(connection, lease, {"bad": True}, now=NOW)
        assert checkpoint(engine, lease) == {"prior": True}
        assert all(value == 0 for value in counts(engine).values())
    finally:
        if owner_engine is not engine:
            owner_engine.dispose()


@pytest.mark.parametrize("kind", ["foreign", "inactive", "closed", "committed", "object"])
def test_invalid_connection_cannot_change_checkpoint(storage, kind):
    engine, _, lease, _, _ = prepare(storage)
    jobs = DurableJobRepository(engine)
    jobs.save_checkpoint(lease, {"prior": True}, now=NOW)
    foreign = create_engine(engine.url)
    connection = foreign.connect() if kind == "foreign" else engine.connect()
    try:
        if kind in {"foreign", "committed"}:
            transaction = connection.begin()
            if kind == "committed":
                transaction.commit()
        if kind == "closed":
            connection.close()
        with pytest.raises(ValueError, match="live transaction"):
            jobs.save_checkpoint_on_connection(object() if kind == "object" else connection, lease, {"bad": True}, now=NOW)
        assert checkpoint(engine, lease) == {"prior": True}
    finally:
        connection.close()
        foreign.dispose()


def live_claim(engine, *, expired):
    jobs = DurableJobRepository(engine)
    now = datetime.now(timezone.utc)
    jobs.enqueue_scheduled(schedule_key="live-checkpoint", scheduled_for=now, kind="live-checkpoint", parameters={}, due_at=now)
    claimed = jobs.claim_next(owner="checkpoint-worker", lease_duration=timedelta(seconds=1.3 if expired else 10), allowed_kinds={"live-checkpoint"})
    assert claimed is not None
    jobs.save_checkpoint(claimed.lease, {"prior": True})
    return jobs, claimed


@pytest.mark.parametrize("position,expired", [("before", True), ("after", True), ("before", False), ("after", False)])
def test_actual_row_wait_and_post_update_wait_fence_complete_pair(storage, position, expired):
    engine, artifact, _, source, values = prepare(storage)
    jobs, claimed = live_claim(engine, expired=expired)
    blocker = engine.connect()
    transaction = blocker.begin()
    guard = claimed.lease.job_id
    if position == "after":
        now = datetime.now(timezone.utc)
        guard = jobs.enqueue_scheduled(schedule_key="checkpoint-guard", scheduled_for=now,
            kind="checkpoint-guard", parameters={}, due_at=now).job_id
    blocker.execute(select(durable_job).where(durable_job.c.job_id == guard).with_for_update()).all()
    entered, pids = threading.Event(), []
    def run():
        with engine.begin() as connection:
            pids.append(connection.scalar(text("SELECT pg_backend_pid()")))
            entered.set()
            if position == "before":
                jobs.save_checkpoint_on_connection(connection, claimed.lease, {"preparing": True})
            stored, identifier, bundle, recovery = pair(connection, engine, artifact, source, values,
                claimed.lease, clock=lambda: datetime.now(timezone.utc))
            if position == "after":
                def wait_after_update(connection, cursor, statement, parameters, context, executemany):
                    if statement.startswith("UPDATE durable_job SET checkpoint"):
                        # Test fault injection: a real row wait after the actual UPDATE.
                        connection.execute(select(durable_job).where(durable_job.c.job_id == guard).with_for_update()).all()
                event.listen(connection, "after_cursor_execute", wait_after_update)
            result = jobs.save_checkpoint_on_connection(connection, claimed.lease, recovery)
            return stored, identifier, bundle, recovery, result
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run)
            assert entered.wait(3)
            waited = False
            until = monotonic() + 3
            while monotonic() < until:
                with engine.connect() as observer:
                    waited = observer.scalar(text("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"), {"pid": pids[0]})
                if waited:
                    break
                sleep(.01)
            assert waited, "must observe an actual PostgreSQL row Lock wait"
            with engine.connect() as observer:
                expires = observer.scalar(select(durable_job.c.lease_expires_at).where(durable_job.c.job_id == claimed.lease.job_id))
            sleep(max(0, (expires - datetime.now(timezone.utc)).total_seconds()) + .05 if expired else .05)
            released = datetime.now(timezone.utc)
            transaction.rollback()
            if expired:
                with pytest.raises(LeaseLost):
                    future.result(timeout=5)
            else:
                stored, identifier, bundle, recovery, result = future.result(timeout=5)
                assert checkpoint(engine, claimed.lease) == recovery
                assert FigureHistoryRepository(engine).load(identifier) == bundle
                if position == "before":
                    assert result.updated_at >= released
    finally:
        if transaction.is_active:
            transaction.rollback()
        blocker.close()
    if expired:
        assert checkpoint(engine, claimed.lease) == {"prior": True}
        assert all(value == 0 for value in counts(engine).values())
    else:
        assert counts(engine)["company_snapshot"] == counts(engine)["exact_figure_bundle"] == 1
        assert counts(engine)["current_company_snapshot"] == 0
