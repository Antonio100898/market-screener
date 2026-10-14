"""Atomic staged snapshot/history composition; financial values are synthetic."""
import os
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from time import monotonic, sleep

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select, text

from screener.durable_jobs import DurableJobRepository, LeaseLost
from screener.figure_history import FigureHistoryConflict, FigureHistoryRepository, _digest
from screener.postgres import (company_snapshot, company_snapshot_artifact, current_company_snapshot,
    durable_job, evidence_artifact, exact_figure_bundle, figure_observation, figure_selection,
    figure_dependency, figure_bundle_coverage, figure_bundle_source, issuer, priced_security, source_item,
    source_item_current)
from screener.shared_companies import IdentityConflict, SharedCompanyRepository, SnapshotArtifact
from tests.test_figure_history import example_bundle
from test_publication_storage import NOW, _claim, _source, storage

pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1", reason="requires real local PostgreSQL/S3")
PAIRED = (issuer, priced_security, company_snapshot, company_snapshot_artifact, exact_figure_bundle,
          figure_observation, figure_selection, figure_dependency, figure_bundle_coverage, figure_bundle_source,
          current_company_snapshot)


def prepare(storage):
    engine, artifact = storage
    lease = _claim(engine, "paired-stage")
    observation = _source(engine, artifact, lease)
    with engine.connect() as connection:
        source = connection.execute(select(source_item_current).where(
            source_item_current.c.source_observation_id == observation)).mappings().one()
    values = dict(issuer_source="SEC", issuer_identifier="1800", security_identifier="sec:0000001800:primary",
        ticker="ABT", security_title="Common Stock", source_accession="filing-1", security_basis="PRIMARY_COMMON_SHARE",
        engine_revision=187, canonical_payload={"value": 100}, artifacts=(SnapshotArtifact(artifact.content_sha256, "raw_filing"),))
    return engine, artifact, lease, source, values


def bundle_for(stored, artifact, source):
    return example_bundle(security_id=stored.security_id, snapshot_id=stored.snapshot_id,
        snapshot_sha256=stored.snapshot_sha256, engine_revision=stored.engine_revision,
        artifact_sha256=artifact.content_sha256, source_item_id=source["source_item_id"],
        source_observation_id=source["source_observation_id"], occurrence_id=source["occurrence_id"])


def counts(engine):
    with engine.connect() as connection:
        return {table.name: connection.scalar(select(func.count()).select_from(table)) for table in PAIRED}


def compose(engine, lease, artifact, source, values, *, clock=lambda: NOW, request="paired", change=None):
    companies, history = SharedCompanyRepository(engine, clock=clock), FigureHistoryRepository(engine, clock=clock)
    with engine.begin() as connection:
        stored = companies.store_candidate_on_connection(connection, lease, **values)
        bundle = bundle_for(stored, artifact, source)
        if change:
            bundle = change(bundle)
        identifier = history.store_on_connection(connection, lease, request_key=request, bundle=bundle)
    return stored, identifier, bundle


def test_actual_caller_transaction_commit_replay_identity_and_new_client(storage):
    engine, artifact, lease, source, values = prepare(storage)
    companies, history = SharedCompanyRepository(engine, clock=lambda: NOW), FigureHistoryRepository(engine, clock=lambda: NOW)
    with engine.begin() as connection:
        stored = companies.store_candidate_on_connection(connection, lease, **values)
        assert stored.snapshot_sha256 == connection.scalar(select(company_snapshot.c.snapshot_sha256))
        assert stored.snapshot_sha256 != stored.payload_sha256
        bundle = bundle_for(stored, artifact, source)
        identifier = history.store_on_connection(connection, lease, request_key="paired", bundle=bundle)
        assert connection.in_transaction()
        # Separate clients cannot observe either half before caller commit.
        assert all(value == 0 for value in counts(engine).values())
    assert history.load(identifier) == bundle
    assert companies.snapshot(stored.snapshot_id).snapshot.snapshot_sha256 == stored.snapshot_sha256
    repeated, again, same = compose(engine, lease, artifact, source, values)
    assert repeated.snapshot_id == stored.snapshot_id and repeated.created is False
    assert again == identifier and same == bundle
    # The old transaction-owning wrapper reuses exactly the same immutable identity.
    assert companies.store_candidate(lease, **values) == repeated
    engine.dispose()
    reopened = create_engine(engine.url)
    try:
        assert FigureHistoryRepository(reopened).load(identifier) == bundle
        assert SharedCompanyRepository(reopened).snapshot(stored.snapshot_id).snapshot.snapshot_sha256 == stored.snapshot_sha256
    finally:
        reopened.dispose()
    process = subprocess.run([sys.executable, "-c", """
import json, sys
from sqlalchemy import create_engine
from screener.storage_config import StorageSettings
from screener.shared_companies import SharedCompanyRepository
from screener.figure_history import FigureHistoryRepository
engine = create_engine(StorageSettings.from_env().database_url)
try:
    snapshot = SharedCompanyRepository(engine).snapshot(int(sys.argv[1]))
    bundle = FigureHistoryRepository(engine).load(int(sys.argv[2]))
    print(json.dumps(dict(snapshot_sha256=snapshot.snapshot.snapshot_sha256, bundle=bundle.model_dump(mode='json'))))
finally:
    engine.dispose()
""", str(stored.snapshot_id), str(identifier)], capture_output=True, text=True, check=True, timeout=15)
    assert json.loads(process.stdout) == dict(snapshot_sha256=stored.snapshot_sha256, bundle=bundle.model_dump(mode="json"))
    result = counts(engine)
    assert result["company_snapshot"] == result["exact_figure_bundle"] == 1
    assert result["current_company_snapshot"] == 0


@pytest.mark.parametrize("fault", ["graph", "hash", "engine", "issuer", "artifact", "source", "occurrence", "security"])
def test_history_admission_failure_rolls_back_new_snapshot_identities_and_links(storage, fault):
    engine, artifact, lease, source, values = prepare(storage)
    def change(bundle):
        if fault in {"hash", "engine", "security"}:
            key, value = {"hash": ("snapshot_sha256", "c" * 64), "engine": ("engine_revision", 188),
                          "security": ("security_id", 999999)}[fault]
            return bundle.model_copy(update={key: value})
        if fault == "graph":
            return bundle.model_copy(update={"selections": (bundle.selections[0].model_copy(update={"value_decimal_text": "101"}),)})
        if fault in {"source", "occurrence"}:
            key = "source_item_id" if fault == "source" else "occurrence_id"
            return bundle.model_copy(update={"sources": (bundle.sources[0].model_copy(update={key: 999999}),)})
        observed = bundle.observations[0]
        key, value = ("issuer_source_identifier", "0000009999") if fault == "issuer" else ("artifact_sha256", "c" * 64)
        return bundle.model_copy(update={"observations": (observed.model_copy(update={"reference": observed.reference.model_copy(update={key: value})}),)})
    expected = {"graph": "preserve its exact observation", "hash": "actual snapshot", "engine": "actual snapshot",
        "security": "actual snapshot", "issuer": "observation issuer differs", "artifact": "observation artifact/source differs",
        "source": "captured issuer/artifact revision", "occurrence": "accepted source occurrence"}
    with pytest.raises((ValueError, ValidationError), match=expected[fault]):
        compose(engine, lease, artifact, source, values, change=change)
    assert all(value == 0 for value in counts(engine).values())
    # Pre-transaction registered artifact and captured source survive unchanged.
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(evidence_artifact)) == 1
        assert connection.scalar(select(source_item_current.c.source_observation_id)) == source["source_observation_id"]


def test_failure_after_both_owner_writes_rolls_back_the_complete_pair(storage):
    engine, artifact, lease, source, values = prepare(storage)
    companies, history = SharedCompanyRepository(engine, clock=lambda: NOW), FigureHistoryRepository(engine, clock=lambda: NOW)
    with pytest.raises(RuntimeError, match="caller late failure"):
        with engine.begin() as connection:
            stored = companies.store_candidate_on_connection(connection, lease, **values)
            history.store_on_connection(connection, lease, request_key="paired", bundle=bundle_for(stored, artifact, source))
            assert connection.scalar(select(func.count()).select_from(exact_figure_bundle)) == 1
            raise RuntimeError("caller late failure")
    assert all(value == 0 for value in counts(engine).values())
    # Neither failed operation consumed a permanent request identity.
    stored, identifier, bundle = compose(engine, lease, artifact, source, values)
    assert stored.created and FigureHistoryRepository(engine).load(identifier) == bundle


@pytest.mark.parametrize("configuration", ["connection", "engine"])
def test_actual_autocommit_rejects_logical_transaction_before_any_writes(storage, configuration):
    engine, _, lease, _, values = prepare(storage)
    owner_engine = create_engine(engine.url, isolation_level="AUTOCOMMIT") if configuration == "engine" else engine
    rejected = False
    try:
        with owner_engine.connect() as connection:
            if configuration == "connection":
                connection = connection.execution_options(isolation_level="AUTOCOMMIT")
            try:
                with connection.begin():
                    assert connection.in_transaction() and connection.get_transaction().is_active
                    assert connection.connection.dbapi_connection.autocommit is True
                    try:
                        SharedCompanyRepository(owner_engine, clock=lambda: NOW).store_candidate_on_connection(connection, lease, **values)
                    except ValueError as exc:
                        assert "autocommit" in str(exc)
                        rejected = True
                    else:
                        raise RuntimeError("late caller failure after staged snapshot")
            except RuntimeError as exc:
                assert str(exc) == "late caller failure after staged snapshot"
        actual = counts(engine)
        assert rejected, f"AUTOCOMMIT admitted a logical transaction and persisted a half pair: {actual}"
        assert all(value == 0 for value in actual.values())
    finally:
        if owner_engine is not engine:
            owner_engine.dispose()


def test_replay_conflict_rolls_back_second_new_snapshot_and_preserves_first(storage):
    engine, artifact, lease, source, values = prepare(storage)
    original, identifier, bundle = compose(engine, lease, artifact, source, values)
    before = counts(engine)
    with pytest.raises(FigureHistoryConflict, match="different exact bundle"):
        compose(engine, lease, artifact, source, {**values, "canonical_payload": {"value": 101}})
    assert counts(engine) == before
    assert FigureHistoryRepository(engine).load(identifier) == bundle
    with pytest.raises(IdentityConflict, match="priced security"):
        compose(engine, lease, artifact, source, {**values, "issuer_identifier": "9999"}, request="foreign")
    assert counts(engine) == before
    assert SharedCompanyRepository(engine).snapshot(original.snapshot_id).snapshot.snapshot_sha256 == original.snapshot_sha256


def test_two_concurrent_caller_transactions_replay_one_pair(storage):
    engine, artifact, lease, source, values = prepare(storage)
    barrier = Barrier(2)
    def run():
        barrier.wait(timeout=5)
        return compose(engine, lease, artifact, source, values)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        results = [f.result(timeout=10) for f in futures]
    assert results[0][0].snapshot_id == results[1][0].snapshot_id
    assert results[0][1] == results[1][1]
    assert sorted(r[0].created for r in results) == [False, True]
    assert counts(engine)["exact_figure_bundle"] == counts(engine)["company_snapshot"] == 1


@pytest.mark.parametrize("lock_kind,expired", [("job", True), ("issuer", True), ("artifact", True),
    ("history_source", True), ("history_catalog", True), ("job", False), ("history_source", False)])
def test_real_lock_wait_fences_atomic_pair(storage, lock_kind, expired):
    engine, artifact, _, source, values = prepare(storage)
    existing_issuer = None
    if lock_kind == "history_catalog":
        with engine.begin() as connection:
            existing_issuer = connection.scalar(issuer.insert().values(source_system="SEC", source_identifier="0000001800").returning(issuer.c.issuer_id))
    baseline = counts(engine)
    now = datetime.now(timezone.utc)
    jobs = DurableJobRepository(engine)
    jobs.enqueue_scheduled(schedule_key="live-paired", scheduled_for=now, kind="live-paired", parameters={}, due_at=now)
    claimed = jobs.claim_next(owner="paired-worker", lease_duration=timedelta(seconds=1.2 if expired else 10), allowed_kinds={"live-paired"})
    assert claimed is not None
    blocker = engine.connect()
    transaction = blocker.begin()
    if lock_kind == "job":
        blocker.execute(select(durable_job).where(durable_job.c.job_id == claimed.lease.job_id).with_for_update()).all()
    elif lock_kind == "issuer":
        blocker.execute(issuer.insert().values(source_system="SEC", source_identifier="0000001800"))
    elif lock_kind == "artifact":
        blocker.execute(select(evidence_artifact).with_for_update()).all()
    elif lock_kind == "history_source":
        blocker.execute(select(source_item).where(source_item.c.source_item_id == source["source_item_id"]).with_for_update()).all()
    else:
        observation = example_bundle(artifact_sha256=artifact.content_sha256).observations[0]
        record = observation.model_dump(mode="json")
        record.pop("key")
        value = record.pop("value_decimal_text")
        blocker.execute(figure_observation.insert().values(artifact_sha256=artifact.content_sha256,
            extraction_revision=observation.reference.extraction_revision,
            location_sha256=_digest(observation.reference.location.model_dump(mode="json")),
            issuer_id=existing_issuer, value_decimal_text=value, record=record))
    entered, pid = Event(), []
    def run():
        with engine.begin() as connection:
            pid.append(connection.scalar(text("SELECT pg_backend_pid()")))
            entered.set()
            companies, history = SharedCompanyRepository(engine), FigureHistoryRepository(engine)
            stored = companies.store_candidate_on_connection(connection, claimed.lease, **values)
            history.store_on_connection(connection, claimed.lease, request_key="paired", bundle=bundle_for(stored, artifact, source))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run)
            assert entered.wait(3)
            waited = False
            until = monotonic() + 3
            while monotonic() < until:
                with engine.connect() as observer:
                    waited = observer.scalar(text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid[0]})
                if waited:
                    break
                sleep(.01)
            assert waited, "must observe a real PostgreSQL lock wait"
            with engine.connect() as observer:
                expires = observer.scalar(select(durable_job.c.lease_expires_at).where(durable_job.c.job_id == claimed.lease.job_id))
            sleep(max(0, (expires - datetime.now(timezone.utc)).total_seconds()) + .05 if expired else .05)
            transaction.rollback()
            if expired:
                with pytest.raises(LeaseLost):
                    future.result(timeout=5)
            else:
                future.result(timeout=5)
    finally:
        if transaction.is_active:
            transaction.rollback()
        blocker.close()
    if expired:
        assert counts(engine) == baseline
    else:
        assert counts(engine)["company_snapshot"] == counts(engine)["exact_figure_bundle"] == 1
        assert counts(engine)["current_company_snapshot"] == 0
