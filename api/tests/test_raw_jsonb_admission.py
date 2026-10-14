"""Unexecuted owning SQL payload frontier controls; memory SQL boundaries only."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from screener import sec_ingestion
from screener.durable_jobs import DurableJobRepository
from screener.postgres import durable_job_item, source_observation
from screener.source_observations import SourceObservationRepository


class _Result:
    def __init__(self, rows):
        self.rows = rows
    def mappings(self):
        return self
    def all(self):
        return self.rows
    def one_or_none(self):
        assert len(self.rows) <= 1
        return None if not self.rows else self.rows[0]


class _Connection:
    def __init__(self, *, size=2, identity=7, autocommit=False, payload_identity=None):
        self.engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self.connection = SimpleNamespace(dbapi_connection=SimpleNamespace(autocommit=autocommit))
        self.size, self.identity = size, identity
        self.payload_identity = identity if payload_identity is None else payload_identity
        self.calls = []
    def execute(self, statement):
        self.calls.append(statement)
        if any(column.key.startswith("_raw_bytes_") for column in statement.selected_columns):
            actual = dict(source_observation_id=self.identity, canonical_metadata={},
                          detected_at=datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
            projected = {}
            for column in statement.selected_columns:
                if column.key.startswith("_raw_bytes_"):
                    original = tuple(column.element.clauses)[0].clause
                    projected[column.key] = (self.size if original.key == "canonical_metadata"
                        else len(str(actual[original.key]).encode()))
                else:
                    projected[column.key] = actual[column.key]
            return _Result([projected])
        return _Result([dict(source_observation_id=self.payload_identity, canonical_metadata={})])


def _read(connection, budget, *, update=False):
    return DurableJobRepository._read_jsonb_row(connection,
        select(source_observation.c.source_observation_id, source_observation.c.canonical_metadata),
        (source_observation.c.source_observation_id,), (source_observation.c.canonical_metadata,),
        budget, for_update=update)


def test_owner_refuses_payload_transfer_at_actual_scalar_size_frontier():
    connection = _Connection(size=64 * 1024 * 1024)
    with pytest.raises(sec_ingestion._AdmissionCapacity):
        _read(connection, sec_ingestion._AdmissionBudget())
    assert len(connection.calls) == 1
    assert connection.calls[0]._for_update_arg.read is True


def test_owner_pins_selected_identity_before_payload():
    from sqlalchemy.sql import operators, visitors
    from sqlalchemy.sql.elements import BinaryExpression, BindParameter
    connection = _Connection(identity=7)
    assert _read(connection, sec_ingestion._AdmissionBudget())["source_observation_id"] == 7
    matches = [expression for criterion in connection.calls[1]._where_criteria
        for expression in visitors.iterate(criterion)
        if isinstance(expression, BinaryExpression) and expression.operator is operators.eq
        and isinstance(expression.right, BindParameter)]
    assert any(expression.left is source_observation.c.source_observation_id
        and expression.right.value == 7 for expression in matches)


def test_owner_refuses_changed_selected_identity_before_caller_codec():
    with pytest.raises(ValueError, match="changed after reservation"):
        _read(_Connection(identity=7, payload_identity=8), sec_ingestion._AdmissionBudget())


def test_owner_retains_strong_mutation_lock_before_sizing():
    connection = _Connection()
    _read(connection, sec_ingestion._AdmissionBudget(), update=True)
    assert connection.calls[0]._for_update_arg.read is False


def test_owner_refuses_autocommit_before_query():
    connection = _Connection(autocommit=True)
    with pytest.raises(ValueError, match="PostgreSQL transaction"):
        _read(connection, sec_ingestion._AdmissionBudget())
    assert connection.calls == []


def test_owner_none_path_keeps_single_original_query():
    connection = _Connection()
    result = _read(connection, None)
    assert result["canonical_metadata"] == {} and len(connection.calls) == 1
    assert connection.calls[0]._for_update_arg is None


def test_owner_refuses_integer_and_timestamp_payload_at_remaining_transfer_frontier():
    connection = _Connection()
    budget = sec_ingestion._AdmissionBudget(read_bytes=64 * 1024 * 1024 - 172)
    with pytest.raises(sec_ingestion._AdmissionCapacity):
        DurableJobRepository._read_jsonb_row(connection,
            select(source_observation.c.source_observation_id, source_observation.c.detected_at,
                   source_observation.c.canonical_metadata),
            (source_observation.c.source_observation_id,),
            (source_observation.c.canonical_metadata,), budget)
    assert len(connection.calls) == 1


def test_owner_item_return_quota_refuses_before_write():
    connection = _Connection()
    budget = sec_ingestion._AdmissionBudget(read_bytes=64 * 1024 * 1024)
    with pytest.raises(sec_ingestion._AdmissionCapacity):
        DurableJobRepository._return_item_row(connection, durable_job_item.insert(), budget)
    assert connection.calls == []


def test_owner_checkpoint_return_quota_refuses_before_update(monkeypatch):
    connection = _Connection()
    repository = DurableJobRepository(connection.engine)
    monkeypatch.setattr(repository, "_lock_live_lease", lambda *args, **kwargs: {})
    budget = sec_ingestion._AdmissionBudget(read_bytes=64 * 1024 * 1024)
    with pytest.raises(sec_ingestion._AdmissionCapacity):
        repository._save_checkpoint_on_connection(connection, SimpleNamespace(job_id=7), {},
            datetime(2026, 10, 10, 12, tzinfo=timezone.utc), _raw_work_budget=budget)
    assert connection.calls == []


def test_owner_observation_return_quota_refuses_before_insert(monkeypatch):
    connection = _Connection()
    repository = SourceObservationRepository(connection.engine)
    monkeypatch.setattr(repository._jobs, "_lock_live_lease", lambda *args, **kwargs: {})
    def locked_item(statement):
        connection.calls.append(statement)
        assert statement._for_update_arg is not None
        return SimpleNamespace(scalar_one=lambda: 7)
    connection.execute = locked_item
    budget = sec_ingestion._AdmissionBudget(read_bytes=64 * 1024 * 1024)
    current = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    with pytest.raises(sec_ingestion._AdmissionCapacity):
        repository._record_on_connection(connection, SimpleNamespace(job_id=7),
            item_row={"source_item_id": 7}, outcome_key="derived:annual.htm", digest="f" * 64,
            values={}, detected=current, now=current, _raw_work_budget=budget)
    assert len(connection.calls) == 1

@pytest.mark.parametrize("wrapper,budgeted", [("repository", True), ("repository", False),
                                           ("context", True), ("context", False)])
def test_actual_public_checkpoint_forwarding_contract(wrapper, budgeted, monkeypatch):
    from dataclasses import asdict, replace
    from threading import Event
    from unittest.mock import MagicMock
    from sqlalchemy.engine import Connection
    from screener.durable_jobs import LeaseToken
    from screener.job_runtime import JobContext
    from test_sec_ingestion import NOW, _job
    engine = object()
    repository = DurableJobRepository(engine)
    connection = MagicMock(spec=Connection)
    connection.closed = False
    connection.engine = engine
    connection.in_transaction.return_value = True
    connection.get_transaction.return_value = SimpleNamespace(is_active=True)
    connection.connection = SimpleNamespace(dbapi_connection=SimpleNamespace(autocommit=False))
    connection.execute.side_effect = AssertionError("public contract must not execute SQL")
    token = LeaseToken(7, "worker", 1)
    value = {"checkpoint_revision": 3, "parameters": {"scope": ["known"]}}
    job = replace(_job({}, kind="sec-retained-candidate-stage-v1"), job_id=7)
    returned = replace(job, checkpoint=value)
    calls = []
    clock = lambda: NOW
    budget = sec_ingestion._AdmissionBudget() if budgeted else None
    def private(actual_connection, actual_lease, actual_value, actual_clock, **kwargs):
        assert actual_connection is connection and actual_lease is token
        assert actual_value == value and actual_clock is clock
        assert kwargs == ({} if budget is None else {"_raw_work_budget": budget})
        if budget is not None:
            assert kwargs["_raw_work_budget"] is budget
        calls.append(True)
        return asdict(returned)
    monkeypatch.setattr(repository, "_save_checkpoint_on_connection", private)
    if wrapper == "repository":
        result = repository.save_checkpoint_on_connection(connection, token, value, now=clock,
            **({} if budget is None else {"_raw_work_budget": budget}))
    else:
        context = JobContext(job, Event(), repository, token, clock)
        result = context.save_checkpoint_on_connection(connection, value,
            **({} if budget is None else {"_raw_work_budget": budget}))
    assert result == returned and calls == [True]
    connection.execute.assert_not_called()