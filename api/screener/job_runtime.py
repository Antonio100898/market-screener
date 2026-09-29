from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .durable_jobs import (
    ClaimedJob,
    DurableJobItemRecord,
    DurableJobRecord,
    DurableJobRepository,
    ItemStatus,
    LeaseLost,
    LeaseToken,
)


Clock = Callable[[], datetime]
JobHandler = Callable[["JobContext"], Mapping[str, Any] | None]
OccurrenceSource = Callable[[datetime], Iterable["ScheduledOccurrence"]]


@dataclass(frozen=True)
class ScheduledOccurrence:
    schedule_key: str
    scheduled_for: datetime
    kind: str
    parameters: Mapping[str, Any]
    due_at: datetime
    priority: int = 0
    max_attempts: int = 3
    deadline_at: datetime | None = None


@dataclass(frozen=True)
class JobContext:
    job: DurableJobRecord
    stopping: threading.Event
    _repository: DurableJobRepository = field(repr=False)
    _lease: LeaseToken = field(repr=False)
    _clock: Clock = field(repr=False)

    @property
    def lease(self) -> LeaseToken:
        return self._lease

    def save_checkpoint(self, checkpoint: Mapping[str, Any]) -> DurableJobRecord:
        return self._repository.save_checkpoint(
            self._lease,
            checkpoint,
            now=self._clock(),
        )

    def record_item_outcome(
        self,
        *,
        item_key: str,
        status: ItemStatus,
        outcome: Mapping[str, Any] | None = None,
        error_summary: str | None = None,
    ) -> DurableJobItemRecord:
        return self._repository.record_item_outcome(
            self._lease,
            item_key=item_key,
            status=status,
            outcome=outcome,
            error_summary=error_summary,
            now=self._clock(),
        )


class DurableJobRuntime:
    def __init__(
        self,
        repository: DurableJobRepository,
        occurrence_source: OccurrenceSource,
        handlers: Mapping[str, JobHandler],
        *,
        worker_count: int = 1,
        poll_interval: timedelta = timedelta(seconds=1),
        lease_duration: timedelta = timedelta(minutes=5),
        heartbeat_interval: timedelta = timedelta(minutes=1),
        clock: Clock = lambda: datetime.now(timezone.utc),
    ) -> None:
        if isinstance(worker_count, bool) or not isinstance(worker_count, int):
            raise ValueError("worker_count must be a positive integer")
        if worker_count <= 0:
            raise ValueError("worker_count must be a positive integer")
        self._poll_seconds = _positive_seconds("poll_interval", poll_interval)
        _positive_seconds("lease_duration", lease_duration)
        self._heartbeat_seconds = _positive_seconds(
            "heartbeat_interval", heartbeat_interval
        )
        if heartbeat_interval >= lease_duration:
            raise ValueError("heartbeat_interval must be shorter than lease_duration")
        if not callable(occurrence_source):
            raise ValueError("occurrence_source must be callable")
        if not callable(clock):
            raise ValueError("clock must be callable")
        if not isinstance(handlers, Mapping):
            raise ValueError("handlers must be a mapping")
        validated_handlers: dict[str, JobHandler] = {}
        for kind, handler in handlers.items():
            if not isinstance(kind, str) or not kind.strip() or kind != kind.strip():
                raise ValueError("handler names must be non-empty trimmed text")
            if not callable(handler):
                raise ValueError(f"handler for {kind!r} must be callable")
            validated_handlers[kind] = handler

        self._repository = repository
        self._occurrence_source = occurrence_source
        self._handlers = validated_handlers
        self._allowed_kinds = frozenset(validated_handlers)
        self._worker_count = worker_count
        self._lease_duration = lease_duration
        self._clock = clock
        self._lifecycle_lock = threading.Lock()
        self._gate_lock = threading.Lock()
        self._error_lock = threading.Lock()
        self._claim_gate_closed = threading.Event()
        self._stop_requested = threading.Event()
        self._threads: tuple[threading.Thread, ...] = ()
        self._errors: deque[str] = deque(maxlen=100)
        self._runtime_id = ""

    @property
    def errors(self) -> tuple[str, ...]:
        with self._error_lock:
            return tuple(self._errors)

    @property
    def running(self) -> bool:
        with self._lifecycle_lock:
            return any(thread.is_alive() for thread in self._threads)

    def start(self) -> bool:
        with self._lifecycle_lock:
            if any(thread.is_alive() for thread in self._threads):
                return False
            self._stop_requested.clear()
            self._claim_gate_closed.clear()
            self._runtime_id = uuid4().hex
            threads = [
                threading.Thread(
                    target=self._coordinator_loop,
                    name=f"durable-coordinator-{self._runtime_id[:8]}",
                    daemon=True,
                )
            ]
            threads.extend(
                threading.Thread(
                    target=self._worker_loop,
                    args=(index,),
                    name=f"durable-worker-{self._runtime_id[:8]}-{index}",
                    daemon=True,
                )
                for index in range(self._worker_count)
            )
            self._threads = tuple(threads)
            for thread in self._threads:
                thread.start()
        return True

    def stop(self, timeout: float | None = None) -> bool:
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("timeout must be non-negative and finite")
        self._claim_gate_closed.set()
        self._stop_requested.set()
        with self._lifecycle_lock:
            threads = self._threads
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in threads:
            remaining = (
                None
                if deadline is None
                else max(0.0, deadline - time.monotonic())
            )
            thread.join(remaining)
        return not any(thread.is_alive() for thread in threads)

    def _coordinator_loop(self) -> None:
        while not self._stop_requested.is_set():
            try:
                now = self._clock()
                occurrences = self._occurrence_source(now)
                for occurrence in occurrences:
                    if not isinstance(occurrence, ScheduledOccurrence):
                        raise TypeError(
                            "occurrence_source must return ScheduledOccurrence values"
                        )
                    with self._gate_lock:
                        if self._claim_gate_closed.is_set():
                            break
                        self._repository.enqueue_scheduled(**vars(occurrence))
                with self._gate_lock:
                    if not self._claim_gate_closed.is_set():
                        self._repository.recover_expired(now=now)
            except Exception as error:
                self._record_error("coordinator", error)
            self._stop_requested.wait(self._poll_seconds)

    def _worker_loop(self, index: int) -> None:
        owner = f"{self._runtime_id}:{index}"
        while not self._claim_gate_closed.is_set():
            claimed = self._claim(owner)
            if claimed is None:
                self._stop_requested.wait(self._poll_seconds)
                continue
            self._run_claimed(claimed)

    def _claim(self, owner: str) -> ClaimedJob | None:
        with self._gate_lock:
            if self._claim_gate_closed.is_set():
                return None
            try:
                return self._repository.claim_next(
                    owner=owner,
                    lease_duration=self._lease_duration,
                    now=self._clock(),
                    allowed_kinds=self._allowed_kinds,
                )
            except Exception as error:
                self._record_error("claim", error)
                return None

    def _run_claimed(self, claimed: ClaimedJob) -> None:
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(claimed.lease, heartbeat_stop, lease_lost),
            name=f"durable-heartbeat-{claimed.job.job_id}",
            daemon=True,
        )
        context = JobContext(
            job=claimed.job,
            stopping=self._stop_requested,
            _repository=self._repository,
            _lease=claimed.lease,
            _clock=self._clock,
        )
        handler_error: Exception | None = None
        final_checkpoint: Mapping[str, Any] | None = None
        heartbeat.start()
        try:
            final_checkpoint = _checkpoint(
                self._handlers[claimed.job.kind](context)
            )
        except LeaseLost:
            lease_lost.set()
        except Exception as error:
            handler_error = error
            self._record_error(f"handler:{claimed.job.kind}", error)
        finally:
            heartbeat_stop.set()
            heartbeat.join()

        if lease_lost.is_set():
            return
        if handler_error is not None:
            self._fail(claimed.lease, handler_error)
            return
        try:
            self._repository.complete(
                claimed.lease,
                final_checkpoint=final_checkpoint,
                now=self._clock(),
            )
        except LeaseLost:
            return
        except Exception as error:
            self._record_error("complete", error)
            self._fail(claimed.lease, error)

    def _heartbeat_loop(
        self,
        lease: LeaseToken,
        heartbeat_stop: threading.Event,
        lease_lost: threading.Event,
    ) -> None:
        while not heartbeat_stop.wait(self._heartbeat_seconds):
            try:
                self._repository.renew(
                    lease,
                    lease_duration=self._lease_duration,
                    now=self._clock(),
                )
            except LeaseLost:
                lease_lost.set()
                return
            except Exception as error:
                self._record_error("heartbeat", error)

    def _fail(self, lease: LeaseToken, error: Exception) -> None:
        summary = f"{type(error).__name__}: {error}"[:2000]
        try:
            self._repository.fail(
                lease,
                error_summary=summary,
                now=self._clock(),
            )
        except LeaseLost:
            return
        except Exception as failure_error:
            self._record_error("fail", failure_error)

    def _record_error(self, component: str, error: Exception) -> None:
        message = f"{component}: {type(error).__name__}: {error}"[:2200]
        with self._error_lock:
            self._errors.append(message)


def _positive_seconds(name: str, value: timedelta) -> float:
    if not isinstance(value, timedelta):
        raise ValueError(f"{name} must be a duration")
    seconds = value.total_seconds()
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return seconds


def _checkpoint(value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("handler checkpoint must be a JSON object")
    try:
        canonical = json.dumps(value, allow_nan=False)
        decoded = json.loads(canonical)
    except (TypeError, ValueError) as error:
        raise ValueError("handler checkpoint must contain JSON values") from error
    if not isinstance(decoded, dict):
        raise ValueError("handler checkpoint must be a JSON object")
    return decoded
