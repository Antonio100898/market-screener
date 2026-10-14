"""Actual SEC handler/storage flow over labelled synthetic HTTP financial bodies."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text, event
from sqlalchemy.engine import make_url
from sqlalchemy import create_engine

from screener import sync, store
from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.durable_jobs import DurableJobRepository, LeaseLost
from screener.figure_history import FigureHistoryRepository
from screener.postgres import company_snapshot, current_company_snapshot, durable_job, exact_figure_bundle, job_schedule_occurrence, create_postgres_engine
from screener.storage_config import StorageSettings
from screener.sec_ingestion import SecCandidateStageHandler, SecResourceFetchHandler
from screener.shared_companies import SharedCompanyRepository, SnapshotArtifact
from screener.evidence import EvidenceBundle
from screener.models import CapturedSource
from screener.sources.sec_json import decode_company_facts
from screener.source_observations import SourceObservationRepository, SourceItemIdentity
from screener.sources.edgar import EdgarTransportResult
from test_sec_exact_annotation import annotated
from test_sec_ingestion_storage import (NOW, LEASE, RESOURCE_ACCESSION, FixtureEdgar,
    storage, _seed_resource_parent, _enqueue_resource_job, _context,
    _resource_responses, _resource_urls, _index)


pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1", reason="requires isolated PostgreSQL/S3")


@pytest.fixture(scope="module")
def database_url():
    # This independent SEC slice requires immutable0017; the0018 owner is still working.
    settings = StorageSettings.from_env()
    name = f"ms_sec_annotation_{uuid4().hex}"
    admin = create_postgres_engine(settings).execution_options(isolation_level="AUTOCOMMIT")
    url = make_url(settings.database_url).set(database=name)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    prior = os.environ.get("SCREENER_DATABASE_URL")
    try:
        os.environ["SCREENER_DATABASE_URL"] = url.render_as_string(hide_password=False)
        revision = os.getenv("SCREENER_SEC_TEST_REVISION", "20261004_0017")
        assert revision in {"20261004_0017", "20261004_0018"}
        command.upgrade(Config("alembic.ini"), revision)
        yield url
    finally:
        if prior is None:
            os.environ.pop("SCREENER_DATABASE_URL", None)
        else:
            os.environ["SCREENER_DATABASE_URL"] = prior
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def setup_flow(storage, key="paired", complete_root=True, facts_cik=1234):
    engine, objects = storage
    jobs = DurableJobRepository(engine)
    parent = _seed_resource_parent(engine, objects, jobs, key)
    root_claim = _enqueue_resource_job(jobs, parent, key)
    raw, _, _ = annotated()
    facts = json.loads(raw)
    facts["cik"] = facts_cik
    for taxonomy in facts["facts"].values():
        for definition in taxonomy.values():
            for entries in definition["units"].values():
                for entry in entries:
                    if entry["accn"] == "k25":
                        entry.update(accn=RESOURCE_ACCESSION, filed="2026-09-28")
    raw = json.dumps(facts).encode()
    responses = _resource_responses()
    url = _resource_urls()["company_facts"]
    responses[url] = EdgarTransportResult(raw, url, "application/json", "annotation-fixture", None)
    source = SourceObservationRepository(engine, clock=lambda: NOW)
    artifacts = SqlArtifactRepository(engine)
    resource = SecResourceFetchHandler(edgar=FixtureEdgar(responses), object_store=objects,
        artifacts=artifacts, observations=source, jobs=jobs, clock=lambda: NOW, annotate_annual=True)
    root_checkpoint = resource(_context(root_claim, jobs))
    if complete_root:
        jobs.complete(root_claim.lease, final_checkpoint=root_checkpoint, now=NOW)
    candidate = jobs.claim_next(owner="annotated-worker", lease_duration=LEASE, now=NOW,
                               allowed_kinds={"sec-candidate-stage"})
    base = "https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/"
    leaves = {base + "annual.htm": EdgarTransportResult(b"<html>annual synthetic fixture</html>", base + "annual.htm", "text/html", None, None),
        base + "R1.htm": EdgarTransportResult(b"<html>Title of each class Common Stock Trading Symbol FIX Name of each exchange Nasdaq</html>", base + "R1.htm", "text/html", None, None)}
    stage = SecCandidateStageHandler(edgar=FixtureEdgar(leaves), object_store=objects,
        artifacts=artifacts, observations=source, companies=SharedCompanyRepository(engine, clock=lambda: NOW),
        history=FigureHistoryRepository(engine, clock=lambda: NOW), clock=lambda: NOW)
    return SimpleNamespace(engine=engine, objects=objects, jobs=jobs, parent=parent, root_claim=root_claim,
        root_checkpoint=root_checkpoint, resource=resource, candidate=candidate, stage=stage, raw=raw,
        source=source, artifacts=artifacts, leaves=leaves)


def counts(engine):
    with engine.connect() as connection:
        return [connection.scalar(select(func.count()).select_from(table))
                for table in (company_snapshot, exact_figure_bundle, current_company_snapshot)]


def saved_checkpoint(engine, job_id):
    with engine.connect() as connection:
        return connection.scalar(select(durable_job.c.checkpoint).where(durable_job.c.job_id == job_id))


def revise_parent(flow, state):
    parent = flow.parent
    later = NOW + timedelta(seconds=1)
    source = SourceObservationRepository(flow.engine, clock=lambda: later)
    if state == "changed":
        return source.record(flow.candidate.lease,
            item=SourceItemIdentity("SEC", "filing", parent.item.source_key,
                parent.item.issuer_source_identifier, parent.item.parent_source_item_id),
            item_outcome_key="probe:parent-revision", state="present",
            metadata={**parent.observation.canonical_metadata, "synthetic_revision": 2},
            artifact_sha256=parent.observation.artifact_sha256,
            source_url=parent.observation.source_url, detected_at=later)
    from screener.sec_ingestion import parse_form_index
    raw = _index()
    assert parse_form_index(raw) == ()
    artifact = store_evidence(raw, "text/plain", flow.objects, flow.artifacts)
    inventory = source.record(flow.candidate.lease,
        item=SourceItemIdentity("SEC", "inventory", "quarterly-index/2026Q3"),
        item_outcome_key="probe:removal-inventory", state="present", metadata={"quarter": "2026Q3"},
        artifact_sha256=artifact.content_sha256,
        source_url="https://www.sec.gov/Archives/edgar/full-index/2026/QTR3/form.idx", detected_at=later)
    return source.record_witnessed_sec_removal(flow.candidate.lease,
        prior_observation_id=parent.observation.source_observation_id,
        item_outcome_key="probe:parent-removal", quarter="2026Q3",
        inventory_source_item_id=inventory.item.source_item_id,
        inventory_observation_id=inventory.observation.source_observation_id,
        inventory_observation_sha256=inventory.observation.observation_sha256,
        inventory_artifact_sha256=artifact.content_sha256, detected_at=later)


def test_actual_resource_stage_pair_and_recovery_without_selectors(storage, monkeypatch):
    flow = setup_flow(storage)
    builds = []
    build = sync.build_snapshot
    def observe(*args, **kwargs):
        snap = build(*args, **kwargs)
        builds.append(snap)
        return snap
    monkeypatch.setattr(sync, "build_snapshot", observe)
    context = _context(flow.candidate, flow.jobs)
    checkpoint = flow.stage(context)
    assert len(builds) == 2 and counts(flow.engine) == [1, 1, 0]
    graph = FigureHistoryRepository(flow.engine).load(checkpoint["exact_bundle_id"])
    assert graph.snapshot_id == checkpoint["snapshot_id"] and graph.snapshot_sha256 == checkpoint["snapshot_sha256"]
    assert graph.pipeline_run_key == checkpoint["pipeline_run_key"]
    assert {s.occurrence_id for s in graph.sources} == {s["occurrence_id"] for s in flow.root_checkpoint["root_deliveries"].values()}
    observed = next(o for o in graph.observations if o.qname == "us-gaap:NetIncomeLoss")
    assert observed.reference.fidelity == "EXACT_LEXICAL"
    parts = observed.reference.location.pointer.split("/")
    value = json.loads(flow.raw)
    for part in parts[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    assert str(value) == observed.value_decimal_text
    fresh = replace(context.job, checkpoint=saved_checkpoint(flow.engine, context.job.job_id))
    reloaded = replace(context, job=fresh)
    monkeypatch.setattr(sync, "build_snapshot", lambda *a, **k: pytest.fail("recovery reselected"))
    assert flow.stage(reloaded) == checkpoint
    assert len(builds) == 2
    assert counts(flow.engine) == [1, 1, 0]


@pytest.mark.parametrize("failure", ["history", "checkpoint", "late_lease"])
def test_pair_failure_rolls_back_snapshot_history_and_checkpoint(storage, monkeypatch, failure):
    flow = setup_flow(storage, failure)
    context = _context(flow.candidate, flow.jobs)
    if failure == "history":
        monkeypatch.setattr(flow.stage._history, "store_on_connection", lambda *a, **k: (_ for _ in ()).throw(ValueError("graph admission")))
    else:
        write = flow.jobs.save_checkpoint_on_connection
        def fail(connection, lease, checkpoint, **kwargs):
            write(connection, lease, checkpoint, **kwargs)
            if failure == "late_lease":
                flow.jobs._lock_live_lease(connection, lease, NOW + LEASE + timedelta(seconds=1))
            raise ValueError("after checkpoint")
        monkeypatch.setattr(flow.jobs, "save_checkpoint_on_connection", fail)
    with pytest.raises((ValueError, LeaseLost)):
        flow.stage(context)
    assert counts(flow.engine) == [0, 0, 0]
    assert saved_checkpoint(flow.engine, context.job.job_id) is None


def test_capture_retry_pins_original_delivery_and_child_identity(storage):
    flow = setup_flow(storage, "root-replay", complete_root=False)
    root = replace(flow.root_claim.job, checkpoint=saved_checkpoint(flow.engine, flow.root_claim.job.job_id))
    before = flow.root_checkpoint
    # The captured resource checkpoint is replayed without HTTP or selecting a current check.
    flow.resource._edgar = FixtureEdgar({})
    assert flow.resource(_context(replace(flow.root_claim, job=root), flow.jobs)) == before
    with flow.engine.connect() as connection:
        children = connection.execute(select(durable_job).where(durable_job.c.kind == "sec-candidate-stage")).mappings().all()
    assert len(children) == 1
    assert children[0]["parameters"]["root_deliveries"] == before["root_deliveries"]
    assert children[0]["parameters"]["capture_revision"] == 2
    contract = {name: before[name] for name in ("roots", "capture_revision", "root_deliveries")}
    digest = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with flow.engine.connect() as connection:
        key = connection.scalar(select(job_schedule_occurrence.c.schedule_key).where(
            job_schedule_occurrence.c.occurrence_id == children[0]["occurrence_id"]))
    assert digest in key


def test_changed_then_restored_source_keeps_actual_original_delivery(storage):
    flow = setup_flow(storage, "a-b-a", complete_root=False)
    original = flow.source.by_sha256(flow.root_checkpoint["roots"]["company_facts"])
    item, observation = original.item, original.observation
    identity = SourceItemIdentity(item.source_system, item.item_kind, item.source_key,
                                  item.issuer_source_identifier, item.parent_source_item_id)
    changed = flow.source.record(flow.candidate.lease, item=identity, item_outcome_key="probe:changed",
        state="present", metadata={**observation.canonical_metadata, "verification_probe": "B"},
        source_url=observation.source_url, artifact_sha256=observation.artifact_sha256,
        etag=observation.etag, last_modified=observation.last_modified)
    restored = flow.source.record(flow.candidate.lease, item=identity, item_outcome_key="probe:restored",
        state="present", metadata=observation.canonical_metadata,
        source_url=observation.source_url, artifact_sha256=observation.artifact_sha256,
        etag=observation.etag, last_modified=observation.last_modified)
    pinned = flow.root_checkpoint["root_deliveries"]["company_facts"]
    assert changed.observation.source_observation_id != observation.source_observation_id
    assert restored.observation.source_observation_id == observation.source_observation_id
    assert restored.delivery.occurrence_id != pinned["occurrence_id"]
    root = replace(flow.root_claim.job, checkpoint=saved_checkpoint(flow.engine, flow.root_claim.job.job_id))
    fresh_resource = SecResourceFetchHandler(edgar=FixtureEdgar({}), object_store=flow.objects,
        artifacts=SqlArtifactRepository(flow.engine), observations=SourceObservationRepository(flow.engine, clock=lambda: NOW),
        jobs=flow.jobs, clock=lambda: NOW, annotate_annual=True)
    assert fresh_resource(_context(replace(flow.root_claim, job=root), flow.jobs)) == flow.root_checkpoint
    checkpoint = flow.stage(_context(flow.candidate, flow.jobs))
    graph = FigureHistoryRepository(flow.engine).load(checkpoint["exact_bundle_id"])
    source = next(s for s in graph.sources if s.source_item_id == item.source_item_id)
    assert source.occurrence_id == pinned["occurrence_id"]


def test_delivery_tampering_and_legacy_checkpoint_fail_before_derivation(storage, monkeypatch):
    flow = setup_flow(storage, "delivery-tamper")
    context = _context(flow.candidate, flow.jobs)
    parameters = dict(context.job.parameters)
    parameters["root_deliveries"] = {key: dict(value) for key, value in parameters["root_deliveries"].items()}
    parameters["root_deliveries"]["company_facts"]["occurrence_id"] += 99999
    monkeypatch.setattr(sync, "build_snapshot", lambda *a, **k: pytest.fail("invalid delivery reached derivation"))
    with pytest.raises((ValueError, KeyError)):
        flow.stage(replace(context, job=replace(context.job, parameters=parameters)))
    legacy = {key: context.job.parameters[key] for key in ("accession", "cik", "roots")}
    legacy.update(filing_observation_id=context.job.parameters["observation_id"], snapshot_id=1, snapshot_created=True)
    with pytest.raises(ValueError, match="checkpoint"):
        flow.stage(replace(context, job=replace(context.job, checkpoint=legacy)))
    assert counts(flow.engine) == [0, 0, 0]


def test_actual_captured_company_facts_other_issuer_fails_before_selectors(storage, monkeypatch):
    monkeypatch.setattr(sync, "build_snapshot", lambda *a, **k: pytest.fail("wrong issuer reached selector"))
    with pytest.raises(ValueError, match="another issuer"):
        setup_flow(storage, "other-issuer", facts_cik=9999)
    assert counts(storage[0]) == [0, 0, 0]


@pytest.mark.parametrize("field,value", [("snapshot_sha256", "0" * 64),
    ("exact_bundle_sha256", "0" * 64), ("pipeline_run_key", "wrong-run"),
    ("security_id", 99999), ("engine_revision", 189), ("engine_identity_sha256", "0" * 64),
    ("snapshot_id", 99999), ("exact_bundle_id", 99999)])
def test_recovery_cannot_replace_committed_pair_identity(storage, monkeypatch, field, value):
    flow = setup_flow(storage, "tamper-" + field)
    context = _context(flow.candidate, flow.jobs)
    checkpoint = flow.stage(context)
    changed = {**checkpoint, field: value}
    monkeypatch.setattr(sync, "build_snapshot", lambda *a, **k: pytest.fail("tampered recovery reselected"))
    with pytest.raises((ValueError, KeyError)):
        flow.stage(replace(context, job=replace(context.job, checkpoint=changed)))
    assert saved_checkpoint(flow.engine, context.job.job_id) == checkpoint
    assert counts(flow.engine) == [1, 1, 0]


def test_new_repository_reclaimed_lease_recovers_same_pair_without_http_or_selection(storage, monkeypatch):
    flow = setup_flow(storage, "reclaimed-pair")
    checkpoint = flow.stage(_context(flow.candidate, flow.jobs))
    restarted = create_engine(flow.engine.url)
    late = NOW + LEASE + timedelta(seconds=1)
    try:
        jobs = DurableJobRepository(restarted)
        recovered = jobs.recover_expired(now=late)
        assert {job.job_id for job in recovered} == {flow.candidate.job.job_id}
        late = max(job.next_retry_at for job in recovered)
        resumed = jobs.claim_next(owner="new-process", lease_duration=LEASE, now=late,
                                  allowed_kinds={"sec-candidate-stage"})
        assert resumed.job.job_id == flow.candidate.job.job_id
        assert resumed.lease.generation > flow.candidate.lease.generation
        stage = SecCandidateStageHandler(edgar=FixtureEdgar({}), object_store=flow.objects,
            artifacts=SqlArtifactRepository(restarted), observations=SourceObservationRepository(restarted, clock=lambda: late),
            companies=SharedCompanyRepository(restarted, clock=lambda: late),
            history=FigureHistoryRepository(restarted, clock=lambda: late), clock=lambda: late)
        monkeypatch.setattr(sync, "build_snapshot", lambda *a, **k: pytest.fail("restarted pair reselected"))
        assert stage(_context(resumed, jobs, clock=lambda: late)) == checkpoint
        assert counts(restarted) == [1, 1, 0]
    finally:
        restarted.dispose()


@pytest.mark.parametrize("parent_state", ["unchanged", "changed", "removed"])
def test_actual_fresh_process_recovers_root_and_pair_without_http_or_selectors(storage, parent_state):
    flow = setup_flow(storage, "actual-new-process", complete_root=False)
    checkpoint = flow.stage(_context(flow.candidate, flow.jobs))
    if parent_state != "unchanged":
        revised = revise_parent(flow, parent_state)
        assert revised.observation.source_observation_id != flow.parent.observation.source_observation_id
    script = '''
import json, sys, threading
from datetime import datetime
from sqlalchemy import create_engine, select
from screener import sync
from screener.artifacts import SqlArtifactRepository
from screener.durable_jobs import DurableJobRepository, LeaseToken, _job_record
from screener.figure_history import FigureHistoryRepository
from screener.job_runtime import JobContext
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import durable_job
from screener.sec_ingestion import SecResourceFetchHandler, SecCandidateStageHandler
from screener.shared_companies import SharedCompanyRepository
from screener.source_observations import SourceObservationRepository
from screener.storage_config import StorageSettings
data=json.load(sys.stdin)
engine=create_engine(data['url'])
now=datetime.fromisoformat(data['now'])
clock=lambda: now
jobs=DurableJobRepository(engine)
settings=StorageSettings.from_env()
objects=ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket)
artifacts=SqlArtifactRepository(engine)
sources=SourceObservationRepository(engine, clock=clock)
class OfflineEdgar:
    def fetch(self, url):
        raise AssertionError('new process fetched source')
def forbidden(*args, **kwargs):
    raise AssertionError('new process rebuilt or reselected Snapshot')
sync.build_snapshot=forbidden
sync._derive_evidence_result=forbidden
SourceObservationRepository.latest_sec_filing=forbidden
def context(identifier):
    with engine.connect() as connection:
        row=connection.execute(select(durable_job).where(durable_job.c.job_id==identifier)).mappings().one()
    job=_job_record(row)
    lease=LeaseToken(job.job_id, job.owner, job.ownership_generation)
    return JobContext(job, threading.Event(), jobs, lease, clock)
resource=SecResourceFetchHandler(edgar=OfflineEdgar(), object_store=objects,
    artifacts=artifacts, observations=sources, jobs=jobs, clock=clock, annotate_annual=True)
stage=SecCandidateStageHandler(edgar=OfflineEdgar(), object_store=objects,
    artifacts=artifacts, observations=sources, companies=SharedCompanyRepository(engine, clock=clock),
    history=FigureHistoryRepository(engine, clock=clock), clock=clock)
print(json.dumps({'root':resource(context(data['root'])), 'pair':stage(context(data['stage']))}))
engine.dispose()
'''
    result = subprocess.run([sys.executable, "-c", script], input=json.dumps(dict(
        url=flow.engine.url.render_as_string(hide_password=False), now=NOW.isoformat(),
        root=flow.root_claim.job.job_id, stage=flow.candidate.job.job_id)),
        text=True, capture_output=True, timeout=25)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"root": flow.root_checkpoint, "pair": checkpoint}
    assert counts(flow.engine) == [1, 1, 0]


@pytest.mark.parametrize("parent_state", ["changed", "removed"])
def test_completed_pair_keeps_captured_parent_but_first_stage_requires_current(storage, monkeypatch, parent_state):
    flow = setup_flow(storage, "parent-" + parent_state)
    context = _context(flow.candidate, flow.jobs)
    checkpoint = flow.stage(context)
    revised = revise_parent(flow, parent_state)
    assert revised.observation.state == ("present" if parent_state == "changed" else "removed")
    assert flow.source.latest_sec_filing(revised.observation.source_observation_id).observation == revised.observation
    monkeypatch.setattr(sync, "_derive_evidence_result", lambda *a, **k: pytest.fail("recovery selected"))
    monkeypatch.setattr(sync, "build_snapshot", lambda *a, **k: pytest.fail("recovery built"))
    with pytest.raises(ValueError, match="not latest"):
        flow.stage(context)  # no checkpoint is first-stage admission, even when a pair exists elsewhere
    if parent_state == "removed":
        parameters = {**context.job.parameters, "observation_id": revised.observation.source_observation_id}
        with pytest.raises(ValueError, match="not present"):
            flow.stage(replace(context, job=replace(context.job, parameters=parameters)))
    monkeypatch.setattr(flow.stage._observations, "latest_sec_filing",
        lambda *a, **k: pytest.fail("completed recovery consulted current"))
    recovered = replace(context, job=replace(context.job, checkpoint=saved_checkpoint(flow.engine, context.job.job_id)))
    assert flow.stage(recovered) == checkpoint
    assert counts(flow.engine) == [1, 1, 0]
    assert flow.source.sec_filing(flow.parent.observation.source_observation_id).observation == flow.parent.observation


def test_completed_pair_rejects_wrong_immutable_parent_return(storage, monkeypatch):
    flow = setup_flow(storage, "wrong-immutable-parent")
    context = _context(flow.candidate, flow.jobs)
    checkpoint = flow.stage(context)
    other = flow.source.record(context.lease,
        item=SourceItemIdentity("SEC", "filing", "0000009999-26-000001", "0000009999"),
        item_outcome_key="probe:other-parent", state="present", metadata={"form": "10-K"},
        artifact_sha256=flow.parent.observation.artifact_sha256, source_url=flow.parent.observation.source_url)
    monkeypatch.setattr(flow.stage._observations, "sec_filing", lambda *a, **k: other)
    recovered = replace(context, job=replace(context.job, checkpoint=checkpoint))
    with pytest.raises(ValueError, match="identity changed"):
        flow.stage(recovered)
    assert counts(flow.engine) == [1, 1, 0]


def test_engine_changes_during_actual_derivation_fail_before_sql_pair(storage, monkeypatch):
    flow = setup_flow(storage, "engine-drift")
    identity = flow.stage._engine_identity()
    identities = iter([identity, "0" * 64])
    monkeypatch.setattr(flow.stage, "_engine_identity", lambda: next(identities))
    with pytest.raises(ValueError, match="engine source changed"):
        flow.stage(_context(flow.candidate, flow.jobs))
    assert counts(flow.engine) == [0, 0, 0]
    assert saved_checkpoint(flow.engine, flow.candidate.job.job_id) is None


@pytest.mark.parametrize("expired", [True, False])
def test_actual_post_checkpoint_lock_wait_fences_whole_sec_pair(storage, expired):
    flow = setup_flow(storage, "live-wait")
    now = datetime.now(timezone.utc)
    jobs = flow.jobs
    jobs.enqueue_scheduled(schedule_key="live-sec-pair", scheduled_for=now,
        kind="live-sec-pair", parameters=flow.candidate.job.parameters, due_at=now)
    claimed = jobs.claim_next(owner="live-pair-worker", lease_duration=timedelta(seconds=2 if expired else 20),
                              allowed_kinds={"live-sec-pair"})
    guard = jobs.enqueue_scheduled(schedule_key="pair-guard", scheduled_for=now,
        kind="pair-guard", parameters={}, due_at=now).job_id
    blocker = flow.engine.connect()
    transaction = blocker.begin()
    blocker.execute(select(durable_job).where(durable_job.c.job_id == guard).with_for_update()).all()
    entered, pids = threading.Event(), []
    live = lambda: datetime.now(timezone.utc)
    flow.stage._clock = live
    flow.stage._observations._clock = live
    flow.stage._companies._clock = live
    flow.stage._history._clock = live
    def wait_after_update(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE durable_job SET checkpoint"):
            pids.append(connection.scalar(text("SELECT pg_backend_pid()")))
            entered.set()
            connection.execute(select(durable_job).where(durable_job.c.job_id == guard).with_for_update()).all()
    event.listen(flow.engine, "after_cursor_execute", wait_after_update)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(flow.stage, _context(claimed, jobs, clock=live))
    try:
        assert entered.wait(5), "must reach actual checkpoint UPDATE"
        waited = False
        until = monotonic() + 3
        while monotonic() < until:
            with flow.engine.connect() as observer:
                waited = observer.scalar(text("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"), {"pid": pids[0]})
            if waited:
                break
            sleep(.01)
        assert waited, "must observe a real PostgreSQL lock wait after pair writes"
        with flow.engine.connect() as observer:
            expires = observer.scalar(select(durable_job.c.lease_expires_at).where(durable_job.c.job_id == claimed.job.job_id))
        sleep(max(0, (expires - live()).total_seconds()) + .05 if expired else .05)
        transaction.rollback()
        if expired:
            with pytest.raises(LeaseLost):
                future.result(timeout=5)
            assert counts(flow.engine) == [0, 0, 0]
            assert saved_checkpoint(flow.engine, claimed.job.job_id) is None
        else:
            checkpoint = future.result(timeout=5)
            assert counts(flow.engine) == [1, 1, 0]
            assert saved_checkpoint(flow.engine, claimed.job.job_id) == checkpoint
    finally:
        if transaction.is_active:
            transaction.rollback()
        blocker.close()
        event.remove(flow.engine, "after_cursor_execute", wait_after_update)
        pool.shutdown(wait=True)


def test_genuine_retained_abt_cache_selected_annual_occurrences_and_pair(storage, monkeypatch):
    # Genuine retained cache bytes are lower fidelity than an original HTTP body.
    engine, objects = storage
    path = Path.home() / ".cache/graham-screener/companyfacts_0000001800.json"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "5d751f73769f5c0a1f9bee0de16cb39d8961cf3f1ebb5558d555bb2678afb4ff"
    jobs = DurableJobRepository(engine)
    jobs.enqueue_scheduled(schedule_key="abt-retained-annotation", scheduled_for=NOW,
        kind="abt-retained-annotation", parameters={"verification": "legacy-cache"}, due_at=NOW)
    claimed = jobs.claim_next(owner="abt-retained-worker", lease_duration=LEASE, now=NOW,
                              allowed_kinds={"abt-retained-annotation"})
    artifacts = SqlArtifactRepository(engine)
    artifact = store_evidence(raw, "application/json", objects, artifacts)
    sources = SourceObservationRepository(engine, clock=lambda: NOW)
    recorded = sources.record(claimed.lease,
        item=SourceItemIdentity("SEC", "aggregate", "verification/companyfacts/0000001800", "0000001800"),
        item_outcome_key="legacy:company_facts", state="present",
        metadata={"role": "company_facts", "cik": "0000001800", "capture_fidelity": "LEGACY_JSON_CACHE"},
        source_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000001800.json",
        artifact_sha256=artifact.content_sha256)
    captured = CapturedSource("company_facts", artifact.content_sha256, recorded.observation.source_url,
        recorded.item.source_item_id, recorded.observation.source_observation_id, None, "DECODED_DECIMAL")
    facts = decode_company_facts(objects.read_verified(artifact.object_key, artifact.content_sha256, artifact.byte_size), source=captured)
    builds = []
    build = sync.build_snapshot
    def observe(*args, **kwargs):
        snapshot = build(*args, **kwargs)
        builds.append(snapshot)
        return snapshot
    monkeypatch.setattr(sync, "build_snapshot", observe)
    result = sync._derive_evidence_result(EvidenceBundle("0000001800", "ABT", facts, None, None,
        captured_sources=(captured,), original_company_facts=facts))
    assert result.status == "ok" and len(builds) == 2
    stage = SecCandidateStageHandler(edgar=FixtureEdgar({}), object_store=objects, artifacts=artifacts,
        observations=sources, companies=SharedCompanyRepository(engine, clock=lambda: NOW),
        history=FigureHistoryRepository(engine, clock=lambda: NOW), clock=lambda: NOW)
    values = dict(issuer_source="SEC", issuer_identifier="0000001800", security_identifier="sec:0000001800:verification",
        ticker="ABT", security_title=None, source_accession=None, security_basis="SEC_TICKER_MAPPING_ONLY",
        quote_currency="USD", engine_revision=store.ENGINE_VERSION, canonical_payload=result.row,
        artifacts=(SnapshotArtifact(artifact.content_sha256, "sec_root_company_facts"),), receipt_ratio=None)
    checkpoint = stage._store_pair(_context(claimed, jobs), values, result, (captured,), stage._engine_identity(), facts)
    assert len(builds) == 2 and counts(engine) == [1, 1, 0]
    graph = FigureHistoryRepository(engine).load(checkpoint["exact_bundle_id"])
    assert graph.observations and all(row.assessment == "PARTIAL" for row in graph.coverage)
    assert all(o.reference.fidelity == "DECODED_DECIMAL" for o in graph.observations)
    assert all(o.reference.artifact_sha256 == artifact.content_sha256 for o in graph.observations)
    assert any(o.qname == "us-gaap:NetIncomeLoss" for o in graph.observations)
    assert saved_checkpoint(engine, claimed.job.job_id) == checkpoint
    proof = dict(source="genuine retained ABT decoded/reserialized cache; not original HTTP precision",
        artifact_sha256=artifact.content_sha256, engine_revision=store.ENGINE_VERSION,
        snapshot_sha256=checkpoint["snapshot_sha256"], exact_bundle_sha256=checkpoint["exact_bundle_sha256"],
        builds=len(builds), observations=len(graph.observations), selections=len(graph.selections),
        coverage=sorted({row.assessment for row in graph.coverage}),
        selected=[dict(path=node.metric_path, state=node.state, reason=node.reason, observation_key=node.observation_key)
                  for node in graph.selections if node.view == "STRICT"],
        original_occurrences=[dict(qname=o.qname, pointer=o.reference.location.pointer,
            lexical=o.source_lexical_text, unit=o.unit.model_dump(mode="json"), period=o.period.model_dump(mode="json"))
            for o in graph.observations])
    (Path(__file__).parents[3] / "local/sec-annotation-genuine-abt-proof-2026-10-04.json").write_text(
        json.dumps(proof, indent=2), encoding="utf-8")
