"""Prepared real plan/runtime acquisition only; synthetic transport, stage stays pending."""
from datetime import datetime, timedelta, timezone
import json
from threading import Event
from time import monotonic, sleep

import pytest
from sqlalchemy import select

from screener import automatic_ingestion as automatic, sec_inventory_plan as inventory
from screener.durable_jobs import DurableJobRepository, RetryPolicy
from screener.postgres import durable_job, company_snapshot, exact_figure_bundle
from screener.sec_ingestion import SecResourceFetchHandler, SecResourcePending
from screener.source_observations import SourceObservationRepository
from screener.sources.edgar import EdgarTransportResult, TICKER_MAP_URL
from screener.storage_config import AutomaticIngestionSettings
from test_sec_ingestion_storage import storage, database_url, pytestmark
from test_automatic_ingestion_storage import fixture_responses, RetainedTransport


class BoundedTransport(RetainedTransport):
    def fetch(self, url, *, maximum_bytes=None):
        result = super().fetch(url)
        if maximum_bytes is not None: assert len(result.data) <= maximum_bytes
        return result


def wait_rows(engine, predicate, timeout=15):
    deadline = monotonic()+timeout
    while monotonic() < deadline:
        with engine.connect() as connection:
            rows = connection.execute(select(durable_job).order_by(durable_job.c.job_id)).mappings().all()
        assert not any(row["status"] == "failed" for row in rows), [(r["kind"], r["error_summary"]) for r in rows]
        if predicate(rows): return rows
        sleep(.02)
    raise AssertionError([(r["kind"],r["status"],r["error_summary"]) for r in rows])


def test_actual_registered_plan_receipt_interruption_drain_reclaim_and_exact_stage_association(storage, tmp_path, monkeypatch):
    engine, objects = storage
    instant = datetime.now(timezone.utc)
    params = dict(request_revision=2, operation_key="actual-partial-caller", operation_at=instant.isoformat(),
        start_date="2026-09-28", end_date="2026-09-28", expected_parent_release_id=None,
        producer_build_sha256=inventory.producer_build_sha256())
    path = tmp_path/"explicit-v2.json"
    key = "automatic-ingestion:v1:sec-finite-inventory-v2:actual-partial-caller"
    path.write_text(json.dumps(dict(plan_revision=1, occurrences=[dict(kind=inventory.PARTIAL_KIND,
        parameters=params, schedule_key=key, scheduled_for=instant.isoformat(), due_at=instant.isoformat())])))
    settings = AutomaticIngestionSettings(True, path, tmp_path/"dedicated-cache")
    responses = fixture_responses()
    responses[TICKER_MAP_URL] = EdgarTransportResult(b'{"0":{"cik_str":1234,"ticker":"FIX","title":"Fixture"}}',
        TICKER_MAP_URL, "application/json", "synthetic-listing", None)
    edgar = BoundedTransport(responses)
    retry_policy = RetryPolicy(base_delay=timedelta(seconds=2), max_delay=timedelta(seconds=2),
                               jitter_seconds=lambda cap:cap)
    monkeypatch.setattr(automatic, "DurableJobRepository",
                        lambda actual_engine:DurableJobRepository(actual_engine, retry_policy=retry_policy))
    committed = []
    ready_committed, ready_release = Event(), Event()
    write = SourceObservationRepository.record_with_acquisition_receipt
    def interrupt(self, lease, **values):
        result = write(self, lease, **values)
        if values["receipt_key"] == "sec-resource-partial:1:ready:complete_submission" and not committed:
            committed.append(result)
            ready_committed.set()
            assert ready_release.wait(15), "test must release committed-ready owner after closing claim gate"
            raise SecResourcePending("synthetic runtime interruption after ready receipt commit")
        return result
    monkeypatch.setattr(SourceObservationRepository, "record_with_acquisition_receipt", interrupt)
    assembly = automatic.AutomaticIngestion(settings, engine=engine, s3=objects.client, edgar=edgar)
    assembly.start()
    try:
        assert ready_committed.wait(15)
        assert assembly.stop(timeout=.05) is False
    finally:
        ready_release.set()
        assert assembly.stop()
    rows = wait_rows(engine, lambda rs:any(r["kind"] == "sec-resource-fetch-v3" and r["status"] == "retry_wait" for r in rs))
    assert len(committed) == 1
    original = committed[0]
    source_url = original.observation.canonical_metadata["partial_acquisition"]["requested_url"]
    first_reads = edgar.urls.count(source_url)
    resource_row = next(r for r in rows if r["kind"] == "sec-resource-fetch-v3")
    assert resource_row["next_retry_at"] is not None
    stage_committed, release = Event(), Event()
    enqueue = DurableJobRepository.enqueue_child
    def stage_barrier(self, lease, **values):
        result = enqueue(self, lease, **values)
        if values["kind"] == "sec-candidate-stage":
            stage_committed.set()
            assert release.wait(15), "test must release owner enqueue after closing claim gate"
        return result
    monkeypatch.setattr(DurableJobRepository, "enqueue_child", stage_barrier)
    resumed = automatic.AutomaticIngestion(settings, engine=engine, s3=objects.client, edgar=edgar)
    resumed.start()
    try:
        assert stage_committed.wait(15)
        assert resumed.stop(timeout=.05) is False
    finally:
        release.set()
        assert resumed.stop()
    with engine.connect() as connection:
        rows = connection.execute(select(durable_job)).mappings().all()
        assert connection.scalar(select(company_snapshot.c.snapshot_id).limit(1)) is None
        assert connection.scalar(select(exact_figure_bundle.c.bundle_id).limit(1)) is None
    producer = next(r for r in rows if r["kind"] == inventory.PARTIAL_KIND)
    resource = next(r for r in rows if r["kind"] == "sec-resource-fetch-v3")
    stage = next(r for r in rows if r["kind"] == "sec-candidate-stage")
    assert producer["status"] == resource["status"] == "succeeded" and stage["status"] == "pending"
    assert resource["ownership_generation"] > original.delivery.ownership_generation
    assert edgar.urls.count(source_url) == first_reads
    jobs = DurableJobRepository(engine)
    producer_job = jobs.scheduled_job(job_id=producer["job_id"], occurrence_id=producer["occurrence_id"],
                                    schedule_key=key, scheduled_for=instant)
    handler = resumed.runtime._handlers[inventory.PARTIAL_KIND]
    root = handler.recover_completed(producer_job)
    assert root.contract == "SEC_INVENTORY_ENUMERATION2" and root.resource_request_count == 1
    result = handler.inspect_completed_entry(job_id=producer["job_id"], occurrence_id=producer["occurrence_id"],
        schedule_key=key, scheduled_for=instant, day="2026-09-28", entry_ordinal=0)
    assert result["capture"]["status"] == "VERIFIED" and result["stage"]["status"] == "MATCHED_ASSOCIATION"
    assert result["stage"]["job_id"] == stage["job_id"]
    assert result["capture"]["checkpoint"]["root_deliveries"]["complete_submission"]["occurrence_id"] == original.delivery.occurrence_id
