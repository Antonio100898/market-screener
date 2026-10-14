import json
import multiprocessing
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from screener.postgres import (active_data_release, issuer, research_identity, research_user,
                              research_workspace, research_workspace_revision)
from screener.research_api import require_principal, router, workspace_repository
from screener.storage_config import StorageSettings
from screener.workspaces import (DisabledResearchUser, VerifiedPrincipal, WorkspaceConflict,
                                WorkspaceNotFound, WorkspaceRepository)


pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1", reason="requires real local PostgreSQL")


@pytest.fixture
def storage(monkeypatch, request):
    settings = StorageSettings.from_env()
    name = f"ms_workspace_{uuid4().hex}"
    assert name.startswith("ms_workspace_") and len(name) == 45
    admin = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    url = make_url(settings.database_url).set(database=name)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url)
    try:
        monkeypatch.setenv("SCREENER_DATABASE_URL", url.render_as_string(hide_password=False))
        config = Config("alembic.ini")
        start = getattr(request, "param", "base")
        command.upgrade(config, start)
        if start == "20261004_0007":
            with engine.begin() as connection:
                connection.execute(issuer.insert().values(source_system="SEC", source_identifier="0000001800"))
        command.upgrade(config, "head")
        yield engine, url, config
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def _client(repository, subject):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_principal] = lambda: VerifiedPrincipal("local-test", subject)
    app.dependency_overrides[workspace_repository] = lambda: repository
    return TestClient(app)


def _edit(client, workspace, *, key="edit-1", query="ABT", revision=1, **extra):
    return client.put(f"/research/workspaces/{workspace}", json={
        "expected_revision": revision, "request_key": key,
        "configuration": {"view": {"q": query, "maxPe": 30}}, **extra,
    })


@pytest.mark.parametrize("storage", ["base", "20261004_0007"], indirect=True)
def test_migration_empty_and_previous_head_keep_shared_data_and_match_metadata(storage):
    engine, _, config = storage
    command.current(config)
    command.heads(config)
    command.check(config)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(config).get_current_head()
        assert connection.scalar(select(active_data_release.c.release_id)) is None
        assert connection.scalar(select(func.count()).select_from(issuer)) in (0, 1)


def test_two_user_api_ownership_history_disabled_status_and_private_headers(storage):
    engine, _, _ = storage
    repository = WorkspaceRepository(engine)
    a, b = _client(repository, "a"), _client(repository, "b")
    workspace_a = a.get("/research/workspaces/default").json()["workspace_id"]
    workspace_b = b.get("/research/workspaces/default").json()["workspace_id"]
    assert workspace_a != workspace_b
    changed_a = _edit(a, workspace_a, query="private A")
    changed_b = _edit(b, workspace_b, query="private B")
    assert changed_a.status_code == changed_b.status_code == 200
    assert a.get(f"/research/workspaces/{workspace_a}").json()["configuration"]["view"]["q"] == "private A"
    assert b.get(f"/research/workspaces/{workspace_b}").json()["configuration"]["view"]["q"] == "private B"
    attacks = [b.get(f"/research/workspaces/{workspace_a}"), _edit(b, workspace_a),
               b.get(f"/research/workspaces/{workspace_a}/revisions")]
    assert [item.status_code for item in attacks] == [404, 404, 404]
    assert all("private A" not in item.text for item in attacks)
    rejected_owner = _edit(b, workspace_b, revision=2, owner_id=repository.resolve_identity(VerifiedPrincipal("local-test", "a")))
    assert rejected_owner.status_code == 422
    histories = a.get(f"/research/workspaces/{workspace_a}/revisions?limit=1")
    assert histories.status_code == 200
    assert histories.json()["next_before"] == 2
    assert a.get(f"/research/workspaces/{workspace_a}/revisions?before=2").json()["revisions"][0]["edit_revision"] == 1
    with engine.begin() as connection:
        owner_a = connection.scalar(select(research_identity.c.user_id).where(research_identity.c.subject == "a"))
        connection.execute(update(research_user).where(research_user.c.user_id == owner_a).values(status="DISABLED"))
    disabled = [a.get("/research/workspaces/default"), a.get(f"/research/workspaces/{workspace_a}"),
                _edit(a, workspace_a, revision=2), a.get(f"/research/workspaces/{workspace_a}/revisions")]
    assert [item.status_code for item in disabled] == [403] * 4
    for response in [changed_a, changed_b, rejected_owner, histories, *attacks, *disabled]:
        assert response.headers["cache-control"] == "private, no-store"
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(issuer)) == 0
        assert connection.scalar(select(active_data_release.c.release_id)) is None


def test_duplicate_first_requests_conflicting_writers_and_replay(storage):
    engine, _, _ = storage
    repository = WorkspaceRepository(engine)
    barrier = Barrier(2)

    def first():
        barrier.wait(timeout=5)
        owner = repository.resolve_identity(VerifiedPrincipal("local-test", "same"))
        return owner, repository.default(owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: first(), range(2)))
    assert results[0] == results[1]
    owner, workspace = results[0]
    other = repository.resolve_identity(VerifiedPrincipal("different-issuer", "same"))
    assert other != owner
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(research_user)) == 2
        assert connection.scalar(select(func.count()).select_from(research_workspace)) == 1
    barrier = Barrier(2)

    def write(key):
        barrier.wait(timeout=5)
        try:
            return repository.replace(owner, workspace["workspace_id"], expected_revision=1,
                                      configuration={"view": {"q": key}}, request_key=key)
        except WorkspaceConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        edits = list(pool.map(write, ["one", "two"]))
    assert sum(item is not None for item in edits) == 1
    accepted = next(item for item in edits if item is not None)
    accepted_key = accepted["configuration"]["view"]["q"]
    engine.dispose()
    assert repository.replace(owner, workspace["workspace_id"], expected_revision=1,
                              configuration={"view": {"q": accepted_key}}, request_key=accepted_key) == accepted
    with pytest.raises(WorkspaceConflict, match="different edit"):
        repository.replace(owner, workspace["workspace_id"], expected_revision=1,
                           configuration={"view": {"q": "changed"}}, request_key=accepted_key)
    repository.replace(owner, workspace["workspace_id"], expected_revision=2,
                       configuration={"view": {"q": "later"}}, request_key="later")
    replay = repository.replace(owner, workspace["workspace_id"], expected_revision=1,
                                configuration={"view": {"q": accepted_key}}, request_key=accepted_key)
    assert replay == accepted
    assert repository.get(owner, workspace["workspace_id"])["edit_revision"] == 3
    assert [row["edit_revision"] for row in repository.history(owner, workspace["workspace_id"])] == [3, 2, 1]


def test_database_history_identity_ownership_and_pointer_constraints(storage):
    engine, _, _ = storage
    repository = WorkspaceRepository(engine)
    owner_a = repository.resolve_identity(VerifiedPrincipal("test", "a"))
    owner_b = repository.resolve_identity(VerifiedPrincipal("test", "b"))
    workspace = repository.default(owner_a)
    workspace_id = workspace["workspace_id"]
    for statement in [
        update(research_workspace_revision).where(research_workspace_revision.c.workspace_id == workspace_id).values(actor="AGENT"),
        research_workspace_revision.delete().where(research_workspace_revision.c.workspace_id == workspace_id),
        update(research_identity).where(research_identity.c.subject == "a").values(user_id=owner_b),
        research_identity.delete().where(research_identity.c.subject == "a"),
        update(research_workspace).where(research_workspace.c.workspace_id == workspace_id).values(owner_id=owner_b),
        update(research_workspace).where(research_workspace.c.workspace_id == workspace_id).values(edit_revision=99),
        research_workspace_revision.insert().values(workspace_id=workspace_id, owner_id=owner_b,
            edit_revision=2, schema_revision=1, configuration={}, request_key="attack", request_sha256="a" * 64, actor="USER"),
    ]:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(statement)
    assert repository.get(owner_a, workspace_id) == workspace
    with pytest.raises(WorkspaceNotFound):
        repository.history(owner_b, workspace_id)
    with engine.begin() as connection:
        connection.execute(update(research_user).where(research_user.c.user_id == owner_a).values(status="DISABLED"))
    with pytest.raises(DisabledResearchUser):
        repository.replace(owner_a, workspace_id, expected_revision=1, configuration={}, request_key="disabled")


def _serve_test_api(database_url, port, subject):
    import uvicorn
    engine = create_engine(database_url)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_principal] = lambda: VerifiedPrincipal("restart-test", subject)
    app.dependency_overrides[workspace_repository] = lambda: WorkspaceRepository(engine)
    uvicorn.run(app, host="127.0.0.1", port=port, workers=1, log_level="error")


def _read_restarted_service(url, subject):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = multiprocessing.get_context("spawn").Process(target=_serve_test_api,
        args=(url.render_as_string(hide_password=False), port, subject))
    process.start()
    try:
        deadline = monotonic() + 20
        while monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/research/workspaces/default", timeout=1) as response:
                    assert response.headers["cache-control"] == "private, no-store"
                    return json.load(response)
            except URLError:
                if not process.is_alive():
                    raise AssertionError("test API process exited before serving")
                sleep(0.05)
        raise AssertionError("test API did not start")
    finally:
        process.terminate()
        process.join(timeout=5)
        assert not process.is_alive()


def test_private_data_survives_two_api_process_restarts(storage):
    engine, url, _ = storage
    repository = WorkspaceRepository(engine)
    for subject in ("a", "b"):
        owner = repository.resolve_identity(VerifiedPrincipal("restart-test", subject))
        workspace = repository.default(owner)
        repository.replace(owner, workspace["workspace_id"], expected_revision=1,
                           configuration={"view": {"q": f"private {subject}"}}, request_key="saved")
        first = _read_restarted_service(url, subject)
        second = _read_restarted_service(url, subject)
        assert first == second
        assert second["configuration"]["view"]["q"] == f"private {subject}"
        assert second["edit_revision"] == 2
