import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from screener.research_api import workspace_repository
from screener.research_run_api import router, _resume_sequence
from screener.research_run_api import run_repository
from screener.research_api import current_owner
from screener import api


@pytest.mark.parametrize("method,path", [
    ("post", "/research/workspaces/1/runs"), ("get", "/research/workspaces/1/runs/1"),
    ("post", "/research/workspaces/1/runs/1/cancel"), ("get", "/research/workspaces/1/runs/1/events"),
    ("get", "/research/workspaces/1/runs/1/stream"),
])
def test_run_routes_reject_spoofed_owner_without_verified_server_identity(method, path):
    class ForbiddenRepository:
        def resolve_identity(self, _): raise AssertionError("unverified request reached private storage")
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[workspace_repository] = lambda: ForbiddenRepository()
    response = getattr(TestClient(app), method)(path, headers={"X-Owner-Id": "1", "X-Test-Principal": "a", "X-Screener-Token": "token"})
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"


def test_stream_cursor_binds_replay_to_one_run():
    assert _resume_sequence(7, None) == 0
    assert _resume_sequence(7, "7:13") == 13
    for invalid in ["8:13", "7:-1", "7:0", "13", "7:1:2", "7:not-an-event"]:
        with pytest.raises(Exception) as error: _resume_sequence(7, invalid)
        assert error.value.status_code == 400


def test_main_app_run_dispatch_stays_closed_before_opening_storage(monkeypatch):
    monkeypatch.delenv("SCREENER_TOKEN", raising=False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("closed run dispatch touched storage or started jobs")

    monkeypatch.setattr(api.jobs, "start_hourly_quotes", forbidden)
    monkeypatch.setitem(api.app.dependency_overrides, current_owner, lambda: 17)
    monkeypatch.setitem(api.app.dependency_overrides, run_repository, forbidden)
    response = TestClient(api.app).post("/research/workspaces/1/runs", json={
        "expected_revision": 1, "release_id": 1,
        "request_key": "manual-local-check", "request_text": "Inspect ABT",
    })
    assert response.status_code == 503
    assert response.json() == {"detail": "research execution is not configured"}
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("method,path,status", [
    ("put", "/research/workspaces/1/runs/1", 405),
    ("delete", "/research/workspaces/1/runs/1/cancel", 405),
    ("get", "/research/workspaces/1/runs/1/missing", 404),
])
def test_main_app_run_routing_errors_are_private(monkeypatch, method, path, status):
    monkeypatch.delenv("SCREENER_TOKEN", raising=False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid method reached private owner or storage")

    monkeypatch.setitem(api.app.dependency_overrides, current_owner, forbidden)
    monkeypatch.setitem(api.app.dependency_overrides, run_repository, forbidden)
    response = TestClient(api.app).request(method, path)
    assert response.status_code == status
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {"detail": "Not Found" if status == 404 else "Method Not Allowed"}


def test_standalone_run_method_rejection_preserves_allow():
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).put("/research/workspaces/1/runs/1")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.headers["cache-control"] == "private, no-store"
