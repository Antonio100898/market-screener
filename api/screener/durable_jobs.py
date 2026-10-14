from __future__ import annotations

import json
import math
import random
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import Text, cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from .postgres import durable_job, durable_job_item, job_schedule_occurrence


JobStatus = Literal["pending", "running", "retry_wait", "succeeded", "failed"]
ItemStatus = Literal["succeeded", "failed"]
_RUNNABLE_STATUSES = ("pending", "retry_wait")
_MAX_ERROR_SUMMARY_LENGTH = 2000
_SOURCE_RECEIPT_PREFIX = "source-acquisition-receipt:"
MAX_SCHEDULE_PARAMETER_BYTES = 1024 * 1024


def _full_jitter(cap_seconds: float) -> float:
    return random.uniform(0.0, cap_seconds)


class JobIdentityConflict(ValueError):
    pass


class LeaseLost(RuntimeError):
    pass


class JobDeferred(RuntimeError):
    """The job made an honest pending result and should be tried again later."""

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
    @staticmethod
    def _read_jsonb_row(connection, statement, identifiers, json_columns, budget, *, for_update=False, maximum_rows=1):
        """Read the same locked row after reserving its actual PostgreSQL payload."""
        if budget is None:
            return connection.execute(statement).mappings().one_or_none()
        if (connection.engine.dialect.name != "postgresql"
                or connection.connection.dbapi_connection.autocommit is not False):
            raise ValueError("raw JSONB admission requires a PostgreSQL transaction")
        scalars = tuple(column for column in statement.selected_columns
                        if not any(column is value for value in json_columns))
        sizes = tuple(json_columns) + scalars
        length_columns = tuple(func.octet_length(cast(column, Text)).label("_raw_bytes_" + str(index))
                               for index, column in enumerate(sizes))
        # IDs are fixed bigint, except the already bounded requested item key.
        budget.sql_row_preflight(identifiers, len(sizes), maximum_rows)
        preflight = statement.with_only_columns(*identifiers, *length_columns, maintain_column_froms=True)
        preflight = preflight.with_for_update() if for_update else preflight.with_for_update(read=True)
        selected_rows = connection.execute(preflight).mappings().all()
        if not selected_rows:
            return None
        if len(selected_rows) != 1:
            raise ValueError("raw JSONB row selection is ambiguous")
        selected = selected_rows[0]
        lengths = tuple(selected["_raw_bytes_" + str(index)] for index in range(len(sizes)))
        budget.sql_row_read(lengths[:len(json_columns)], lengths[len(json_columns):])
        exact = statement.where(*(column == selected[column.key] for column in identifiers))
        row = connection.execute(exact).mappings().one_or_none()
        if row is None or any(row[column.key] != selected[column.key] for column in identifiers):
            raise ValueError("selected raw JSONB row changed after reservation")
        return row

    @staticmethod
    def _return_item_row(connection, statement, budget):
        if budget is None:
            return connection.execute(statement.returning(*durable_job_item.c)).mappings().one()
        budget.sql_row_preflight((durable_job_item.c.job_id, durable_job_item.c.item_key), 0)
        identity = connection.execute(statement.returning(durable_job_item.c.job_id,
            durable_job_item.c.item_key)).mappings().one()
        row = DurableJobRepository._read_jsonb_row(connection, select(durable_job_item).where(
            durable_job_item.c.job_id == identity["job_id"], durable_job_item.c.item_key == identity["item_key"]),
            (durable_job_item.c.job_id, durable_job_item.c.item_key), (durable_job_item.c.outcome,),
            budget, for_update=True)
        if row is None:
            raise ValueError("written item disappeared within its locked transaction")
        return row

    def __init__(self, engine: Engine, *, retry_policy: RetryPolicy | None = None):
        self.engine = engine
        self.retry_policy = retry_policy or RetryPolicy()

    def job_for_occurrence(
        self, *, schedule_key: str, scheduled_for: datetime, kind: str,
        parameters: Mapping[str, Any], due_at: datetime, priority: int = 0,
        max_attempts: int = 3, deadline_at: datetime | None = None,
    ) -> DurableJobRecord | None:
        """Recover a lost enqueue return by exact immutable scheduling declaration."""
        if type(priority) is not int or type(max_attempts) is not int:
            raise ValueError("schedule priority/max_attempts must be exact integers")
        key = _bounded_schedule_text("schedule key", schedule_key, 1024)
        job_kind = _bounded_schedule_text("job kind", kind, 256)
        _, parameter_json = _bounded_schedule_parameters(parameters)
        scheduled = _schedule_datetime("scheduled_for", scheduled_for)
        due = _schedule_datetime("due_at", due_at)
        deadline = _schedule_datetime("deadline_at", deadline_at) if deadline_at is not None else None
        job_priority = _integer("priority", priority)
        attempts = _positive_integer("max_attempts", max_attempts)
        if not -2**31 <= job_priority < 2**31 or attempts >= 2**31:
            raise ValueError("schedule priority/attempts must fit PostgreSQL integer")
        if deadline is not None and deadline <= due:
            raise ValueError("deadline_at must be after due_at")
        with self.engine.connect() as connection:
            row = connection.execute(select(durable_job, job_schedule_occurrence.c.occurrence_id.label("matched_occurrence_id"))
                .select_from(job_schedule_occurrence.outerjoin(durable_job,
                    durable_job.c.occurrence_id == job_schedule_occurrence.c.occurrence_id))
                .where(job_schedule_occurrence.c.schedule_key == key,
                       job_schedule_occurrence.c.scheduled_for == scheduled)).mappings().one_or_none()
        if row is None:
            return None
        if row["job_id"] is None:
            raise JobIdentityConflict("schedule occurrence has no associated durable job")
        immutable = dict(kind=job_kind, due_at=due, priority=job_priority,
                         max_attempts=attempts, deadline_at=deadline)
        _, actual_json = _bounded_schedule_parameters(row["parameters"])
        if (any(row[name] != value for name, value in immutable.items())
                or any(type(row[name]) is not int for name in ("priority", "max_attempts"))
                or actual_json != parameter_json):
            raise JobIdentityConflict("schedule occurrence identifies different immutable job content")
        return _job_record(row)

    def scheduled_job(
        self, *, job_id: int, occurrence_id: int, schedule_key: str,
        scheduled_for: datetime, _raw_work_budget=None,
    ) -> DurableJobRecord:
        """Read one recorded child association without enqueueing or selecting latest."""
        job = _positive_integer("job_id", job_id)
        occurrence = _positive_integer("occurrence_id", occurrence_id)
        if job > 2**63 - 1 or occurrence > 2**63 - 1:
            raise ValueError("recorded child IDs must fit PostgreSQL bigint")
        key = _required_text("schedule_key", schedule_key)
        if key != schedule_key or "\0" in key or len(key.encode("utf-8")) > 1024:
            raise ValueError("exact schedule key must be canonical text within1024 bytes")
        scheduled = _utc_datetime("scheduled_for", scheduled_for)
        with self.engine.connect() as connection:
            statement = select(durable_job).join(job_schedule_occurrence,
                durable_job.c.occurrence_id == job_schedule_occurrence.c.occurrence_id,
            ).where(durable_job.c.job_id == job, durable_job.c.occurrence_id == occurrence,
                job_schedule_occurrence.c.schedule_key == key,
                job_schedule_occurrence.c.scheduled_for == scheduled)
            row = self._read_jsonb_row(connection, statement, (durable_job.c.job_id,),
                (durable_job.c.parameters, durable_job.c.checkpoint), _raw_work_budget)
        if row is None:
            raise JobIdentityConflict("recorded child job/occurrence/key/time association is absent")
        return _job_record(row)

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
        now: datetime | Callable[[], datetime] | None = None,
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
        current = _utc_datetime("now", now() if callable(now) else now or datetime.now(timezone.utc))
        immutable = {
            "kind": job_kind,
            "parameters": job_parameters,
            "priority": job_priority,
            "due_at": due,
            "max_attempts": attempt_limit,
            "deadline_at": deadline,
        }
        with self.engine.begin() as connection:
            self._lock_live_lease(connection, lease, now)
            row = self._enqueue_on_connection(
                connection,
                schedule_key=schedule,
                scheduled_for=scheduled,
                immutable=immutable,
            )
            self._lock_live_lease(connection, lease, now)
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
        now: datetime | Callable[[], datetime] | None = None,
        allowed_kinds: Collection[str] | None = None,
    ) -> ClaimedJob | None:
        worker = _required_text("owner", owner)
        duration = _positive_duration("lease_duration", lease_duration)
        current = _utc_datetime("now", now() if callable(now) else now or datetime.now(timezone.utc))
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
        now: datetime | Callable[[], datetime] | None = None,
    ) -> DurableJobRecord:
        duration = _positive_duration("lease_duration", lease_duration)
        current = _utc_datetime("now", now() if callable(now) else now or datetime.now(timezone.utc))
        with self.engine.begin() as connection:
            self._lock_live_lease(connection, lease, now)
            current = _utc_datetime("now", now() if callable(now) else now or datetime.now(timezone.utc))
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
        now: datetime | Callable[[], datetime] | None = None,
        _raw_work_budget=None,
    ) -> DurableJobRecord:
        value = _json_object("checkpoint", checkpoint)
        with self.engine.begin() as connection:
            row = self._save_checkpoint_on_connection(connection, lease, value, now,
                **({} if _raw_work_budget is None else dict(_raw_work_budget=_raw_work_budget)))
        return _job_record(row)

    def save_checkpoint_on_connection(
        self,
        connection: Connection,
        lease: LeaseToken,
        checkpoint: Mapping[str, Any],
        *,
        now: datetime | Callable[[], datetime] | None = None,
        _raw_work_budget=None,
    ) -> DurableJobRecord:
        """Write a provisional checkpoint without committing the caller's work."""
        value = _json_object("checkpoint", checkpoint)
        if (not isinstance(connection, Connection) or connection.closed
                or connection.engine is not self.engine or not connection.in_transaction()
                or not connection.get_transaction().is_active):
            raise ValueError("checkpoint requires a live transaction from this repository engine")
        if getattr(connection.connection.dbapi_connection, "autocommit", None) is not False:
            raise ValueError("checkpoint requires DBAPI autocommit disabled")
        return _job_record(self._save_checkpoint_on_connection(connection, lease, value, now,
            **({} if _raw_work_budget is None else dict(_raw_work_budget=_raw_work_budget))))

    def _save_checkpoint_on_connection(
        self,
        connection: Connection,
        lease: LeaseToken,
        value: dict[str, Any],
        now: datetime | Callable[[], datetime] | None,
        _raw_work_budget=None,
    ) -> Mapping[str, Any]:
        budget_args = {} if _raw_work_budget is None else dict(_raw_work_budget=_raw_work_budget)
        self._lock_live_lease(connection, lease, now, **budget_args)
        current = _utc_datetime("now", now() if callable(now) else now or datetime.now(timezone.utc))
        statement = (
            update(durable_job)
            .where(durable_job.c.job_id == lease.job_id)
            .values(checkpoint=value, updated_at=current)
        )
        if _raw_work_budget is None:
            row = connection.execute(statement.returning(*durable_job.c)).mappings().one()
        else:
            _raw_work_budget.sql_row_preflight((durable_job.c.job_id,), 0)
            identity = connection.execute(statement.returning(durable_job.c.job_id)).scalar_one()
            row = self._read_jsonb_row(connection, select(durable_job).where(durable_job.c.job_id == identity),
                (durable_job.c.job_id,), (durable_job.c.parameters, durable_job.c.checkpoint),
                _raw_work_budget, for_update=True)
            if row is None:
                raise LeaseLost("checkpoint job disappeared within its locked transaction")
        self._lock_live_lease(connection, lease, now, **budget_args)
        return row

    def record_item_outcome(
        self,
        lease: LeaseToken,
        *,
        item_key: str,
        status: ItemStatus,
        outcome: Mapping[str, Any] | None = None,
        error_summary: str | None = None,
        now: datetime | Callable[[], datetime] | None = None,
    ) -> DurableJobItemRecord:
        key = _required_text("item key", item_key)
        if key.startswith(_SOURCE_RECEIPT_PREFIX):
            raise ValueError("source acquisition receipts require immutable insertion")
        if status not in ("succeeded", "failed"):
            raise ValueError("item status must be succeeded or failed")
        result = _json_object("outcome", outcome) if outcome is not None else None
        error = _error_summary(error_summary)
        current = _utc_datetime("now", now() if callable(now) else now or datetime.now(timezone.utc))
        with self.engine.begin() as connection:
            row = self._record_item_outcome_on_connection(
                connection,
                lease,
                item_key=key,
                status=status,
                outcome=result,
                error_summary=error,
                now=now,
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
        now: datetime | Callable[[], datetime] | None,
        lease_is_locked: bool = False,
        _raw_work_budget=None,
    ) -> Mapping[str, Any]:
        budget_args = {} if _raw_work_budget is None else dict(_raw_work_budget=_raw_work_budget)
        if item_key.startswith(_SOURCE_RECEIPT_PREFIX):
            raise ValueError("source acquisition receipts require immutable insertion")
        if not lease_is_locked:
            self._lock_live_lease(connection, lease, now, **budget_args)
        statement = (
            select(durable_job_item)
            .where(
                durable_job_item.c.job_id == lease.job_id,
                durable_job_item.c.item_key == item_key,
            )
            .with_for_update()
        )
        existing = self._read_jsonb_row(connection, statement,
            (durable_job_item.c.job_id, durable_job_item.c.item_key), (durable_job_item.c.outcome,),
            _raw_work_budget, for_update=True)
        self._lock_live_lease(connection, lease, now, **budget_args)
        current = _utc_datetime("now", now() if callable(now) else now or datetime.now(timezone.utc))
        if (
            existing is not None
            and existing["status"] == status
            and existing["outcome"] == outcome
            and existing["error_summary"] == error_summary
        ):
            return existing
        if existing is None:
            return self._return_item_row(connection,
                durable_job_item.insert()
                .values(
                    job_id=lease.job_id,
                    item_key=item_key,
                    status=status,
                    attempts=1,
                    outcome=outcome,
                    error_summary=error_summary,
                    updated_at=current,
                    finished_at=current,
                ), _raw_work_budget)
        return self._return_item_row(connection,
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
                updated_at=current,
                finished_at=current,
            ), _raw_work_budget)

    @staticmethod
    def _item_outcome_on_connection(connection: Connection, job_id: int, item_key: str, *, _raw_work_budget=None):
        statement = select(durable_job_item).where(
            durable_job_item.c.job_id == job_id,
            durable_job_item.c.item_key == item_key,
        )
        return DurableJobRepository._read_jsonb_row(connection, statement,
            (durable_job_item.c.job_id, durable_job_item.c.item_key), (durable_job_item.c.outcome,), _raw_work_budget)

    def _record_source_receipt_on_connection(
        self, connection: Connection, lease: LeaseToken, *, item_key: str,
        outcome: Mapping[str, Any], now: Callable[[], datetime],
        _raw_work_budget=None,
    ) -> Mapping[str, Any]:
        budget_args = {} if _raw_work_budget is None else dict(_raw_work_budget=_raw_work_budget)
        if not item_key.startswith(_SOURCE_RECEIPT_PREFIX):
            raise ValueError("source receipt requires its reserved namespace")
        value = _json_object("source receipt", outcome)
        self._lock_live_lease(connection, lease, now, **budget_args)
        existing = self._item_outcome_on_connection(connection, lease.job_id, item_key, **budget_args)
        self._lock_live_lease(connection, lease, now, **budget_args)
        if existing is not None:
            # Canonical text keeps JSON booleans and equal-valued floats distinct.
            if (existing["status"] != "succeeded" or existing["error_summary"] is not None
                    or existing["attempts"] != 1
                    or json.dumps(existing["outcome"], sort_keys=True, separators=(",", ":"))
                    != json.dumps(value, sort_keys=True, separators=(",", ":"))):
                raise JobIdentityConflict("source acquisition receipt is immutable")
            return existing
        current = _utc_datetime("receipt clock", now())
        return self._return_item_row(connection, durable_job_item.insert().values(
            job_id=lease.job_id, item_key=item_key, status="succeeded", attempts=1,
            outcome=value, error_summary=None, updated_at=current, finished_at=current,
        ), _raw_work_budget)

    def complete(
        self,
        lease: LeaseToken,
        *,
        final_checkpoint: Mapping[str, Any] | None = None,
        now: datetime | Callable[[], datetime] | None = None,
    ) -> DurableJobRecord:
        checkpoint = (
            _json_object("final checkpoint", final_checkpoint)
            if final_checkpoint is not None
            else None
        )
        current = _utc_datetime("now", now() if callable(now) else now or datetime.now(timezone.utc))
        with self.engine.begin() as connection:
            self._lock_live_lease(connection, lease, now)
            current = _utc_datetime("now", now() if callable(now) else now or datetime.now(timezone.utc))
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
        now: datetime | Callable[[], datetime] | None = None,
    ) -> DurableJobRecord:
        error = _required_error_summary(error_summary)
        checkpoint = (
            _json_object("final checkpoint", final_checkpoint)
            if final_checkpoint is not None
            else None
        )
        current = _utc_datetime("now", now() if callable(now) else now or datetime.now(timezone.utc))
        with self.engine.begin() as connection:
            locked = self._lock_live_lease(connection, lease, now)
            current = _utc_datetime("now", now() if callable(now) else now or datetime.now(timezone.utc))
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

    def defer(
        self,
        lease: LeaseToken,
        *,
        reason: str,
        now: datetime | Callable[[], datetime] | None = None,
    ) -> DurableJobRecord:
        summary = _required_error_summary(reason)
        current = _utc_datetime("now", now() if callable(now) else now or datetime.now(timezone.utc))
        with self.engine.begin() as connection:
            locked = self._lock_live_lease(connection, lease, now)
            current = _utc_datetime("now", now() if callable(now) else now or datetime.now(timezone.utc))
            next_retry = self.retry_policy.retry_at(
                attempts=locked["ownership_generation"],
                now=current,
            )
            deadline = locked["deadline_at"]
            terminal = deadline is not None and next_retry >= deadline
            stored_summary = (
                f"job deadline stopped pending retry: {summary}"[:2000]
                if terminal
                else summary
            )
            row = connection.execute(
                update(durable_job)
                .where(durable_job.c.job_id == lease.job_id)
                .values(
                    status="failed" if terminal else "retry_wait",
                    attempts=(
                        locked["attempts"]
                        if terminal
                        else locked["attempts"] - 1
                    ),
                    owner=None,
                    lease_expires_at=None,
                    next_retry_at=None if terminal else next_retry,
                    error_summary=stored_summary,
                    updated_at=current,
                    finished_at=current if terminal else None,
                )
                .returning(*durable_job.c)
            ).mappings().one()
        return _job_record(row)

    def recover_expired(
        self,
        *,
        now: datetime | Callable[[], datetime] | None = None,
        limit: int = 100,
    ) -> tuple[DurableJobRecord, ...]:
        current = _utc_datetime("now", now() if callable(now) else now or datetime.now(timezone.utc))
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
        now: datetime | Callable[[], datetime] | None = None,
        _raw_work_budget=None,
    ) -> Mapping[str, Any]:
        token = _lease_token(lease)
        columns = tuple(column for column in durable_job.c if column.key not in {"parameters", "checkpoint"})
        statement = (
            select(durable_job) if _raw_work_budget is None else select(*columns)
        ).where(durable_job.c.job_id == token.job_id).with_for_update()
        row = self._read_jsonb_row(connection, statement, (durable_job.c.job_id,), (),
            _raw_work_budget, for_update=True)
        # Evaluate the live clock after FOR UPDATE, which may have waited past expiry.
        current = _utc_datetime("lease clock", now() if callable(now) else now or datetime.now(timezone.utc))
        if (
            row is None
            or row["status"] != "running"
            or row["owner"] != token.owner
            or row["ownership_generation"] != token.generation
            or row["lease_expires_at"] <= current
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


def _bounded_schedule_text(name, value, maximum):
    if type(value) is not str or len(value) > maximum:
        raise ValueError(f"{name} must be text within{maximum} codepoints")
    result = _required_text(name, value)
    if result != value or "\0" in result or len(result.encode("utf-8")) > maximum:
        raise ValueError(f"{name} must be canonical text within{maximum} UTF8 bytes")
    return result


def _schedule_datetime(name, value):
    try:
        return _utc_datetime(name, value)
    except OverflowError as error:
        raise ValueError(f"{name} cannot be represented in UTC") from error


def _bounded_schedule_parameters(value):
    """Admit bounded strict JSON before allocating its detached canonical copy."""
    if type(value) is not dict:
        raise ValueError("schedule parameters must be a JSON object")
    stack, nodes = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 100000 or depth > 32:
            raise ValueError("schedule JSON exceeds node/depth limit")
        if type(item) is dict:
            if len(item) > 10000:
                raise ValueError("schedule JSON object exceeds10000 entries")
            if len(stack) + 2 * len(item) > 100000 - nodes:
                raise ValueError("schedule JSON pending work exceeds node limit")
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("schedule JSON requires exact string object keys")
                stack.extend(((key, depth + 1), (child, depth + 1)))
        elif type(item) is list:
            if len(item) > 10000:
                raise ValueError("schedule JSON array exceeds10000 entries")
            if len(stack) + len(item) > 100000 - nodes:
                raise ValueError("schedule JSON pending work exceeds node limit")
            stack.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            if len(item) > 65536 or len(item.encode("utf-8")) > 65536:
                raise ValueError("schedule JSON text exceeds64KiB")
        elif type(item) is int:
            if not -2**63 <= item < 2**63:
                raise ValueError("schedule JSON integer exceeds signed64bit")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("schedule JSON number must be finite")
        elif item is not None and type(item) is not bool:
            raise ValueError("schedule JSON contains unsupported value")
    parts, size = [], 0
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)
    for part in encoder.iterencode(value):
        size += len(part.encode("utf-8"))
        if size > MAX_SCHEDULE_PARAMETER_BYTES:
            raise ValueError("schedule JSON parameters exceed1MiB")
        parts.append(part)
    canonical = "".join(parts)
    return json.loads(canonical), canonical


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
