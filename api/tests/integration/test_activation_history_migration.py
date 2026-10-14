"""Forward0018 leaves old sealed history untouched and qualifies new endpoints."""
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from test_initial_publication_migration import previous_head, pytestmark
from test_publication_witness_migration import populate_legacy


def state(engine):
    with engine.connect() as connection:
        return {"version": connection.scalar(text("SELECT version_num FROM alembic_version")),
            "rows": {name: connection.scalars(text(f"SELECT to_jsonb(t) - 'figure_history_revision' FROM {name} t ORDER BY to_jsonb(t)::text")).all()
                for name in ("data_release", "release_company", "publication_candidate", "publication_candidate_source",
                    "release_company_lineage", "release_company_source", "active_data_release", "source_observation", "source_check_occurrence", "source_item_current")},
            "columns": connection.execute(text("SELECT column_name,is_nullable FROM information_schema.columns WHERE table_name='data_release' ORDER BY ordinal_position")).all()}


@pytest.mark.parametrize("populated", [False, True])
def test_actual_empty_populated0017_to_head_preserves_every_row_and_null_marker(previous_head, populated):
    engine, config = previous_head
    command.upgrade(config, "20261004_0017")
    if populated: populate_legacy(engine)
    before = state(engine)
    command.upgrade(config, "20261004_0018")
    command.current(config); command.heads(config)
    after = state(engine)
    assert after["version"] == "20261004_0018"
    assert after["rows"] == before["rows"]
    assert after["columns"] == before["columns"] + [("figure_history_revision", "YES")]
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM data_release WHERE figure_history_revision IS NOT NULL")) == 0
        assert connection.scalar(text("SELECT count(*) FROM figure_activation_transition")) == 0
        assert connection.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgname IN ('qualify_figure_activation_endpoint','freeze_figure_activation_transition')")) == 2
        definition = connection.scalar(text("SELECT indexdef FROM pg_indexes WHERE indexname='uq_figure_transition_output'"))
        assert "NULLS NOT DISTINCT" in definition


def test_actual0018_lock_timeout_rollback_then_populated_retry_parity(previous_head):
    engine, config = previous_head
    command.upgrade(config, "20261004_0017"); populate_legacy(engine)
    before = state(engine)
    with engine.connect() as blocker:
        tx = blocker.begin(); blocker.execute(text("SELECT count(*) FROM data_release"))
        start = monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(command.upgrade, config, "20261004_0018")
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                deadline = monotonic()+2
                while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
                    assert monotonic()<deadline and not future.done(); sleep(.01)
            with pytest.raises(OperationalError, match="lock timeout"): future.result(8)
        elapsed = monotonic()-start
        assert 4.5<elapsed<8; tx.rollback()
    assert state(engine) == before
    command.upgrade(config, "20261004_0018")
    after = state(engine)
    assert after["rows"] == before["rows"] and after["version"] == "20261004_0018"
    print(f"Actual0018 table lock timeout {elapsed:.3f}s; marker/schema/version/old rows rolled back; retry and actual-head parity passed.")
