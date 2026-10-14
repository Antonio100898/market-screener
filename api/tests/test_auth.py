"""Write protection, used when the instance is reachable off-machine."""
import os

import pytest
from fastapi.testclient import TestClient

from screener import api
from screener.research_api import workspace_repository


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SCREENER_TOKEN", "s3cret")
    return TestClient(api.app)


def test_reads_stay_open(client):
    assert client.get("/config").json() == {"write_protected": True}
    assert client.get("/health").status_code == 200


def test_write_without_token_is_refused(client):
    r = client.post("/sync", json={"command": "export"})
    assert r.status_code == 401
    assert "token" in r.json()["detail"]


def test_write_with_wrong_token_is_refused(client):
    r = client.post("/sync", json={"command": "export"}, headers={"X-Screener-Token": "guess"})
    assert r.status_code == 401


def test_write_with_token_is_allowed_through(client):
    # reaches the handler; 409 means the job layer answered, not the gate
    r = client.post("/sync", json={"command": "nope"}, headers={"X-Screener-Token": "s3cret"})
    assert r.status_code == 409


def test_unset_token_leaves_localhost_open(monkeypatch):
    monkeypatch.delenv("SCREENER_TOKEN", raising=False)
    c = TestClient(api.app)
    assert c.get("/config").json() == {"write_protected": False}
    assert c.post("/sync", json={"command": "nope"}).status_code == 409   # not 401


@pytest.mark.parametrize("shared_token,supplied", [(None, None), ("s3cret", None),
                                                   ("s3cret", "guess"), ("s3cret", "s3cret")])
def test_mounted_research_routes_require_identity_before_private_storage(monkeypatch, shared_token, supplied):
    if shared_token is None:
        monkeypatch.delenv("SCREENER_TOKEN", raising=False)
    else:
        monkeypatch.setenv("SCREENER_TOKEN", shared_token)

    def forbidden_factory():
        raise AssertionError("anonymous request opened private storage")

    monkeypatch.setitem(api.app.dependency_overrides, workspace_repository, forbidden_factory)
    client = TestClient(api.app)
    headers = {"X-Owner-Id": "1", "Authorization": "Bearer unverified"}
    if supplied:
        headers["X-Screener-Token"] = supplied
    for method, path in [("get", "/research/workspaces/default"),
                         ("get", "/research/workspaces/1"),
                         ("get", "/research/workspaces/1/revisions"),
                         ("put", "/research/workspaces/1"),
                         ("post", "/research/workspaces/1/runs"),
                         ("get", "/research/workspaces/1/runs/1"),
                         ("post", "/research/workspaces/1/runs/1/cancel"),
                         ("get", "/research/workspaces/1/runs/1/events"),
                         ("get", "/research/workspaces/1/runs/1/stream")]:
        kwargs = {"headers": headers}
        if method == "put":
            kwargs["json"] = {"expected_revision": 1, "request_key": "edit", "configuration": {}}
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "private, no-store"


def test_research_routing_errors_are_private_on_main_app(monkeypatch):
    monkeypatch.delenv("SCREENER_TOKEN", raising=False)
    client = TestClient(api.app)
    for response in [client.get("/research/missing"), client.post("/research/workspaces/default")]:
        assert response.status_code in (404, 405)
        assert response.headers["cache-control"] == "private, no-store"


def test_assumption_detail_failure_returns_strict_row_not_http_500(monkeypatch):
    base = {"ticker": "TEST", "cik": "0000000001", "annual_ratios": {}}

    class Connection:
        def close(self):
            pass

    class BrokenLoader:
        def load(self, cik, ticker):
            raise RuntimeError("cached filing bundle is unavailable")

    monkeypatch.setattr(api, "_dashboard_payload", lambda: {"rows": [base]})
    monkeypatch.setattr(api.store, "connect", Connection)
    monkeypatch.setattr(api.evidence, "EvidenceLoader", lambda conn, edgar: BrokenLoader())

    response = TestClient(api.app).get("/company/TEST/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ticker"] == "TEST"
    assert payload["assumption_mode"]["status"] == "UNAVAILABLE"
    assert "cached filing bundle is unavailable" in payload["assumption_mode"]["note"]


def test_assumption_detail_can_still_request_strict_values(monkeypatch):
    base = {"ticker": "TEST", "cik": "0000000001", "annual_ratios": {}}
    monkeypatch.setattr(api, "_dashboard_payload", lambda: {"rows": [base]})

    response = TestClient(api.app).get(
        "/company/TEST/dashboard?assume_absent_zero=false")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ticker"] == base["ticker"]
    assert payload["latest_quarterly_filing"] is None


def test_ratio_gap_overlay_uses_zero_or_not_measurable_without_dashes():
    row = {"annual_ratios": {2025: {"net_margin": 12.5}}}

    details = api._fill_assumed_ratio_gaps(row)
    cell = row["annual_ratios"][2025]

    assert cell["net_margin"] == 12.5
    assert cell["revenue"] == 0
    assert cell["award_pct"] == 0
    assert "pe_undefined" in cell
    assert "current_ratio_undefined" in cell
    assert not any(value is None for value in cell.values())
    assert any(item["field"] == "revenue" and item["fiscal_year"] == 2025
               for item in details)


def test_latest_quarterly_filing_uses_newest_10q_or_6k(monkeypatch):
    class Edgar:
        def submissions(self, cik):
            return {"filings": {"recent": {
                "form": ["10-Q", "8-K", "10-Q/A"],
                "filingDate": ["2026-05-01", "2026-06-01", "2026-08-01"],
                "reportDate": ["2026-03-31", "2026-05-31", "2026-06-30"],
                "accessionNumber": ["0000000001-26-000001", "0000000001-26-000002", "0000000001-26-000003"],
                "primaryDocument": ["q1.htm", "current.htm", "q2.htm"],
            }}}

    monkeypatch.setattr(api, "_edgar", Edgar())
    filing = api._latest_quarterly_filing("0000000001")

    assert filing["form"] == "10-Q/A"
    assert filing["filed"] == "2026-08-01"
    assert filing["period"] == "2026-06-30"
    assert filing["url"].endswith("000000000126000003/0000000001-26-000003-index.htm")


def test_company_detail_includes_latest_quarterly_filing(monkeypatch):
    base = {"ticker": "TEST", "cik": "0000000001", "annual_ratios": {}}
    monkeypatch.setattr(api, "_dashboard_payload", lambda: {"rows": [base]})
    monkeypatch.setattr(api, "_latest_quarterly_filing", lambda cik: {
        "form": "10-Q", "filed": "2026-08-01", "period": "2026-06-30",
        "accession": "0000000001-26-000003", "document": "testq.htm",
        "url": "https://www.sec.gov/Archives/edgar/data/1/testq-index.htm",
    })

    response = TestClient(api.app).get("/company/TEST/dashboard?assume_absent_zero=false")

    assert response.status_code == 200
    assert response.json()["latest_quarterly_filing"]["form"] == "10-Q"
