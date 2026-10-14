"""Prepared UUID SQL/S3 opt-in continuation; transport is synthetic, never SEC HTTP."""
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from screener import postgres
from screener.artifacts import SqlArtifactRepository
from screener.durable_jobs import DurableJobRepository
from screener.sec_ingestion import SecResourceFetchHandler, SecResourcePending
from screener.source_observations import SourceObservationRepository
from screener.sources.edgar import NoXbrlDataError
from test_sec_ingestion_storage import (storage, database_url, pytestmark, NOW, LEASE, _context,
    _seed_resource_parent, _enqueue_resource_job, _resource_responses, _resource_urls, FixtureEdgar)


class BoundedFixtureEdgar(FixtureEdgar):
    def fetch(self, url, *, maximum_bytes):
        response = super().fetch(url)
        assert len(response.data) <= maximum_bytes
        return response


def setup(storage, key):
    engine, objects = storage
    jobs = DurableJobRepository(engine)
    parent = _seed_resource_parent(engine, objects, jobs, key)
    claim = _enqueue_resource_job(jobs, parent, key)
    clock = NS(now=NOW)
    source = SourceObservationRepository(engine, clock=lambda:clock.now)
    transport = BoundedFixtureEdgar(_resource_responses())
    resource = SecResourceFetchHandler(edgar=transport, object_store=objects,
        artifacts=SqlArtifactRepository(engine), observations=source, jobs=jobs,
        clock=lambda:clock.now, annotate_annual=True, retain_partial_acquisitions=True)
    return NS(engine=engine, objects=objects, jobs=jobs, parent=parent, claim=claim,
              clock=clock, source=source, transport=transport, resource=resource)


def source_state(engine):
    with engine.connect() as connection:
        return [connection.execute(select(table).order_by(*table.primary_key)).mappings().all() for table in
                (postgres.source_item, postgres.source_observation, postgres.source_check_occurrence)]


def checkpoint(f, job_id):
    with f.engine.connect() as connection:
        return connection.scalar(select(postgres.durable_job.c.checkpoint).where(postgres.durable_job.c.job_id == job_id))


def test_actual_commit_before_return_reclaim_keeps_original_ready_delivery(storage, monkeypatch):
    f = setup(storage, "partial-commit-reclaim")
    original = f.source.record_with_acquisition_receipt
    acquired = []
    def interrupt(lease, **values):
        result = original(lease, **values)
        acquired.append(result)
        raise RuntimeError("after source receipt commit")
    monkeypatch.setattr(f.source, "record_with_acquisition_receipt", interrupt)
    with pytest.raises(RuntimeError, match="after source receipt"):
        f.resource(_context(f.claim, f.jobs))
    assert len(acquired) == 1 and checkpoint(f, f.claim.job.job_id) is None
    expired = f.jobs.recover_expired(now=NOW+timedelta(minutes=6))
    f.clock.now = next(row.next_retry_at for row in expired if row.job_id == f.claim.job.job_id)
    reclaimed = f.jobs.claim_next(owner="partial-reclaimed", lease_duration=LEASE, now=f.clock.now,
                                 allowed_kinds={f.claim.job.kind})
    monkeypatch.setattr(f.source, "record_with_acquisition_receipt", original)
    first_url = _resource_urls()["complete_submission"]
    f.transport.responses[first_url] = NoXbrlDataError("must not refetch")
    result = f.resource(_context(reclaimed, f.jobs, clock=lambda:f.clock.now))
    first = acquired[0]
    assert f.transport.urls.count(first_url) == 1
    assert result["roots"]["complete_submission"] == first.observation.observation_sha256
    assert result["root_deliveries"]["complete_submission"]["occurrence_id"] == first.delivery.occurrence_id
    assert result["root_deliveries"]["complete_submission"]["ownership_generation"] == 1
    assert checkpoint(f, reclaimed.job.job_id) == result


def test_actual_two_producers_reuse_source_revision_with_distinct_original_deliveries(storage):
    f = setup(storage, "partial-dedupe-first")
    first = f.resource(_context(f.claim, f.jobs))
    second_claim = _enqueue_resource_job(f.jobs, f.parent, "partial-dedupe-second")
    second = f.resource(_context(second_claim, f.jobs))
    assert first["roots"] == second["roots"]
    for role in first["roots"]:
        d1, d2 = first["root_deliveries"][role], second["root_deliveries"][role]
        assert (d1["source_item_id"], d1["source_observation_id"]) == (d2["source_item_id"], d2["source_observation_id"])
        assert d1["occurrence_id"] != d2["occurrence_id"] and d1["job_id"] != d2["job_id"]
        with f.engine.connect() as connection:
            outcomes = connection.execute(select(postgres.durable_job_item.c.outcome).where(
                postgres.durable_job_item.c.job_id.in_([f.claim.job.job_id, second_claim.job.job_id]),
                postgres.durable_job_item.c.item_key == "source-acquisition-receipt:sec-resource-partial:1:ready:"+role)).scalars().all()
        assert len(outcomes) == 2 and outcomes[0]["request_identity_sha256"] != outcomes[1]["request_identity_sha256"]


def test_actual_not_found_disposition_survives_same_claim_then_new_generation_progresses(storage):
    f = setup(storage, "partial-failure")
    url = _resource_urls()["submissions"]
    replacement = f.transport.responses[url]
    f.transport.responses[url] = NoXbrlDataError("original absent report")
    with pytest.raises(SecResourcePending) as first:
        f.resource(_context(f.claim, f.jobs))
    before = source_state(f.engine)
    f.transport.responses[url] = replacement
    with pytest.raises(SecResourcePending) as replay:
        f.resource(_context(f.claim, f.jobs))
    assert str(replay.value) == str(first.value) and source_state(f.engine) == before
    assert f.transport.urls.count(url) == 1
    deferred = f.jobs.defer(f.claim.lease, reason=str(first.value), now=NOW)
    retry = deferred.next_retry_at
    f.clock.now = retry
    new = f.jobs.claim_next(owner="partial-new-generation", lease_duration=LEASE, now=retry,
                           allowed_kinds={f.claim.job.kind})
    result = f.resource(_context(new, f.jobs, clock=lambda:retry))
    assert result["root_deliveries"]["complete_submission"]["ownership_generation"] == 1
    assert result["root_deliveries"]["submissions"]["ownership_generation"] == new.lease.generation
    assert f.transport.urls.count(url) == 2
