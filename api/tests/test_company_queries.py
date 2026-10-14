import pytest
import json
from pathlib import Path
import subprocess
from fastapi.testclient import TestClient

from screener import api, company_query_api
from screener.company_queries import CompanyQuery, CursorCodec, InvalidQuery, QueryUnavailable, RestartQuery


SECRET = b"query-test-key-persist-across-restarts-123456"


def test_cursor_survives_recreation_and_rejects_tampering_and_bad_config():
    token = CursorCodec(SECRET).encode({"release": 2, "values": [None, "A", 1]})
    assert CursorCodec(SECRET).decode(token) == {"release": 2, "values": [None, "A", 1]}
    with pytest.raises(InvalidQuery):
        CursorCodec(SECRET).decode(token[:-2] + "AA")
    with pytest.raises(QueryUnavailable):
        CursorCodec(b"").encode({})


def test_cursor_codec_enforces_same_token_bound_on_encode_and_decode():
    codec = CursorCodec(SECRET)
    with pytest.raises(InvalidQuery):
        codec.encode({"text": "漢" * 2000})
    with pytest.raises(InvalidQuery):
        codec.decode("A" * 8193)


@pytest.mark.parametrize("values", [{"max_pe": float("nan")}, {"limit": 301}, {"min_met": 7},
                                  {"tracked_only": True}, {"workspace_revision": 1}, {"lens": "bad"},
                                  {"sorts": (("price", 1), ("price", -1))}])
def test_invalid_or_private_query_is_explicit(values):
    with pytest.raises(InvalidQuery):
        CompanyQuery(**values)


def test_query_fingerprint_ignores_selection_order_and_search_case():
    assert CompanyQuery(q=" A ", sectors=("b", "a")).fingerprint() == CompanyQuery(q="a", sectors=("a", "b")).fingerprint()
    assert CompanyQuery(q="a" * 1000, sorts=(("n_pass", 1),)).q == "a" * 1000


def test_public_router_validates_shapes_and_maps_domain_errors(monkeypatch):
    seen = []

    class Repository:
        def list(self, **kwargs):
            seen.append(kwargs)
            return {"rows": [], "next_cursor": None}

        def detail(self, **kwargs):
            if kwargs.get("release_id") == 99:
                raise RestartQuery("missing")
            return {"security_id": 1, "row": {"ticker": "A"}}

        def status(self):
            raise QueryUnavailable("projection missing")

    monkeypatch.setattr(company_query_api, "query_repository", lambda: Repository())
    client = TestClient(api.app)
    response = client.get("/api/v1/companies", params=[("sector", "Energy"), ("sector", "Industrials"),
                                                        ("sort", "returns:1"), ("sort", "pe:-1"), ("limit", 2)])
    assert response.status_code == 200, response.text
    assert seen[0]["query"].sectors == ("Energy", "Industrials")
    assert seen[0]["query"].sorts == (("returns", 1), ("pe", -1))
    assert client.get("/api/v1/companies?max_pe=NaN").status_code == 422
    assert client.get("/api/v1/companies?tracked_only=true").status_code == 400
    assert client.get("/api/v1/companies?unknown=1").status_code == 422
    assert client.get("/api/v1/companies/by-ticker/A?release=99").status_code == 409
    assert client.get("/api/v1/companies/by-ticker/A").json()["row"]["ticker"] == "A"
    assert client.get("/api/v1/data-status").status_code == 503


def test_mounted_query_contract_preserves_private_and_legacy_routes():
    client = TestClient(api.app)
    contract = client.get("/api/v1/capabilities")
    assert contract.status_code == 200
    assert contract.json()["api_contract_major"] == 1
    assert contract.json()["source_validity_supported"]
    assert contract.json()["source_validity_scope"] == "CAPTURED_DEPENDENCIES_ONLY"
    assert not contract.json()["numeric_invalidation_assessed"]
    assert not contract.json()["newest_filings_assessed"]
    assert contract.json()["official_financial_readiness"] == "NOT_ASSESSED"
    assert not contract.json()["private_queries_supported"]
    assert client.get("/research/workspaces/default").status_code == 401
    routes = set(client.get("/openapi.json").json()["paths"])
    assert {"/companies/{ticker}", "/company/{ticker}/dashboard", "/dashboard.json",
            "/screen/enterprising/{ticker}", "/fundamentals/{ticker}"} <= routes


def test_sql_oracle_executes_actual_app_callbacks_on_small_payload(tmp_path):
    root = Path(__file__).parents[2]
    rows = [{"ticker": ticker, "name": ticker, "criteria": [{"n": 1, "status": "PASS", "value": pe}],
             "n_pass": 1, "annual_eps": {"2025": 1}, "graham_profile": "OPERATING"} for ticker, pe in (("A", 2), ("B", 8))]
    baseline, cases = tmp_path / "baseline.json", tmp_path / "cases.json"
    baseline.write_text(json.dumps({"engine_version": 181, "rows": rows}), encoding="utf-8")
    cases.write_text(json.dumps([{"name": "pe", "query": {"max_pe": 5}}]), encoding="utf-8")
    result = subprocess.run(["node", str(root / "local/query-sql-parity-2026-10-04/oracle.mjs"),
                             str(baseline), str(cases), str(root / "web/src/App.jsx")], capture_output=True, text=True, check=True)
    oracle = json.loads(result.stdout)
    assert oracle["rows"] == 2 and oracle["cases"][0]["tickers"] == ["A"]
    assert oracle["cases"][0]["counts"]["met_1"] == 1
    assert oracle["facets"]["sectors"] == [["Unclassified", 2]]
