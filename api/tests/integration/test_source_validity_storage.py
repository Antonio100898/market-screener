import json
import os
import subprocess
import sys
from datetime import timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from screener.durable_jobs import DurableJobRepository
from screener.postgres import (active_data_release, company_snapshot, data_release, issuer, priced_security,
    release_company, release_company_lineage, release_company_source,
    source_check_occurrence, source_item, source_item_current, source_observation, source_validity_generation)
from screener.source_observations import SourceItemIdentity, SourceObservationRepository
from screener.source_validity import SourceValidityRepository
from screener.storage_config import StorageSettings

from test_publication_storage import NOW, _claim, _company, _setup, _source, storage


pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1", reason="requires real local PostgreSQL and S3")


def test_release_bound_lineage_tracks_source_status_and_keeps_financial_values(storage):
    engine, artifact, lease, publication, first, original, _, candidate, source_id = _setup(storage)
    validity = SourceValidityRepository(engine)
    baseline = validity.assess(first.release_id, [original.security_id])
    assert baseline["companies"][0]["status"] == "NOT_ASSESSED"
    second = publication.publish(lease, request_key="validated", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=first.release_id)
    matched = validity.assess(second.release_id, [original.security_id])
    assert matched["companies"][0]["status"] == "CAPTURED_MATCH"
    with engine.connect() as connection:
        before = connection.execute(select(company_snapshot)).mappings().all()
    observations = SourceObservationRepository(engine, clock=lambda: NOW)
    for index, (state, reason, expected) in enumerate([
        ("present", "metadata_edited", "REVALIDATION_REQUIRED"),
        ("pending", "not_found", "INPUTS_PENDING"),
        ("unavailable", "viewing_period_expired", "SOURCE_ACCESS_UNAVAILABLE"),
        ("unavailable", "not_disclosed", "SOURCE_ACCESS_UNAVAILABLE"),
        ("removed", "absent_from_rebuilt_quarter", "SOURCE_REMOVED_REVIEW_REQUIRED"),
        ("removed", "withdrawn", "WITHDRAWN"),
    ]):
        observations.record(lease, item=SourceItemIdentity("SEC", "filing", "filing-1", "0000001800"),
            item_outcome_key=f"change-{index}", state=state, metadata={"reason": reason},
            source_url="https://www.sec.gov/filing-1", artifact_sha256=artifact.content_sha256 if state == "present" else None,
            detected_at=NOW + timedelta(seconds=index + 1))
        assessed = validity.assess(second.release_id, [original.security_id])
        assert assessed["companies"][0]["status"] == "REVALIDATION_REQUIRED"
        assert assessed["companies"][0]["source_statuses"] == [expected]
        assert assessed["companies"][0]["dependencies"][0]["bound_observation_id"] == source_id
        assert assessed["validity_generation"] > matched["validity_generation"]
        assert assessed["numeric_invalidation_assessed"] is False
    with engine.connect() as connection:
        assert connection.execute(select(company_snapshot)).mappings().all() == before
    engine.dispose()
    assert validity.assess(second.release_id, [original.security_id]) == assessed
    process = subprocess.run([sys.executable, "-c", """
import json, sys
from sqlalchemy import create_engine
from screener.source_validity import SourceValidityRepository
from screener.storage_config import StorageSettings
engine = create_engine(StorageSettings.from_env().database_url)
try:
    result = SourceValidityRepository(engine).assess(int(sys.argv[1]), [int(sys.argv[2])])
    print(json.dumps(result, default=str))
finally:
    engine.dispose()
""", str(second.release_id), str(original.security_id)], check=True, capture_output=True, text=True, timeout=15)
    assert json.loads(process.stdout) == json.loads(json.dumps(assessed, default=str))


def test_replay_and_delayed_check_do_not_rewind_current_and_failed_outcome_rolls_back(storage, monkeypatch):
    engine, artifact, lease, _, _, _, _, _, _ = _setup(storage)
    observations = SourceObservationRepository(engine, clock=lambda: NOW)
    item = SourceItemIdentity("SEC", "resource", "runtime", "0000001800")
    common = dict(item=item, metadata={}, source_url="https://www.sec.gov/runtime")
    first = observations.record(lease, item_outcome_key="A", state="present", artifact_sha256=artifact.content_sha256, detected_at=NOW, **common)
    second = observations.record(lease, item_outcome_key="B", state="pending", detected_at=NOW + timedelta(seconds=2), **common)
    observations.record(lease, item_outcome_key="A", state="present", artifact_sha256=artifact.content_sha256, detected_at=NOW + timedelta(seconds=3), **common)
    observations.record(lease, item_outcome_key="delayed", state="present", artifact_sha256=artifact.content_sha256, detected_at=NOW + timedelta(seconds=1), **common)
    assert observations.latest_item("SEC", "resource", "runtime").observation.source_observation_id == second.observation.source_observation_id
    with engine.connect() as connection:
        counts = tuple(connection.scalar(select(func.count()).select_from(table)) for table in (source_observation, source_check_occurrence))
        generation = connection.scalar(select(source_validity_generation.c.generation))

    def fail(*args, **kwargs):
        raise RuntimeError("simulated checkpoint failure")

    monkeypatch.setattr(observations._jobs, "_record_item_outcome_on_connection", fail)
    with pytest.raises(RuntimeError, match="checkpoint"):
        observations.record(lease, item_outcome_key="failed", state="unavailable", detected_at=NOW + timedelta(seconds=4), **common)
    with engine.connect() as connection:
        assert tuple(connection.scalar(select(func.count()).select_from(table)) for table in (source_observation, source_check_occurrence)) == counts
        assert connection.scalar(select(source_validity_generation.c.generation)) == generation
    assert observations.latest_item("SEC", "resource", "runtime").observation.source_observation_id == second.observation.source_observation_id
    assert first.observation.source_observation_id != second.observation.source_observation_id


def test_identical_fresh_check_updates_occurrence_without_changing_validity_generation(storage):
    engine, artifact, lease, _, _, _, _, _, _ = _setup(storage)
    observations = SourceObservationRepository(engine, clock=lambda: NOW)
    item = SourceItemIdentity("SEC", "resource", "unchanged", "0000001800")
    common = dict(item=item, state="present", metadata={}, source_url="https://www.sec.gov/unchanged",
                  artifact_sha256=artifact.content_sha256)
    first = observations.record(lease, item_outcome_key="first-check", detected_at=NOW, **common)
    with engine.connect() as connection:
        generation = connection.scalar(select(source_validity_generation.c.generation))
        occurrence = connection.scalar(select(source_item_current.c.occurrence_id).where(
            source_item_current.c.source_observation_id == first.observation.source_observation_id))
    rechecked_at = NOW + timedelta(seconds=1)
    second = observations.record(lease, item_outcome_key="new-check", detected_at=rechecked_at, **common)
    assert second.observation.source_observation_id == first.observation.source_observation_id
    with engine.connect() as connection:
        pointer = connection.execute(select(source_item_current).where(
            source_item_current.c.source_observation_id == first.observation.source_observation_id)).mappings().one()
        assert pointer["occurrence_id"] != occurrence
        assert pointer["checked_at"] == rechecked_at
        assert connection.scalar(select(source_validity_generation.c.generation)) == generation


def test_inherited_complete_lineage_is_immutable_and_other_company_stays_unknown(storage):
    engine, artifact, lease, publication, first, original, _, candidate, _ = _setup(storage)
    second = publication.publish(lease, request_key="second", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=first.release_id)
    other = _company(engine, artifact, value=300, ticker="OTHER", identifier="2", lease=lease)
    source = _source(engine, artifact, lease, identifier="2", key="other")
    candidate2 = publication.register_candidate(lease, request_key="other-candidate", snapshot_id=other.snapshot_id,
        expected_parent_release_id=second.release_id, source_observation_ids=(source,), cause_type="source_change")
    third = publication.publish(lease, request_key="third", candidate_ids=(candidate2.candidate_id,), expected_parent_release_id=second.release_id)
    validity = SourceValidityRepository(engine)
    assert [row["status"] for row in validity.assess(third.release_id, [original.security_id, other.security_id])["companies"]] == ["CAPTURED_MATCH", "CAPTURED_MATCH"]
    _source(engine, artifact, lease, revision=2, state="pending", detected_at=NOW + timedelta(seconds=1))
    changed = {row["security_id"]: row for row in validity.assess(third.release_id, [original.security_id, other.security_id])["companies"]}
    assert changed[original.security_id]["status"] == "REVALIDATION_REQUIRED"
    assert changed[other.security_id]["status"] == "CAPTURED_MATCH"
    for statement in [release_company_source.delete(), update(release_company_lineage).values(assessment="NOT_ASSESSED"),
                      update(source_check_occurrence).values(item_key="changed"), source_check_occurrence.delete(), source_item_current.delete()]:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(statement)


def test_populated_0009_migration_marks_legacy_checks_and_release_lineage_unassessed(monkeypatch):
    settings = StorageSettings.from_env()
    name = f"ms_validity_upgrade_{uuid4().hex}"
    assert name.startswith("ms_validity_upgrade_") and len(name) == 52
    admin = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    url = make_url(settings.database_url).set(database=name)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url)
    try:
        monkeypatch.setenv("SCREENER_DATABASE_URL", url.render_as_string(hide_password=False))
        config = Config("alembic.ini")
        command.upgrade(config, "20261004_0009")
        lease = _claim(engine, "legacy")
        with engine.begin() as connection:
            item = connection.scalar(source_item.insert().values(source_system="SEC", item_kind="resource", source_key="legacy").returning(source_item.c.source_item_id))
            observation = connection.scalar(source_observation.insert().values(source_item_id=item, observation_sha256="a" * 64,
                state="pending", canonical_metadata={}, source_url="https://www.sec.gov/legacy", detected_at=NOW,
                detecting_job_id=lease.job_id).returning(source_observation.c.source_observation_id))
            issuer_id = connection.scalar(issuer.insert().values(source_system="SEC", source_identifier="0000001800").returning(issuer.c.issuer_id))
            security = connection.scalar(priced_security.insert().values(issuer_id=issuer_id, security_identifier="legacy").returning(priced_security.c.security_id))
            snapshot = connection.scalar(company_snapshot.insert().values(security_id=security, engine_revision=187,
                payload_sha256="a" * 64, snapshot_sha256="a" * 64, canonical_payload={"value": 100},
                ticker="ABT", security_basis="PRIMARY_COMMON_SHARE").returning(company_snapshot.c.snapshot_id))
            release = connection.scalar(data_release.insert().values(request_key="legacy", publishing_job_id=lease.job_id).returning(data_release.c.release_id))
            connection.execute(release_company.insert().values(release_id=release, security_id=security, snapshot_id=snapshot))
            connection.execute(update(data_release).where(data_release.c.release_id == release).values(published_at=NOW))
            connection.execute(update(active_data_release).values(release_id=release))
        command.upgrade(config, "head")
        command.current(config); command.heads(config); command.check(config)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(config).get_current_head()
            pointer = connection.execute(select(source_item_current)).mappings().one()
            assert pointer["source_observation_id"] == observation
            assert pointer["occurrence_id"] is None and pointer["checked_at"] is None
            assert connection.scalar(select(source_validity_generation.c.generation)) == 0
            assert connection.scalar(select(release_company_lineage.c.assessment)) == "NOT_ASSESSED"
            assert connection.scalar(select(company_snapshot.c.canonical_payload)) == {"value": 100}
        assert SourceValidityRepository(engine).assess(release, [security])["companies"][0]["status"] == "NOT_ASSESSED"
        observations = SourceObservationRepository(engine, clock=lambda: NOW)
        observations.record(lease, item=SourceItemIdentity("SEC", "resource", "legacy"), item_outcome_key="delayed",
            state="unavailable", metadata={}, source_url="https://www.sec.gov/legacy", detected_at=NOW - timedelta(seconds=1))
        assert observations.latest_item("SEC", "resource", "legacy").observation.source_observation_id == observation
        observations.record(lease, item=SourceItemIdentity("SEC", "resource", "legacy"), item_outcome_key="rechecked",
            state="pending", metadata={}, source_url="https://www.sec.gov/legacy", detected_at=NOW + timedelta(seconds=1))
        with engine.connect() as connection:
            assert connection.scalar(select(source_item_current.c.occurrence_id)) is not None
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
