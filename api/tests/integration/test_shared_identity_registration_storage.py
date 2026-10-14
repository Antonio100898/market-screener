"""Prepared real PostgreSQL identity-only transactions; no source or snapshot seed."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest
from sqlalchemy import create_engine, func, select, text

from screener.durable_jobs import DurableJobRepository, LeaseLost
from screener.postgres import issuer, priced_security, company_snapshot, company_snapshot_artifact, current_company_snapshot
from screener.shared_companies import SharedCompanyRepository, IdentityConflict
from test_durable_job_storage import database_url, pytestmark


@pytest.fixture
def identity_store(database_url):
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE current_company_snapshot, company_snapshot_artifact, company_snapshot, "
            "priced_security, issuer, durable_job_item, durable_job, job_schedule_occurrence RESTART IDENTITY CASCADE"))
    yield SharedCompanyRepository(engine), DurableJobRepository(engine)
    engine.dispose()


def claim(jobs, *, seconds=300):
    now = datetime.now(timezone.utc)
    job = jobs.enqueue_scheduled(schedule_key="identity-only-actual", scheduled_for=now, kind="identity-only-actual",
        parameters={"scope": "identity only"}, due_at=now, max_attempts=1)
    actual = jobs.claim_next(owner="identity-actual", allowed_kinds={"identity-only-actual"}, lease_duration=timedelta(seconds=seconds))
    assert actual.job.job_id == job.job_id
    return actual


def values(issuer_id="1066923"):
    return dict(issuer_source="SEC", issuer_identifier=issuer_id, security_identifier="sec:0001066923:primary-listed-security")


def counts(repository):
    with repository.engine.connect() as connection:
        return {table.name: connection.scalar(select(func.count()).select_from(table))
            for table in (issuer, priced_security, company_snapshot, company_snapshot_artifact, current_company_snapshot)}


def test_actual_leased_identity_replay_without_financial_snapshot(identity_store):
    repository, jobs = identity_store
    actual = claim(jobs)
    first = repository.register_identity(actual.lease, **values())
    assert type(first.issuer_id) is int and first.issuer_id > 0
    assert type(first.security_id) is int and first.security_id > 0
    assert repository.register_identity(actual.lease, **{**values(), "issuer_source": " sec ", "issuer_identifier": " 1066923 "}) == first
    with repository.engine.connect() as connection:
        actual_issuer = connection.execute(select(issuer)).mappings().one()
        actual_security = connection.execute(select(priced_security)).mappings().one()
    assert (actual_issuer["source_system"], actual_issuer["source_identifier"]) == ("SEC", "0001066923")
    assert (actual_issuer["issuer_id"], actual_security["security_id"]) == (first.issuer_id, first.security_id)
    assert (actual_security["issuer_id"], actual_security["security_identifier"]) == (first.issuer_id, values()["security_identifier"])
    assert counts(repository) == dict(issuer=1, priced_security=1, company_snapshot=0, company_snapshot_artifact=0, current_company_snapshot=0)
    jobs.complete(actual.lease)


def test_actual_foreign_issuer_conflict_rolls_back_stray_issuer(identity_store):
    repository, jobs = identity_store
    actual = claim(jobs)
    first = repository.register_identity(actual.lease, **values())
    with pytest.raises(IdentityConflict, match="priced security"):
        repository.register_identity(actual.lease, **values("12345"))
    assert counts(repository) == dict(issuer=1, priced_security=1, company_snapshot=0, company_snapshot_artifact=0, current_company_snapshot=0)
    assert repository.register_identity(actual.lease, **values()) == first
    jobs.complete(actual.lease)


def test_actual_end_lease_expiry_rolls_back_both_identities(identity_store, monkeypatch):
    repository, jobs = identity_store
    actual = claim(jobs, seconds=2)
    inserted, release = Event(), Event()
    original = repository._put_security
    def after_real_security(connection, data):
        row = original(connection, data)
        inserted.set()
        assert release.wait(10), "release actual expiry barrier"
        return row
    monkeypatch.setattr(repository, "_put_security", after_real_security)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(repository.register_identity, actual.lease, **values())
        try:
            assert inserted.wait(5), "real identity insert reached"
            remaining = max(0, (actual.job.lease_expires_at - datetime.now(timezone.utc)).total_seconds())
            Event().wait(remaining + .025)
        finally:
            release.set()
        with pytest.raises(LeaseLost):
            future.result(timeout=10)
    assert counts(repository) == dict(issuer=0, priced_security=0, company_snapshot=0, company_snapshot_artifact=0, current_company_snapshot=0)
