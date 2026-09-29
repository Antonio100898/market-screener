import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from screener.durable_jobs import (
    ClaimedJob,
    DurableJobItemRecord,
    DurableJobRecord,
    JobDeferred,
    LeaseLost,
    LeaseToken,
)
from screener.job_runtime import DurableJobRuntime, ScheduledOccurrence


NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)


def _record(job_id, kind, priority=0):
    return DurableJobRecord(
        job_id=job_id,
        occurrence_id=job_id,
        kind=kind,
        parameters={},
        status="pending",
        priority=priority,
        due_at=NOW,
        attempts=0,
        max_attempts=3,
        next_retry_at=None,
        deadline_at=None,
        owner=None,
        lease_expires_at=None,
        ownership_generation=0,
        heartbeat_at=None,
        checkpoint=None,
        error_summary=None,
        created_at=NOW,
        updated_at=NOW,
        finished_at=None,
    )


class FakeRepository:
    def __init__(self):
        self.lock = threading.Lock()
        self.queue = []
        self.occurrences = {}
        self.active = {}
        self.claim_calls = []
        self.completed = []
        self.failed = []
        self.deferred = []
        self.renewals = 0
        self.recoveries = 0
        self.checkpoints = []
        self.items = []
        self.lose_on_renew = False

    def seed(self, kind, priority=0):
        with self.lock:
            job = _record(len(self.occurrences) + len(self.queue) + 1, kind, priority)
            self.queue.append(job)
            return job

    def enqueue_scheduled(self, **values):
        identity = (values["schedule_key"], values["scheduled_for"])
        with self.lock:
            if identity not in self.occurrences:
                job = _record(
                    len(self.occurrences) + len(self.queue) + 1,
                    values["kind"],
                    values["priority"],
                )
                self.occurrences[identity] = job
                self.queue.append(job)
            return self.occurrences[identity]

    def recover_expired(self, *, now):
        with self.lock:
            self.recoveries += 1
        return ()

    def claim_next(self, *, owner, lease_duration, now, allowed_kinds=None):
        with self.lock:
            self.claim_calls.append(frozenset(allowed_kinds or ()))
            candidates = [job for job in self.queue if job.kind in allowed_kinds]
            if not candidates:
                return None
            job = min(candidates, key=lambda value: (-value.priority, value.job_id))
            self.queue.remove(job)
            running = replace(
                job,
                status="running",
                attempts=job.attempts + 1,
                owner=owner,
                ownership_generation=job.ownership_generation + 1,
                lease_expires_at=now + lease_duration,
            )
            lease = LeaseToken(running.job_id, owner, running.ownership_generation)
            self.active[running.job_id] = lease
            return ClaimedJob(running, lease)

    def renew(self, lease, *, lease_duration, now):
        with self.lock:
            self._check(lease)
            self.renewals += 1
            if self.lose_on_renew:
                self.active.pop(lease.job_id)
                raise LeaseLost("replaced")

    def save_checkpoint(self, lease, checkpoint, *, now):
        with self.lock:
            self._check(lease)
            self.checkpoints.append(dict(checkpoint))
        return _record(lease.job_id, "known")

    def record_item_outcome(self, lease, **values):
        with self.lock:
            self._check(lease)
            self.items.append(values)
        return DurableJobItemRecord(
            job_id=lease.job_id,
            item_key=values["item_key"],
            status=values["status"],
            attempts=1,
            outcome=values.get("outcome"),
            error_summary=values.get("error_summary"),
            updated_at=values["now"],
            finished_at=values["now"],
        )

    def complete(self, lease, *, final_checkpoint, now):
        with self.lock:
            self._check(lease)
            self.active.pop(lease.job_id)
            self.completed.append((lease.job_id, final_checkpoint))
        return replace(_record(lease.job_id, "known"), status="succeeded")

    def fail(self, lease, *, error_summary, now):
        with self.lock:
            self._check(lease)
            self.active.pop(lease.job_id)
            self.failed.append((lease.job_id, error_summary))
        return replace(_record(lease.job_id, "known"), status="failed")

    def defer(self, lease, *, reason, now):
        with self.lock:
            self._check(lease)
            self.active.pop(lease.job_id)
            self.deferred.append((lease.job_id, reason))
        return replace(_record(lease.job_id, "known"), status="retry_wait")

    def _check(self, lease):
        if self.active.get(lease.job_id) != lease:
            raise LeaseLost("stale")


def _runtime(repository, source, handlers, **overrides):
    values = {
        "worker_count": 1,
        "poll_interval": timedelta(milliseconds=5),
        "lease_duration": timedelta(milliseconds=100),
        "heartbeat_interval": timedelta(milliseconds=10),
        "clock": lambda: NOW,
    }
    values.update(overrides)
    return DurableJobRuntime(repository, source, handlers, **values)


def _wait_for(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("condition was not reached")


def test_runtime_validates_configuration():
    repository = FakeRepository()

    with pytest.raises(ValueError, match="worker_count"):
        _runtime(repository, lambda _now: (), {}, worker_count=0)
    with pytest.raises(ValueError, match="positive and finite"):
        _runtime(repository, lambda _now: (), {}, poll_interval=timedelta(0))
    with pytest.raises(ValueError, match="shorter than"):
        _runtime(
            repository,
            lambda _now: (),
            {},
            heartbeat_interval=timedelta(milliseconds=100),
        )
    with pytest.raises(ValueError, match="handler names"):
        _runtime(repository, lambda _now: (), {" ": lambda _context: None})


def test_duplicate_ticks_enqueue_once_and_start_is_idempotent():
    repository = FakeRepository()
    calls = 0
    occurrence = ScheduledOccurrence(
        schedule_key="sec-current",
        scheduled_for=NOW,
        kind="known",
        parameters={},
        due_at=NOW,
    )

    def source(_now):
        nonlocal calls
        calls += 1
        return [occurrence]

    runtime = _runtime(repository, source, {"known": lambda _context: None})
    assert runtime.start() is True
    assert runtime.start() is False
    _wait_for(lambda: calls >= 3 and len(repository.completed) == 1)
    assert runtime.stop(1) is True
    assert len(repository.occurrences) == 1


def test_workers_filter_unknown_kinds_and_database_priority_wins():
    repository = FakeRepository()
    repository.seed("unknown", priority=1000)
    repository.seed("known", priority=1)
    repository.seed("known", priority=100)
    order = []

    def handler(context):
        order.append(context.job.priority)
        context.save_checkpoint({"page": context.job.job_id})
        context.record_item_outcome(
            item_key=str(context.job.job_id),
            status="succeeded",
            outcome={"ok": True},
        )
        return {"done": context.job.job_id}

    runtime = _runtime(repository, lambda _now: (), {"known": handler})
    runtime.start()
    _wait_for(lambda: len(repository.completed) == 2)
    assert runtime.stop(1) is True

    assert order == [100, 1]
    assert [job.kind for job in repository.queue] == ["unknown"]
    assert all(kinds == frozenset({"known"}) for kinds in repository.claim_calls)
    assert len(repository.checkpoints) == len(repository.items) == 2


def test_coordinator_survives_a_tick_error_and_handler_failures_are_visible():
    repository = FakeRepository()
    calls = 0
    occurrence = ScheduledOccurrence(
        schedule_key="edinet-current",
        scheduled_for=NOW,
        kind="known",
        parameters={},
        due_at=NOW,
    )

    def source(_now):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("bad tick")
        return [occurrence]

    def handler(_context):
        raise ValueError("bad document")

    runtime = _runtime(repository, source, {"known": handler})
    runtime.start()
    _wait_for(lambda: len(repository.failed) == 1 and calls >= 2)
    assert runtime.stop(1) is True
    assert any("coordinator: RuntimeError: bad tick" in error for error in runtime.errors)
    assert any("handler:known: ValueError: bad document" in error for error in runtime.errors)


def test_drain_is_bounded_stops_claims_and_keeps_heartbeats_running():
    repository = FakeRepository()
    repository.seed("known", priority=2)
    repository.seed("known", priority=1)
    started = threading.Event()
    release = threading.Event()
    saw_stop = threading.Event()

    def handler(context):
        started.set()
        while not release.wait(0.002):
            if context.stopping.is_set():
                saw_stop.set()
        return {"drained": True}

    runtime = _runtime(repository, lambda _now: (), {"known": handler})
    runtime.start()
    assert started.wait(1)
    assert runtime.stop(0.03) is False
    assert saw_stop.is_set()
    assert repository.renewals > 0
    assert len(repository.claim_calls) == 1

    release.set()
    assert runtime.stop(1) is True
    assert len(repository.completed) == 1
    assert len(repository.queue) == 1


def test_stop_closes_scheduler_gate_while_occurrence_source_is_blocked():
    repository = FakeRepository()
    source_started = threading.Event()
    source_release = threading.Event()
    occurrence = ScheduledOccurrence(
        schedule_key="too-late",
        scheduled_for=NOW,
        kind="known",
        parameters={},
        due_at=NOW,
    )

    def source(_now):
        source_started.set()
        source_release.wait()
        return [occurrence]

    runtime = _runtime(repository, source, {"known": lambda _context: None})
    runtime.start()
    assert source_started.wait(1)
    assert runtime.stop(0.01) is False
    source_release.set()
    assert runtime.stop(1) is True
    assert repository.occurrences == {}
    assert repository.recoveries == 0


def test_stop_timeout_does_not_wait_for_an_in_flight_claim():
    claim_started = threading.Event()
    claim_release = threading.Event()

    class SlowClaimRepository(FakeRepository):
        def claim_next(self, **values):
            claim_started.set()
            claim_release.wait()
            return super().claim_next(**values)

    repository = SlowClaimRepository()
    runtime = _runtime(repository, lambda _now: (), {"known": lambda _context: None})
    runtime.start()
    assert claim_started.wait(1)

    started = time.monotonic()
    assert runtime.stop(0) is False
    assert time.monotonic() - started < 0.05

    claim_release.set()
    assert runtime.stop(1) is True


def test_lease_loss_suppresses_stale_terminal_write():
    repository = FakeRepository()
    repository.seed("known")
    repository.lose_on_renew = True

    def handler(_context):
        time.sleep(0.03)
        return {"stale": True}

    runtime = _runtime(repository, lambda _now: (), {"known": handler})
    runtime.start()
    _wait_for(lambda: repository.renewals == 1)
    _wait_for(lambda: not repository.active)
    assert runtime.stop(1) is True
    assert repository.completed == []
    assert repository.failed == []


def test_invalid_returned_checkpoint_fails_the_job():
    repository = FakeRepository()
    repository.seed("known")
    runtime = _runtime(
        repository,
        lambda _now: (),
        {"known": lambda _context: {"bad": float("nan")}},
    )

    runtime.start()
    _wait_for(lambda: len(repository.failed) == 1)
    assert runtime.stop(1) is True
    assert "handler checkpoint must contain JSON values" in repository.failed[0][1]


def test_pending_handler_is_deferred_without_becoming_a_failure():
    repository = FakeRepository()
    repository.seed("known")

    def handler(_context):
        raise JobDeferred("source is still pending")

    runtime = _runtime(repository, lambda _now: (), {"known": handler})
    runtime.start()
    _wait_for(lambda: len(repository.deferred) == 1)
    assert runtime.stop(1) is True

    assert repository.failed == []
    assert "source is still pending" in repository.deferred[0][1]
    assert not any("handler:known" in error for error in runtime.errors)
