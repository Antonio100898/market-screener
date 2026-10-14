"""Real public stores must forward the operation budget into the existing lease reader."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from sqlalchemy.engine import Connection
from screener import sec_ingestion as sec
from screener.durable_jobs import LeaseToken
from screener.figure_history import FigureHistoryRepository
from screener.shared_companies import SharedCompanyRepository
from test_figure_history import example_bundle

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


class LegacyFullLeaseRead(RuntimeError):
    pass


@pytest.mark.parametrize("owner_kind", ["company", "history"])
@pytest.mark.parametrize("budgeted", [True, False])
def test_public_store_lease_budget_reaches_actual_preflight_before_payload(monkeypatch, owner_kind, budgeted):
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    connection = MagicMock(spec=Connection)
    connection.engine = engine
    connection.closed = False
    connection.in_transaction.return_value = True
    connection.get_transaction.return_value.is_active = True
    connection.connection.dbapi_connection.autocommit = False
    token = LeaseToken(1, "budget-owner", 1)
    reached = []
    class RefuseAtActualLeasePreflight(sec._AdmissionBudget):
        def sql_row_preflight(self, identifiers, length_count, maximum_rows=1):
            assert self is budget
            assert tuple((column.table.name, column.key) for column in identifiers) == (("durable_job", "job_id"),)
            reached.append("durable-preflight")
            raise sec._AdmissionCapacity("actual lease preflight capacity refusal")
    budget = RefuseAtActualLeasePreflight() if budgeted else None
    def forbidden_payload(statement, *args, **kwargs):
        columns = set(statement.selected_columns.keys())
        assert {"parameters", "checkpoint"}.issubset(columns)
        reached.append("default-full-JSONB-lease-read")
        raise LegacyFullLeaseRead("legacy unbudgeted lease SELECT reached before any transfer")
    connection.execute.side_effect = forbidden_payload
    repository = (SharedCompanyRepository(engine, clock=lambda: NOW) if owner_kind == "company"
                  else FigureHistoryRepository(engine, clock=lambda: NOW))
    actual_lock = repository._jobs._lock_live_lease
    forwarded = []
    def observe_lock(actual_connection, actual_lease, actual_clock, **kwargs):
        assert actual_connection is connection and actual_lease is token
        forwarded.append(kwargs)
        return actual_lock(actual_connection, actual_lease, actual_clock, **kwargs)
    monkeypatch.setattr(repository._jobs, "_lock_live_lease", observe_lock)
    expected = sec._AdmissionCapacity if budgeted else LegacyFullLeaseRead
    with pytest.raises(expected, match="actual lease preflight" if budgeted else "legacy unbudgeted"):
        if owner_kind == "company":
            repository.store_candidate_on_connection(connection, token, issuer_source="SEC", issuer_identifier="0001066923",
                security_identifier="sec:0001066923:primary-listed-security", ticker="FTFT", engine_revision=192,
                security_title=None, source_accession=None, security_basis="SEC_TICKER_MAPPING_ONLY",
                canonical_payload={"annual_eps": {"2020": 2.06}}, _raw_work_budget=budget)
        else:
            repository.store_on_connection(connection, token, request_key="fixture:lease-budget",
                bundle=example_bundle(), _raw_work_budget=budget)
    assert reached == (["durable-preflight"] if budgeted else ["default-full-JSONB-lease-read"])
    assert forwarded == ([{"_raw_work_budget": budget}] if budgeted else [{}])
    assert connection.execute.call_count == (0 if budgeted else 1)
    assert not connection.commit.called and not connection.rollback.called