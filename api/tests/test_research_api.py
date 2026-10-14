import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from screener.research_api import require_principal, router, workspace_repository
from screener.workspaces import (DisabledResearchUser, VerifiedPrincipal, WorkspaceConflict,
                                WorkspaceNotFound)


def make_app(principal=None, repository=None):
    app = FastAPI()
    app.include_router(router)
    if principal is not None:
        app.dependency_overrides[require_principal] = lambda: principal
    if repository is not None:
        app.dependency_overrides[workspace_repository] = lambda: repository
    return app


@pytest.mark.parametrize("method,path", [
    ("get", "/research/workspaces/default"), ("get", "/research/workspaces/1"),
    ("get", "/research/workspaces/1/revisions"), ("put", "/research/workspaces/1"),
    ("post", "/research/workspaces/1/restore"),
])
def test_private_reads_and_writes_deny_without_server_identity(method, path):
    class ForbiddenRepository:
        def resolve_identity(self, _):
            raise AssertionError("unauthenticated request reached private storage")

    client = TestClient(make_app(repository=ForbiddenRepository()))
    response = getattr(client, method)(path, headers={"X-Owner-Id": "1", "X-Screener-Token": "token"})
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"


def test_validation_not_found_and_storage_failure_are_private():
    class Repository:
        def resolve_identity(self, _):
            return 1

        def get(self, *_):
            raise WorkspaceNotFound("workspace not found")

        def default(self, *_):
            raise OperationalError("private params", {"secret": "not for response"}, Exception("database error"))

    client = TestClient(make_app(VerifiedPrincipal("local-test", "a"), Repository()))
    responses = [client.get("/research/workspaces/2"), client.get("/research/workspaces/default"),
                 client.put("/research/workspaces/1", json={"owner_id": 2})]
    assert [item.status_code for item in responses] == [404, 503, 422]
    for response in responses:
        assert response.headers["cache-control"] == "private, no-store"
        assert "not for response" not in response.text


def test_unexpected_private_error_does_not_leak_details_or_allow_caching():
    class Repository:
        def resolve_identity(self, _):
            raise RuntimeError("private secret")

    response = TestClient(make_app(VerifiedPrincipal("local-test", "a"), Repository())).get("/research/workspaces/default")
    assert response.status_code == 500
    assert response.headers["cache-control"] == "private, no-store"
    assert "private secret" not in response.text


def test_private_method_rejections_are_uncached_before_route_handler():
    response = TestClient(make_app()).delete("/research/workspaces/1")
    assert response.status_code == 405
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("patch", [
    {"expected_revision": True}, {"expected_revision": 0}, {"expected_revision": "2"},
    {"source_revision": True}, {"source_revision": 0}, {"source_revision": "1"},
    {"request_key": " "}, {"request_key": "x" * 201},
    {"configuration": {}}, {"owner_id": 2}, {"actor": "AGENT"},
    {"created_at": "2026-10-04"}, {"label": "undo"},
])
def test_restore_body_cannot_supply_identity_configuration_or_invalid_revisions(patch):
    class Repository:
        def resolve_identity(self, _):
            return 1

        def restore(self, *_args, **_kwargs):
            raise AssertionError("invalid restore reached the writer")

    response = TestClient(make_app(VerifiedPrincipal("local-test", "a"), Repository())).post(
        "/research/workspaces/1/restore",
        json={"expected_revision": 2, "source_revision": 1, "request_key": "restore-1", **patch},
    )
    assert response.status_code == 422
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("failure,status", [
    (None, 200), (WorkspaceNotFound("workspace revision not found"), 404),
    (DisabledResearchUser("research account is unavailable"), 403),
    (WorkspaceConflict("workspace changed; reload before editing", 4), 409),
    (OperationalError("secret query", {}, Exception("private secret")), 503),
    (RuntimeError("private secret"), 500),
])
def test_restore_reuses_server_owner_and_private_result_error_contract(failure, status):
    class Repository:
        def resolve_identity(self, principal):
            assert principal == VerifiedPrincipal("local-test", "a")
            return 7

        def restore(self, owner, workspace, **kwargs):
            assert (owner, workspace, kwargs) == (7, 3, {
                "expected_revision": 2, "source_revision": 1, "request_key": "restore-1",
            })
            if failure is not None:
                raise failure
            return {"workspace_id": 3, "edit_revision": 3, "restored_from_revision": 1}

    client = TestClient(make_app(VerifiedPrincipal("local-test", "a"), Repository()))
    response = client.post("/research/workspaces/3/restore", headers={"X-Owner-Id": "9"},
        json={"expected_revision": 2, "source_revision": 1, "request_key": "restore-1"})
    assert response.status_code == status
    assert response.headers["cache-control"] == "private, no-store"
    assert "private secret" not in response.text
    if status == 200:
        assert response.json()["restored_from_revision"] == 1
    if status == 409:
        assert response.json()["detail"]["current_revision"] == 4


def test_restore_method_rejection_is_private():
    response = TestClient(make_app()).put("/research/workspaces/1/restore")
    assert response.status_code == 405
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("method,path,allow", [
    (method, path, allow)
    for path, allow in [
        ("/research/workspaces/default", "GET"),
        ("/research/workspaces/1", "GET"),
        ("/research/workspaces/1/revisions", "GET"),
        ("/research/workspaces/1/restore", "POST"),
        ("/research/workspaces/1/runs", "POST"),
        ("/research/workspaces/1/runs/1", "GET"),
        ("/research/workspaces/1/runs/1/cancel", "POST"),
        ("/research/workspaces/1/runs/1/events", "GET"),
        ("/research/workspaces/1/runs/1/stream", "GET"),
    ]
    for method in ("HEAD", "DELETE", "OPTIONS")
] + [("POST", "/research/workspaces/1", "GET"),
     ("POST", "/research/workspaces/1/revisions", "GET"),
     ("GET", "/research/workspaces/1/restore", "POST"),
     ("PUT", "/research/workspaces/1/restore", "POST")])
def test_actual_app_named_private_method_rejection_precedes_static_mount(monkeypatch, method, path, allow):
    from screener import api, research_run_api

    def forbidden_factory():
        raise AssertionError("method rejection accessed private storage")

    monkeypatch.delenv("SCREENER_TOKEN", raising=False)
    monkeypatch.setitem(api.app.dependency_overrides, workspace_repository, forbidden_factory)
    monkeypatch.setitem(api.app.dependency_overrides, research_run_api.run_repository, forbidden_factory)
    response = TestClient(api.app).request(method, path)
    assert response.status_code == 405
    assert response.headers["cache-control"] == "private, no-store"
    assert set(response.headers["allow"].split(", ")) == {allow}


@pytest.mark.parametrize("method,path", [
    ("GET", "/research/workspaces/default"), ("GET", "/research/workspaces/1"),
    ("PUT", "/research/workspaces/1"), ("POST", "/research/workspaces/1/restore"),
    ("GET", "/research/workspaces/1/revisions"), ("POST", "/research/workspaces/1/runs"),
    ("GET", "/research/workspaces/1/runs/1"), ("POST", "/research/workspaces/1/runs/1/cancel"),
    ("GET", "/research/workspaces/1/runs/1/events"), ("GET", "/research/workspaces/1/runs/1/stream"),
])
def test_actual_app_supported_private_methods_still_require_identity(monkeypatch, method, path):
    from screener import api, research_run_api

    def forbidden_factory():
        raise AssertionError("anonymous request accessed private storage")

    monkeypatch.delenv("SCREENER_TOKEN", raising=False)
    monkeypatch.setitem(api.app.dependency_overrides, workspace_repository, forbidden_factory)
    monkeypatch.setitem(api.app.dependency_overrides, research_run_api.run_repository, forbidden_factory)
    response = TestClient(api.app).request(method, path, json={})
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"


def test_actual_app_default_and_separate_workspace_get_put_restore_handlers_remain_reachable(monkeypatch):
    from screener import api
    from screener.workspace_config import WorkspaceConfig

    class Repository:
        def resolve_identity(self, principal):
            assert principal == VerifiedPrincipal("local-test", "route-control")
            return 7

        def default(self, owner):
            assert owner == 7
            return {"selected": "default"}

        def get(self, owner, workspace):
            assert (owner, workspace) == (7, 1)
            return {"selected": "get"}

        def replace(self, owner, workspace, **kwargs):
            assert (owner, workspace, kwargs["expected_revision"]) == (7, 1, 1)
            return {"selected": "put"}

        def restore(self, owner, workspace, **kwargs):
            assert (owner, workspace, kwargs["source_revision"]) == (7, 1, 1)
            return {"selected": "restore"}

    monkeypatch.delenv("SCREENER_TOKEN", raising=False)
    monkeypatch.setitem(api.app.dependency_overrides, require_principal,
                        lambda: VerifiedPrincipal("local-test", "route-control"))
    monkeypatch.setitem(api.app.dependency_overrides, workspace_repository, lambda: Repository())
    client = TestClient(api.app)
    responses = [client.get("/research/workspaces/default"), client.get("/research/workspaces/1"),
                 client.put("/research/workspaces/1", json={"expected_revision": 1,
                     "request_key": "route-control", "configuration": WorkspaceConfig().model_dump(mode="json")}),
                 client.post("/research/workspaces/1/restore", json={"expected_revision": 2,
                     "source_revision": 1, "request_key": "route-restore"})]
    assert [response.json() for response in responses] == [
        {"selected": name} for name in ("default", "get", "put", "restore")]
    assert all(response.status_code == 200 for response in responses)
    assert all(response.headers["cache-control"] == "private, no-store" for response in responses)
