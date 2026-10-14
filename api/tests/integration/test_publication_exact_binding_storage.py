"""Real PG/S3 publication mechanics; source numerical authenticity is not certified."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import IntegrityError

from screener.durable_jobs import DurableJobRepository, LeaseLost
from screener.figure_history import FigureHistoryRepository, BundleSource
from screener.postgres import (active_data_release, data_release, publication_candidate, release_candidate,
    release_company, release_company_source, release_company_lineage, source_item_current, source_item,
    source_check_occurrence, source_observation, durable_job_item, exact_figure_bundle, company_snapshot)
from screener.company_query_schema import release_company_projection
from screener.publication import PublicationRepository, PublicationIdentityConflict, StalePublicationSource
from screener.source_observations import SourceObservationRepository, SourceItemIdentity
from screener.source_validity import accepted_witnesses
from screener.display_publication import DisplayReleasePreparer, PreparedDisplayRelease, canonical_bytes
from test_publication_storage import storage, NOW, _claim, _source, _company, _metadata_and_document, pytestmark
from test_figure_history_storage import bound_bundle
from test_display_publication_storage import _input


def setup(storage):
    engine, lease, company, bundle = bound_bundle(storage)
    history = FigureHistoryRepository(engine, clock=lambda: NOW)
    identifier = history.store(lease, request_key="graph-A", bundle=bundle)
    return engine, lease, company, bundle, identifier, PublicationRepository(engine, clock=lambda: NOW)


def register(parts, key="candidate", parent=None, **changes):
    engine, lease, company, bundle, identifier, repository = parts
    values = dict(request_key=key, snapshot_id=company.snapshot_id, expected_parent_release_id=parent,
        source_observation_ids=(bundle.sources[0].source_observation_id,), cause_type="source_change",
        exact_bundle_id=identifier, expected_exact_run_key=bundle.pipeline_run_key)
    values.update(changes)
    return repository.register_candidate(lease, **values)


def publish(parts, candidate, key="release", parent=None):
    return parts[-1].publish(parts[1], request_key=key, candidate_ids=(candidate.candidate_id,), expected_parent_release_id=parent)


def lineage(engine, release_id):
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(release_company_source).where(
            release_company_source.c.release_id == release_id).order_by(release_company_source.c.source_observation_id)).mappings()]


def record(storage, key, revision, *, source_key="filing-1", state="present"):
    engine, artifact = storage
    lease = _claim(engine, key)
    return SourceObservationRepository(engine, clock=lambda: NOW).record(lease,
        item=SourceItemIdentity("SEC", "filing", source_key, "0000001800"), item_outcome_key=key,
        state=state, metadata={"revision": revision}, source_url="https://www.sec.gov/filing-1",
        artifact_sha256=artifact.content_sha256 if state == "present" else None)


def test_explicit_same_snapshot_two_graphs_and_null_bound_replay_conflict(storage):
    parts = setup(storage)
    engine, lease, company, bundle, identifier, repository = parts
    candidate = register(parts)
    assert candidate.exact_bundle_id == identifier and register(parts) == candidate
    second = FigureHistoryRepository(engine, clock=lambda: NOW).store(lease, request_key="graph-B",
        bundle=bundle.model_copy(update={"pipeline_run_key": "derive:other"}))
    for changes in (dict(exact_bundle_id=None, expected_exact_run_key=None),
                    dict(exact_bundle_id=second, expected_exact_run_key="derive:other")):
        with pytest.raises(PublicationIdentityConflict): register(parts, **changes)
    legacy = register(parts, key="legacy", exact_bundle_id=None, expected_exact_run_key=None)
    assert legacy.exact_bundle_id is None
    with pytest.raises(PublicationIdentityConflict): register(parts, key="legacy")
    release = publish(parts, candidate)
    with engine.connect() as connection:
        assert connection.scalar(select(release_company.c.exact_bundle_id)) == identifier
    assert publish(parts, candidate) == release
    engine.dispose()
    assert PublicationRepository(engine).release(release.release_id) == release


@pytest.mark.parametrize("changes", [dict(exact_bundle_id=True), dict(exact_bundle_id=0), dict(exact_bundle_id=-1),
    dict(exact_bundle_id=1.5), dict(exact_bundle_id=999999), dict(expected_exact_run_key=None),
    dict(expected_exact_run_key="other"), dict(exact_bundle_id=None, expected_exact_run_key="derive:test")])
def test_explicit_binding_run_and_identity_rejection(storage, changes):
    parts = setup(storage)
    with pytest.raises(ValueError): register(parts, **changes)
    with parts[0].connect() as connection:
        assert connection.scalar(select(func.count()).select_from(publication_candidate)) == 0


def test_replay_after_source_change_is_immutable_but_activation_rejects(storage):
    parts = setup(storage)
    candidate = register(parts)
    record(storage, "source-B", 2)
    assert register(parts) == candidate
    with pytest.raises(StalePublicationSource): publish(parts, candidate)
    assert parts[-1].active() is None


@pytest.mark.parametrize("publish_b", [False, True])
def test_a_b_a_reuses_capture_one_but_records_actual_activation_three(storage, publish_b):
    parts = setup(storage)
    engine, lease, company, bundle, identifier, repository = parts
    first = publish(parts, register(parts), key="A1")
    original = lineage(engine, first.release_id)
    assert original[0]["accepted_occurrence_id"] == bundle.sources[0].occurrence_id
    changed = record(storage, "source-B", 2)
    parent = first
    if publish_b:
        bsource = changed.observation.source_observation_id
        with engine.connect() as connection:
            pointer = connection.execute(select(source_item_current)).mappings().one()
        b_bundle = bundle.model_copy(update={"pipeline_run_key": "derive:B", "sources": (
            BundleSource(source_item_id=pointer["source_item_id"], source_observation_id=bsource, occurrence_id=pointer["occurrence_id"]),)})
        b_id = FigureHistoryRepository(engine, clock=lambda: NOW).store(lease, request_key="graph-B", bundle=b_bundle)
        b_candidate = register(parts, key="candidate-B", parent=first.release_id, source_observation_ids=(bsource,),
            exact_bundle_id=b_id, expected_exact_run_key="derive:B")
        parent = publish(parts, b_candidate, key="B2", parent=first.release_id)
    returned = record(storage, "source-A3", 1)
    assert returned.observation.source_observation_id == bundle.sources[0].source_observation_id and not returned.created
    current = None
    with engine.connect() as connection:
        current = connection.execute(select(source_item_current)).mappings().one()
    third = publish(parts, register(parts, key="candidate-A3", parent=parent.release_id), key="A3", parent=parent.release_id)
    assert lineage(engine, third.release_id)[0]["accepted_occurrence_id"] == current["occurrence_id"] != bundle.sources[0].occurrence_id
    assert lineage(engine, first.release_id) == original
    assert FigureHistoryRepository(engine).load(identifier).sources[0].occurrence_id == bundle.sources[0].occurrence_id
    record(storage, "source-A4", 1)
    assert publish(parts, register(parts, key="candidate-A3", parent=parent.release_id), key="A3", parent=parent.release_id) == third
    assert lineage(engine, third.release_id)[0]["accepted_occurrence_id"] == current["occurrence_id"]


def test_inherited_unvalidated_member_preserves_binding_and_old_witness(storage):
    parts = setup(storage)
    first = publish(parts, register(parts))
    before = lineage(parts[0], first.release_id)
    record(storage, "unchanged-check", 1)
    engine, lease = parts[:2]
    other = _company(engine, storage[1], identifier="2000", ticker="XYZ", lease=lease)
    source = _source(engine, storage[1], lease, identifier="2000", key="other")
    candidate = parts[-1].register_candidate(lease, request_key="other", snapshot_id=other.snapshot_id,
        expected_parent_release_id=first.release_id, source_observation_ids=(source,), cause_type="source_change")
    child = publish(parts, candidate, key="inherited", parent=first.release_id)
    inherited = [row for row in lineage(engine, child.release_id) if row["security_id"] == parts[2].security_id]
    assert [{**row, "release_id": first.release_id} for row in inherited] == before
    with engine.connect() as connection:
        assert connection.scalar(select(release_company.c.exact_bundle_id).where(release_company.c.release_id == child.release_id,
            release_company.c.security_id == parts[2].security_id)) == parts[4]


def prepare_display(storage, parts, *, bound=True, price=10):
    preparer, objects, company = _input(storage, parts[2], parts[3].sources[0].source_observation_id, price=price)
    if bound: company = replace(company, exact_bundle_id=parts[4], expected_exact_run_key=parts[3].pipeline_run_key)
    prepared = preparer.prepare([company], capture_mode="caller_captured_enrichment", limitations=["Synthetic mechanics only"])
    return preparer, objects, company, prepared


def test_manifest_two_first_publication_and_display_only_preserve_exact_binding(storage):
    parts = setup(storage)
    preparer, objects, company, prepared = prepare_display(storage, parts)
    manifest, _ = preparer.verify(prepared)
    assert manifest["contract_revision"] == 2 and manifest["members"][0]["exact_bundle_id"] == parts[4]
    candidate = register(parts)
    first = parts[-1].publish_display(parts[1], request_key="display1", candidate_ids=(candidate.candidate_id,),
        expected_parent_release_id=None, prepared=prepared, object_store=objects)
    record(storage, "check2", 1)
    _, _, _, changed = prepare_display(storage, parts, price=12)
    child = parts[-1].publish_display(parts[1], request_key="display2", expected_parent_release_id=first.release_id,
        prepared=changed, object_store=objects)
    assert lineage(parts[0], child.release_id)[0]["accepted_occurrence_id"] != lineage(parts[0], first.release_id)[0]["accepted_occurrence_id"]
    with parts[0].connect() as connection:
        assert set(connection.scalars(select(release_company.c.exact_bundle_id))) == {parts[4]}
    assert parts[-1].publish_display(parts[1], request_key="display1", candidate_ids=(candidate.candidate_id,),
        expected_parent_release_id=None, prepared=prepared, object_store=objects) == first


def test_literal_pre_extension_manifest_one_verified_and_replayed(storage):
    parts = setup(storage)
    preparer, objects, company = _input(storage, parts[2], parts[3].sources[0].source_observation_id)
    path = Path("../tmp/verification-runtime/before-publication-exact-binding/display_publication.py")
    spec = importlib.util.spec_from_file_location("screener.before_display_binding", path)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    old_prepared = module.DisplayReleasePreparer(parts[0], objects).prepare([company],
        capture_mode="caller_captured_enrichment", limitations=["Synthetic mechanics only"])
    new_prepared = preparer.prepare([company], capture_mode="caller_captured_enrichment", limitations=["Synthetic mechanics only"])
    assert old_prepared.manifest_bytes == new_prepared.manifest_bytes
    assert old_prepared.input_bundle_sha256 == new_prepared.input_bundle_sha256
    assert json.loads(old_prepared.manifest_bytes)["contract_revision"] == 1
    candidate = register(parts, exact_bundle_id=None, expected_exact_run_key=None)
    first = parts[-1].publish_display(parts[1], request_key="old1", candidate_ids=(candidate.candidate_id,),
        expected_parent_release_id=None, prepared=old_prepared, object_store=objects)
    assert parts[-1].publish_display(parts[1], request_key="old1", candidate_ids=(candidate.candidate_id,),
        expected_parent_release_id=None, prepared=old_prepared, object_store=objects) == first
    print("Preserved pre-extension manifest1 SHA256", old_prepared.input_bundle_sha256)


def test_old_manifest_cannot_authorize_bound_member_and_late_failure_rolls_back_every_write(storage, monkeypatch):
    parts = setup(storage)
    _, objects, _, old = prepare_display(storage, parts, bound=False)
    candidate = register(parts)
    with pytest.raises(ValueError, match="exactly one projection"):
        parts[-1].publish_display(parts[1], request_key="old-bound", candidate_ids=(candidate.candidate_id,),
            expected_parent_release_id=None, prepared=old, object_store=objects)
    _, _, _, prepared = prepare_display(storage, parts)
    import screener.publication as owner
    original = owner.insert_release_lineage
    def fail_after(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("late witness rollback")
    monkeypatch.setattr(owner, "insert_release_lineage", fail_after)
    with pytest.raises(RuntimeError, match="late witness rollback"):
        parts[-1].publish_display(parts[1], request_key="late", candidate_ids=(candidate.candidate_id,),
            expected_parent_release_id=None, prepared=prepared, object_store=objects)
    with parts[0].connect() as connection:
        for table in (data_release, release_company, release_candidate, release_company_source, release_company_lineage, release_company_projection):
            assert connection.scalar(select(func.count()).select_from(table)) == 0
        assert connection.scalar(select(func.count()).select_from(exact_figure_bundle)) == 1
        assert connection.scalar(select(func.count()).select_from(durable_job_item).where(durable_job_item.c.item_key.like("publication:%"))) == 0


def test_graph_only_source_is_registered_locked_and_changes_block_activation(storage):
    parts = setup(storage)
    extra = record(storage, "extra", 1, source_key="extra-filing")
    with parts[0].connect() as connection:
        pointer = connection.execute(select(source_item_current).where(source_item_current.c.source_item_id == extra.item.source_item_id)).mappings().one()
    graph = parts[3].model_copy(update={"pipeline_run_key": "derive:extra", "sources": (*parts[3].sources,
        BundleSource(source_item_id=extra.item.source_item_id, source_observation_id=extra.observation.source_observation_id, occurrence_id=pointer["occurrence_id"]))})
    identifier = FigureHistoryRepository(parts[0], clock=lambda: NOW).store(parts[1], request_key="extra-graph", bundle=graph)
    candidate = register(parts, exact_bundle_id=identifier, expected_exact_run_key="derive:extra")
    assert extra.observation.source_observation_id in candidate.source_observation_ids
    record(storage, "extra-change", 2, source_key="extra-filing")
    with pytest.raises(StalePublicationSource): publish(parts, candidate)
    assert parts[-1].active() is None


def test_full_binding_load_uses_one_batched_observation_lookup(storage):
    parts = setup(storage)
    observations, selections = [], []
    template = parts[3]
    for n in range(1000):
        key = f"source-{n:04}"
        reference = template.observations[0].reference.model_copy(update={"location": template.observations[0].reference.location.model_copy(update={"pointer": f"/facts/us-gaap/Assets/units/USD/{n}"})})
        observations.append(template.observations[0].model_copy(update={"key": key,"reference": reference}))
        selections.append(template.selections[0].model_copy(update={"node_key": f"assets-{n:04}","metric_path": f"balance_sheet.assets_{n:04}","observation_key": key}))
    graph = template.model_copy(update={"observations": tuple(observations),"selections": tuple(selections),
        "coverage": (template.coverage[0].model_copy(update={"metric_paths": tuple(n.metric_path for n in selections)}),), "pipeline_run_key": "derive:1000"})
    identifier = FigureHistoryRepository(parts[0], clock=lambda: NOW).store(parts[1],request_key="large",bundle=graph)
    parts = (*parts[:3], graph, identifier, parts[-1])
    statements = []
    def observe(conn, cursor, sql, params, context, many): statements.append(sql)
    event.listen(parts[0], "before_cursor_execute", observe)
    try: register(parts)
    finally: event.remove(parts[0], "before_cursor_execute", observe)
    observed = [sql for sql in statements if "JOIN figure_observation" in sql]
    assert len(observed) == 1 and "jsonb_to_recordset" in observed[0]
    print("Registration SQL count", len(statements), "batched observation lookup", len(observed))


def test_raw_witness_pair_cross_item_cross_revision_and_sealed_guards(storage):
    parts = setup(storage)
    first = publish(parts, register(parts))
    witness = lineage(parts[0], first.release_id)[0]
    extra = record(storage, "extra", 1, source_key="extra-filing")
    changed = record(storage, "changed", 2)
    with parts[0].connect() as connection:
        pointers = {r["source_item_id"]: r for r in connection.execute(select(source_item_current)).mappings()}
    # Unsealed release lets the native constraints, rather than the seal trigger, reject each tuple.
    for changes, error in ((dict(accepted_occurrence_id=None), "ck_release_source_witness_pair"),
        (dict(activation_source_item_id=extra.item.source_item_id), "fk_release_source_item_binding"),
        (dict(accepted_occurrence_id=pointers[parts[3].sources[0].source_item_id]["occurrence_id"]), "fk_release_source_accepted_check")):
        with pytest.raises(IntegrityError, match=error):
            with parts[0].begin() as connection:
                release = connection.scalar(data_release.insert().values(request_key="raw", publishing_job_id=parts[1].job_id).returning(data_release.c.release_id))
                connection.execute(release_company.insert().values(release_id=release, security_id=parts[2].security_id, snapshot_id=parts[2].snapshot_id, exact_bundle_id=parts[4]))
                connection.execute(release_company_lineage.insert().values(release_id=release, security_id=parts[2].security_id, assessment="COMPLETE"))
                connection.execute(release_company_source.insert().values(**{**witness, "release_id": release, **changes}))
    with pytest.raises(IntegrityError, match="immutable|sealed|published"):
        with parts[0].begin() as connection:
            connection.execute(update(release_company_source).where(release_company_source.c.release_id == first.release_id).values(accepted_occurrence_id=None, activation_source_item_id=None))


@pytest.mark.parametrize("expires", [False, True])
def test_expired_lease_after_witness_lock_wait_rolls_back_then_valid_retry(storage, monkeypatch, expires):
    parts = setup(storage)
    candidate = register(parts)
    jobs = DurableJobRepository(parts[0]); now = datetime.now(timezone.utc)
    jobs.enqueue_scheduled(schedule_key="real-publisher", scheduled_for=now, kind="real-publisher", parameters={}, due_at=now)
    live = jobs.claim_next(owner="real-publisher", lease_duration=timedelta(seconds=1 if expires else 10), allowed_kinds={"real-publisher"})
    repository = PublicationRepository(parts[0])
    import screener.publication as owner
    original = owner.insert_release_lineage
    entered = Event()
    def wait_late(connection, *args, **kwargs):
        original(connection, *args, **kwargs)
        entered.set()
        connection.execute(text("SELECT pg_advisory_xact_lock(9017)"))
    monkeypatch.setattr(owner, "insert_release_lineage", wait_late)
    with parts[0].connect() as blocker:
        tx = blocker.begin()
        blocker.execute(text("SELECT pg_advisory_xact_lock(9017)"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(repository.publish, live.lease, request_key="expire", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=None)
            assert entered.wait(5)
            with parts[0].connect() as observer:
                deadline = monotonic()+3
                while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
                    assert monotonic()<deadline; sleep(.01)
            sleep(1.1 if expires else .05); tx.rollback()
            if expires:
                with pytest.raises(LeaseLost): future.result(5)
            else:
                release = future.result(5)
    if not expires:
        assert repository.active() == release
        assert lineage(parts[0], release.release_id)[0]["accepted_occurrence_id"] == parts[3].sources[0].occurrence_id
        return
    assert repository.active() is None
    with parts[0].connect() as connection:
        for table in (data_release, release_company, release_candidate, release_company_source, release_company_lineage):
            assert connection.scalar(select(func.count()).select_from(table)) == 0
    monkeypatch.setattr(owner, "insert_release_lineage", original)
    assert publish(parts, candidate).member_count == 1


def test_bound_resource_replay_does_not_rediscover_a_later_same_artifact_revision(storage):
    parts = setup(storage)
    filing, resource = _metadata_and_document(parts[0],storage[1],parts[1])
    with parts[0].connect() as connection:
        pointers = {r["source_observation_id"]:r for r in connection.execute(select(source_item_current)).mappings()}
    graph = parts[3].model_copy(update={"pipeline_run_key":"derive:resource","sources": tuple(BundleSource(
        source_item_id=pointers[value]["source_item_id"],source_observation_id=value,occurrence_id=pointers[value]["occurrence_id"]) for value in (resource,))})
    identifier = FigureHistoryRepository(parts[0],clock=lambda:NOW).store(parts[1],request_key="resource",bundle=graph)
    changes = dict(source_observation_ids=(filing,),exact_bundle_id=identifier,expected_exact_run_key="derive:resource")
    candidate = register(parts,**changes)
    _metadata_and_document(parts[0],storage[1],parts[1],resource_revision=2)
    assert register(parts,**changes)==candidate
    with pytest.raises(StalePublicationSource): publish(parts,candidate)


def test_binding_rejects_another_actual_snapshot_and_unsealed_graph(storage):
    parts=setup(storage)
    other=_company(parts[0],storage[1],value=90,lease=parts[1])
    with pytest.raises(ValueError,match="snapshot/security/producer"):
        register(parts,snapshot_id=other.snapshot_id)
    with parts[0].begin() as connection:
        row=dict(connection.execute(select(exact_figure_bundle)).mappings().one())
        row.pop("bundle_id");row["sealed_at"]=None;row["request_key"]="unsealed"
        identifier=connection.scalar(exact_figure_bundle.insert().values(**row).returning(exact_figure_bundle.c.bundle_id))
    with pytest.raises(ValueError,match="sealed"):
        register(parts,exact_bundle_id=identifier)


@pytest.mark.parametrize("field,value",[("exact_bundle_sha256","c"*64),("expected_exact_run_key","wrong"),("exact_bundle_id",99999)])
def test_manifest_two_immutable_graph_identity_forgery_is_rejected(storage,field,value):
    parts=setup(storage)
    preparer,objects,company,prepared=prepare_display(storage,parts)
    manifest=json.loads(prepared.manifest_bytes);rows=json.loads(prepared.rows_bytes)
    entry=manifest["members"][0];entry[field]=value
    payload={k:v for k,v in entry.items() if k!="input_bundle_sha256"}
    entry["input_bundle_sha256"]=preparer._store(payload)
    rows[0]["input_bundle_sha256"]=entry["input_bundle_sha256"]
    forged=PreparedDisplayRelease(prepared.projection_revision,preparer._store(manifest),canonical_bytes(manifest),canonical_bytes(rows))
    with pytest.raises(ValueError):preparer.verify(forged)


@pytest.mark.parametrize("expires",[False,True])
def test_graph_source_sorted_lock_wait_uses_live_lease(storage,expires):
    parts=setup(storage)
    extra=record(storage,"lock-extra",1,source_key="extra")
    with parts[0].connect() as connection:
        pointer=connection.execute(select(source_item_current).where(source_item_current.c.source_item_id==extra.item.source_item_id)).mappings().one()
    graph=parts[3].model_copy(update={"pipeline_run_key":"derive:lock","sources":(*parts[3].sources,BundleSource(
        source_item_id=extra.item.source_item_id,source_observation_id=extra.observation.source_observation_id,occurrence_id=pointer["occurrence_id"]))})
    identifier=FigureHistoryRepository(parts[0],clock=lambda:NOW).store(parts[1],request_key="lock-graph",bundle=graph)
    candidate=register(parts,exact_bundle_id=identifier,expected_exact_run_key="derive:lock")
    jobs=DurableJobRepository(parts[0]);now=datetime.now(timezone.utc)
    jobs.enqueue_scheduled(schedule_key="source-lock",scheduled_for=now,kind="source-lock",parameters={},due_at=now)
    live=jobs.claim_next(owner="source-lock",lease_duration=timedelta(seconds=1 if expires else 10),allowed_kinds={"source-lock"})
    repo=PublicationRepository(parts[0])
    with parts[0].connect() as blocker:
        tx=blocker.begin();blocker.execute(select(source_item.c.source_item_id).where(source_item.c.source_item_id==extra.item.source_item_id).with_for_update()).one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(repo.publish,live.lease,request_key="source-lock",candidate_ids=(candidate.candidate_id,),expected_parent_release_id=None)
            with parts[0].connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                deadline=monotonic()+3
                while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
                    assert monotonic()<deadline and not future.done();sleep(.01)
            sleep(1.1 if expires else .05);tx.rollback()
            if expires:
                with pytest.raises(LeaseLost):future.result(5)
            else:release=future.result(5)
    if expires:assert repo.active() is None
    else:
        assert repo.active()==release
        assert len(lineage(parts[0],release.release_id))==2


def test_bound_publication_replays_with_original_witnesses_in_a_new_process(storage,tmp_path):
    parts=setup(storage);candidate=register(parts);first=publish(parts,candidate)
    before=lineage(parts[0],first.release_id)
    record(storage,"after-publication",2)
    path=tmp_path/"replay.json"
    path.write_text(json.dumps(dict(lease=asdict(parts[1]),candidate_id=candidate.candidate_id,bundle_id=parts[4],snapshot_id=parts[2].snapshot_id,source_id=parts[3].sources[0].source_observation_id)))
    process=subprocess.run([sys.executable,"-c", """
import json,sys
from datetime import datetime,timezone
from sqlalchemy import create_engine,select
from screener.storage_config import StorageSettings
from screener.publication import PublicationRepository
from screener.durable_jobs import LeaseToken
from screener.postgres import release_company_source
v=json.load(open(sys.argv[1]));engine=create_engine(StorageSettings.from_env().database_url)
try:
 repo=PublicationRepository(engine,clock=lambda:datetime(2026,10,4,9,tzinfo=timezone.utc));lease=LeaseToken(**v['lease'])
 candidate=repo.register_candidate(lease,request_key='candidate',snapshot_id=v['snapshot_id'],expected_parent_release_id=None,source_observation_ids=(v['source_id'],),cause_type='source_change',exact_bundle_id=v['bundle_id'],expected_exact_run_key='derive:test')
 release=repo.publish(lease,request_key='release',candidate_ids=(candidate.candidate_id,),expected_parent_release_id=None)
 with engine.connect() as c: witnesses=[dict(r) for r in c.execute(select(release_company_source).where(release_company_source.c.release_id==release.release_id)).mappings()]
 print(json.dumps({'release_id':release.release_id,'witnesses':witnesses}))
finally: engine.dispose()
""",str(path)],capture_output=True,text=True,check=True,timeout=15)
    assert json.loads(process.stdout)==dict(release_id=first.release_id,witnesses=before)


def test_accepted_pointer_cannot_be_replaced_by_unknown_after_registration(storage):
    parts=setup(storage);candidate=register(parts)
    with pytest.raises(IntegrityError,match="requires an accepted check"):
        with parts[0].begin() as connection:
            connection.execute(update(source_item_current).values(occurrence_id=None,checked_at=None))
    release=publish(parts,candidate)
    assert lineage(parts[0],release.release_id)[0]["accepted_occurrence_id"]==parts[3].sources[0].occurrence_id


def test_display_another_graph_for_same_snapshot_is_a_transactional_mismatch(storage):
    parts=setup(storage);candidate=register(parts)
    identifier=FigureHistoryRepository(parts[0],clock=lambda:NOW).store(parts[1],request_key="display-other",
        bundle=parts[3].model_copy(update={"pipeline_run_key":"derive:other"}))
    other=(*parts[:3],parts[3].model_copy(update={"pipeline_run_key":"derive:other"}),identifier,parts[-1])
    _,objects,_,prepared=prepare_display(storage,other)
    with pytest.raises(ValueError,match="exactly one projection"):
        parts[-1].publish_display(parts[1],request_key="different-graph",candidate_ids=(candidate.candidate_id,),
            expected_parent_release_id=None,prepared=prepared,object_store=objects)
    assert parts[-1].active() is None


def test_graph_only_source_writer_waits_and_preserves_its_real_removed_result(storage,monkeypatch):
    parts=setup(storage)
    extra=record(storage,"barrier-extra",1,source_key="barrier-extra")
    with parts[0].connect() as connection:
        pointer=connection.execute(select(source_item_current).where(source_item_current.c.source_item_id==extra.item.source_item_id)).mappings().one()
    graph=parts[3].model_copy(update={"pipeline_run_key":"derive:barrier","sources":(*parts[3].sources,BundleSource(
        source_item_id=extra.item.source_item_id,source_observation_id=extra.observation.source_observation_id,occurrence_id=pointer["occurrence_id"]))})
    identifier=FigureHistoryRepository(parts[0],clock=lambda:NOW).store(parts[1],request_key="barrier-graph",bundle=graph)
    candidate=register(parts,exact_bundle_id=identifier,expected_exact_run_key="derive:barrier")
    entered,resume=Event(),Event();original=parts[-1]._activate
    def held(*args):
        entered.set();assert resume.wait(10);return original(*args)
    monkeypatch.setattr(parts[-1],"_activate",held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        published=pool.submit(publish,parts,candidate)
        assert entered.wait(5)
        changed=pool.submit(record,storage,"barrier-removed",2,source_key="barrier-extra",state="removed")
        try:
            with parts[0].connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                deadline=monotonic()+3
                while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
                    assert monotonic()<deadline and not changed.done();sleep(.01)
        finally:resume.set()
        release=published.result(10);removed=changed.result(10)
    witness=next(r for r in lineage(parts[0],release.release_id) if r["source_observation_id"]==extra.observation.source_observation_id)
    assert witness["accepted_occurrence_id"]==pointer["occurrence_id"]
    with parts[0].connect() as connection:
        assert connection.scalar(select(source_item_current.c.source_observation_id).where(source_item_current.c.source_item_id==extra.item.source_item_id))==removed.observation.source_observation_id
    assert removed.observation.state=="removed"


def test_bound_replay_ignores_new_same_artifact_source_items(storage):
    parts=setup(storage);candidate=register(parts)
    lease=_claim(parts[0],"new-resource")
    SourceObservationRepository(parts[0],clock=lambda:NOW).record(lease,
        item=SourceItemIdentity("SEC","resource","new-same-artifact","0000001800"),item_outcome_key="new-resource",state="present",
        metadata={"role":"primary_document"},source_url="https://www.sec.gov/new-resource",artifact_sha256=storage[1].content_sha256)
    assert register(parts)==candidate
    assert publish(parts,candidate).member_count==1


def test_pre_check_legacy_source_cannot_claim_exact_accepted_activation(monkeypatch):
    from uuid import uuid4
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from screener.storage_config import StorageSettings
    from screener.artifacts import SqlArtifactRepository,store_evidence
    from test_figure_history_contract2_storage import objects
    from tests.test_figure_history import example_bundle
    settings=StorageSettings.from_env();base=make_url(settings.database_url)
    assert base.host=="127.0.0.1" and base.port==55432
    name="ms_binding_legacy_"+uuid4().hex
    admin=create_engine(base.set(database="postgres"),isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine=create_engine(base.set(database=name))
    try:
        monkeypatch.setenv("SCREENER_DATABASE_URL",engine.url.render_as_string(hide_password=False))
        config=Config("alembic.ini");command.upgrade(config,"20261004_0009")
        artifact=store_evidence(b"synthetic legacy source before accepted checks","application/json",objects(),SqlArtifactRepository(engine))
        lease=_claim(engine,"legacy-unknown")
        company=_company(engine,artifact)
        with engine.begin() as connection:
            item=connection.scalar(source_item.insert().values(source_system="SEC",item_kind="filing",source_key="filing-1",issuer_source_identifier="0000001800").returning(source_item.c.source_item_id))
            observed=connection.scalar(source_observation.insert().values(source_item_id=item,observation_sha256="a"*64,state="present",canonical_metadata={},
                source_url="https://www.sec.gov/filing-1",artifact_sha256=artifact.content_sha256,detected_at=NOW,detecting_job_id=lease.job_id).returning(source_observation.c.source_observation_id))
            snapshot_hash=connection.scalar(select(company_snapshot.c.snapshot_sha256))
        command.upgrade(config,"head")
        with engine.connect() as connection:
            assert connection.scalar(select(source_item_current.c.occurrence_id)) is None
        bundle=example_bundle(security_id=company.security_id,snapshot_id=company.snapshot_id,snapshot_sha256=snapshot_hash,
            artifact_sha256=artifact.content_sha256,source_item_id=item,source_observation_id=observed,occurrence_id=1)
        bundle=bundle.model_copy(update={"sources":(bundle.sources[0].model_copy(update={"occurrence_id":None}),),"coverage":(bundle.coverage[0].model_copy(update={"assessment":"PARTIAL","reason":"Accepted legacy source check was never captured"}),)})
        identifier=FigureHistoryRepository(engine,clock=lambda:NOW).store(lease,request_key="legacy-partial",bundle=bundle)
        parts=(engine,lease,company,bundle,identifier,PublicationRepository(engine,clock=lambda:NOW))
        candidate=register(parts)
        with pytest.raises(ValueError,match="accepted source-check"):publish(parts,candidate)
        assert parts[-1].active() is None
        legacy=register(parts,key="legacy-unbound",exact_bundle_id=None,expected_exact_run_key=None)
        release=publish(parts,legacy,key="legacy-unbound")
        witness=lineage(engine,release.release_id)[0]
        assert witness["accepted_occurrence_id"] is witness["activation_source_item_id"] is None
    finally:
        engine.dispose()
        with admin.connect() as connection:connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
