"""Synthetic graph PG/S3 mechanics; no filing or financial acceptance claim."""
import json
import subprocess
import sys
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Event
from time import monotonic, sleep

import pytest
from sqlalchemy import create_engine, event, func, select, text, update
from sqlalchemy.exc import IntegrityError

from screener.durable_jobs import DurableJobRepository, LeaseLost
from screener.figure_history import FigureHistoryRepository, OutputGraphProjection, activation_inventory, Period, BundleSource
from screener.postgres import (figure_activation_transition as transitions, data_release, release_company,
    exact_figure_bundle, figure_selection, source_item_current, source_item, durable_job_item,
    release_company_source, active_data_release, release_candidate, release_company_lineage)
from screener.publication import PublicationRepository, StalePublicationParent
from test_publication_storage import storage, NOW, _claim, _company, _source, pytestmark
from test_publication_exact_binding_storage import setup, register, publish, record, lineage
from tests.test_activation_history import reported, partial_period
from tests.test_figure_history import validate_changed
from test_figure_history_contract2_storage import prepared_bundle


def rows(engine):
    with engine.connect() as connection:
        return [dict(r) for r in connection.execute(select(transitions).order_by(transitions.c.transition_id)).mappings()]


def change(parts, key, *, value=None, parent=None, bundle=None, **changes):
    old = parts[3]
    if bundle is None:
        observed = old.observations[0].model_copy(update={
            "reference": old.observations[0].reference.model_copy(update={"extraction_revision": key}),
            **({"value_decimal_text": value, "source_lexical_text": value} if value is not None else {})})
        selected = old.selections[0].model_copy(update={**changes, **({"value_decimal_text": value} if value is not None else {})})
        bundle = validate_changed(old, observations=(observed,), selections=(selected,), pipeline_run_key=key)
    identifier = FigureHistoryRepository(parts[0], clock=lambda: NOW).store(parts[1], request_key=key, bundle=bundle)
    candidate = register(parts, key="candidate-"+key, parent=parent, exact_bundle_id=identifier, expected_exact_run_key=bundle.pipeline_run_key)
    return publish(parts, candidate, key="release-"+key, parent=parent), identifier, bundle


def test_exact_100_90_95_release_pointers_restart_and_sealed_replay(storage):
    parts = setup(storage)
    first = publish(parts, register(parts))
    second, second_id, _ = change(parts, "90", value="90.0000", parent=first.release_id)
    third, third_id, _ = change(parts, "95", value="95.0000", parent=second.release_id)
    actual = rows(parts[0])
    assert len(actual) == 3
    assert [(r["previous_bundle_id"], r["replacement_bundle_id"]) for r in actual] == [(None, parts[4]), (parts[4], second_id), (second_id, third_id)]
    assert all(r["context_assessment"] == "NOT_ASSESSED" for r in actual)
    assert second.figure_history_revision == third.figure_history_revision == 1
    record(storage, "later-source", 2)
    candidate = register(parts, key="candidate-95", parent=second.release_id, exact_bundle_id=third_id, expected_exact_run_key="95")
    assert publish(parts, candidate, key="release-95", parent=second.release_id) == third
    parts[0].dispose()
    assert rows(parts[0]) == actual
    process = subprocess.run([sys.executable, "-c", """
import json
from sqlalchemy import create_engine,select
from screener.storage_config import StorageSettings
from screener.postgres import figure_activation_transition
engine=create_engine(StorageSettings.from_env().database_url)
with engine.connect() as c:
 print(json.dumps([dict(r) for r in c.execute(select(figure_activation_transition).order_by(figure_activation_transition.c.transition_id)).mappings()],default=str))
engine.dispose()
"""], capture_output=True, text=True, check=True, timeout=15)
    assert json.loads(process.stdout) == json.loads(json.dumps(actual, default=str))


@pytest.mark.parametrize("value", ["-0.0000", "1E-20000", "1E+200000", "9007199254740993.1234567890123456789"])
def test_extreme_exact_endpoint_strings_remain_in_original_graph(storage, value):
    parts = setup(storage)
    first = publish(parts, register(parts))
    release, identifier, bundle = change(parts, "extreme", value=value, parent=first.release_id)
    row = rows(parts[0])[-1]
    assert row["replacement_bundle_id"] == identifier and row["value_text_changed"]
    assert FigureHistoryRepository(parts[0]).load(identifier).selections[0].value_decimal_text == value
    assert bundle.selections[0].value_decimal_text == value and release.figure_history_revision == 1


@pytest.mark.parametrize("old_unknown,new_unknown", [(True, True), (True, False), (False, True)])
def test_unknown_inventory_both_pointers_and_distinct_known_period(storage, old_unknown, new_unknown):
    parts = setup(storage)
    unknown = partial_period(parts[3], Period(kind="UNKNOWN", origin="UNKNOWN"))
    unknown = reported(unknown, reference=unknown.observations[0].reference.model_copy(
        update={"extraction_revision": "unknown-period-fixture"}))
    if old_unknown:
        first, _, _ = change(parts, "unknown-first", bundle=unknown)
    else:
        first = publish(parts, register(parts))
    graph = unknown if new_unknown else parts[3]
    graph = validate_changed(graph, pipeline_run_key="next-inventory")
    second, _, _ = change(parts, "unknown-next", bundle=graph, parent=first.release_id)
    actual = [r for r in rows(parts[0]) if r["release_id"] == second.release_id]
    unassessed = [r for r in actual if r["period_kind"] == "UNKNOWN"]
    assert len(unassessed) == 1 and unassessed[0]["reason"] == "unknown-period-unmatched"
    if old_unknown and new_unknown:
        assert len(actual) == 1 and unassessed[0]["previous_node_key"] and unassessed[0]["replacement_node_key"]
    else:
        assert len(actual) == 2 and all(r["endpoint_presence_changed"] for r in actual)


def test_semantic_admin_noise_equal_graph_and_inheritance_make_no_event(storage):
    parts = setup(storage)
    first = publish(parts, register(parts))
    initial = rows(parts[0])
    graph = validate_changed(parts[3], pipeline_run_key="noise", selections=(parts[3].selections[0].model_copy(update={"node_key": "local-alias"}),))
    second, _, _ = change(parts, "noise", bundle=graph, parent=first.release_id)
    assert rows(parts[0]) == initial
    other = _company(parts[0], storage[1], identifier="2000", ticker="XYZ", lease=parts[1])
    source = _source(parts[0], storage[1], parts[1], identifier="2000", key="other")
    candidate = parts[-1].register_candidate(parts[1], request_key="other", snapshot_id=other.snapshot_id,
        expected_parent_release_id=second.release_id, source_observation_ids=(source,), cause_type="source_change")
    third = publish(parts, candidate, key="inherited", parent=second.release_id)
    assert rows(parts[0]) == initial and third.figure_history_revision == 1


def test_legacy_null_and_explicit_no_value_remain_unassessed(storage):
    parts = setup(storage)
    legacy = publish(parts, register(parts, exact_bundle_id=None, expected_exact_run_key=None))
    assert rows(parts[0]) == [] and legacy.figure_history_revision == 1
    bound = publish(parts, register(parts, key="bound", parent=legacy.release_id), key="bound", parent=legacy.release_id)
    row, = rows(parts[0])
    assert row["previous_bundle_id"] is None and row["reason"] == "endpoint-unavailable"
    selected = parts[3].selections[0].model_copy(update={"state": "INVALIDATED", "observation_key": None,
        "value_decimal_text": None, "reason": "unproved-original-owner-label"})
    graph = validate_changed(parts[3], pipeline_run_key="no-value", selections=(selected,),
        coverage=(parts[3].coverage[0].model_copy(update={"assessment": "PARTIAL", "reason": "unassessed-invalidation"}),))
    _, identifier, _ = change(parts, "no-value", bundle=graph, parent=bound.release_id)
    row = rows(parts[0])[-1]
    assert row["replacement_bundle_id"] == identifier and row["state_changed"]
    assert row["context_assessment"] == "NOT_ASSESSED" and not row["endpoint_presence_changed"]


@pytest.mark.parametrize("publish_b", [True, False])
def test_a_b_a_exact_endpoints_and_actual_check_three_not_capture_one(storage, publish_b):
    parts = setup(storage); first = publish(parts, register(parts))
    before = lineage(parts[0], first.release_id)
    b = record(storage, "B-check", 2)
    parent = first
    if publish_b:
        graph = reported(parts[3], value_decimal_text="90.0000", source_lexical_text="90.0000",
            reference=parts[3].observations[0].reference.model_copy(update={"extraction_revision": "synthetic-B"}))
        graph = validate_changed(graph, pipeline_run_key="B", sources=(BundleSource(source_item_id=b.item.source_item_id,
            source_observation_id=b.observation.source_observation_id, occurrence_id=b.delivery.occurrence_id),))
        identifier = FigureHistoryRepository(parts[0], clock=lambda: NOW).store(parts[1], request_key="B", bundle=graph)
        bparts = (*parts[:3], graph, identifier, parts[-1])
        parent = publish(bparts, register(bparts, key="B", parent=first.release_id), key="B", parent=first.release_id)
    a = record(storage, "A-check3", 1)
    third = publish(parts, register(parts, key="A3", parent=parent.release_id), key="A3", parent=parent.release_id)
    actual = rows(parts[0])
    assert len(actual) == (3 if publish_b else 1)
    if publish_b:
        assert actual[-1]["previous_bundle_id"] == identifier and actual[-1]["replacement_bundle_id"] == parts[4]
        assert actual[-1]["value_text_changed"]
    witness = lineage(parts[0], third.release_id)
    assert witness[0]["accepted_occurrence_id"] == a.delivery.occurrence_id != parts[3].sources[0].occurrence_id
    assert lineage(parts[0], first.release_id) == before
    record(storage, "A-check4", 1)
    assert publish(parts, register(parts, key="A3", parent=parent.release_id), key="A3", parent=parent.release_id) == third
    assert rows(parts[0]) == actual and lineage(parts[0], third.release_id) == witness


def open_release(connection, parts, parent):
    release = connection.scalar(data_release.insert().values(request_key="direct", parent_release_id=parent,
        publishing_job_id=parts[1].job_id, figure_history_revision=1).returning(data_release.c.release_id))
    connection.execute(release_company.insert().values(release_id=release, security_id=parts[2].security_id,
        snapshot_id=parts[2].snapshot_id, exact_bundle_id=parts[4]))
    return release


@pytest.mark.parametrize("changes", [dict(metric_path="foreign"), dict(view="ASSUMED"),
    dict(period_end=date(2025, 12, 30)), dict(period_kind="DURATION", period_start=date(2025, 1, 1)),
    dict(previous_bundle_id=None), dict(replacement_node_key=None), dict(parent_release_id=None),
    dict(previous_bundle_id=9999), dict(replacement_bundle_id=9999), dict(security_id=9999),
    dict(previous_node_key="nonexistent"), dict(replacement_node_key="nonexistent"),
    dict(previous_bundle_id=None, previous_node_key=None), dict(replacement_bundle_id=None, replacement_node_key=None),
    dict(context_assessment="COMPARABLE"), dict(period_kind="UNKNOWN", period_end=None, reason="assessed", context_assessment="INCOMPARABLE")])
def test_direct_sql_cannot_forge_endpoint_parent_member_or_shape(storage, changes):
    parts = setup(storage)
    first = publish(parts, register(parts))
    row = dict(rows(parts[0])[0]); row.pop("transition_id")
    with pytest.raises(IntegrityError):
        with parts[0].begin() as connection:
            release = open_release(connection, parts, first.release_id)
            row.update(release_id=release, parent_release_id=first.release_id, previous_bundle_id=parts[4], previous_node_key="assets")
            row.update(changes)
            connection.execute(transitions.insert().values(**row))
    assert len(rows(parts[0])) == 1


def test_initial_previous_endpoint_null_safe_guard_and_unique_period(storage):
    parts = setup(storage)
    with parts[0].begin() as connection:
        identifier = open_release(connection, parts, None)
        records = activation_inventory(release_id=identifier, parent_release_id=None, security_id=parts[2].security_id,
            previous_bundle_id=None, previous=None, replacement_bundle_id=parts[4], replacement=OutputGraphProjection(parts[3]))
        values = records[0].model_dump()
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(transitions.insert().values(**{**values, "previous_bundle_id": parts[4], "previous_node_key": "assets"}))
        connection.execute(transitions.insert().values(**values))
        with pytest.raises(IntegrityError, match="uq_figure_transition_output"):
            with connection.begin_nested():
                connection.execute(transitions.insert().values(**values))
        connection.rollback()


def test_direct_sql_intermediate_cannot_be_an_activation_endpoint(storage):
    engine, lease, company, graph, owner, prepared = prepared_bundle(storage, graph="basis")
    identifier = owner.store(lease, request_key="basis", bundle=graph, prepared_documents=prepared)
    parts = engine, lease, company, graph, identifier, PublicationRepository(engine, clock=lambda: NOW)
    first = publish(parts, register(parts))
    original = dict(rows(engine)[0]); original.pop("transition_id")
    intermediate = next(n for n in graph.selections if n.purpose == "INTERMEDIATE")
    with pytest.raises(IntegrityError, match="OUTPUT"):
        with engine.begin() as connection:
            release = open_release(connection, parts, first.release_id)
            connection.execute(transitions.insert().values(**{**original, "release_id": release,
                "parent_release_id": first.release_id, "previous_bundle_id": identifier,
                "previous_node_key": intermediate.node_key}))
    assert len(rows(engine)) == 1


@pytest.mark.parametrize("term,assessment", [("1.00", "NOT_ASSESSED"), ("2", "INCOMPARABLE")])
def test_actual_action_context_lexical_change_and_unequal_term_neighbor(storage, term, assessment):
    engine, lease, company, graph, owner, prepared = prepared_bundle(storage, graph="basis")
    identifier = owner.store(lease, request_key="action-before", bundle=graph, prepared_documents=prepared)
    parts = engine, lease, company, graph, identifier, PublicationRepository(engine, clock=lambda: NOW)
    first = publish(parts, register(parts))
    changed = validate_changed(graph, pipeline_run_key="action-after", actions=(graph.actions[0].model_copy(update={
        "old_shares_text": "8.00", "new_shares_text": term}),))
    next_id = owner.store(lease, request_key="action-after", bundle=changed, prepared_documents=prepared)
    next_parts = engine, lease, company, changed, next_id, parts[-1]
    second = publish(next_parts, register(next_parts, key="action-after", parent=first.release_id), key="action-after", parent=first.release_id)
    row = rows(engine)[-1]
    assert row["release_id"] == second.release_id and row["provenance_changed"]
    assert row["context_assessment"] == assessment and not row["value_text_changed"]
    assert owner.load(identifier).actions[0].new_shares_text == "1"
    assert owner.load(next_id).actions[0].new_shares_text == term


@pytest.mark.parametrize("operation", ["update", "delete", "insert", "marker"])
def test_sealed_history_and_pre_hook_marker_cannot_be_changed(storage, operation):
    parts = setup(storage); first = publish(parts, register(parts)); before = rows(parts[0])
    with pytest.raises(IntegrityError):
        with parts[0].begin() as connection:
            if operation == "update": connection.execute(update(transitions).values(reason="rewrite"))
            elif operation == "delete": connection.execute(transitions.delete())
            elif operation == "marker": connection.execute(update(data_release).values(figure_history_revision=None))
            else:
                value = dict(before[0]); value.pop("transition_id")
                connection.execute(transitions.insert().values(**value))
    assert rows(parts[0]) == before


@pytest.mark.parametrize("mode", ["logical-autocommit", "direct-dbapi", "autobegin"])
def test_hook_requires_real_transaction_with_autobegin_control(storage, mode):
    parts = setup(storage)
    repository = FigureHistoryRepository(parts[0])
    with parts[0].connect() as connection:
        if mode == "logical-autocommit": connection = connection.execution_options(isolation_level="AUTOCOMMIT"); connection.begin()
        elif mode == "direct-dbapi": connection.connection.dbapi_connection.autocommit = True; connection.begin()
        else:
            release = open_release(connection, parts, None)
            repository.record_activation_on_connection(connection, release_id=release, parent_release_id=None,
                memberships=[dict(security_id=parts[2].security_id, snapshot_id=parts[2].snapshot_id, exact_bundle_id=parts[4])], candidate_ids=())
            assert len(connection.execute(select(transitions)).all()) == 1
            connection.rollback()
            assert rows(parts[0]) == []
            return
        try:
            with pytest.raises(ValueError, match="real transaction"):
                repository.record_activation_on_connection(connection, release_id=1, parent_release_id=None, memberships=[], candidate_ids=())
        finally:
            connection.rollback()
            if mode == "direct-dbapi": connection.connection.dbapi_connection.autocommit = False
    assert rows(parts[0]) == []


@pytest.mark.parametrize("expires", [True, False])
def test_real_late_transition_table_lock_expiry_rolls_back_or_validly_commits(storage, expires):
    parts = setup(storage); candidate = register(parts)
    jobs = DurableJobRepository(parts[0]); now = datetime.now(timezone.utc)
    jobs.enqueue_scheduled(schedule_key="late-history", scheduled_for=now, kind="late-history", parameters={}, due_at=now)
    live = jobs.claim_next(owner="late-history", lease_duration=timedelta(seconds=1 if expires else 10), allowed_kinds={"late-history"})
    repository = PublicationRepository(parts[0])
    with parts[0].connect() as blocker:
        tx = blocker.begin(); blocker.execute(text("LOCK TABLE figure_activation_transition IN SHARE MODE"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(repository.publish, live.lease, request_key="late-history", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=None)
            with parts[0].connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                deadline = monotonic()+3
                while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
                    assert monotonic()<deadline and not future.done(); sleep(.01)
            sleep(1.1 if expires else .05); tx.rollback()
            if expires:
                with pytest.raises(LeaseLost): future.result(5)
            else: release = future.result(5)
    with parts[0].connect() as connection:
        if expires:
            for table in (transitions, data_release, release_company, release_candidate, release_company_source, release_company_lineage):
                assert connection.scalar(select(func.count()).select_from(table)) == 0
            assert connection.scalar(select(active_data_release.c.release_id)) is None
            assert not connection.scalar(select(durable_job_item.c.job_id).where(durable_job_item.c.job_id == live.lease.job_id))
        else:
            assert repository.active() == release and len(rows(parts[0])) == 1


def test_injected_after_history_failure_rolls_back_entire_release(storage, monkeypatch):
    parts = setup(storage); candidate = register(parts)
    original = FigureHistoryRepository.record_activation_on_connection
    def failed(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("after-history")
    monkeypatch.setattr(FigureHistoryRepository, "record_activation_on_connection", failed)
    with pytest.raises(RuntimeError, match="after-history"): publish(parts, candidate)
    assert rows(parts[0]) == [] and parts[-1].active() is None
    with parts[0].connect() as connection:
        for table in (data_release, release_company, release_candidate, release_company_source, release_company_lineage):
            assert connection.scalar(select(func.count()).select_from(table)) == 0
    monkeypatch.setattr(FigureHistoryRepository, "record_activation_on_connection", original)
    assert publish(parts, candidate).figure_history_revision == 1


@pytest.mark.parametrize("operation", ["publish", "bootstrap"])
@pytest.mark.parametrize("mode", ["logical-autocommit", "direct-dbapi", "ordinary"])
def test_publication_transaction_entry_rejects_autocommit_before_any_write(storage, operation, mode):
    parts = setup(storage)
    candidate = register(parts)
    if operation == "bootstrap": _company(parts[0], storage[1])
    def snapshot():
        with parts[0].connect() as connection:
            return {t.name: connection.execute(select(t)).mappings().all() for t in (
                data_release, release_company, release_candidate, release_company_source, release_company_lineage,
                active_data_release, transitions, durable_job_item)}
    before = snapshot()
    engine = create_engine(parts[0].url, **({"isolation_level": "AUTOCOMMIT"} if mode == "logical-autocommit" else {}))
    if mode == "direct-dbapi":
        def direct(dbapi_connection, connection_record): dbapi_connection.autocommit = True
        event.listen(engine, "connect", direct)
    repo = PublicationRepository(engine, clock=lambda: NOW)
    def execute():
        return repo.bootstrap(parts[1], request_key="autocommit") if operation == "bootstrap" else repo.publish(
            parts[1], request_key="autocommit", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=None)
    try:
        if mode == "ordinary":
            assert execute().figure_history_revision == 1
        else:
            with pytest.raises(ValueError, match="real transaction"): execute()
            assert snapshot() == before
    finally:
        engine.dispose()


def test_parent_race_has_one_sealed_history_and_one_rebase(storage):
    parts = setup(storage); candidate = register(parts)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish, parts, candidate, key=k) for k in ("one", "two")]
        results = []
        for future in futures:
            try: results.append(future.result(8))
            except StalePublicationParent: results.append("rebase")
    assert sum(r == "rebase" for r in results) == 1 and len(rows(parts[0])) == 1


def test_many_occurrences_use_batched_graph_load_and_bounded_memory(storage):
    parts = setup(storage)
    observed = parts[3].observations[0]
    observations = tuple(observed.model_copy(update={"key": f"input-{i}", "reference": observed.reference.model_copy(update={
        "location": observed.reference.location.model_copy(update={"pointer": f"/facts/occurrence/{i}"})})}) for i in range(1000))
    graph = validate_changed(parts[3], observations=(observed, *observations), pipeline_run_key="inventory-1000")
    identifier = FigureHistoryRepository(parts[0], clock=lambda: NOW).store(parts[1], request_key="large", bundle=graph)
    candidate = register(parts, exact_bundle_id=identifier, expected_exact_run_key="inventory-1000")
    statements = []
    def captured(conn, cursor, statement, parameters, context, executemany): statements.append(statement)
    event.listen(parts[0], "before_cursor_execute", captured)
    tracemalloc.start()
    try:
        release = publish(parts, candidate)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop(); event.remove(parts[0], "before_cursor_execute", captured)
    batches = [s for s in statements if "jsonb_to_recordset" in s]
    # One prior publication admission load, one new hook graph load; no per-occurrence reads.
    assert len(batches) == 2 and len(statements) < 65 and len(rows(parts[0])) == 1
    print(f"1001 observation bundle: {len(statements)} SQL statements, {len(batches)} batched catalog loads, one transition, peak {peak} bytes; release {release.release_id}.")
