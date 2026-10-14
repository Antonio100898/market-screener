"""Disposable SQL/private tool fixtures; no model or production handler dispatch."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Event
from time import monotonic, sleep

import pytest
from sqlalchemy import event, func, select, text, update

import test_company_query_storage as query_fixtures
from test_company_query_storage import storage, witnessed, pytestmark
from screener.company_queries import CompanyQuery, RestartQuery, QueryUnavailable
from screener.company_query_schema import release_company_projection
from screener.durable_jobs import DurableJobRepository, LeaseLost, RetryPolicy
from screener.job_runtime import DurableJobRuntime
from screener.postgres import (company_snapshot, durable_job,
    research_run, research_run_event, research_user, research_workspace_revision)
from screener.research_runs import ResearchRunRepository, RESEARCH_JOB_KIND, TERMINAL, RunConflict
from screener.research_tools import ResearchCompanyTools, ResearchToolStopped
from screener.workspaces import VerifiedPrincipal, WorkspaceRepository


@pytest.fixture(autouse=True)
def enough_rows_for_real_tool_paging(monkeypatch):
    original = query_fixtures._rows
    def rows():
        values = original()
        for index in range(55):
            row = deepcopy(values[0])
            row.update(ticker=f"X{index:03}", name=f"Tool paging fixture {index:03}")
            values.append(row)
        return values
    monkeypatch.setattr(query_fixtures, "_rows", rows)


def prepare(engine, queries, release, *, subject="a", key="first", claim=True, seconds=30):
    workspaces = WorkspaceRepository(engine)
    owner = workspaces.resolve_identity(VerifiedPrincipal("tool-fixture", subject))
    workspace = workspaces.default(owner)["workspace_id"]
    runs = ResearchRunRepository(engine)
    run = runs.create(owner, workspace, expected_revision=1, release_id=release,
        request_key=key, request_text="Private text must not be loaded by read tools")
    jobs = DurableJobRepository(engine, retry_policy=RetryPolicy(jitter_seconds=lambda _: 0))
    lease = None
    if claim:
        lease = jobs.claim_next(owner=f"tool-fixture-{subject}", lease_duration=timedelta(seconds=seconds),
            allowed_kinds={RESEARCH_JOB_KIND}).lease
        assert lease.job_id == run["job_id"]
        runs.begin(lease, run["run_id"])
    return runs, ResearchCompanyTools(runs, queries), run, lease, owner, workspace, jobs


def test_real_tools_paging_exact_detail_privacy_and_no_workspace_or_shared_mutation(storage):
    engine, queries, release, _, selected, _ = storage
    runs, tools, run, lease, owner, workspace, _ = prepare(engine, queries, release)
    statements = []
    def witness(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())
    with engine.connect() as connection:
        before_snapshots = connection.scalar(select(func.count()).select_from(company_snapshot))
        before_edits = connection.scalar(select(func.count()).select_from(research_workspace_revision))
    event.listen(engine, "before_cursor_execute", witness)
    try:
        first = tools.search(lease, run["run_id"], {})
        second = tools.search(lease, run["run_id"], {"cursor": first["next_cursor"]})
        detail = tools.detail(lease, run["run_id"], {"ticker": "A"})
        repeated = tools.detail(lease, run["run_id"], {"security_id": selected[1].security_id})
    finally:
        event.remove(engine, "before_cursor_execute", witness)
    assert len(first["rows"]) == 50 and len(second["rows"]) == 11
    assert len({row["security_id"] for row in first["rows"] + second["rows"]}) == 61
    assert all(set(row) == {"security_id", "ticker", "name"} for row in first["rows"])
    assert detail == repeated == queries.detail(ticker="A", release_id=release, assume_absent_zero=False)
    assert "security_id" not in detail["row"]
    assert detail["source_validity"]["official_financial_readiness"] == "NOT_ASSESSED"
    assert tools.detail(lease, run["run_id"], {"ticker": "ABSENT"}) is None
    assert tools.search(lease, run["run_id"], {"q": "'; DROP TABLE issuer;--"})["rows"] == []
    assert all("request_text" not in statement and "from research_workspace_revision" not in statement
        for statement in statements)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(company_snapshot)) == before_snapshots
        assert connection.scalar(select(func.count()).select_from(research_workspace_revision)) == before_edits
    assert [event["kind"] for event in runs.events(owner, workspace, run["run_id"])["events"]] == ["CREATED", "STARTED"]


def test_lease_cannot_use_another_owner_run(storage):
    engine, queries, release, *_ = storage
    _, tools, _, first_lease, *_ = prepare(engine, queries, release)
    _, _, other_run, other_lease, *_ = prepare(engine, queries, release, subject="b")
    with pytest.raises(LeaseLost, match="another research job"):
        tools.detail(first_lease, other_run["run_id"], {"ticker": "A"})
    assert tools.detail(other_lease, other_run["run_id"], {"ticker": "A"})["row"]["ticker"] == "A"


def test_active_release_and_workspace_changes_do_not_move_tool_snapshot(storage):
    engine, queries, release, rows, _, publish = storage
    runs, tools, run, lease, owner, workspace, _ = prepare(engine, queries, release)
    before = tools.detail(lease, run["run_id"], {"ticker": "A"})
    changed = deepcopy(rows)
    changed[1]["price"] = 999
    newer = publish(changed, parent=release)
    WorkspaceRepository(engine).replace(owner, workspace, expected_revision=1, request_key="later",
        configuration={"view": {"q": "later"}})
    assert queries.detail(ticker="A")["release"]["release_id"] == newer
    assert tools.detail(lease, run["run_id"], {"ticker": "A"}) == before
    assert runs.tool_access(lease, run["run_id"])["initial_workspace_revision"] == 1


def test_canonical_only_published_run_release_does_not_fall_back(storage):
    engine, queries, release, rows, _, publish = storage
    canonical = publish(rows, parent=release, canonical_only=True)
    _, tools, run, lease, *_ = prepare(engine, queries, canonical)
    with pytest.raises(QueryUnavailable):
        tools.detail(lease, run["run_id"], {"ticker": "A"})


def test_source_generation_changes_restart_paging_without_changing_financial_row(witnessed):
    engine, queries, release, _, record = witnessed
    _, tools, run, lease, *_ = prepare(engine, queries, release)
    first = tools.search(lease, run["run_id"], {})
    before = tools.detail(lease, run["run_id"], {"ticker": "A"})
    record("same-tool-input", second=1)
    assert tools.search(lease, run["run_id"], {"cursor": first["next_cursor"]})["rows"]
    record("changed-tool-input", state="pending", reason="not_found", second=2)
    with pytest.raises(RestartQuery):
        tools.search(lease, run["run_id"], {"cursor": first["next_cursor"]})
    after = tools.detail(lease, run["run_id"], {"ticker": "A"})
    assert after["row"] == before["row"] and after["release"] == before["release"]
    assert after["source_validity"]["validity_generation"] > before["source_validity"]["validity_generation"]
    assert after["source_validity"]["official_financial_readiness"] == "NOT_ASSESSED"


@pytest.mark.parametrize("action", ["cancel", "revoke", "expire", "replacement"])
@pytest.mark.parametrize("operation", ["search", "detail"])
def test_shared_sql_wait_rechecks_cancel_revocation_expiry_and_generation(storage, action, operation):
    engine, queries, release, *_ = storage
    runs, tools, run, lease, owner, workspace, jobs = prepare(engine, queries, release)
    reached = Event()
    def witness(_connection, _cursor, statement, _parameters, _context, _many):
        if "release_company_projection" in statement and statement.lstrip().startswith("SELECT"):
            reached.set()
    blocker = engine.connect()
    transaction = blocker.begin()
    blocker.execute(text("LOCK TABLE release_company_projection IN ACCESS EXCLUSIVE MODE"))
    event.listen(engine, "before_cursor_execute", witness)
    expected = ResearchToolStopped if action in {"cancel", "revoke"} else LeaseLost
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            inputs = {} if operation == "search" else {"ticker": "A"}
            future = pool.submit(getattr(tools, operation), lease, run["run_id"], inputs)
            try:
                assert reached.wait(5)
                assert not future.done()
                with engine.connect() as connection:
                    assert connection.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event_type='Lock' AND query LIKE '%release_company_projection%'")) >= 1
                if action == "cancel":
                    assert runs.cancel(owner, workspace, run["run_id"])["state"] == "CANCEL_REQUESTED"
                elif action == "revoke":
                    with engine.begin() as connection:
                        connection.execute(update(research_user).where(research_user.c.user_id == owner).values(status="DISABLED"))
                else:
                    with engine.begin() as connection:
                        connection.execute(update(durable_job).where(durable_job.c.job_id == lease.job_id)
                            .values(lease_expires_at=datetime.now(timezone.utc) + timedelta(milliseconds=120)))
                    sleep(0.2)
                    if action == "replacement":
                        jobs.recover_expired()
                        replacement = jobs.claim_next(owner="replacement", lease_duration=timedelta(seconds=30),
                            allowed_kinds={RESEARCH_JOB_KIND}).lease
                        assert replacement.generation > lease.generation
                        runs.begin(replacement, run["run_id"])
            finally:
                transaction.commit()
            with pytest.raises(expected):
                future.result(timeout=10)
    finally:
        event.remove(engine, "before_cursor_execute", witness)
        blocker.close()
    with engine.connect() as connection:
        state = connection.scalar(select(research_run.c.state).where(research_run.c.run_id == run["run_id"]))
        events = connection.execute(select(research_run_event.c.kind, research_run_event.c.payload)
            .where(research_run_event.c.run_id == run["run_id"])).all()
    assert all(kind != "PROGRESS" for kind, _ in events)
    if action == "revoke":
        assert state == "DENIED" and events[-1].payload == {"state": "DENIED"}
    elif action == "cancel":
        assert runs.finish(lease, run["run_id"])["state"] == "CANCELLED"
    else:
        assert state == "RUNNING"


@pytest.mark.parametrize("table", [durable_job, research_run], ids=["job", "run"])
def test_metadata_fence_rejects_lease_expiry_after_actual_private_lock_wait(storage, table):
    engine, queries, release, *_ = storage
    runs, _, run, lease, _, _, _ = prepare(engine, queries, release)
    reached = Event()
    def witness(_connection, _cursor, statement, _parameters, _context, _many):
        if f"FROM {table.name} " in statement and "FOR UPDATE" in statement:
            reached.set()
    with engine.begin() as connection:
        connection.execute(update(durable_job).where(durable_job.c.job_id == lease.job_id)
            .values(lease_expires_at=datetime.now(timezone.utc) + timedelta(milliseconds=300)))
    blocker = engine.connect()
    transaction = blocker.begin()
    identity = table.c.job_id == lease.job_id if table is durable_job else table.c.run_id == run["run_id"]
    blocker.execute(select(table).where(identity).with_for_update()).first()
    event.listen(engine, "before_cursor_execute", witness)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(runs.tool_access, lease, run["run_id"])
            try:
                assert reached.wait(5) and not future.done()
                sleep(0.4)
            finally:
                transaction.commit()
            with pytest.raises(LeaseLost):
                future.result(timeout=5)
    finally:
        event.remove(engine, "before_cursor_execute", witness)
        blocker.close()


def test_replacement_must_begin_run_before_tool_access(storage):
    engine, queries, release, *_ = storage
    runs, tools, run, lease, _, _, jobs = prepare(engine, queries, release, seconds=0.2)
    sleep(0.3)
    jobs.recover_expired()
    replacement = jobs.claim_next(owner="replacement", lease_duration=timedelta(seconds=30),
        allowed_kinds={RESEARCH_JOB_KIND}).lease
    with pytest.raises(RunConflict, match="not started"):
        tools.detail(replacement, run["run_id"], {"ticker": "A"})
    runs.begin(replacement, run["run_id"])
    assert tools.detail(replacement, run["run_id"], {"ticker": "A"})["row"]["ticker"] == "A"


@pytest.mark.parametrize("terminal_before_retry", [False, True])
def test_existing_runtime_resumes_typed_fixture_tools_without_persisting_results(storage, terminal_before_retry):
    engine, queries, release, *_ = storage
    runs, _, run, old_lease, owner, workspace, jobs = prepare(engine, queries, release, seconds=0.2)
    if terminal_before_retry:
        runs.finish(old_lease, run["run_id"])
    sleep(0.3)
    jobs.recover_expired()
    delivered = []
    def fixture_handler(context):
        state = runs.begin(context.lease, context.job.parameters["run_id"])
        if state["state"] in TERMINAL:
            return {"deterministic_read_tool_fixture": True}
        tools = ResearchCompanyTools(ResearchRunRepository(engine), queries)
        found = tools.search(context.lease, run["run_id"], {"q": "Alpha"})
        detail = tools.detail(context.lease, run["run_id"], {"ticker": "A"})
        delivered.append((found, detail))
        runs.progress(context.lease, run["run_id"], event_key="read-fixture", stage="read_complete")
        runs.finish(context.lease, run["run_id"])
        return {"deterministic_read_tool_fixture": True}
    runtime = DurableJobRuntime(jobs, lambda _: [], {RESEARCH_JOB_KIND: fixture_handler},
        worker_count=1, poll_interval=timedelta(milliseconds=20), lease_duration=timedelta(seconds=3),
        heartbeat_interval=timedelta(seconds=1))
    runtime.start()
    try:
        deadline = monotonic() + 8
        while monotonic() < deadline:
            with engine.connect() as connection:
                if connection.scalar(select(durable_job.c.status).where(durable_job.c.job_id == run["job_id"])) == "succeeded":
                    break
            sleep(0.03)
        else:
            pytest.fail(f"test-only runtime did not finish: {runtime.errors}")
    finally:
        assert runtime.stop(timeout=5)
    assert len(delivered) == (0 if terminal_before_retry else 1)
    assert runs.get(owner, workspace, run["run_id"])["state"] == "SUCCEEDED"
    assert all(set(item["payload"]) in ({"state"}, {"stage"})
        for item in runs.events(owner, workspace, run["run_id"])["events"])
    with engine.connect() as connection:
        assert connection.scalar(select(durable_job.c.checkpoint).where(durable_job.c.job_id == run["job_id"])) == {
            "deterministic_read_tool_fixture": True}
