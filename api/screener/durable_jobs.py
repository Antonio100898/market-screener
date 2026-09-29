from __future__ import annotations

import json
import math
import random
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from .postgres import durable_job, durable_job_item, job_schedule_occurrence


JobStatus = Literal["pending", "running", "retry_wait", "succeeded", "failed"]
ItemStatus = Literal["succeeded", "failed"]
_RUNNABLE_STATUSES = ("pending", "retry_wait")
_MAX_ERROR_SUMMARY_LENGTH = 2000


def _full_jitter(cap_seconds: float) -> float:
    return random.uniform(0.0, cap_seconds)


class JobIdentityConflict(ValueError):
    pass


class LeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaseToken:
    job_id: int
    owner: str
    generation: int


@dataclass(frozen=True)
class DurableJobRecord:
    job_id: int
    occurrence_id: int | None
    kind: str
    parameters: dict[str, Any]
    status: JobStatus
    priority: int
    due_at: datetime
    attempts: int
    max_attempts: int
    next_retry_at: datetime | None
    deadline_at: datetime | None
    owner: str | None
    lease_expires_at: datetime | None
    ownership_generation: int
    heartbeat_at: datetime | None
    checkpoint: dict[str, Any] | None
    error_summary: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class DurableJobItemRecord:
    job_id: int
    item_key: str
    status: ItemStatus
    attempts: int
    outcome: dict[str, Any] | None
    error_summary: str | None
    updated_at: datetime
    finished_at: datetime


@dataclass(frozen=True)
class ClaimedJob:
    job: DurableJobRecord
    lease: LeaseToken


@dataclass(frozen=True)
class RetryPolicy:
    base_delay: timedelta = timedelta(seconds=30)
    max_delay: timedelta = timedelta(hours=1)
    jitter_seconds: Callable[[float], float] = _full_jitter

    def __post_init__(self) -> None:
        base_seconds = self.base_delay.total_seconds()
        max_seconds = self.max_delay.total_seconds()
        if not math.isfinite(base_seconds) or base_seconds <= 0:
            raise ValueError("base retry delay must be positive and finite")
        if not math.isfinite(max_seconds) or max_seconds < base_seconds:
            raise ValueError(
                "maximum retry delay must be finite and at least the base delay"
            )

    def retry_at(self, *, attempts: int, now: datetime) -> datetime:
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError("attempts must be a positive integer")
        current = _utc_datetime("now", now)
        base_seconds = self.base_delay.total_seconds()
        max_seconds = self.max_delay.total_seconds()
        exponent_limit = max(0, math.ceil(math.log2(max_seconds / base_seconds)))
        cap_seconds = min(
            max_seconds,
            base_seconds * (2 ** min(attempts - 1, exponent_limit)),
        )
        delay_seconds = self.jitter_seconds(cap_seconds)
        if (
            isinstance(delay_seconds, bool)
            or not isinstance(delay_seconds, (int, float))
            or not math.isfinite(delay_seconds)
            or delay_seconds < 0
            or delay_seconds > cap_seconds
        ):
            raise ValueError(
                "retry jitter must return seconds between zero and the delay cap"
            )
        return current + timedelta(seconds=delay_seconds)


class DurableJobRepository:
    def __init__(self, engine: Engine, *, retry_policy: RetryPolicy | None = None):
        self.engine = engine
        self.retry_policy = retry_policy or RetryPolicy()

    def enqueue_scheduled(
        self,
        *,
        schedule_key: str,
        scheduled_for: datetime,
        kind: str,
        parameters: Mapping[str, Any],
        due_at: datetime,
        priority: int = 0,
        max_attempts: int = 3,
        deadline_at: datetime | None = None,
    ) -> DurableJobRecord:
        schedule = _required_text("schedule key", schedule_key)
        scheduled = _utc_datetime("scheduled_for", scheduled_for)
        job_kind = _required_text("job kind", kind)
        job_parameters = _json_object("parameters", parameters)
        due = _utc_datetime("due_at", due_at)
        deadline = (
            _utc_datetime("deadline_at", deadline_at)
            if deadline_at is not None
            else None
        )
        job_priority = _integer("priority", priority)
        attempt_limit = _positive_integer("max_attempts", max_attempts)
        if deadline is not None and deadline <= due:
            raise ValueError("deadline_at must be after due_at")

        immutable = {
            "kind": job_kind,
            "parameters": job_parameters,
            "priority": job_priority,
            "due_at": due,
            "max_attempts": attempt_limit,
            "deadline_at": deadline,
        }
        with self.engine.begin() as connection:
            row = self._enqueue_on_connection(
                connection,
                schedule_key=schedule,
                scheduled_for=scheduled,
                immutable=immutable,
            )
        return _job_record(row)

    def enqueue_child(
        self,
        lease: LeaseToken,
        *,
        child_key: str,
        scheduled_for: datetime,
        kind: str,
        parameters: Mapping[str, Any],
        due_at: datetime,
        priority: int = 0,
        max_attempts: int = 3,
        deadline_at: datetime | None = None,
        now: datetime | None = None,
    ) -> DurableJobRecord:
        schedule = _required_text("child key", child_key)
        scheduled = _utc_datetime("scheduled_for", scheduled_for)
        job_kind = _required_text("job kind", kind)
        job_parameters = _json_object("parameters", parameters)
        due = _utc_datetime("due_at", due_at)
        deadline = (
            _utc_datetime("deadline_at", deadline_at)
            if deadline_at is not None
            else None
        )
        job_priority = _integer("priority", priority)
        attempt_limit = _positive_integer("max_attempts", max_attempts)
        if deadline is not None and deadline <= due:
            raise ValueError("deadline_at must be after due_at")
        current = _utc_datetime("now", now or datetime.now(timezone.utc))
        immutable = {
            "kind": job_kind,
            "parameters": job_parameters,
            "priority": job_priority,
            "due_at": due,
            "max_attempts": attempt_limit,
            "deadline_at": deadline,
        }
        with self.engine.begin() as connection:
            self._lock_live_lease(connection, lease, current)
            row = self._enqueue_on_connection(
                connection,
                schedule_key=schedule,
                scheduled_for=scheduled,
                immutable=immutable,
            )
        return _job_record(row)

    @staticmethod
    def _enqueue_on_connection(
        connection: Connection,
        *,
        schedule_key: str,
        scheduled_for: datetime,
        immutable: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        occurrence_id = connection.scalar(
            insert(job_schedule_occurrence)
            .values(schedule_key=schedule_key, scheduled_for=scheduled_for)
            .on_conflict_do_nothing(
                constraint="uq_job_schedule_occurrence_identity"
            )
            .returning(job_schedule_occurrence.c.occurrence_id)
        )
        if occurrence_id is None:
            occurrence_id = connection.scalar(
                select(job_schedule_occurrence.c.occurrence_id).where(
                    job_schedule_occurrence.c.schedule_key == schedule_key,
                    job_schedule_occurrence.c.scheduled_for == scheduled_for,
                )
            )
        statement = (
            insert(durable_job)
            .values(
                occurrence_id=occurrence_id,
                **immutable,
                status="pending",
                attempts=0,
                next_retry_at=None,
                owner=None,
                lease_expires_at=None,
                ownership_generation=0,
                heartbeat_at=None,
                checkpoint=None,
                error_summary=None,
                finished_at=None,
            )
            .on_conflict_do_nothing(constraint="uq_durable_job_occurrence")
            .returning(*durable_job.c)
        )
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            row = connection.execute(
                select(durable_job).where(
                    durable_job.c.occurrence_id == occurrence_id
                )
            ).mappings().one()
            if any(row[field] != value for field, value in immutable.items()):
                raise JobIdentityConflict(
                    "schedule occurrence already identifies different job content"
                )
        return row

    def claim_next(
        self,
        *,
        owner: str,
        lease_duration: timedelta,
        now: datetime | None = None,
        allowed_kinds: Collection[str] | None = None,
    ) -> ClaimedJob | None:
        worker = _required_text("owner", owner)
        duration = _positive_duration("lease_duration", lease_duration)
        current = _utc_datetime("now", now or datetime.now(timezone.utc))
        kinds = _job_kinds(allowed_kinds)
        if kinds == ():
            return None
        lease_expires = current + duration
        kind_filter = () if kinds is None else (durable_job.c.kind.in_(kinds),)

        with self.engine.begin() as connection:
            expired_job_id = connection.scalar(
                select(durable_job.c.job_id)
                .where(
                    durable_job.c.status.in_(_RUNNABLE_STATUSES),
                    durable_job.c.deadline_at.is_not(None),
                    durable_job.c.deadline_at <= current,
                    *kind_filter,
                )
                .order_by(durable_job.c.deadline_at, durable_job.c.job_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if expired_job_id is not None:
                connection.execute(
                    update(durable_job)
                    .where(durable_job.c.job_id == expired_job_id)
                    .values(
                        status="failed",
                        next_retry_at=None,
                        error_summary="job deadline expired before claim",
                        updated_at=current,
                        finished_at=current,
                    )
                )
            job_id = connection.scalar(
                select(durable_job.c.job_id)
                .where(
                    durable_job.c.status.in_(_RUNNABLE_STATUSES),
                    durable_job.c.due_at <= current,
                    or_(
                        durable_job.c.status == "pending",
                        durable_job.c.next_retry_at <= current,
                    ),
                    durable_job.c.attempts < durable_job.c.max_attempts,
                    or_(
                        durable_job.c.deadline_at.is_(None),
                        durable_job.c.deadline_at > current,
                    ),
                    *kind_filter,
                )
                .order_by(
                    durable_job.c.priority.desc(),
                    durable_job.c.due_at,
                    durable_job.c.job_id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job_id is None:
                return None
            row = connection.execute(
                update(durable_job)
                .where(durable_job.c.job_id == job_id)
                .values(
                    status="running",
                    attempts=durable_job.c.attempts + 1,
                    next_retry_at=None,
                    owner=worker,
                    lease_expires_at=lease_expires,
                    ownership_generation=durable_job.c.ownership_generation + 1,
                    heartbeat_at=current,
                    error_summary=None,
                    updated_at=current,
                )
                .returning(*durable_job.c)
            ).mappings().one()
        job = _job_record(row)
        return ClaimedJob(
            job=job,
            lease=LeaseToken(job.job_id, worker, job.ownership_generation),
        )

    def renew(
        self,
        lease: LeaseToken,
        *,
        lease_duration: timedelta,
        now: datetime | None = None,
    ) -> DurableJobRecord:
        duration = _positive_duration("lease_duration", lease_duration)
        current = _utc_datetime("now", now or datetime.now(timezone.utc))
        with self.engine.begin() as connection:
            self._lock_live_lease(connection, lease, current)
            row = connection.execute(
                update(durable_job)
                .where(durable_job.c.job_id == lease.job_id)
                .values(
                    lease_expires_at=current + duration,
                    heartbeat_at=current,
                    updated_at=current,
                )
                .returning(*durable_job.c)
            ).mappings().one()
        return _job_record(row)

    def save_checkpoint(
        self,
        lease: LeaseToken,
        checkpoint: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> DurableJobRecord:
        value = _json_object("checkpoint", checkpoint)
        current = _utc_datetime("now", now or datetime.now(timezone.utc))
        with self.engine.begin() as connection:
            self._lock_live_lease(connection, lease, current)
            row = connection.execute(
                update(durable_job)
                .where(durable_job.c.job_id == lease.job_id)
                .values(checkpoint=value, updated_at=current)
                .returning(*durable_job.c)
            ).mappings().one()
        return _job_record(row)

    def record_item_outcome(
        self,
        lease: LeaseToken,
        *,
        item_key: str,
        status: ItemStatus,
        outcome: Mapping[str, Any] | None = None,
        error_summary: str | None = None,
        now: datetime | None = None,
    ) -> DurableJobItemRecord:
        key = _required_text("item key", item_key)
        if status not in ("succeeded", "failed"):
            raise ValueError("item status must be succeeded or failed")
        result = _json_object("outcome", outcome) if outcome is not None else None
        error = _error_summary(error_summary)
        current = _utc_datetime("now", now or datetime.now(timezone.utc))
        with self.engine.begin() as connection:
            row = self._record_item_outcome_on_connection(
                connection,
                lease,
                item_key=key,
                status=status,
                outcome=result,
                error_summary=error,
                now=current,
            )
        return _item_record(row)

    def _record_item_outcome_on_connection(
        self,
        connection: Connection,
        lease: LeaseToken,
        *,
        item_key: str,
        status: ItemStatus,
        outcome: dict[str, Any] | None,
        error_summary: str | None,
        now: datetime,
        lease_is_locked: bool = False,
    ) -> Mapping[str, Any]:
        if not lease_is_locked:
            self._lock_live_lease(connection, lease, now)
        existing = connection.execute(
            select(durable_job_item)
            .where(
                durable_job_item.c.job_id == lease.job_id,
                durable_job_item.c.item_key == item_key,
            )
            .with_for_update()
        ).mappings().one_or_none()
        if (
            existing is not None
            and existing["status"] == status
            and existing["outcome"] == outcome
            and existing["error_summary"] == error_summary
        ):
            return existing
        if existing is None:
            return connection.execute(
                durable_job_item.insert()
                .values(
                    job_id=lease.job_id,
                    item_key=item_key,
                    status=status,
                    attempts=1,
                    outcome=outcome,
                    error_summary=error_summary,
                    updated_at=now,
                    finished_at=now,
                )
                .returning(*durable_job_item.c)
            ).mappings().one()
        return connection.execute(
            update(durable_job_item)
            .where(
                durable_job_item.c.job_id == lease.job_id,
                durable_job_item.c.item_key == item_key,
            )
            .values(
                status=status,
                attempts=durable_job_item.c.attempts + 1,
                outcome=outcome,
                error_summary=error_summary,
                updated_at=now,
                finished_at=now,
            )
            .returning(*durable_job_item.c)
        ).mappings().one()

    def complete(
        self,
        lease: LeaseToken,
        *,
        final_checkpoint: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> DurableJobRecord:
        checkpoint = (
            _json_object("final checkpoint", final_checkpoint)
            if final_checkpoint is not None
            else None
        )
        current = _utc_datetime("now", now or datetime.now(timezone.utc))
        with self.engine.begin() as connection:
            self._lock_live_lease(connection, lease, current)
            values: dict[str, Any] = {
                "status": "succeeded",
                "owner": None,
                "lease_expires_at": None,
                "next_retry_at": None,
                "error_summary": None,
                "updated_at": current,
                "finished_at": current,
            }
            if checkpoint is not None:
                values["checkpoint"] = checkpoint
            row = connection.execute(
                update(durable_job)
                .where(durable_job.c.job_id == lease.job_id)
                .values(**values)
                .returning(*durable_job.c)
            ).mappings().one()
        return _job_record(row)

    def fail(
        self,
        lease: LeaseToken,
        *,
        error_summary: str,
        final_checkpoint: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> DurableJobRecord:
        error = _required_error_summary(error_summary)
        checkpoint = (
            _json_object("final checkpoint", final_checkpoint)
            if final_checkpoint is not None
            else None
        )
        current = _utc_datetime("now", now or datetime.now(timezone.utc))
        with self.engine.begin() as connection:
            locked = self._lock_live_lease(connection, lease, current)
            values = self._failed_or_retry_values(locked, current, error)
            if checkpoint is not None:
                values["checkpoint"] = checkpoint
            row = connection.execute(
                update(durable_job)
                .where(durable_job.c.job_id == lease.job_id)
                .values(**values)
                .returning(*durable_job.c)
            ).mappings().one()
        return _job_record(row)

    def recover_expired(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> tuple[DurableJobRecord, ...]:
        current = _utc_datetime("now", now or datetime.now(timezone.utc))
        batch_size = _positive_integer("limit", limit)
        with self.engine.begin() as connection:
            rows = connection.execute(
                select(durable_job)
                .where(
                    durable_job.c.status == "running",
                    durable_job.c.lease_expires_at <= current,
                )
                .order_by(durable_job.c.lease_expires_at, durable_job.c.job_id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            ).mappings().all()
            recovered = []
            for locked in rows:
                values = self._failed_or_retry_values(
                    locked,
                    current,
                    "worker lease expired",
                )
                recovered.append(
                    connection.execute(
                        update(durable_job)
                        .where(durable_job.c.job_id == locked["job_id"])
                        .values(**values)
                        .returning(*durable_job.c)
                    ).mappings().one()
                )
        return tuple(_job_record(row) for row in recovered)

    def _lock_live_lease(
        self,
        connection: Connection,
        lease: LeaseToken,
        now: datetime,
    ) -> Mapping[str, Any]:
        token = _lease_token(lease)
        row = connection.execute(
            select(durable_job)
            .where(durable_job.c.job_id == token.job_id)
            .with_for_update()
        ).mappings().one_or_none()
        if (
            row is None
            or row["status"] != "running"
            or row["owner"] != token.owner
            or row["ownership_generation"] != token.generation
            or row["lease_expires_at"] <= now
        ):
            raise LeaseLost("job lease is absent, expired, or no longer owned")
        return row

    def _failed_or_retry_values(
        self,
        row: Mapping[str, Any],
        now: datetime,
        error_summary: str,
    ) -> dict[str, Any]:
        deadline = row["deadline_at"]
        terminal = row["attempts"] >= row["max_attempts"] or (
            deadline is not None and now >= deadline
        )
        next_retry = None
        if not terminal:
            next_retry = self.retry_policy.retry_at(
                attempts=row["attempts"],
                now=now,
            )
            terminal = deadline is not None and next_retry >= deadline
        return {
            "status": "failed" if terminal else "retry_wait",
            "owner": None,
            "lease_expires_at": None,
            "next_retry_at": None if terminal else next_retry,
            "error_summary": error_summary,
            "updated_at": now,
            "finished_at": now if terminal else None,
        }


def _job_record(row: Mapping[str, Any]) -> DurableJobRecord:
    return DurableJobRecord(
        **{field: row[field] for field in DurableJobRecord.__dataclass_fields__}
    )


def _item_record(row: Mapping[str, Any]) -> DurableJobItemRecord:
    return DurableJobItemRecord(
        **{field: row[field] for field in DurableJobItemRecord.__dataclass_fields__}
    )


def _required_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value.strip()


def _error_summary(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("error summary must be text")
    if len(value) > _MAX_ERROR_SUMMARY_LENGTH:
        raise ValueError("error summary must be at most 2000 characters")
    return value


def _required_error_summary(value: str) -> str:
    error = _error_summary(value)
    if error is None or not error.strip():
        raise ValueError("error summary must not be empty")
    return error


def _utc_datetime(name: str, value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _integer(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _positive_integer(name: str, value: int) -> int:
    result = _integer(name, value)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _positive_duration(name: str, value: timedelta) -> timedelta:
    if not isinstance(value, timedelta):
        raise ValueError(f"{name} must be a duration")
    seconds = value.total_seconds()
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return value


def _json_object(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    try:
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        decoded = json.loads(canonical)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must contain JSON values") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"{name} must be a JSON object")
    return decoded


def _lease_token(value: LeaseToken) -> LeaseToken:
    if not isinstance(value, LeaseToken):
        raise ValueError("lease must be a LeaseToken")
    return LeaseToken(
        job_id=_positive_integer("lease job_id", value.job_id),
        owner=_required_text("lease owner", value.owner),
        generation=_positive_integer("lease generation", value.generation),
    )


def _job_kinds(value: Collection[str] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Collection):
        raise ValueError("allowed job kinds must be a collection of names")
    return tuple(sorted({_required_text("allowed job kind", kind) for kind in value}))
