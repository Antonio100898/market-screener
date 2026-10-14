"""Actual private restore transactions; test identities exist only in disposable apps."""
import hashlib
import json
import multiprocessing
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
from sqlalchemy import create_engine, event, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError

from screener.postgres import research_user, research_workspace, research_workspace_revision
from screener.storage_config import StorageSettings
from screener.workspace_config import validate_config
from screener.workspaces import DisabledResearchUser, VerifiedPrincipal, WorkspaceConflict, WorkspaceNotFound, WorkspaceRepository
from test_workspace_storage import _client, _serve_test_api

pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1", reason="requires real local PostgreSQL")
ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "api/migrations/versions/20261004_0020_workspace_restore_source.py"


def _upgrade(config):
    frozen = ROOT / "tmp/verification-runtime/applied-0020/20261004_0020_workspace_restore_source.py"
    body = MIGRATION.read_bytes()
    if frozen.exists():
        assert frozen.read_bytes() == body, "applied0020 must remain byte-identical"
    command.upgrade(config, "20261004_0020")
    if not frozen.exists():
        frozen.parent.mkdir(parents=True, exist_ok=True)
        frozen.write_bytes(body)
        (ROOT / "local/private-restore-backend-applied0020-2026-10-04.json").write_text(json.dumps({
            "revision": "20261004_0020", "parent": "20261004_0019",
            "first_successful_application_utc": datetime.now(timezone.utc).isoformat(),
            "sha256": hashlib.sha256(body).hexdigest(), "copy": str(frozen),
            "target": "verified disposable ms_workspace_restore_<uuid> database",
        }, indent=2), encoding="utf-8")


@pytest.fixture
def previous_head(monkeypatch):
    settings = StorageSettings.from_env()
    original = make_url(settings.database_url)
    assert original.host == "127.0.0.1" and original.port == 55432
    name = "ms_workspace_restore_" + uuid4().hex
    assert name.startswith("ms_workspace_restore_") and len(name) == 53
    admin = create_engine(original, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = original.set(database=name)
    engine = create_engine(url)
    try:
        monkeypatch.setenv("SCREENER_DATABASE_URL", url.render_as_string(hide_password=False))
        config = Config("alembic.ini")
        command.upgrade(config, "20261004_0019")
        yield engine, url, config
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def storage(previous_head):
    engine, url, config = previous_head
    _upgrade(config)
    yield engine, url, config


def _owner(repository, subject="a"):
    owner = repository.resolve_identity(VerifiedPrincipal("local-test", subject))
    return owner, repository.default(owner)


def _restore(client, workspace, expected, source, key="restore"):
    return client.post(f"/research/workspaces/{workspace}/restore", json={
        "expected_revision": expected, "source_revision": source, "request_key": key,
    })


def _old_state(engine):
    with engine.connect() as connection:
        return {name: connection.scalars(text(
            f"SELECT to_jsonb(t) - 'restored_from_revision' FROM {name} t ORDER BY to_jsonb(t)::text"
        )).all() for name in ("research_user", "research_identity", "research_workspace", "research_workspace_revision")}


def _populate_old(engine):
    configurations = [validate_config({"view": {"q": q}, "columns": ["price", "ticker"]}) for q in ("初期", "before", "latest")]
    with engine.begin() as connection:
        owner = connection.scalar(text("INSERT INTO research_user DEFAULT VALUES RETURNING user_id"))
        connection.execute(text("INSERT INTO research_identity(issuer,subject,user_id) VALUES ('local-test','a',:owner)"), {"owner": owner})
        workspace = connection.scalar(text("INSERT INTO research_workspace(owner_id,schema_revision,edit_revision) VALUES (:owner,1,3) RETURNING workspace_id"), {"owner": owner})
        for revision, configuration in enumerate(configurations, 1):
            digest_input = {"expected_revision": revision-1, "configuration": configuration, "actor": "USER"}
            digest = hashlib.sha256(json.dumps(digest_input, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
            connection.execute(text("""INSERT INTO research_workspace_revision
                (workspace_id,owner_id,edit_revision,schema_revision,configuration,request_key,request_sha256,actor)
                VALUES (:workspace,:owner,:revision,1,CAST(:configuration AS jsonb),:key,:digest,'USER')"""),
                {"workspace": workspace, "owner": owner, "revision": revision,
                 "configuration": json.dumps(configuration, ensure_ascii=False), "key": f"old-{revision}", "digest": digest})
    return owner, workspace, configurations


@pytest.mark.parametrize("populated", [False, True])
def test_empty_populated0019_upgrade_preserves_old_rows_and_put_replay(previous_head, populated):
    engine, _, config = previous_head
    if populated:
        owner, workspace, configurations = _populate_old(engine)
    before = _old_state(engine)
    _upgrade(config)
    assert _old_state(engine) == before
    command.current(config); command.heads(config); command.check(config)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(config).get_current_head() == "20261004_0020"
        assert connection.scalar(text("SELECT count(*) FROM research_workspace_revision WHERE restored_from_revision IS NOT NULL")) == 0
    if populated:
        repository = WorkspaceRepository(engine)
        replay = repository.replace(owner, workspace, expected_revision=1, configuration=configurations[1], request_key="old-2")
        assert replay["edit_revision"] == 2 and replay["restored_from_revision"] is None
        assert replay["configuration"] == configurations[1]
        assert repository.get(owner, workspace)["edit_revision"] == 3


def test_populated_migration_lock_timeout_rolls_back_then_retries(previous_head):
    engine, _, config = previous_head
    _populate_old(engine)
    before = _old_state(engine)
    with engine.connect() as blocker:
        transaction = blocker.begin()
        blocker.execute(text("SELECT count(*) FROM research_workspace_revision"))
        start = monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_upgrade, config)
            _wait_for_lock(engine, future)
            with pytest.raises(OperationalError, match="lock timeout"):
                future.result(timeout=8)
        elapsed = monotonic()-start
        assert 4.5 < elapsed < 8
        transaction.rollback()
    assert _old_state(engine) == before
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261004_0019"
        assert connection.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_name='research_workspace_revision' AND column_name='restored_from_revision'")) == 0
    _upgrade(config)
    assert _old_state(engine) == before
    command.check(config)
    print(f"Actual0020 migration blocked {elapsed:.3f}s; schema/version/old rows rolled back; populated retry/parity passed.")


def test_two_user_http_restore_history_and_disable_are_private(storage):
    engine, _, _ = storage
    repository = WorkspaceRepository(engine)
    a, b = _client(repository, "a"), _client(repository, "b")
    wa = a.get("/research/workspaces/default").json()["workspace_id"]
    wb = b.get("/research/workspaces/default").json()["workspace_id"]
    for client, workspace, query in ((a, wa, "private A"), (b, wb, "private B")):
        assert client.put(f"/research/workspaces/{workspace}", json={"expected_revision": 1, "request_key": "saved", "configuration": {"view": {"q": query}}}).status_code == 200
    replies = [a.get(f"/research/workspaces/{wb}"), a.get(f"/research/workspaces/{wb}/revisions"),
        a.put(f"/research/workspaces/{wb}", json={"expected_revision": 2, "request_key": "attack", "configuration": {}}),
        _restore(a, wb, 2, 1), _restore(b, wa, 2, 1)]
    assert [response.status_code for response in replies] == [404]*5
    accepted = _restore(a, wa, 2, 1)
    assert accepted.status_code == 200 and accepted.json()["restored_from_revision"] == 1
    assert b.get(f"/research/workspaces/{wb}").json()["configuration"]["view"]["q"] == "private B"
    history = a.get(f"/research/workspaces/{wa}/revisions?limit=1")
    assert history.json()["revisions"][0]["restored_from_revision"] == 1 and history.json()["next_before"] == 3
    owner = repository.resolve_identity(VerifiedPrincipal("local-test", "a"))
    with engine.begin() as connection:
        connection.execute(update(research_user).where(research_user.c.user_id == owner).values(status="DISABLED"))
    disabled = [a.get(f"/research/workspaces/{wa}"), a.get(f"/research/workspaces/{wa}/revisions"), _restore(a, wa, 3, 2)]
    assert [response.status_code for response in disabled] == [403]*3
    for response in [*replies, accepted, history, *disabled]:
        assert response.headers["cache-control"] == "private, no-store"


def test_restore_appends_actual_source_and_replays_after_newer_edits(storage):
    engine, _, _ = storage
    repository = WorkspaceRepository(engine)
    owner, initial = _owner(repository)
    workspace = initial["workspace_id"]
    repository.replace(owner, workspace, expected_revision=1, configuration={"view": {"q": "second"}, "columns": ["price"]}, request_key="second")
    repository.replace(owner, workspace, expected_revision=2, configuration={"view": {"q": "third"}}, request_key="third")
    before = repository.history(owner, workspace)
    restored = repository.restore(owner, workspace, expected_revision=3, source_revision=1, request_key="restore")
    assert restored["edit_revision"] == 4 and restored["restored_from_revision"] == 1
    assert restored["configuration"] == initial["configuration"]
    assert repository.history(owner, workspace, before=4) == before
    copied = repository.restore(owner, workspace, expected_revision=4, source_revision=4, request_key="restore-of-restore")
    assert copied["edit_revision"] == 5 and copied["restored_from_revision"] == 4
    equal_save = repository.replace(owner, workspace, expected_revision=5, configuration=copied["configuration"], request_key="equal-save")
    assert equal_save["edit_revision"] == 6 and equal_save["restored_from_revision"] is None
    engine.dispose()
    assert repository.restore(owner, workspace, expected_revision=3, source_revision=1, request_key="restore") == restored
    assert repository.get(owner, workspace)["edit_revision"] == 6
    for operation in (
        lambda: repository.restore(owner, workspace, expected_revision=3, source_revision=4, request_key="restore"),
        lambda: repository.restore(owner, workspace, expected_revision=4, source_revision=1, request_key="restore"),
        lambda: repository.replace(owner, workspace, expected_revision=3, configuration=initial["configuration"], request_key="restore"),
        lambda: repository.restore(owner, workspace, expected_revision=5, source_revision=5, request_key="equal-save"),
    ):
        with pytest.raises(WorkspaceConflict, match="different edit"):
            operation()
    with pytest.raises(WorkspaceConflict, match="reload"):
        repository.restore(owner, workspace, expected_revision=3, source_revision=1, request_key="stale")
    assert repository.get(owner, workspace)["edit_revision"] == 6


def test_missing_foreign_source_and_sql_restore_identity_are_rejected(storage):
    engine, _, _ = storage
    repository = WorkspaceRepository(engine)
    owner, initial = _owner(repository)
    other, foreign = _owner(repository, "b")
    workspace = initial["workspace_id"]
    repository.replace(other, foreign["workspace_id"], expected_revision=1, configuration={}, request_key="foreign-2")
    for source in (2, 99):
        with pytest.raises(WorkspaceNotFound):
            repository.restore(owner, workspace, expected_revision=1, source_revision=source, request_key=f"missing-{source}")
    for source, revision in ((0, 2), (-1, 2), (2, 2), (3, 2), (2, 3)):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(research_workspace_revision.insert().values(workspace_id=workspace, owner_id=owner,
                    edit_revision=revision, schema_revision=1, configuration={}, request_key=f"bad-{source}-{revision}",
                    request_sha256="a"*64, actor="USER", restored_from_revision=source))
    restored = repository.restore(owner, workspace, expected_revision=1, source_revision=1, request_key="valid")
    for statement in (update(research_workspace_revision).where(research_workspace_revision.c.workspace_id == workspace).values(restored_from_revision=None),
        research_workspace_revision.delete().where(research_workspace_revision.c.workspace_id == workspace)):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(statement)
    assert repository.get(owner, workspace) == restored


def test_concurrent_save_restore_has_one_winner(storage):
    engine, _, _ = storage
    repository = WorkspaceRepository(engine)
    owner, initial = _owner(repository)
    workspace = initial["workspace_id"]
    barrier = Barrier(2)
    def edit(restore):
        barrier.wait(timeout=5)
        try:
            if restore:
                return repository.restore(owner, workspace, expected_revision=1, source_revision=1, request_key="restore")
            return repository.replace(owner, workspace, expected_revision=1, configuration={"view": {"q": "save"}}, request_key="save")
        except WorkspaceConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(edit, [True, False]))
    assert sum(result is not None for result in outcomes) == 1
    assert len(repository.history(owner, workspace)) == 2
    assert repository.get(owner, workspace) == next(result for result in outcomes if result is not None)


def test_late_pointer_failure_rolls_back_restore_and_exact_retry(storage):
    engine, _, _ = storage
    repository = WorkspaceRepository(engine)
    owner, initial = _owner(repository)
    workspace = initial["workspace_id"]
    before = repository.history(owner, workspace)
    def fail_pointer(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE research_workspace SET"):
            raise RuntimeError("injected late pointer failure")
    event.listen(engine, "before_cursor_execute", fail_pointer)
    try:
        with pytest.raises(RuntimeError, match="late pointer"):
            repository.restore(owner, workspace, expected_revision=1, source_revision=1, request_key="retry")
    finally:
        event.remove(engine, "before_cursor_execute", fail_pointer)
    assert repository.get(owner, workspace) == initial and repository.history(owner, workspace) == before
    restored = repository.restore(owner, workspace, expected_revision=1, source_revision=1, request_key="retry")
    assert restored["edit_revision"] == 2 and restored["restored_from_revision"] == 1
    assert repository.restore(owner, workspace, expected_revision=1, source_revision=1, request_key="retry") == restored


def _wait_for_lock(engine, future):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
        deadline = monotonic()+3
        while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
            assert monotonic() < deadline and not future.done()
            sleep(.01)


@pytest.mark.parametrize("disable", [False, True])
def test_user_lock_wait_rechecks_disable_before_context(storage, disable):
    engine, _, _ = storage
    repository = WorkspaceRepository(engine)
    owner, initial = _owner(repository)
    workspace = initial["workspace_id"]
    with engine.connect() as blocker:
        transaction = blocker.begin()
        blocker.execute(select(research_user.c.user_id).where(research_user.c.user_id == owner).with_for_update())
        if disable:
            blocker.execute(update(research_user).where(research_user.c.user_id == owner).values(status="DISABLED"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(repository.restore, owner, workspace, expected_revision=1, source_revision=1, request_key="waited")
            _wait_for_lock(engine, future)
            with engine.connect() as observer:
                assert observer.scalar(text("SELECT count(*) FROM research_workspace_revision")) == 1
            transaction.commit()
            if disable:
                with pytest.raises(DisabledResearchUser):
                    future.result(timeout=5)
            else:
                assert future.result(timeout=5)["restored_from_revision"] == 1
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM research_workspace_revision")) == (1 if disable else 2)


def test_source_lookup_and_bounded_history_use_existing_pk(storage):
    engine, _, _ = storage
    repository = WorkspaceRepository(engine)
    owner, initial = _owner(repository)
    workspace = initial["workspace_id"]
    with engine.begin() as connection:
        connection.execute(text("""INSERT INTO research_workspace_revision
            (workspace_id,owner_id,edit_revision,schema_revision,configuration,request_key,request_sha256,actor)
            SELECT :workspace,:owner,n,1,'{}'::jsonb,'synthetic-'||n,repeat('a',64),'USER'
            FROM generate_series(2,2001) n"""), {"workspace": workspace, "owner": owner})
        connection.execute(update(research_workspace).where(research_workspace.c.workspace_id == workspace).values(edit_revision=2001))
        connection.execute(text("ANALYZE research_workspace_revision"))
        plans = [connection.scalar(text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + sql), {"workspace": workspace, "owner": owner})
            for sql in ("SELECT * FROM research_workspace_revision WHERE workspace_id=:workspace AND owner_id=:owner AND edit_revision=1",
                "SELECT * FROM research_workspace_revision WHERE workspace_id=:workspace AND owner_id=:owner AND edit_revision<1500 ORDER BY edit_revision DESC LIMIT 10")]
    def indexes(node):
        return [node.get("Index Name"), *[index for child in node.get("Plans", []) for index in indexes(child)]]
    for plan, expected in zip(plans, (1, 10)):
        assert "pk_research_workspace_revision" in indexes(plan[0]["Plan"])
        assert plan[0]["Plan"]["Actual Rows"] == expected
    rows = repository.history(owner, workspace, before=1500, limit=10)
    assert [row["edit_revision"] for row in rows] == list(range(1499,1489,-1))
    print("Synthetic2001-revision source/history existing-PK plans: " + json.dumps(plans))


def _restore_in_process(url, subject, workspace):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    process = multiprocessing.get_context("spawn").Process(target=_serve_test_api,
        args=(url.render_as_string(hide_password=False), port, subject))
    process.start()
    try:
        deadline = monotonic()+20
        while True:
            try:
                with urlopen(f"http://127.0.0.1:{port}/research/workspaces/default", timeout=1) as response:
                    assert json.load(response)["workspace_id"] == workspace
                break
            except URLError:
                assert monotonic() < deadline and process.is_alive(); sleep(.05)
        request = Request(f"http://127.0.0.1:{port}/research/workspaces/{workspace}/restore",
            data=json.dumps({"expected_revision": 2, "source_revision": 1, "request_key": "restart-restore"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=5) as response:
            assert response.headers["cache-control"] == "private, no-store"
            return json.load(response)
    finally:
        process.terminate(); process.join(timeout=5)
        assert not process.is_alive()


def test_exact_restore_replay_survives_actual_api_process_restart(storage):
    engine, url, _ = storage
    repository = WorkspaceRepository(engine)
    for subject in ("a", "b"):
        owner = repository.resolve_identity(VerifiedPrincipal("restart-test", subject))
        initial = repository.default(owner)
        workspace = initial["workspace_id"]
        repository.replace(owner, workspace, expected_revision=1, configuration={"view": {"q": subject}}, request_key="second")
        first = _restore_in_process(url, subject, workspace)
        repository.replace(owner, workspace, expected_revision=3, configuration={"view": {"q": "later"}}, request_key="later")
        second = _restore_in_process(url, subject, workspace)
        assert first == second and second["edit_revision"] == 3 and second["restored_from_revision"] == 1
        assert second["configuration"] == initial["configuration"]
        assert repository.get(owner, workspace)["edit_revision"] == 4
