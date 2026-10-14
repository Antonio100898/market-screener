"""First publication through the existing owner; finances are fixture data."""
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event
from time import monotonic, sleep

import pytest
from sqlalchemy import func, select, text, update

from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.display_publication import DisplayReleasePreparer
from screener.durable_jobs import LeaseLost
from screener.postgres import (active_data_release, company_snapshot, current_company_snapshot, data_release,
    durable_job, publication_candidate, release_company, source_item, source_observation, source_item_current)
from screener.publication import PublicationRepository, PublicationIdentityConflict, StalePublicationParent, StalePublicationSource
from screener.source_observations import SourceItemIdentity, SourceObservationRepository
from test_publication_storage import storage, _claim, _company, _source, NOW
from test_display_publication_storage import _prepared, _input

pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1", reason="requires isolated PostgreSQL/S3")


def staged(storage, *, key="first"):
    engine, artifact = storage
    lease = _claim(engine, key)
    snapshot = _company(engine, artifact, lease=lease)
    source = _source(engine, artifact, lease)
    repository = PublicationRepository(engine, clock=lambda: NOW)
    candidate = repository.register_candidate(lease, request_key="candidate:" + key, snapshot_id=snapshot.snapshot_id,
        expected_parent_release_id=None, source_observation_ids=(source,), cause_type="source_change")
    preparer, objects, prepared, company = _prepared(storage, snapshot, source)
    return engine, lease, repository, snapshot, source, candidate, preparer, objects, prepared, company


def publish(parts, key="first-display", *, repository=None, lease=None, prepared=None):
    _, token, owner, _, _, candidate, _, objects, original, _ = parts
    return (repository or owner).publish_display(lease or token, request_key=key,
        candidate_ids=(candidate.candidate_id,), expected_parent_release_id=None,
        prepared=prepared or original, object_store=objects)


def unchanged_empty(engine):
    with engine.connect() as connection:
        assert connection.scalar(select(active_data_release.c.release_id)) is None
        assert connection.scalar(select(func.count()).select_from(data_release)) == 0
        assert connection.scalar(select(func.count()).select_from(release_company)) == 0
        assert connection.scalar(select(func.count()).select_from(current_company_snapshot)) == 0


def wait_for_lock(engine, future):
    deadline = monotonic() + 3
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        while not connection.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
            assert not future.done() and monotonic() < deadline
            sleep(.01)


def test_first_staged_display_replay_reopen_and_no_legacy_selection(storage):
    parts = staged(storage)
    engine, lease, repository, snapshot, source, candidate, _, objects, prepared, _ = parts
    first = publish(parts)
    assert first.parent_release_id is None and first.member_count == 1 and first.projection_revision == prepared.projection_revision
    reopened = PublicationRepository(engine, clock=lambda: NOW)
    assert publish(parts, repository=reopened) == first
    assert reopened.register_candidate(lease, request_key=candidate.request_key, snapshot_id=snapshot.snapshot_id,
        expected_parent_release_id=None, source_observation_ids=(source,), cause_type="source_change") == candidate
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(current_company_snapshot)) == 0
        assert connection.scalar(select(release_company.c.exact_bundle_id)) is None
    with pytest.raises(StalePublicationParent):
        reopened.register_candidate(lease, request_key="late-new-initial", snapshot_id=snapshot.snapshot_id,
            expected_parent_release_id=None, source_observation_ids=(source,), cause_type="source_change")
    with pytest.raises(PublicationIdentityConflict):
        reopened.register_candidate(lease, request_key=candidate.request_key, snapshot_id=snapshot.snapshot_id,
            expected_parent_release_id=first.release_id, source_observation_ids=(source,), cause_type="source_change")
    child_candidate = reopened.register_candidate(lease, request_key="positive-parent", snapshot_id=snapshot.snapshot_id,
        expected_parent_release_id=first.release_id, source_observation_ids=(source,), cause_type="source_change")
    child = reopened.publish_display(lease, request_key="positive-child", candidate_ids=(child_candidate.candidate_id,),
        expected_parent_release_id=first.release_id, prepared=prepared, object_store=objects)
    assert child.parent_release_id == first.release_id
    assert publish(parts, repository=reopened) == first


def test_first_cohort_has_only_staged_members_and_complete_projection(storage):
    parts = staged(storage)
    engine, lease, repository, snapshot, source, candidate, preparer, objects, _, company = parts
    other = _company(engine, storage[1], ticker="XYZ", identifier="2000", lease=lease)
    other_source = _source(engine, storage[1], lease, identifier="2000", key="filing-2")
    registered = repository.register_candidate(lease, request_key="other", snapshot_id=other.snapshot_id,
        expected_parent_release_id=None, source_observation_ids=(other_source,), cause_type="source_change")
    _, _, other_input = _input(storage, other, other_source, ticker="XYZ", cik="0000002000")
    full = preparer.prepare([company, other_input], capture_mode="caller_captured_enrichment", limitations=["Synthetic financial fixture"])
    first = repository.publish_display(lease, request_key="cohort", candidate_ids=(candidate.candidate_id, registered.candidate_id),
        expected_parent_release_id=None, prepared=full, object_store=objects)
    assert first.member_count == 2
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(current_company_snapshot)) == 0


@pytest.mark.parametrize("problem", ["empty", "missing-candidate", "wrong-membership", "corrupt-object", "projection-failure"])
def test_first_failures_leave_no_release_or_selection(storage, monkeypatch, problem):
    parts = staged(storage)
    engine, lease, repository, _, _, candidate, _, objects, prepared, _ = parts
    ids = () if problem == "empty" else (99999,) if problem == "missing-candidate" else (candidate.candidate_id,)
    if problem == "wrong-membership":
        other = _company(engine, storage[1], value=95, lease=lease)
        _, _, prepared, _ = _prepared(storage, other, parts[4])
    elif problem == "corrupt-object":
        monkeypatch.setattr(objects, "read_verified", lambda *args: (_ for _ in ()).throw(ValueError("corrupt verified object")))
    elif problem == "projection-failure":
        monkeypatch.setattr(DisplayReleasePreparer, "insert", lambda *args: (_ for _ in ()).throw(ValueError("projection failed")))
    with pytest.raises(ValueError):
        repository.publish_display(lease, request_key="failed", candidate_ids=ids, expected_parent_release_id=None,
            prepared=prepared, object_store=objects)
    unchanged_empty(engine)


@pytest.mark.parametrize("changed", ["parent", "candidate", "manifest"])
def test_first_release_request_key_preserves_identity(storage, changed):
    parts = staged(storage)
    first = publish(parts)
    _, lease, repository, snapshot, source, candidate, _, objects, prepared, _ = parts
    parent = first.release_id if changed == "parent" else None
    ids = () if changed == "candidate" else (candidate.candidate_id,)
    if changed == "manifest":
        _, _, prepared, _ = _prepared(storage, snapshot, source, price=20)
    with pytest.raises((PublicationIdentityConflict, ValueError)):
        repository.publish_display(lease, request_key="first-display", candidate_ids=ids,
            expected_parent_release_id=parent, prepared=prepared, object_store=objects)
    assert repository.active() == first


def test_two_first_publishers_have_one_winner(storage):
    parts = staged(storage)
    engine = parts[0]
    other_lease = _claim(engine, "competing-publisher")
    barrier = Barrier(2)
    def attempt(lease, key):
        barrier.wait(timeout=5)
        try:
            return publish(parts, key, lease=lease)
        except StalePublicationParent:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [f.result(timeout=15) for f in (pool.submit(attempt, parts[1], "winner-a"), pool.submit(attempt, other_lease, "winner-b"))]
    assert sum(item is not None for item in outcomes) == 1
    assert parts[2].active() == next(item for item in outcomes if item is not None)


def test_identical_first_publication_replays_after_process_restart(storage, tmp_path):
    parts = staged(storage)
    first = publish(parts)
    prepared = parts[8]
    path = tmp_path / "first-publication-replay.json"
    path.write_text(json.dumps({"lease": asdict(parts[1]), "candidate_id": parts[5].candidate_id,
        "prepared": {"projection_revision": prepared.projection_revision, "input_bundle_sha256": prepared.input_bundle_sha256,
            "manifest_bytes": prepared.manifest_bytes.decode(), "rows_bytes": prepared.rows_bytes.decode()}, "clock": NOW.isoformat()}))
    process = subprocess.run([sys.executable, "-c", """
import json, sys
from datetime import datetime
from sqlalchemy import create_engine
from screener.storage_config import StorageSettings
from screener.object_store import ImmutableObjectStore,create_s3_client
from screener.display_publication import PreparedDisplayRelease
from screener.durable_jobs import LeaseToken
from screener.publication import PublicationRepository
data=json.load(open(sys.argv[1]));settings=StorageSettings.from_env();engine=create_engine(settings.database_url)
try:
    prepared=data['prepared'];prepared['manifest_bytes']=prepared['manifest_bytes'].encode();prepared['rows_bytes']=prepared['rows_bytes'].encode()
    record=PublicationRepository(engine,clock=lambda:datetime.fromisoformat(data['clock'])).publish_display(LeaseToken(**data['lease']),request_key='first-display',candidate_ids=(data['candidate_id'],),expected_parent_release_id=None,prepared=PreparedDisplayRelease(**prepared),object_store=ImmutableObjectStore(create_s3_client(settings),settings.s3_bucket))
    print(json.dumps({'release_id':record.release_id,'parent':record.parent_release_id,'members':record.member_count}))
finally: engine.dispose()
""", str(path)], text=True, capture_output=True, check=True, timeout=15)
    assert json.loads(process.stdout) == {"release_id": first.release_id, "parent": None, "members": 1}


def test_first_publisher_source_barrier_preserves_later_removed_outcome(storage, monkeypatch):
    parts = staged(storage)
    engine, _, repository, *_ = parts
    writer = _claim(engine, "source-after-first")
    entered, resume = Event(), Event()
    activate = repository._activate
    def held(*args):
        entered.set()
        assert resume.wait(timeout=10)
        return activate(*args)
    monkeypatch.setattr(repository, "_activate", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(publish, parts)
        assert entered.wait(timeout=5)
        source_write = pool.submit(_source, engine, storage[1], writer, state="removed", revision=2)
        try: wait_for_lock(engine, source_write)
        finally: resume.set()
        first = publication.result(timeout=10)
        changed = source_write.result(timeout=10)
    assert repository.active() == first
    with engine.connect() as connection:
        assert connection.scalar(select(source_observation.c.state).where(source_observation.c.source_observation_id == changed)) == "removed"
        assert connection.scalar(select(source_item_current.c.source_observation_id)) == changed


def test_new_initial_registration_waiting_behind_publisher_sees_stale_empty_parent(storage, monkeypatch):
    parts = staged(storage)
    engine, _, repository, snapshot, source, *_ = parts
    other = _claim(engine, "late-registration")
    entered, resume = Event(), Event()
    activate = repository._activate
    def held(*args):
        entered.set()
        assert resume.wait(timeout=10)
        return activate(*args)
    monkeypatch.setattr(repository, "_activate", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(publish, parts)
        assert entered.wait(timeout=5)
        registration = pool.submit(repository.register_candidate, other, request_key="blocked-initial", snapshot_id=snapshot.snapshot_id,
            expected_parent_release_id=None, source_observation_ids=(source,), cause_type="source_change")
        try:
            wait_for_lock(engine, registration)
        finally:
            resume.set()
        first = publication.result(timeout=10)
        with pytest.raises(StalePublicationParent): registration.result(timeout=10)
    assert repository.active() == first
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(publication_candidate)) == 1


@pytest.mark.parametrize("state", ["unavailable", "removed"])
def test_initial_publish_rejects_changed_source(storage, state):
    parts = staged(storage)
    _source(parts[0], storage[1], parts[1], state=state, revision=2)
    with pytest.raises(StalePublicationSource): publish(parts)
    unchanged_empty(parts[0])


@pytest.mark.parametrize("operation", ["register", "publish"])
@pytest.mark.parametrize("lock", ["singleton", "source", "snapshot"])
def test_real_initial_lease_expiry_after_lock_wait_rolls_back(storage, operation, lock):
    parts = staged(storage)
    engine, _, _, snapshot, source, *_ = parts
    lease = _claim(engine, "real-expiry")
    expiration = datetime.now(timezone.utc) + timedelta(milliseconds=400)
    with engine.begin() as connection:
        connection.execute(update(durable_job).where(durable_job.c.job_id == lease.job_id).values(lease_expires_at=expiration))
    owner = PublicationRepository(engine)
    with engine.connect() as blocker:
        transaction = blocker.begin()
        if lock == "singleton": blocker.execute(select(active_data_release).with_for_update()).all()
        elif lock == "source": blocker.execute(select(source_item).with_for_update()).all()
        else: blocker.execute(select(company_snapshot).with_for_update()).all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            if operation == "publish": future = pool.submit(publish, parts, "expired-first", repository=owner, lease=lease)
            else: future = pool.submit(owner.register_candidate, lease, request_key="expired-initial", snapshot_id=snapshot.snapshot_id,
                expected_parent_release_id=None, source_observation_ids=(source,), cause_type="source_change")
            try:
                wait_for_lock(engine, future)
                while datetime.now(timezone.utc) <= expiration: sleep(.01)
            finally: transaction.rollback()
            with pytest.raises(LeaseLost): future.result(timeout=10)
    unchanged_empty(engine)


def test_actual_retained_filing_body_can_feed_first_staged_owner_without_financial_claim(storage):
    evidence = json.loads(Path("../local/exact-history-contract-anchor-evidence-2026-10-04.json").read_text())["anchors"][0]
    body = Path(evidence["path"]).read_bytes()
    assert hashlib.sha256(body).hexdigest() == evidence["artifact_sha256"]
    engine, _ = storage
    lease = _claim(engine, "retained-filing-initial")
    _, objects, _ = _input(storage, _company(engine, storage[1], lease=lease), _source(engine, storage[1], lease))
    artifact = store_evidence(body, "text/html", objects, SqlArtifactRepository(engine))
    snapshot = _company(engine, artifact, ticker="FTFT", identifier="1066923", lease=lease)
    source_owner = SourceObservationRepository(engine, clock=lambda: NOW)
    recorded = source_owner.record(lease, item=SourceItemIdentity("SEC", "filing", "0001213900-16-018959", "0001066923"),
        item_outcome_key="genuine-historical-filing", state="present", metadata={"historical_capture_only": True},
        source_url=evidence["source_document"], artifact_sha256=artifact.content_sha256)
    source = recorded.observation.source_observation_id
    repository = PublicationRepository(engine, clock=lambda: NOW)
    candidate = repository.register_candidate(lease, request_key="genuine-body-fixture", snapshot_id=snapshot.snapshot_id,
        expected_parent_release_id=None, source_observation_ids=(source,), cause_type="source_change")
    preparer, _, display = _input(storage, snapshot, source, ticker="FTFT", cik="0001066923")
    prepared = preparer.prepare([display], capture_mode="caller_captured_enrichment",
        limitations=["Genuine2016 retained R4 filing bytes; synthetic financial/display fixture. No latest financial or whole production-flow acceptance."])
    first = repository.publish_display(lease, request_key="retained-body-first", candidate_ids=(candidate.candidate_id,),
        expected_parent_release_id=None, prepared=prepared, object_store=objects)
    assert first.member_count == 1
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(current_company_snapshot)) == 0
