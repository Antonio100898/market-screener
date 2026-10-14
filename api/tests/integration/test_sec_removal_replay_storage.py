"""Exact witnessed removal replay through the existing source writer."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from time import monotonic, sleep

import pytest
from sqlalchemy import select, text

from screener.durable_jobs import DurableJobRepository, LeaseLost
from screener.postgres import source_item
from screener.source_observations import SourceItemIdentity, SourceObservationRepository
from test_publication_storage import storage, NOW, _claim, pytestmark
from test_source_delivery_storage import state
from test_source_observation_storage import _sec_filing_metadata


def _removed(storage):
    engine, artifact = storage
    lease = _claim(engine, "removal-replay")
    repo = SourceObservationRepository(engine, clock=lambda: NOW)
    inventory = repo.record(
        lease, item=SourceItemIdentity("SEC", "inventory", "quarterly-index/2026Q3"),
        item_outcome_key="inventory", state="present", metadata={"quarter": "2026Q3"},
        artifact_sha256=artifact.content_sha256,
        source_url="https://www.sec.gov/Archives/edgar/full-index/2026/QTR3/form.idx",
    )
    accession = "0000001800-26-000001"
    prior = repo.record(
        lease, item=SourceItemIdentity("SEC", "filing", accession, "0000001800"),
        item_outcome_key="prior", state="present", metadata=_sec_filing_metadata(accession),
        artifact_sha256=artifact.content_sha256, source_url="https://www.sec.gov/prior",
    )
    args = dict(
        prior_observation_id=prior.observation.source_observation_id,
        item_outcome_key="removed", quarter="2026Q3",
        inventory_source_item_id=inventory.item.source_item_id,
        inventory_observation_id=inventory.observation.source_observation_id,
        inventory_observation_sha256=inventory.observation.observation_sha256,
        inventory_artifact_sha256=artifact.content_sha256,
        detected_at=NOW + timedelta(seconds=1),
    )
    removed = repo.record_witnessed_sec_removal(lease, **args)
    return repo, lease, prior, removed, args


@pytest.mark.parametrize("later_event", [False, True])
def test_exact_removal_replay_returns_original_delivery_without_rewinding(storage, later_event):
    repo, lease, prior, removed, args = _removed(storage)
    if later_event:
        repo.record(
            lease, item=SourceItemIdentity("SEC", "filing", prior.item.source_key, "0000001800"),
            item_outcome_key="later", state="pending", metadata={"revision": "later"},
            source_url="https://www.sec.gov/later", detected_at=NOW + timedelta(seconds=2),
        )
    before = state(storage[0])
    replay = SourceObservationRepository(storage[0], clock=lambda: NOW).record_witnessed_sec_removal(
        lease, **{**args, "detected_at": NOW + timedelta(seconds=10)}
    )
    assert replay.observation == removed.observation
    assert replay.delivery == removed.delivery
    assert not replay.created and state(storage[0]) == before


@pytest.mark.parametrize("fault", ["key", "job", "generation", "witness", "new-witness"])
def test_stale_removal_requires_the_exact_retained_request(storage, fault):
    repo, lease, prior, removed, args = _removed(storage)
    expected = "not latest"
    if fault == "key":
        args = {**args, "item_outcome_key": "unseen"}
    elif fault == "job":
        lease = _claim(storage[0], "other-removal-request")
    elif fault == "generation":
        jobs = DurableJobRepository(storage[0])
        recovered = jobs.recover_expired(now=NOW + timedelta(hours=1))
        assert {job.job_id for job in recovered} == {lease.job_id}
        resumed_at = max(job.next_retry_at for job in recovered)
        repo = SourceObservationRepository(storage[0], clock=lambda: resumed_at)
        reclaimed = jobs.claim_next(
            owner="replacement", lease_duration=timedelta(minutes=5), now=resumed_at,
            allowed_kinds={"removal-replay"},
        )
        assert reclaimed.lease.job_id == lease.job_id and reclaimed.lease.generation > lease.generation
        lease = reclaimed.lease
    elif fault == "witness":
        args = {**args, "inventory_observation_sha256": "0" * 64}
        expected = "inventory witness is invalid"
    else:
        inventory = repo.record(
            lease, item=SourceItemIdentity("SEC", "inventory", "quarterly-index/2026Q3"),
            item_outcome_key="new-inventory", state="present",
            metadata={"quarter": "2026Q3", "revision": 2},
            artifact_sha256=storage[1].content_sha256,
            source_url="https://www.sec.gov/Archives/edgar/full-index/2026/QTR3/form.idx",
            detected_at=NOW + timedelta(seconds=2),
        )
        args = {**args, "inventory_observation_id": inventory.observation.source_observation_id,
                "inventory_observation_sha256": inventory.observation.observation_sha256}
    before = state(storage[0])
    with pytest.raises(ValueError, match=expected):
        repo.record_witnessed_sec_removal(lease, **args)
    assert state(storage[0]) == before


def test_removal_replay_late_outcome_failure_rolls_back(storage, monkeypatch):
    repo, lease, prior, removed, args = _removed(storage)
    before = state(storage[0])

    def fail(*args, **kwargs):
        raise RuntimeError("late replay outcome failure")

    monkeypatch.setattr(repo._jobs, "_record_item_outcome_on_connection", fail)
    with pytest.raises(RuntimeError, match="late replay outcome failure"):
        repo.record_witnessed_sec_removal(lease, **args)
    assert state(storage[0]) == before


def test_exact_removal_replay_still_requires_live_lease(storage):
    repo, lease, prior, removed, args = _removed(storage)
    before = state(storage[0])
    expired = SourceObservationRepository(storage[0], clock=lambda: NOW + timedelta(hours=1))
    with pytest.raises(LeaseLost):
        expired.record_witnessed_sec_removal(lease, **args)
    assert state(storage[0]) == before


@pytest.mark.parametrize("expires", [False, True])
def test_removal_replay_source_lock_wait_rechecks_live_clock(storage, expires):
    repo, lease, prior, removed, args = _removed(storage)
    engine = storage[0]
    began = monotonic()
    replay_repo = SourceObservationRepository(
        engine, clock=lambda: NOW + timedelta(seconds=monotonic() - began)
    )
    jobs = DurableJobRepository(engine)
    # Existing request identity remains the same; shorten only its live lease.
    jobs.renew(lease, lease_duration=timedelta(seconds=.5 if expires else 10), now=NOW)
    before = state(engine)
    with engine.connect() as blocker:
        transaction = blocker.begin()
        blocker.execute(select(source_item).where(
            source_item.c.source_item_id == prior.item.source_item_id
        ).with_for_update()).one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(replay_repo.record_witnessed_sec_removal, lease, **args)
            try:
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                    deadline = monotonic() + 3
                    while not observer.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event_type='Lock'"
                    )):
                        assert monotonic() < deadline and not future.done()
                        sleep(.01)
                sleep(.65 if expires else .02)
            finally:
                transaction.rollback()
            if expires:
                with pytest.raises(LeaseLost):
                    future.result(5)
            else:
                result = future.result(5)
                assert result.observation == removed.observation and result.delivery == removed.delivery
    assert state(engine) == before
