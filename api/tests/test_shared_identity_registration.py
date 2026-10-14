"""Public identity transaction using the existing SQLite owner compatibility fixture."""
from dataclasses import fields
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from screener.durable_jobs import LeaseLost, LeaseToken
from screener.postgres import issuer, priced_security, company_snapshot, company_snapshot_artifact, current_company_snapshot
from screener.shared_companies import IdentityConflict, RegisteredIdentity
from test_shared_companies import repository, _count


VALUES = dict(issuer_source=" sec ", issuer_identifier=" 1066923 ", security_identifier=" sec:0001066923:primary-listed-security ")
LEASE = LeaseToken(7, "identity-worker", 1)


def live(repository, *, expired_at_end=False):
    calls = []
    repository._clock = lambda: datetime.now(timezone.utc)
    def lock(connection, lease, clock):
        assert connection.in_transaction() and lease is LEASE and clock is repository._clock
        if calls and not calls[-1].closed:
            assert connection is calls[-1]
        calls.append(connection)
        if expired_at_end and len(calls) == 2:
            assert connection.scalar(select(func.count()).select_from(issuer)) == 1
            assert connection.scalar(select(func.count()).select_from(priced_security)) == 1
            raise LeaseLost("simulated end-lease expiry")
    repository._jobs = SimpleNamespace(_lock_live_lease=lock)
    return calls


def test_register_replay_returns_only_stable_actual_ids_without_snapshot(repository, monkeypatch):
    calls = live(repository)
    def forbidden(*args, **kwargs):
        pytest.fail("identity registration reached snapshot/selection/artifact owner")
    for name in ("_put_snapshot", "_link_artifacts", "_select_current"):
        monkeypatch.setattr(repository, name, forbidden)
    first = repository.register_identity(LEASE, **VALUES)
    assert isinstance(first, RegisteredIdentity) and tuple(field.name for field in fields(first)) == ("issuer_id", "security_id")
    assert repository.register_identity(LEASE, **VALUES) == first
    assert len(calls) == 4
    with repository.engine.connect() as connection:
        row = connection.execute(select(issuer)).mappings().one()
        security = connection.execute(select(priced_security)).mappings().one()
    assert (row["source_system"], row["source_identifier"]) == ("SEC", "0001066923")
    assert (row["issuer_id"], security["security_id"]) == (first.issuer_id, first.security_id)
    assert security["security_identifier"] == "sec:0001066923:primary-listed-security"
    for table in (company_snapshot, company_snapshot_artifact, current_company_snapshot):
        assert _count(repository, table) == 0


def test_wrong_issuer_conflict_rolls_back_new_issuer(repository):
    live(repository)
    first = repository.register_identity(LEASE, **VALUES)
    with pytest.raises(IdentityConflict, match="priced security"):
        repository.register_identity(LEASE, **{**VALUES, "issuer_identifier": "12345"})
    assert _count(repository, issuer) == _count(repository, priced_security) == 1
    assert repository.register_identity(LEASE, **VALUES) == first


def test_end_lease_refusal_rolls_back_both_actual_identity_inserts(repository):
    live(repository, expired_at_end=True)
    with pytest.raises(LeaseLost, match="end-lease"):
        repository.register_identity(LEASE, **VALUES)
    assert _count(repository, issuer) == _count(repository, priced_security) == 0


def test_entry_lease_refusal_prevents_identity_writes(repository):
    def lost(*args, **kwargs):
        raise LeaseLost("entry lease lost")
    repository._jobs = SimpleNamespace(_lock_live_lease=lost)
    with pytest.raises(LeaseLost, match="entry"):
        repository.register_identity(LEASE, **VALUES)
    assert _count(repository, issuer) == _count(repository, priced_security) == 0


@pytest.mark.parametrize("lease", [None, object(), LeaseToken(True, "worker", 1), LeaseToken(0, "worker", 1),
    LeaseToken(2**63, "worker", 1), LeaseToken(1, "worker", 1.0), LeaseToken(1, "worker", 0),
    LeaseToken(1, None, 1), LeaseToken(1, "", 1), LeaseToken(1, "bad\0owner", 1)], ids=["none", "object", "bool-job", "zero-job", "large-job", "float-gen", "zero-gen", "null-owner", "empty-owner", "NUL-owner"])
def test_bad_lease_refuses_before_transaction(repository, monkeypatch, lease):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid scalar reached transaction")
    monkeypatch.setattr(repository.engine, "begin", forbidden)
    with pytest.raises((TypeError, ValueError)):
        repository.register_identity(lease, **VALUES)


@pytest.mark.parametrize("field,value", [("issuer_source", None), ("issuer_source", 1), ("issuer_source", ""),
    ("issuer_identifier", "bad-cik"), ("issuer_identifier", "x"*4097), ("security_identifier", None),
    ("security_identifier", False), ("security_identifier", " "), ("security_identifier", "bad\0id"),
    ("security_identifier", "é"*2049)], ids=["null-source", "number-source", "empty-source", "bad-cik", "long-cik", "null-security", "bool-security", "empty-security", "NUL-security", "UTF8-security"])
def test_bad_identity_scalars_refuse_before_transaction(repository, monkeypatch, field, value):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid scalar reached transaction")
    monkeypatch.setattr(repository.engine, "begin", forbidden)
    with pytest.raises(ValueError):
        repository.register_identity(LEASE, **{**VALUES, field: value})
