"""Atomic acquisition recovery using real storage over labelled synthetic sources."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import timedelta, timezone
from time import monotonic, sleep

import pytest
from sqlalchemy import select, text, update

from screener import source_observations as owner
from screener.durable_jobs import DurableJobRepository, LeaseLost, _SOURCE_RECEIPT_PREFIX
from screener.postgres import durable_job_item, source_item
from screener.source_observations import SourceItemIdentity, SourceObservationRepository, SourceObservationIdentityConflict
from test_publication_storage import storage, NOW, LEASE, _claim, pytestmark
from test_source_delivery_storage import current, state


REQUEST = "a" * 64


def acquire(storage, lease, *, key="inventory:ticker-map", kind="inventory", clock=lambda: NOW, **changes):
    engine, artifact = storage
    identity = (SourceItemIdentity("SEC", "inventory", "synthetic-ticker-map") if kind == "inventory"
        else SourceItemIdentity("SEC", "filing", "0000001800-26-000001", "0000001800"))
    values = dict(receipt_key=key, request_identity_sha256=REQUEST, item=identity,
        item_outcome_key="source:" + key, state="present", metadata={"fixture": "synthetic", "revision": 1},
        artifact_sha256=artifact.content_sha256, source_url="https://www.sec.gov/synthetic-source",
        etag="synthetic-etag", last_modified="synthetic-last-modified", detected_at=NOW)
    values.update(changes)
    return SourceObservationRepository(engine, clock=clock).record_with_acquisition_receipt(lease, **values)


def recover(engine, lease, *, key="inventory:ticker-map", **changes):
    values = dict(producer_job_id=lease.job_id, receipt_key=key, request_identity_sha256=REQUEST)
    values.update(changes)
    return SourceObservationRepository(engine).acquisition_receipt(**values)


@pytest.mark.parametrize("kind,key", [("inventory", "inventory:ticker-map"), ("filing", "request:physical-row:7")])
def test_atomic_inventory_and_filing_receipt_exact_replay_and_legacy_outcome(storage, monkeypatch, kind, key):
    engine = storage[0]
    lease = _claim(engine, "source-acquisition:" + kind)
    first = acquire(storage, lease, kind=kind, key=key)
    before = state(engine)
    def forbidden(*args, **kwargs):
        pytest.fail("receipt recovery consulted current selection")
    monkeypatch.setattr(owner, "current_observation_id", forbidden)
    monkeypatch.setattr(owner, "current_observations", forbidden)
    recovered = recover(engine, lease, key=key)
    replay = acquire(storage, lease, kind=kind, key=key, detected_at=NOW + timedelta(seconds=1))
    assert first.created and not recovered.created and not replay.created
    assert recovered.item == replay.item == first.item
    assert recovered.observation == replay.observation == first.observation
    assert asdict(recovered.delivery) == asdict(replay.delivery) == asdict(first.delivery)
    assert state(engine) == before
    with engine.connect() as connection:
        outcomes = dict(connection.execute(select(durable_job_item.c.item_key, durable_job_item.c.outcome)
            .where(durable_job_item.c.job_id == lease.job_id)).all())
    assert outcomes["source:" + key] == dict(source_item_id=first.item.source_item_id,
        source_observation_id=first.observation.source_observation_id,
        observation_sha256=first.observation.observation_sha256)
    assert outcomes[_SOURCE_RECEIPT_PREFIX + key]["delivery"] == {
        **asdict(first.delivery), "checked_at": first.delivery.checked_at.astimezone(timezone.utc).isoformat()}
    with pytest.raises(ValueError, match="immutable insertion"):
        DurableJobRepository(engine).record_item_outcome(lease,
            item_key=_SOURCE_RECEIPT_PREFIX + key, status="succeeded",
            outcome=outcomes[_SOURCE_RECEIPT_PREFIX + key])
    assert state(engine) == before
    recovered.observation.canonical_metadata["revision"] = "caller-mutation"
    assert recover(engine, lease, key=key).observation == first.observation
    assert state(engine) == before


def test_failure_after_source_record_before_receipt_commit_rolls_back_everything(storage, monkeypatch):
    engine = storage[0]
    lease = _claim(engine, "receipt-crash")
    before = state(engine)
    def crash(*args, **kwargs):
        # The source outcome really exists in this transaction, but is not committed.
        connection = args[1]
        assert connection.scalar(select(durable_job_item.c.outcome).where(
            durable_job_item.c.job_id == lease.job_id, durable_job_item.c.item_key == "source:inventory:ticker-map"))
        raise RuntimeError("synthetic crash before receipt commit")
    monkeypatch.setattr(DurableJobRepository, "_record_source_receipt_on_connection", crash)
    with pytest.raises(RuntimeError, match="before receipt commit"):
        acquire(storage, lease)
    assert state(engine) == before
    assert recover(engine, lease) is None


def test_reclaim_and_later_equal_byte_occurrence_keep_distinct_original_deliveries(storage):
    engine = storage[0]
    lease = _claim(engine, "receipt-reclaim")
    first = acquire(storage, lease)
    later = NOW + timedelta(minutes=6)
    jobs = DurableJobRepository(engine)
    expired = jobs.recover_expired(now=later)
    assert len(expired) == 1
    later = expired[0].next_retry_at
    claimed = jobs.claim_next(owner="receipt-new-owner", lease_duration=LEASE,
        now=later, allowed_kinds={"receipt-reclaim"})
    assert claimed.lease.job_id == lease.job_id and claimed.lease.generation == lease.generation + 1
    before = state(engine)
    assert acquire(storage, claimed.lease, clock=lambda: later).delivery == first.delivery
    assert recover(engine, claimed.lease).delivery == first.delivery
    assert state(engine) == before
    with pytest.raises(LeaseLost):
        acquire(storage, lease, clock=lambda: later)
    next_lease = _claim(engine, "receipt-later-occurrence")
    second = acquire(storage, next_lease, detected_at=NOW + timedelta(seconds=1))
    assert not second.created and second.observation == first.observation
    assert second.delivery.job_id != first.delivery.job_id
    assert second.delivery.occurrence_id != first.delivery.occurrence_id
    pointer = current(engine)
    assert recover(engine, lease).delivery == first.delivery
    assert recover(engine, next_lease).delivery == second.delivery
    assert current(engine) == pointer


@pytest.mark.parametrize("changes", [
    {"request_identity_sha256": "c" * 64}, {"metadata": {"revision": 2}},
    {"item": SourceItemIdentity("SEC", "inventory", "other-source")},
    {"item_outcome_key": "other-source-outcome"}, {"etag": "other-etag"},
])
def test_same_receipt_refuses_changed_request_or_source_without_new_delivery(storage, changes):
    engine = storage[0]
    lease = _claim(engine, "receipt-conflict")
    acquire(storage, lease)
    before = state(engine)
    with pytest.raises(SourceObservationIdentityConflict):
        acquire(storage, lease, **changes)
    assert state(engine) == before


@pytest.mark.parametrize("path,value", [
    (("receipt_revision",), True), (("receipt_revision",), 1.0),
    (("receipt_key",), "request:other"), (("request_identity_sha256",), "c" * 64),
    (("delivery", "ownership_generation"), True), (("delivery", "ownership_generation"), 1.0),
    (("delivery", "ownership_generation"), 99), (("delivery", "item_key"), "different-original-key"),
    (("delivery", "checked_at"), (NOW + timedelta(seconds=1)).isoformat()),
    (("source_item", "source_key"), "different-source"), (("unexpected",), "caller-claim"),
])
def test_malformed_or_mismatched_durable_receipt_refuses_read_and_write(storage, path, value):
    engine = storage[0]
    lease = _claim(engine, "receipt-corruption")
    acquire(storage, lease)
    with engine.begin() as connection:
        where = ((durable_job_item.c.job_id == lease.job_id)
            & (durable_job_item.c.item_key == _SOURCE_RECEIPT_PREFIX + "inventory:ticker-map"))
        payload = connection.scalar(select(durable_job_item.c.outcome).where(where))
        container = payload
        for name in path[:-1]:
            container = container[name]
        container[path[-1]] = value
        connection.execute(update(durable_job_item).where(where).values(outcome=payload))
    before = state(engine)
    with pytest.raises((ValueError, SourceObservationIdentityConflict)):
        recover(engine, lease)
    with pytest.raises((ValueError, SourceObservationIdentityConflict)):
        acquire(storage, lease)
    assert state(engine) == before


def test_same_key_concurrent_changed_writers_accept_only_one_immutable_receipt(storage):
    engine = storage[0]
    lease = _claim(engine, "receipt-concurrent")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(acquire, storage, lease, metadata={"revision": number}) for number in (1, 2)]
        results, conflicts = [], 0
        for future in futures:
            try:
                results.append(future.result(timeout=10))
            except SourceObservationIdentityConflict:
                conflicts += 1
    assert len(results) == conflicts == 1
    recovered = recover(engine, lease)
    assert recovered.observation == results[0].observation and recovered.delivery == results[0].delivery
    assert SourceObservationRepository(engine).history(results[0].item.source_item_id) == (results[0].observation,)


def test_expiry_after_actual_source_lock_wait_rolls_back_receipt_source_and_outcome(storage):
    engine = storage[0]
    seed_lease = _claim(engine, "receipt-lock-seed")
    seed = acquire(storage, seed_lease)
    lease = _claim(engine, "receipt-lock-writer")
    clock = [NOW]
    before = state(engine)
    with engine.connect() as blocker:
        transaction = blocker.begin()
        blocker.execute(select(source_item.c.source_item_id).where(
            source_item.c.source_item_id == seed.item.source_item_id).with_for_update()).scalar_one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(acquire, storage, lease, clock=lambda: clock[0], metadata={"revision": 2})
            try:
                deadline = monotonic() + 5
                while True:
                    with engine.connect() as observer:
                        blocked = observer.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE datname=current_database() AND wait_event_type='Lock' "
                            "AND query LIKE '%source_item%' AND query LIKE '%FOR UPDATE%')"))
                    if blocked:
                        break
                    assert monotonic() < deadline and not future.done(), "writer did not reach source FOR UPDATE wait"
                    sleep(.01)
                clock[0] = NOW + timedelta(minutes=6)
                assert not future.done()
            finally:
                transaction.rollback()
            with pytest.raises(LeaseLost):
                future.result(timeout=10)
    assert state(engine) == before
    assert recover(engine, lease) is None
