from contextlib import contextmanager
from datetime import date
import pytest
from fastapi.testclient import TestClient
from screener import api, company_query_api
from screener.company_queries import CompanyQueryRepository, InvalidQuery, HistoryNotFound
from screener.figure_history import FigureHistoryRepository
SECRET=b"catalog-unit-persistent-secret-1234567890"

@pytest.mark.parametrize("path,params",[("figures",{"limit":301}),("selected",{}),("selected",{"handle":"x","metric_path":"forbidden"}),("figures",{"unknown":"x"})])
def test_catalog_http_validation_precedes_store(monkeypatch,path,params):
    monkeypatch.setattr(company_query_api,"query_repository",lambda:pytest.fail("store accessed"))
    response=TestClient(api.app).get("/api/v1/companies/1/figure-history/"+path,params=params)
    assert response.status_code==422 and response.headers["cache-control"]=="no-store"

@pytest.mark.parametrize("path,params,method",[("figures",{"view":"CONSERVATIVE","release":7},"figure_catalog"),("selected",{"handle":"signed","operation":"activations","cursor":"next"},"figure_selected")])
def test_catalog_http_delegates_bounded_shapes(monkeypatch,path,params,method):
    calls=[]
    class Repo: pass
    repo=Repo();setattr(repo,method,lambda **kw: calls.append(kw) or {"success":True})
    monkeypatch.setattr(company_query_api,"query_repository",lambda:repo)
    response=TestClient(api.app).get("/api/v1/companies/1/figure-history/"+path,params=params)
    assert response.status_code==200 and response.headers["cache-control"]=="no-store"
    assert calls[0]["security_id"]==1 and calls[0]["limit"]==300

@pytest.mark.parametrize("changes",[{"domain":"figure-history-record"},{"revision":True},{"security":2},{"view":"AUTO"},{"root":"x"},{"bundle":True},{"extra":"x"}])
def test_selector_identity_rejects_before_sql(changes):
    repo=CompanyQueryRepository(None,cursor_secret=SECRET)
    value=dict(domain="figure-history-selector",revision=1,pin=1,security=1,bundle=1,root="a"*64,view="STRICT")|changes
    with pytest.raises(InvalidQuery):repo.figure_selected(security_id=1,handle=repo.cursors.encode(value))

@pytest.mark.parametrize("view",["STRICT","ASSUMED","CONSERVATIVE"])
def test_selector_constructs_actual_period_in_same_transaction(monkeypatch,view):
    repo=CompanyQueryRepository(None,cursor_secret=SECRET);connection=object();seen=[]
    @contextmanager
    def snapshot():
        seen.append("begin");yield connection;seen.append("end")
    monkeypatch.setattr(repo,"_read_snapshot",snapshot);monkeypatch.setattr(repo,"_history_timeout",lambda c:None)
    node=dict(view=view,metric_path="名"*40000)
    monkeypatch.setattr(repo,"_history_output_admission",lambda c,**kw: node)
    monkeypatch.setattr("screener.company_queries.history_selection_record",lambda n:dict(period=dict(kind="DURATION",start="2024-10-01",end="2025-09-30")))
    def read(c,**kw):
        assert c is connection;seen.append("read");return kw
    monkeypatch.setattr(repo,"_figure_history_on_connection",read)
    handle=repo.cursors.encode(dict(domain="figure-history-selector",revision=1,pin=1,security=1,bundle=1,root="a"*64,view=view))
    assert len(handle)<1000
    result=repo.figure_selected(security_id=1,handle=handle,operation="activations")
    assert seen==["begin","read","end"] and result["query"].period_start==date(2024,10,1)
    assert result["query"].metric_path==node["metric_path"] and result["query"].view==view

@pytest.mark.parametrize("error,status",[(InvalidQuery("bad"),400),(HistoryNotFound("private"),404)])
def test_catalog_errors_are_no_store(monkeypatch,error,status):
    class Repo:
        def figure_catalog(self,**kw):raise error
    monkeypatch.setattr(company_query_api,"query_repository",lambda:Repo())
    response=TestClient(api.app).get("/api/v1/companies/1/figure-history/figures")
    assert response.status_code==status and response.headers["cache-control"]=="no-store"
    assert "private" not in response.text

@pytest.mark.parametrize("suffix",["figures","selected","dependencies","source-events","activations",""])
@pytest.mark.parametrize("method",["POST","HEAD","DELETE"])
def test_all_history_method_rejections_are_uncached(monkeypatch,suffix,method):
    monkeypatch.setattr(company_query_api,"query_repository",lambda:pytest.fail("method rejection read repository"))
    response=TestClient(api.app).request(method,"/api/v1/companies/1/figure-history"+("/"+suffix if suffix else ""))
    assert response.status_code==405 and response.headers["cache-control"]=="no-store"
    assert "GET" in response.headers["allow"]
