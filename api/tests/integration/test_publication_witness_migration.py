"""Forward0017 preserves sealed0016 rows and adds only nullable witness guards."""
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from test_initial_publication_migration import previous_head, populate, pytestmark


def state(engine):
    with engine.connect() as connection:
        return dict(version=connection.scalar(text("SELECT version_num FROM alembic_version")),
            rows={name: connection.scalars(text(f"SELECT to_jsonb(t) - 'activation_source_item_id' - 'accepted_occurrence_id' FROM {name} t ORDER BY to_jsonb(t)::text")).all()
                for name in ("data_release", "release_company", "publication_candidate", "release_company_lineage", "release_company_source", "active_data_release", "source_observation", "source_check_occurrence", "source_item_current")},
            columns=connection.execute(text("SELECT column_name,is_nullable FROM information_schema.columns WHERE table_name='release_company_source' ORDER BY ordinal_position")).all())


def populate_legacy(engine):
    parts = populate(engine)
    source, security, snapshot, job = parts[-1], parts[6].security_id, parts[6].snapshot_id, parts[2].job_id
    with engine.begin() as connection:
        release = connection.scalar(text("INSERT INTO data_release(request_key,publishing_job_id,parent_release_id) VALUES('old-sealed-lineage',:job,:parent) RETURNING release_id"), dict(job=job,parent=parts[4].release_id))
        connection.execute(text("INSERT INTO release_company(release_id,security_id,snapshot_id) VALUES(:release,:security,:snapshot)"), dict(release=release,security=security,snapshot=snapshot))
        connection.execute(text("INSERT INTO release_company_lineage(release_id,security_id,assessment) VALUES(:release,:security,'COMPLETE')"), dict(release=release,security=security))
        connection.execute(text("INSERT INTO release_company_source(release_id,security_id,source_observation_id) VALUES(:release,:security,:source)"), dict(release=release,security=security,source=source))
        connection.execute(text("UPDATE data_release SET published_at=now() WHERE release_id=:release"),dict(release=release))
    return parts


@pytest.mark.parametrize("populated", [False, True])
def test_real_empty_and_populated_0016_upgrade_null_witnesses_and_head_parity(previous_head, populated):
    engine, config = previous_head
    command.upgrade(config, "20261004_0016")
    if populated: populate_legacy(engine)
    before = state(engine)
    command.upgrade(config, "20261004_0017")
    after = state(engine)
    assert after["version"] == "20261004_0017"
    assert before["rows"] == after["rows"]
    assert after["columns"] == before["columns"] + [("activation_source_item_id", "YES"),("accepted_occurrence_id", "YES")]
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM release_company_source WHERE activation_source_item_id IS NOT NULL OR accepted_occurrence_id IS NOT NULL")) == 0
        if populated: assert connection.scalar(text("SELECT count(*) FROM release_company_source")) == 1


def test_populated0016_migration_lock_timeout_rolls_back_and_retry_has_head_parity(previous_head):
    engine, config = previous_head
    command.upgrade(config,"20261004_0016")
    populate_legacy(engine)
    before = state(engine)
    with engine.connect() as blocker:
        transaction = blocker.begin()
        blocker.execute(text("SELECT count(*) FROM release_company_source"))
        start = monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(command.upgrade, config,"20261004_0017")
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                deadline = monotonic()+2
                while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
                    assert monotonic()<deadline and not future.done(); sleep(.01)
            with pytest.raises(OperationalError,match="lock timeout"): future.result(8)
        elapsed = monotonic()-start
        assert 4.5<elapsed<8
        transaction.rollback()
    assert state(engine)==before
    command.upgrade(config,"20261004_0017")
    assert state(engine)["rows"]==before["rows"]
    assert state(engine)["version"]=="20261004_0017"
    print(f"Observed0017 lock wait {elapsed:.3f}s, transactional old-schema/old-row rollback and valid retry; actual-head parity is checked by0018")
