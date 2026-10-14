import multiprocessing
import os
import threading
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


def _runtime_for_start_failure(repository, handler, *, clock=lambda: NOW, source=lambda _now: (), **options):
    return DurableJobRuntime(
        repository,
        source,
        {"source-discovery": handler},
        poll_interval=timedelta(milliseconds=10),
        lease_duration=timedelta(seconds=1),
        heartbeat_interval=timedelta(milliseconds=20),
        clock=clock,
        **options,
    )


def _enqueue_start_failure(repository, key, *, max_attempts=3, now=NOW):
    return repository.enqueue_scheduled(
        schedule_key=key, scheduled_for=now, kind="source-discovery",
        parameters={}, due_at=now, max_attempts=max_attempts,
    )


def _stored_job(engine, job_id):
    with engine.connect() as connection:
        return dict(connection.execute(
            select(durable_job).where(durable_job.c.job_id == job_id)
        ).mappings().one())


@pytest.mark.parametrize("max_attempts", [1, 3])
def test_heartbeat_bootstrap_uses_durable_failure_and_same_worker_continues(engine, monkeypatch, max_attempts):
    repository = DurableJobRepository(
        engine, retry_policy=RetryPolicy(jitter_seconds=lambda cap: cap)
    )
    first = _enqueue_start_failure(repository, "heartbeat-first", max_attempts=max_attempts)
    second = _enqueue_start_failure(repository, "heartbeat-second")
    current = [NOW]
    handled = []
    failures = []
    original_fail = repository.fail
    original_start = threading.Thread.start
    injected = threading.Event()

    def fail(lease, **options):
        result = original_fail(lease, **options)
        failures.append((lease, options["now"], threading.get_ident(), result))
        return result

    def handler(context):
        handled.append((context.job.job_id, threading.get_ident()))
        return {"completed": context.job.job_id}

    def start(thread):
        if thread.name == f"durable-heartbeat-{first.job_id}" and not injected.is_set():
            injected.set()
            raise RuntimeError("injected heartbeat bootstrap failure")
        return original_start(thread)

    runtime = _runtime_for_start_failure(repository, handler, clock=lambda: current[0])
    monkeypatch.setattr(repository, "fail", fail)
    monkeypatch.setattr(threading.Thread, "start", start)
    try:
        assert runtime.start()
        _wait_for(lambda: _job_status(engine, second.job_id) == "succeeded")
        row = _stored_job(engine, first.job_id)
        assert row["status"] == ("failed" if max_attempts == 1 else "retry_wait")
        assert row["attempts"] == 1 and row["owner"] is None
        assert row["lease_expires_at"] is None
        assert row["error_summary"] == "RuntimeError: injected heartbeat bootstrap failure"
        assert len(failures) == 1
        lease, clock, worker_ident, result = failures[0]
        assert lease.job_id == first.job_id and lease.generation == 1
        assert clock is runtime._clock and result.status == row["status"]
        assert handled == [(second.job_id, worker_ident)]
        assert runtime._threads[1].ident == worker_ident and runtime._threads[1].is_alive()
        assert any("heartbeat-start" in error for error in runtime.errors)
        if max_attempts == 3:
            assert row["next_retry_at"] == NOW + timedelta(seconds=30)
            assert row["finished_at"] is None
            current[0] = NOW + timedelta(seconds=31)
            _wait_for(lambda: _job_status(engine, first.job_id) == "succeeded")
            assert _stored_job(engine, first.job_id)["attempts"] == 2
            assert handled[-1] == (first.job_id, worker_ident)
        else:
            assert row["next_retry_at"] is None and row["finished_at"] == NOW
    finally:
        assert runtime.stop(2)


@pytest.mark.parametrize("replace_owner", [False, True])
def test_heartbeat_bootstrap_cannot_fail_expired_or_replaced_attempt(engine, monkeypatch, replace_owner):
    repository = _repository(engine)
    stored = _enqueue_start_failure(repository, "heartbeat-stale")
    claimed = repository.claim_next(owner="old-worker", lease_duration=timedelta(seconds=1), now=NOW)
    current = NOW + timedelta(seconds=2)
    replacement = None
    if replace_owner:
        repository.recover_expired(now=current)
        replacement = repository.claim_next(owner="replacement", lease_duration=timedelta(seconds=5), now=current)
        repository.save_checkpoint(replacement.lease, {"replacement": True}, now=current)
    before = _stored_job(engine, stored.job_id)
    handled = []
    runtime = _runtime_for_start_failure(repository, lambda context: handled.append(context), clock=lambda: current)
    original_start = threading.Thread.start

    def start(thread):
        if thread.name == f"durable-heartbeat-{stored.job_id}":
            raise RuntimeError("injected stale heartbeat bootstrap")
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    runtime._run_claimed(claimed)
    assert handled == [] and runtime.errors
    assert _stored_job(engine, stored.job_id) == before
    if replacement is not None:
        assert replacement.lease.generation > claimed.lease.generation
        repository.complete(replacement.lease, final_checkpoint={"replacement_completed": True}, now=current)
        assert _job_status(engine, stored.job_id) == "succeeded"


@pytest.mark.parametrize("expire_while_waiting", [False, True])
def test_heartbeat_bootstrap_failure_rechecks_live_clock_after_job_row_wait(engine, monkeypatch, expire_while_waiting):
    repository = _repository(engine)
    current = datetime.now(timezone.utc)
    stored = _enqueue_start_failure(repository, "heartbeat-lock", max_attempts=1, now=current)
    claimed = repository.claim_next(
        owner="waiting-worker", now=current,
        lease_duration=timedelta(milliseconds=350) if expire_while_waiting else timedelta(seconds=5),
    )
    before = _stored_job(engine, stored.job_id)
    handled = []
    runtime = _runtime_for_start_failure(repository, lambda context: handled.append(context), clock=lambda: datetime.now(timezone.utc))
    original_start = threading.Thread.start
    unexpected = []

    def start(thread):
        if thread.name == f"durable-heartbeat-{stored.job_id}":
            raise RuntimeError("injected row-wait heartbeat bootstrap")
        return original_start(thread)

    def attempt():
        try:
            runtime._run_claimed(claimed)
        except Exception as error:
            unexpected.append(error)

    monkeypatch.setattr(threading.Thread, "start", start)
    thread = threading.Thread(target=attempt, name="bootstrap-row-wait-probe")
    blocker = engine.connect()
    transaction = blocker.begin()
    blocker.execute(select(durable_job.c.job_id).where(durable_job.c.job_id == stored.job_id).with_for_update())
    try:
        thread.start()
        def actual_lock_wait():
            with engine.connect() as connection:
                return connection.scalar(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                    "AND wait_event_type='Lock' AND query LIKE '%durable_job%'"
                )) > 0
        _wait_for(actual_lock_wait)
        if expire_while_waiting:
            _wait_for(lambda: datetime.now(timezone.utc) > claimed.job.lease_expires_at)
        transaction.commit()
    finally:
        if transaction.is_active:
            transaction.rollback()
        blocker.close()
        thread.join(2)
    assert not thread.is_alive() and unexpected == [] and handled == []
    after = _stored_job(engine, stored.job_id)
    if expire_while_waiting:
        assert after == before
    else:
        assert after["status"] == "failed" and after["attempts"] == 1
        assert after["finished_at"] >= before["updated_at"]
        assert after["error_summary"] == "RuntimeError: injected row-wait heartbeat bootstrap"


def test_partial_runtime_start_preserves_pending_rows_and_live_handler_drain(engine, monkeypatch):
    repository = _repository(engine)
    entered = threading.Event()
    release = threading.Event()
    scheduled = threading.Event()
    source_calls = []
    handled = []
    now = datetime.now(timezone.utc)
    occurrences = [ScheduledOccurrence(
        schedule_key=f"partial-start-{index}", scheduled_for=now,
        kind="source-discovery", parameters={}, due_at=now,
    ) for index in range(3)]

    def source(_now):
        source_calls.append(_now)
        scheduled.set()
        return occurrences

    def handler(context):
        handled.append(context.job.job_id)
        entered.set()
        assert release.wait(3)
        return {"drained": True}

    runtime = _runtime_for_start_failure(
        repository, handler, source=source, worker_count=2,
        clock=lambda: datetime.now(timezone.utc),
    )
    original_start = threading.Thread.start
    failure = RuntimeError("injected partial worker bootstrap")

    def start(thread):
        if thread.name.startswith("durable-worker-") and thread.name.endswith("-0"):
            assert scheduled.wait(1)
            _wait_for(lambda: _count_jobs(engine) == 3)
        if thread.name.startswith("durable-worker-") and thread.name.endswith("-1"):
            assert entered.wait(1)
            raise failure
        return original_start(thread)

    def _count_jobs(engine):
        with engine.connect() as connection:
            return connection.scalar(select(func.count()).select_from(durable_job))

    try:
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, "start", start)
            with pytest.raises(RuntimeError) as raised:
                runtime.start()
            assert raised.value is failure
        assert runtime._claim_gate_closed.is_set() and runtime._stop_requested.is_set()
        assert all(thread.ident is not None for thread in runtime._threads)
        assert runtime.running and runtime.start() is False
        assert runtime.stop(.05) is False
        row = _stored_job(engine, handled[0])
        _wait_for(lambda: _stored_job(engine, handled[0])["heartbeat_at"] > row["heartbeat_at"])
        assert _count_jobs(engine) == 3 and len(handled) == 1
        with engine.connect() as connection:
            assert sorted(connection.scalars(select(durable_job.c.status))) == ["pending", "pending", "running"]
            assert connection.scalar(select(func.count()).select_from(job_schedule_occurrence)) == 3
        calls_before_drain = len(source_calls)
        release.set()
        assert runtime.stop(2) and not runtime.running
        assert len(source_calls) == calls_before_drain
        assert runtime.start()
        _wait_for(lambda: all(_job_status(engine, index) == "succeeded" for index in (1, 2, 3)))
        assert sorted(handled) == [1, 2, 3]
        with engine.connect() as connection:
            assert list(connection.scalars(select(durable_job.c.attempts))) == [1, 1, 1]
    finally:
        release.set()
        assert runtime.stop(2)
