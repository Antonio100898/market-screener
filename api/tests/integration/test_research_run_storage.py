"""Real PostgreSQL lifecycle proof with deterministic fixtures, never model research."""
import json
import multiprocessing
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from screener.durable_jobs import DurableJobRepository, LeaseLost, RetryPolicy
from screener import auth
from screener.job_runtime import DurableJobRuntime
from screener.postgres import data_release, durable_job, research_run, research_run_event, research_user
from screener.research_api import require_principal, workspace_repository
from screener.research_run_api import router, require_research_dispatch, run_repository
from screener.research_runs import ResearchRunRepository, RESEARCH_JOB_KIND, RunConflict, TERMINAL
from screener.storage_config import StorageSettings
from screener.workspaces import VerifiedPrincipal, WorkspaceRepository, WorkspaceNotFound, DisabledResearchUser


pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1", reason="requires real local PostgreSQL")


@pytest.fixture
def storage(monkeypatch, request):
    settings = StorageSettings.from_env()
    name = "ms_research_" + uuid4().hex
    assert name.startswith("ms_research_") and name.isidentifier()
    admin = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    url = make_url(settings.database_url).set(database=name)
    with admin.connect() as connection: connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url)
    try:
        monkeypatch.setenv("SCREENER_DATABASE_URL", url.render_as_string(hide_password=False))
        config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        start = getattr(request, "param", "base")
        command.upgrade(config, start)
        if start == "20261004_0010":
            prior = WorkspaceRepository(engine)
            prior_owner = prior.resolve_identity(VerifiedPrincipal("migration-fixture", "existing"))
            prior_workspace = prior.default(prior_owner)
            prior.replace(prior_owner, prior_workspace["workspace_id"], expected_revision=1, request_key="saved",
                configuration={"view": {"q": "retained private config"}})
        command.upgrade(config, "head")
        if start == "20261004_0010":
            assert prior.get(prior_owner, prior_workspace["workspace_id"])["configuration"]["view"]["q"] == "retained private config"
        jobs = DurableJobRepository(engine)
        now = datetime.now(timezone.utc)
        job = jobs.enqueue_scheduled(schedule_key="lifecycle-release-fixture", scheduled_for=now,
            kind="lifecycle-release-fixture", parameters={}, due_at=now)
        # A sealed empty canonical fixture pins identity only; it proves no financial readiness.
        with engine.begin() as connection:
            release = connection.scalar(data_release.insert().values(request_key="lifecycle-fixture",
                publishing_job_id=job.job_id).returning(data_release.c.release_id))
            connection.execute(update(data_release).where(data_release.c.release_id == release).values(published_at=now))
        workspaces = WorkspaceRepository(engine)
        owner = workspaces.resolve_identity(VerifiedPrincipal("lifecycle-test", "a"))
        workspace = workspaces.default(owner)["workspace_id"]
        yield engine, url, config, owner, workspace, release
    finally:
        engine.dispose()
        with admin.connect() as connection: connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def _create(storage, *, key="first", owner=None, workspace=None):
    engine, _, _, a, w, release = storage
    return ResearchRunRepository(engine).create(a if owner is None else owner, w if workspace is None else workspace,
        expected_revision=1, release_id=release, request_key=key, request_text="Private lifecycle fixture request")


def _claim(engine, *, seconds=30):
    return DurableJobRepository(engine).claim_next(owner="lifecycle-fixture", lease_duration=timedelta(seconds=seconds),
        allowed_kinds={RESEARCH_JOB_KIND}).lease


def _app(engine, subject="a", *, dispatch=True):
    app = FastAPI(); app.include_router(router)
    app.middleware("http")(auth.require_token)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.dependency_overrides[require_principal] = lambda: VerifiedPrincipal("lifecycle-test", subject)
    app.dependency_overrides[workspace_repository] = lambda: WorkspaceRepository(engine)
    app.dependency_overrides[run_repository] = lambda: ResearchRunRepository(engine)
    if dispatch: app.dependency_overrides[require_research_dispatch] = lambda: True
    return app


@pytest.mark.parametrize("storage", ["base", "20261004_0010"], indirect=True)
def test_empty_and_previous_head_upgrade_have_no_metadata_drift(storage):
    engine, _, config, *_ = storage
    command.current(config); command.heads(config); command.check(config)
    with engine.connect() as connection:
        assert connection.scalar(text("select version_num from alembic_version")) == ScriptDirectory.from_config(config).get_current_head()
        assert connection.scalar(select(func.count()).select_from(research_run)) == 0


def test_private_creation_exact_replay_conflict_revision_and_two_owners(storage):
    engine, _, _, owner, workspace, release = storage
    repository = ResearchRunRepository(engine)
    a, b = TestClient(_app(engine)), TestClient(_app(engine, "b"))
    path = f"/research/workspaces/{workspace}/runs"
    body = {"expected_revision": 1, "release_id": release, "request_key": "same", "request_text": "private A"}
    run = a.post(path, json=body).json()
    assert a.post(path, json=body).json() == run
    assert a.post(path, json={**body, "request_text": "changed"}).status_code == 409
    assert a.post(path, json={**body, "request_key": "revision", "expected_revision": 2}).status_code == 409
    assert a.post(path, json={**body, "owner_id": owner}).status_code == 422
    responses = [b.get(f"{path}/{run['run_id']}"), b.get(f"{path}/{run['run_id']}/events"),
        b.post(f"{path}/{run['run_id']}/cancel"), b.get(f"{path}/{run['run_id']}/stream"), b.post(path, json=body)]
    assert [response.status_code for response in responses] == [404] * 5
    assert all(response.headers["cache-control"] == "private, no-store" for response in responses)
    assert all("private A" not in response.text for response in responses)
    blocked = TestClient(_app(engine, dispatch=False)).post(path, json=body)
    assert blocked.status_code == 503
    assert blocked.headers["cache-control"] == "private, no-store"
    WorkspaceRepository(engine).replace(owner, workspace, expected_revision=1, request_key="later", configuration={"view": {"q": "later"}})
    assert a.post(path, json=body).json() == run
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(research_run)) == 1
        assert connection.scalar(select(func.count()).select_from(durable_job).where(durable_job.c.kind == RESEARCH_JOB_KIND)) == 1
    assert repository.get(owner, workspace, run["run_id"])["initial_workspace_revision"] == 1


def test_duplicate_requests_cancel_complete_race_and_event_replay(storage):
    engine, _, _, owner, workspace, _ = storage
    barrier = Barrier(2)
    def create(): barrier.wait(timeout=5); return _create(storage)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create) for _ in range(2)]
        runs = [future.result(timeout=10) for future in futures]
    assert runs[0] == runs[1]
    run, repository, lease = runs[0], ResearchRunRepository(engine), _claim(engine)
    repository.begin(lease, run["run_id"])
    context = repository.context(lease, run["run_id"])
    assert context["context"]["workspace"]["edit_revision"] == 1
    repository.progress(lease, run["run_id"], event_key="step", stage="deterministic_fixture")
    before = repository.get(owner, workspace, run["run_id"])
    repository.progress(lease, run["run_id"], event_key="step", stage="deterministic_fixture")
    assert repository.get(owner, workspace, run["run_id"]) == before
    with pytest.raises(RunConflict): repository.progress(lease, run["run_id"], event_key="step", stage="changed")
    barrier = Barrier(2)
    def cancel(): barrier.wait(timeout=5); return repository.cancel(owner, workspace, run["run_id"])
    def finish(): barrier.wait(timeout=5); return repository.finish(lease, run["run_id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cancel), pool.submit(finish)]
        [future.result(timeout=10) for future in futures]
    final = repository.get(owner, workspace, run["run_id"])
    assert final["state"] in {"SUCCEEDED", "CANCELLED"}
    assert repository.cancel(owner, workspace, run["run_id"]) == final
    assert repository.finish(lease, run["run_id"]) == final
    events, cursor = [], 0
    while True:
        batch = repository.events(owner, workspace, run["run_id"], after=cursor, limit=2)
        assert len(batch["events"]) <= 2
        events += batch["events"]; cursor = batch["next_after"]
        if not batch["has_more"]: break
    assert [event["sequence"] for event in events] == list(range(1, final["last_event_seq"] + 1))
    assert sum(event["kind"] in TERMINAL for event in events) == 1
    with pytest.raises(RunConflict): repository.events(owner, workspace, run["run_id"], after=cursor + 1)


def test_revocation_races_worker_and_service_reconciliation_without_private_reads(storage):
    engine, _, _, owner, workspace, _ = storage
    run = _create(storage); lease = _claim(engine); repository = ResearchRunRepository(engine)
    repository.begin(lease, run["run_id"])
    barrier = Barrier(2)
    def revoke():
        barrier.wait(timeout=5)
        with engine.begin() as connection: connection.execute(update(research_user).where(research_user.c.user_id == owner).values(status="DISABLED"))
    def progress():
        barrier.wait(timeout=5)
        return repository.progress(lease, run["run_id"], event_key="racing", stage="deterministic_fixture")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(revoke), pool.submit(progress)]
        [future.result(timeout=10) for future in futures]
    assert repository.context(lease, run["run_id"])["context"] is None
    assert repository.begin(lease, run["run_id"])["state"] == "DENIED"
    assert repository.finish(lease, run["run_id"])["state"] == "DENIED"
    for operation in [repository.get, repository.events, repository.cancel]:
        with pytest.raises(DisabledResearchUser): operation(owner, workspace, run["run_id"])
    with engine.connect() as connection:
        events = list(connection.execute(select(research_run_event).where(research_run_event.c.run_id == run["run_id"])).mappings())
        assert events[-1]["kind"] == "DENIED"
        assert events[-1]["payload"] == {"state": "DENIED"}
    # Reconciliation also terminates revoked pending jobs without loading their text/configuration.
    workspaces = WorkspaceRepository(engine)
    b = workspaces.resolve_identity(VerifiedPrincipal("lifecycle-test", "b")); w = workspaces.default(b)["workspace_id"]
    pending = _create(storage, key="pending", owner=b, workspace=w)
    with engine.begin() as connection: connection.execute(update(research_user).where(research_user.c.user_id == b).values(status="DISABLED"))
    results = repository.reconcile(limit=1)
    assert results["runs"][0]["run_id"] == pending["run_id"]
    assert results["runs"][0]["state"] == "DENIED"
    c = workspaces.resolve_identity(VerifiedPrincipal("lifecycle-test", "c")); wc = workspaces.default(c)["workspace_id"]
    d = workspaces.resolve_identity(VerifiedPrincipal("lifecycle-test", "d")); wd = workspaces.default(d)["workspace_id"]
    active_first = _create(storage, key="active-first", owner=c, workspace=wc)
    revoked_later = _create(storage, key="revoked-later", owner=d, workspace=wd)
    with engine.begin() as connection: connection.execute(update(research_user).where(research_user.c.user_id == d).values(status="DISABLED"))
    first_batch = repository.reconcile(limit=1)
    assert first_batch["runs"][0]["run_id"] == active_first["run_id"]
    later = repository.reconcile(after=first_batch["next_after"], limit=1)
    assert later["runs"][0]["run_id"] == revoked_later["run_id"]
    assert later["runs"][0]["state"] == "DENIED"


def test_stale_lease_rollback_database_immutability_and_terminal_queue_failure(storage, monkeypatch):
    engine, _, _, owner, workspace, _ = storage
    run = _create(storage); repository = ResearchRunRepository(engine); lease = _claim(engine)
    repository.begin(lease, run["run_id"])
    before = repository.get(owner, workspace, run["run_id"])
    original = repository._append
    def rollback(*args, **kwargs): original(*args, **kwargs); raise RuntimeError("fixture rollback after writes")
    monkeypatch.setattr(repository, "_append", rollback)
    with pytest.raises(RuntimeError): repository.progress(lease, run["run_id"], event_key="rollback", stage="fixture")
    monkeypatch.setattr(repository, "_append", original)
    assert repository.get(owner, workspace, run["run_id"]) == before
    with engine.begin() as connection:
        connection.execute(update(durable_job).where(durable_job.c.job_id == lease.job_id).values(lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
    with pytest.raises(LeaseLost): repository.progress(lease, run["run_id"], event_key="stale", stage="fixture")
    jobs = DurableJobRepository(engine, retry_policy=RetryPolicy(jitter_seconds=lambda _: 0))
    jobs.recover_expired(); replacement = _claim(engine)
    assert replacement.generation > lease.generation
    with pytest.raises(LeaseLost): repository.finish(lease, run["run_id"])
    for statement in [research_run_event.delete(), update(research_run_event).values(payload={}),
        update(research_run).values(owner_id=999), update(research_run).values(request_text="overwritten")]:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection: connection.execute(statement)
    repository.begin(replacement, run["run_id"])
    with engine.begin() as connection: connection.execute(update(durable_job).where(durable_job.c.job_id == replacement.job_id).values(max_attempts=2))
    jobs.fail(replacement, error_summary="fixture terminal queue failure")
    assert repository.reconcile()["runs"][0]["state"] == "FAILED"
    assert repository.get(owner, workspace, run["run_id"])["state"] == "FAILED"


def _crash_worker(url, ready, terminal_before_crash):
    engine = create_engine(url); repository = ResearchRunRepository(engine)
    lease = _claim(engine, seconds=0.4)
    with engine.connect() as connection: run_id = connection.scalar(select(research_run.c.run_id).where(research_run.c.job_id == lease.job_id))
    repository.begin(lease, run_id)
    repository.progress(lease, run_id, event_key="persisted", stage="deterministic_fixture")
    if terminal_before_crash: repository.finish(lease, run_id)
    ready.send(run_id); ready.close()
    os._exit(0)


@pytest.mark.parametrize("terminal_before_crash", [False, True])
def test_worker_process_death_reclaim_and_terminal_replay_use_existing_runtime(storage, terminal_before_crash):
    engine, url, _, owner, workspace, _ = storage
    run = _create(storage); parent, child = multiprocessing.get_context("spawn").Pipe(duplex=False)
    process = multiprocessing.get_context("spawn").Process(target=_crash_worker, args=(url.render_as_string(hide_password=False), child, terminal_before_crash))
    process.start(); assert parent.poll(15); assert parent.recv() == run["run_id"]
    process.join(timeout=10); assert process.exitcode == 0
    sleep(0.5)
    jobs = DurableJobRepository(engine, retry_policy=RetryPolicy(jitter_seconds=lambda _: 0)); jobs.recover_expired()
    repository = ResearchRunRepository(engine)
    def fixture_handler(context):
        state = repository.begin(context.lease, context.job.parameters["run_id"])
        if state["state"] not in TERMINAL:
            repository.progress(context.lease, state["run_id"], event_key="persisted", stage="deterministic_fixture")
            repository.finish(context.lease, state["run_id"])
        return {"deterministic_lifecycle_fixture": True}
    recovery_cursor = 0
    def lifecycle_recovery(_):
        nonlocal recovery_cursor
        recovery_cursor = repository.reconcile(after=recovery_cursor)["next_after"]
        return []
    runtime = DurableJobRuntime(jobs, lifecycle_recovery, {RESEARCH_JOB_KIND: fixture_handler}, worker_count=1,
        poll_interval=timedelta(milliseconds=20), lease_duration=timedelta(seconds=3), heartbeat_interval=timedelta(seconds=1))
    runtime.start()
    try:
        deadline = monotonic() + 10
        while monotonic() < deadline:
            with engine.connect() as connection:
                if connection.scalar(select(durable_job.c.status).where(durable_job.c.job_id == run["job_id"])) == "succeeded": break
            sleep(0.03)
        else: pytest.fail("fixture runtime did not resume")
    finally: assert runtime.stop(timeout=5)
    events = repository.events(owner, workspace, run["run_id"])["events"]
    assert sum(event["kind"] == "PROGRESS" for event in events) == 1
    assert repository.get(owner, workspace, run["run_id"])["state"] == "SUCCEEDED"


def _serve_fixture(url, port):
    import uvicorn
    engine = create_engine(url); repository = ResearchRunRepository(engine)
    def fixture_handler(context):
        run_id = context.job.parameters["run_id"]
        state = repository.begin(context.lease, run_id)
        if state["state"] in TERMINAL: return {"deterministic_lifecycle_fixture": True}
        if state["state"] != "CANCEL_REQUESTED":
            repository.progress(context.lease, run_id, event_key="http-fixture", stage="waiting_for_test_cancellation")
        while not context.stopping.is_set():
            state = repository.context(context.lease, run_id)["run"]
            if state["state"] in TERMINAL: return {"deterministic_lifecycle_fixture": True}
            if state["state"] == "CANCEL_REQUESTED":
                repository.finish(context.lease, run_id); return {"deterministic_lifecycle_fixture": True}
            sleep(0.05)
        return None
    recovery_cursor = 0
    def lifecycle_recovery(_):
        nonlocal recovery_cursor
        recovery_cursor = repository.reconcile(after=recovery_cursor)["next_after"]
        return []
    runtime = DurableJobRuntime(DurableJobRepository(engine), lifecycle_recovery, {RESEARCH_JOB_KIND: fixture_handler},
        worker_count=1, poll_interval=timedelta(milliseconds=20), lease_duration=timedelta(seconds=3), heartbeat_interval=timedelta(seconds=1))
    runtime.start()
    try: uvicorn.run(_app(engine), host="127.0.0.1", port=port, log_level="error")
    finally: runtime.stop(timeout=5); engine.dispose()


def _server(url, path):
    with socket.socket() as listener: listener.bind(("127.0.0.1", 0)); port = listener.getsockname()[1]
    process = multiprocessing.get_context("spawn").Process(target=_serve_fixture, args=(url.render_as_string(hide_password=False), port))
    process.start(); origin = f"http://127.0.0.1:{port}"
    deadline = monotonic() + 15
    while monotonic() < deadline:
        try:
            with urlopen(origin + path, timeout=1): return process, origin
        except URLError:
            if not process.is_alive(): break
            sleep(0.05)
    process.terminate(); process.join(timeout=5); pytest.fail("fixture HTTP service did not start")


def _frame(response):
    frame = {}
    while True:
        line = response.readline().decode().strip()
        if not line: return frame
        if ": " in line:
            key, value = line.split(": ", 1); frame[key] = value


def test_actual_http_disconnect_replay_cancel_and_api_worker_restart(storage):
    engine, url, _, owner, workspace, _ = storage
    run = _create(storage); path = f"/research/workspaces/{workspace}/runs/{run['run_id']}"
    process, origin = _server(url, path)
    try:
        with urlopen(Request(origin + path + "/stream", headers={"Accept-Encoding": "gzip"}), timeout=10) as response:
            assert response.headers["cache-control"] == "private, no-store"
            assert response.headers.get("content-encoding") is None
            first = _frame(response); assert first["id"] == f"{run['run_id']}:1"
        sleep(0.1)
        assert ResearchRunRepository(engine).get(owner, workspace, run["run_id"])["state"] == "RUNNING"
        with urlopen(Request(origin + path + "/stream", headers={"Last-Event-ID": first["id"]}), timeout=10) as response:
            resumed = _frame(response); assert resumed["id"] == f"{run['run_id']}:2"
            with urlopen(Request(origin + path + "/cancel", method="POST"), timeout=5) as canceled:
                assert json.load(canceled)["state"] == "CANCEL_REQUESTED"
            frames = [resumed]
            while True:
                event = _frame(response)
                if not event: break
                frames.append(event)
            assert json.loads(frames[-1]["data"])["kind"] == "CANCELLED"
            assert len({frame["id"] for frame in frames}) == len(frames)
        last_id = frames[-1]["id"]
    finally: process.terminate(); process.join(timeout=5); assert not process.is_alive()

    # New API/worker process reads prior events and a persisted pending cancellation.
    pending = _create(storage, key="restart-cancel")
    ResearchRunRepository(engine).cancel(owner, workspace, pending["run_id"])
    process, origin = _server(url, path)
    try:
        with urlopen(Request(origin + path + "/stream", headers={"Last-Event-ID": first["id"]}), timeout=5) as response:
            replayed = []
            while True:
                event = _frame(response)
                if not event: break
                replayed.append(event["id"])
            assert replayed[-1] == last_id
        deadline = monotonic() + 10
        while monotonic() < deadline:
            if ResearchRunRepository(engine).get(owner, workspace, pending["run_id"])["state"] == "CANCELLED": break
            sleep(0.05)
        else: pytest.fail("restarted fixture worker ignored persisted cancellation")
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(durable_job).where(durable_job.c.kind == RESEARCH_JOB_KIND)) == 2
    finally: process.terminate(); process.join(timeout=5); assert not process.is_alive()



def test_actual_http_stream_revocation_terminates_and_service_denial_has_no_private_context(storage):
    engine, url, _, owner, workspace, _ = storage
    run = _create(storage); path = f"/research/workspaces/{workspace}/runs/{run['run_id']}"
    process, origin = _server(url, path)
    try:
        with urlopen(origin + path + "/stream", timeout=10) as response:
            first = _frame(response); assert first["id"] == f"{run['run_id']}:1"
            with engine.begin() as connection:
                connection.execute(update(research_user).where(research_user.c.user_id == owner).values(status="DISABLED"))
            frames = []
            while True:
                frame = _frame(response)
                if not frame: break
                frames.append(frame)
            assert frames[-1]["event"] == "denied"
            assert json.loads(frames[-1]["data"]) == {"state": "DENIED"}
            assert "Private lifecycle fixture request" not in json.dumps(frames)
        deadline = monotonic() + 10
        while monotonic() < deadline:
            with engine.connect() as connection:
                if connection.scalar(select(research_run.c.state).where(research_run.c.run_id == run["run_id"])) == "DENIED": break
            sleep(0.05)
        else: pytest.fail("existing runtime did not reconcile revocation")
        with engine.connect() as connection:
            denied = connection.execute(select(research_run_event.c.payload).where(research_run_event.c.run_id == run["run_id"], research_run_event.c.kind == "DENIED")).scalar_one()
            assert denied == {"state": "DENIED"}
    finally: process.terminate(); process.join(timeout=5); assert not process.is_alive()
