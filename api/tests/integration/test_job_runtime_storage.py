import multiprocessing
import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from screener.durable_jobs import DurableJobRepository, RetryPolicy
from screener.job_runtime import DurableJobRuntime, ScheduledOccurrence
from screener.postgres import create_postgres_engine, durable_job, job_schedule_occurrence
from screener.storage_config import StorageSettings


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_STORAGE_INTEGRATION") != "1",
    reason="set RUN_STORAGE_INTEGRATION=1 with local PostgreSQL running",
)

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def database_url():
    settings = StorageSettings.from_env()
    database_name = f"ms_runtime_{uuid4().hex}"
    admin_engine = create_postgres_engine(settings).execution_options(
        isolation_level="AUTOCOMMIT"
    )
    url = make_url(settings.database_url).set(database=database_name)
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    previous_url = os.environ.get("SCREENER_DATABASE_URL")
    try:
        os.environ["SCREENER_DATABASE_URL"] = url.render_as_string(
            hide_password=False
        )
        command.upgrade(Config("alembic.ini"), "head")
        yield url
    finally:
        if previous_url is None:
            os.environ.pop("SCREENER_DATABASE_URL", None)
        else:
            os.environ["SCREENER_DATABASE_URL"] = previous_url
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.fixture
def engine(database_url):
    value = create_engine(database_url)
    with value.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE durable_job_item, durable_job, "
                "job_schedule_occurrence RESTART IDENTITY CASCADE"
            )
        )
    yield value
    value.dispose()


def _repository(engine):
    return DurableJobRepository(
        engine,
        retry_policy=RetryPolicy(jitter_seconds=lambda _cap: 0),
    )


def _wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def _job_status(engine, job_id=None):
    statement = select(durable_job.c.status)
    if job_id is not None:
        statement = statement.where(durable_job.c.job_id == job_id)
    with engine.connect() as connection:
        return connection.scalar(statement)


def test_two_runtimes_enqueue_one_occurrence_and_execute_once(engine, database_url):
    occurrence = ScheduledOccurrence(
        schedule_key="sec-current",
        scheduled_for=NOW,
        kind="source-discovery",
        parameters={"source": "sec"},
        due_at=NOW,
    )
    handled = []

    def handler(context):
        handled.append(context.job.job_id)
        return {"done": True}

    second_engine = create_engine(database_url)
    runtimes = [
        DurableJobRuntime(
            repository,
            lambda _now: [occurrence],
            {"source-discovery": handler},
            poll_interval=timedelta(milliseconds=10),
            lease_duration=timedelta(seconds=1),
            heartbeat_interval=timedelta(milliseconds=100),
            clock=lambda: NOW,
        )
        for repository in (_repository(engine), _repository(second_engine))
    ]
    try:
        for runtime in runtimes:
            runtime.start()
        _wait_for(lambda: _job_status(engine) == "succeeded")
    finally:
        drained = [runtime.stop(2) for runtime in runtimes]
        second_engine.dispose()
    assert all(drained)

    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(job_schedule_occurrence)
        ) == 1
        row = connection.execute(select(durable_job)).mappings().one()
    assert row["status"] == "succeeded"
    assert row["attempts"] == 1
    assert handled == [row["job_id"]]


def _claim_checkpoint_and_wait(database_url, ready):
    engine = create_engine(database_url)
    try:
        repository = DurableJobRepository(engine)
        def handler(context):
            context.save_checkpoint({"last_document": "saved-before-death"})
            ready.set()
            time.sleep(60)

        runtime = DurableJobRuntime(
            repository,
            lambda _now: (),
            {"source-discovery": handler},
            poll_interval=timedelta(milliseconds=10),
            lease_duration=timedelta(milliseconds=200),
            heartbeat_interval=timedelta(milliseconds=50),
            clock=lambda: NOW,
        )
        runtime.start()
        time.sleep(60)
    finally:
        engine.dispose()


def test_terminated_process_recovers_checkpoint_with_higher_generation(
    engine,
    database_url,
):
    repository = _repository(engine)
    stored = repository.enqueue_scheduled(
        schedule_key="restart",
        scheduled_for=NOW,
        kind="source-discovery",
        parameters={},
        due_at=NOW,
    )
    process_context = multiprocessing.get_context("spawn")
    ready = process_context.Event()
    process = process_context.Process(
        target=_claim_checkpoint_and_wait,
        args=(database_url.render_as_string(hide_password=False), ready),
    )
    process.start()
    try:
        assert ready.wait(3)
    finally:
        if process.is_alive():
            process.terminate()
        process.join(3)
        if process.is_alive():
            process.kill()
            process.join(3)
    assert process.exitcode is not None

    with engine.connect() as connection:
        killed = connection.execute(
            select(durable_job).where(durable_job.c.job_id == stored.job_id)
        ).mappings().one()
    assert killed["status"] == "running"
    assert killed["checkpoint"] == {"last_document": "saved-before-death"}
    old_generation = killed["ownership_generation"]

    recovery_time = NOW + timedelta(seconds=1)
    seen = []

    def resume(context):
        seen.append((context.job.ownership_generation, context.job.checkpoint))
        return {"last_document": "completed-after-recovery"}

    runtime = DurableJobRuntime(
        repository,
        lambda _now: (),
        {"source-discovery": resume},
        poll_interval=timedelta(milliseconds=10),
        lease_duration=timedelta(seconds=1),
        heartbeat_interval=timedelta(milliseconds=100),
        clock=lambda: recovery_time,
    )
    try:
        runtime.start()
        _wait_for(lambda: _job_status(engine, stored.job_id) == "succeeded")
    finally:
        assert runtime.stop(2)

    with engine.connect() as connection:
        completed = connection.execute(
            select(durable_job).where(durable_job.c.job_id == stored.job_id)
        ).mappings().one()
    assert seen == [
        (old_generation + 1, {"last_document": "saved-before-death"})
    ]
    assert completed["ownership_generation"] == old_generation + 1
    assert completed["checkpoint"] == {"last_document": "completed-after-recovery"}
