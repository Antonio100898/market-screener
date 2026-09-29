import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from screener.durable_jobs import (
    DurableJobRepository,
    JobIdentityConflict,
    LeaseLost,
    RetryPolicy,
)
from screener.postgres import (
    create_postgres_engine,
    durable_job,
    durable_job_item,
    job_schedule_occurrence,
)
from screener.storage_config import StorageSettings


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_STORAGE_INTEGRATION") != "1",
    reason="set RUN_STORAGE_INTEGRATION=1 with local PostgreSQL running",
)

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)


@pytest.fixture(scope="module")
def database_url():
    settings = StorageSettings.from_env()
    database_name = f"ms_jobs_{uuid4().hex}"
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
def repository(database_url):
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE durable_job_item, durable_job, "
                "job_schedule_occurrence RESTART IDENTITY CASCADE"
            )
        )
    yield DurableJobRepository(
        engine,
        retry_policy=RetryPolicy(jitter_seconds=lambda cap: cap),
    )
    engine.dispose()


def _enqueue(repository, key, **overrides):
    values = {
        "schedule_key": key,
        "scheduled_for": NOW,
        "kind": "source-discovery",
        "parameters": {"source": key},
        "due_at": NOW,
        "priority": 10,
        "max_attempts": 3,
        "deadline_at": NOW + timedelta(days=1),
    }
    values.update(overrides)
    return repository.enqueue_scheduled(**values)


def _claim(repository, owner="worker-1", now=NOW):
    claimed = repository.claim_next(
        owner=owner,
        lease_duration=LEASE,
        now=now,
    )
    assert claimed is not None
    return claimed


def test_concurrent_duplicate_occurrence_returns_one_identical_job(repository):
    barrier = Barrier(2)

    def enqueue():
        barrier.wait()
        return _enqueue(repository, "sec-current")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _index: enqueue(), range(2)))

    assert first.job_id == second.job_id
    with repository.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(job_schedule_occurrence)
        ) == 1
        assert connection.scalar(select(func.count()).select_from(durable_job)) == 1

    with pytest.raises(JobIdentityConflict, match="different job content"):
        _enqueue(repository, "sec-current", parameters={"source": "changed"})


def test_claim_prefers_priority_then_due_time_then_stable_identity(repository):
    first = _enqueue(repository, "first", priority=10)
    _enqueue(repository, "low", priority=1, due_at=NOW - timedelta(hours=1))
    _enqueue(repository, "second", priority=10)

    claimed_first = _claim(repository)
    claimed_second = _claim(repository, owner="worker-2")
    claimed_low = _claim(repository, owner="worker-3")

    assert claimed_first.job.job_id == first.job_id
    assert claimed_second.job.priority == 10
    assert claimed_second.job.job_id > claimed_first.job.job_id
    assert claimed_low.job.priority == 1
    assert claimed_first.job.attempts == 1
    assert claimed_first.job.ownership_generation == 1


def test_live_parent_lease_guards_stable_child_enqueue(repository):
    _enqueue(repository, "parent", kind="sec-recent-discovery", priority=30)
    parent = _claim(repository)
    values = {
        "child_key": "sec-resource-fetch:0000001234:accession:observation",
        "scheduled_for": NOW - timedelta(days=1),
        "kind": "sec-resource-fetch",
        "parameters": {
            "accession": "0000001234-26-000001",
            "observation_id": 7,
        },
        "due_at": NOW - timedelta(days=1),
        "priority": parent.job.priority,
        "now": NOW,
    }

    first = repository.enqueue_child(parent.lease, **values)
    second = repository.enqueue_child(parent.lease, **values)

    assert first.job_id == second.job_id
    assert first.priority == parent.job.priority
    with pytest.raises(LeaseLost):
        repository.enqueue_child(
            parent.lease,
            **{
                **values,
                "child_key": "sec-resource-fetch:late",
                "now": NOW + LEASE,
            },
        )
    with repository.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(durable_job)
        ) == 2


def test_two_workers_cannot_claim_the_same_job(repository):
    stored = _enqueue(repository, "single-claim")
    barrier = Barrier(2)

    def claim(owner):
        barrier.wait()
        return repository.claim_next(
            owner=owner,
            lease_duration=LEASE,
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("worker-1", "worker-2")))

    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1
    assert claimed[0].job.job_id == stored.job_id
    assert claimed[0].lease.owner in {"worker-1", "worker-2"}


def test_claim_filter_leaves_other_job_kinds_queued(repository):
    _enqueue(repository, "unknown", kind="unknown", priority=100)
    expected = _enqueue(repository, "known", kind="known", priority=1)

    claimed = repository.claim_next(
        owner="specialized-worker",
        lease_duration=LEASE,
        now=NOW,
        allowed_kinds={"known"},
    )

    assert claimed is not None
    assert claimed.job.job_id == expected.job_id
    assert claimed.job.kind == "known"
    with repository.engine.connect() as connection:
        unknown_status = connection.scalar(
            select(durable_job.c.status).where(durable_job.c.kind == "unknown")
        )
    assert unknown_status == "pending"


def test_database_rejects_a_running_job_without_active_ownership(repository):
    stored = _enqueue(repository, "invalid-state")

    with pytest.raises(IntegrityError):
        with repository.engine.begin() as connection:
            connection.execute(
                durable_job.update()
                .where(durable_job.c.job_id == stored.job_id)
                .values(status="running")
            )


def test_renewal_and_atomic_final_checkpoint(repository):
    _enqueue(repository, "edinet-current")
    claimed = _claim(repository)

    renewed = repository.renew(
        claimed.lease,
        lease_duration=timedelta(minutes=10),
        now=NOW + timedelta(minutes=1),
    )
    completed = repository.complete(
        claimed.lease,
        final_checkpoint={"last_document": "S100TEST"},
        now=NOW + timedelta(minutes=2),
    )

    assert renewed.lease_expires_at == NOW + timedelta(minutes=11)
    assert renewed.heartbeat_at == NOW + timedelta(minutes=1)
    assert completed.status == "succeeded"
    assert completed.checkpoint == {"last_document": "S100TEST"}
    assert completed.owner is None
    assert completed.lease_expires_at is None
    assert completed.finished_at == NOW + timedelta(minutes=2)
    with pytest.raises(LeaseLost):
        repository.save_checkpoint(
            claimed.lease,
            {"late": True},
            now=NOW + timedelta(minutes=3),
        )


def test_retry_wait_and_maximum_attempts_make_terminal_failure_visible(repository):
    _enqueue(repository, "retry", max_attempts=2)
    first = _claim(repository)

    retry = repository.fail(
        first.lease,
        error_summary="temporary source outage",
        final_checkpoint={"page": 2},
        now=NOW + timedelta(minutes=1),
    )
    assert retry.status == "retry_wait"
    assert retry.attempts == 1
    assert retry.next_retry_at == NOW + timedelta(minutes=1, seconds=30)
    assert retry.checkpoint == {"page": 2}
    assert repository.claim_next(
        owner="too-early",
        lease_duration=LEASE,
        now=NOW + timedelta(minutes=1, seconds=29),
    ) is None

    second = _claim(
        repository,
        owner="worker-2",
        now=NOW + timedelta(minutes=1, seconds=30),
    )
    terminal = repository.fail(
        second.lease,
        error_summary="source remains unavailable",
        now=NOW + timedelta(minutes=2),
    )

    assert terminal.status == "failed"
    assert terminal.attempts == 2
    assert terminal.next_retry_at is None
    assert terminal.finished_at == NOW + timedelta(minutes=2)
    assert terminal.error_summary == "source remains unavailable"


def test_deadline_prevents_a_retry_beyond_it(repository):
    _enqueue(
        repository,
        "deadline",
        max_attempts=5,
        deadline_at=NOW + timedelta(seconds=20),
    )
    claimed = _claim(repository)

    terminal = repository.fail(
        claimed.lease,
        error_summary="retry would miss deadline",
        now=NOW + timedelta(seconds=1),
    )

    assert terminal.status == "failed"
    assert terminal.attempts == 1
    assert terminal.next_retry_at is None


def test_deferred_source_delivery_does_not_spend_error_attempts(repository):
    stored = _enqueue(repository, "pending-source", max_attempts=2)
    current = NOW
    delays = []

    for index in range(9):
        claimed = _claim(repository, owner=f"pending-{index}", now=current)
        assert claimed.job.job_id == stored.job_id
        deferred = repository.defer(
            claimed.lease,
            reason="source has not disseminated the resource",
            now=current,
        )
        assert deferred.status == "retry_wait"
        assert deferred.attempts == 0
        delays.append(deferred.next_retry_at - current)
        current = deferred.next_retry_at

    assert delays == [
        timedelta(seconds=30),
        timedelta(minutes=1),
        timedelta(minutes=2),
        timedelta(minutes=4),
        timedelta(minutes=8),
        timedelta(minutes=16),
        timedelta(minutes=32),
        timedelta(hours=1),
        timedelta(hours=1),
    ]

    repeated = _enqueue(repository, "pending-source", max_attempts=2)
    assert repeated.job_id == stored.job_id
    assert repeated.status == "retry_wait"
    final_claim = _claim(repository, owner="available", now=current)
    completed = repository.complete(final_claim.lease, now=current)
    assert completed.status == "succeeded"
    assert completed.attempts == 1


def test_deferred_source_delivery_still_obeys_deadline(repository):
    _enqueue(
        repository,
        "pending-deadline",
        deadline_at=NOW + timedelta(seconds=20),
    )
    claimed = _claim(repository)

    terminal = repository.defer(
        claimed.lease,
        reason="source is pending",
        now=NOW + timedelta(seconds=1),
    )

    assert terminal.status == "failed"
    assert terminal.attempts == 1
    assert terminal.next_retry_at is None
    assert terminal.error_summary.startswith(
        "job deadline stopped pending retry:"
    )


def test_deferred_backoff_progression_survives_repository_restart(
    repository,
    database_url,
):
    _enqueue(repository, "pending-restart")
    first = _claim(repository)
    initial = repository.defer(
        first.lease,
        reason="source is pending",
        now=NOW,
    )
    assert initial.next_retry_at == NOW + timedelta(seconds=30)

    restarted_engine = create_engine(database_url)
    restarted = DurableJobRepository(
        restarted_engine,
        retry_policy=RetryPolicy(jitter_seconds=lambda cap: cap),
    )
    try:
        second = _claim(
            restarted,
            owner="restarted",
            now=initial.next_retry_at,
        )
        assert second.job.ownership_generation == 2
        resumed = restarted.defer(
            second.lease,
            reason="source remains pending",
            now=initial.next_retry_at,
        )
        assert resumed.next_retry_at == initial.next_retry_at + timedelta(minutes=1)
        assert resumed.attempts == 0
    finally:
        restarted_engine.dispose()


def test_item_outcomes_can_arrive_out_of_order_and_retry_in_place(repository):
    _enqueue(repository, "items")
    claimed = _claim(repository)

    repository.record_item_outcome(
        claimed.lease,
        item_key="document-b",
        status="failed",
        error_summary="malformed document",
        now=NOW + timedelta(seconds=2),
    )
    repository.record_item_outcome(
        claimed.lease,
        item_key="document-a",
        status="succeeded",
        outcome={"artifact": "a" * 64},
        now=NOW + timedelta(seconds=3),
    )
    retried = repository.record_item_outcome(
        claimed.lease,
        item_key="document-b",
        status="succeeded",
        outcome={"artifact": "b" * 64},
        now=NOW + timedelta(seconds=4),
    )
    repeated = repository.record_item_outcome(
        claimed.lease,
        item_key="document-b",
        status="succeeded",
        outcome={"artifact": "b" * 64},
        now=NOW + timedelta(seconds=5),
    )

    with repository.engine.connect() as connection:
        rows = connection.execute(
            select(durable_job_item).order_by(durable_job_item.c.item_key)
        ).mappings().all()
    assert [row["item_key"] for row in rows] == ["document-a", "document-b"]
    assert [row["status"] for row in rows] == ["succeeded", "succeeded"]
    assert [row["attempts"] for row in rows] == [1, 2]
    assert retried.attempts == repeated.attempts == 2


def test_restart_recovery_increases_generation_and_rejects_old_owner_mutations(
    repository,
    database_url,
):
    _enqueue(repository, "restart")
    old = _claim(repository)

    restarted_engine = create_engine(database_url)
    restarted = DurableJobRepository(
        restarted_engine,
        retry_policy=RetryPolicy(jitter_seconds=lambda cap: cap),
    )
    try:
        recovery_time = NOW + LEASE
        recovered = restarted.recover_expired(now=recovery_time)
        assert len(recovered) == 1
        assert recovered[0].status == "retry_wait"
        assert recovered[0].owner is None
        assert recovered[0].next_retry_at == recovery_time + timedelta(seconds=30)

        _assert_all_mutations_reject(restarted, old.lease, recovery_time)

        replacement = _claim(
            restarted,
            owner="replacement",
            now=recovered[0].next_retry_at,
        )
        assert replacement.job.ownership_generation > old.lease.generation
        _assert_all_mutations_reject(
            restarted,
            old.lease,
            recovered[0].next_retry_at,
        )
        completed = restarted.complete(
            replacement.lease,
            final_checkpoint={"resumed": True},
            now=recovered[0].next_retry_at,
        )
        assert completed.status == "succeeded"
        assert completed.checkpoint == {"resumed": True}
    finally:
        restarted_engine.dispose()


def _assert_all_mutations_reject(repository, lease, now):
    operations = (
        lambda: repository.renew(lease, lease_duration=LEASE, now=now),
        lambda: repository.save_checkpoint(lease, {"stale": True}, now=now),
        lambda: repository.record_item_outcome(
            lease,
            item_key="stale-item",
            status="succeeded",
            now=now,
        ),
        lambda: repository.complete(lease, now=now),
        lambda: repository.fail(lease, error_summary="stale failure", now=now),
        lambda: repository.defer(lease, reason="stale pending", now=now),
    )
    for operation in operations:
        with pytest.raises(LeaseLost):
            operation()
