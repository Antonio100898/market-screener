"""Synthetic sealed graphs prove read navigation, not native financial truth."""
import json
from pathlib import Path
import pytest
from sqlalchemy import event, text
from fastapi.testclient import TestClient
from screener import api, company_query_api
from screener.company_queries import CompanyQueryRepository, InvalidQuery, HistoryNotFound
from screener.figure_history import FigureHistoryRepository, history_name_digest
from tests.test_figure_history import validate_changed
from test_publication_storage import storage, NOW, pytestmark
from test_publication_exact_binding_storage import setup, register, publish, record
from test_activation_history_storage import change
SECRET=b"catalog-real-immutable-key-123456789012345"
def reader(parts):return CompanyQueryRepository(parts[0],cursor_secret=SECRET)
def url(parts):return f"/api/v1/companies/{parts[2].security_id}/figure-history"

def test_real_catalog_selected_history_and_pin_support_http(storage,monkeypatch):
    parts=setup(storage);first=publish(parts,register(parts));second,_,_=change(parts,"90",value="90.0000",parent=first.release_id);third,_,_=change(parts,"95",value="95.0000",parent=second.release_id)
    repo=reader(parts);monkeypatch.setattr(company_query_api,"query_repository",lambda:repo);client=TestClient(api.app)
    response=client.get(url(parts)+"/figures",params={"limit":1})
    assert response.status_code==200,response.text
    assert response.headers["cache-control"]=="no-store"
    page=response.json();assert page["release"]["release_id"]==third.release_id
    item=page["rows"][0];handle=item["selector_handle"];assert len(handle)<1000
    assert item["endpoint"]["selection"]["value_decimal_text"]=="95.0000"
    fourth,_,_=change(parts,"96",value="96.0000",parent=third.release_id)
    for operation in ("timeline","activations"):
        params={"handle":handle,"operation":operation,"limit":1};result=client.get(url(parts)+"/selected",params=params)
        assert result.status_code==200,result.text
        history=result.json();assert history["release"]["release_id"]==third.release_id
        ids=[r["release_id"] for r in history["rows"]]
        while history["next_cursor"]:
            history=client.get(url(parts)+"/selected",params=params|{"cursor":history["next_cursor"]}).json()
            assert history["release"]["release_id"]==third.release_id;ids.extend(r["release_id"] for r in history["rows"])
        assert ids==[third.release_id,second.release_id,first.release_id]
    inputs=client.get(url(parts)+"/dependencies",params={"handle":item["endpoint"]["supporting_records"]["inputs"]}).json()
    assert inputs["rows"][0]["observation"]["value_decimal_text"]=="95.0000"
    source=client.get(url(parts)+"/dependencies",params={"handle":item["endpoint"]["supporting_records"]["sources"]}).json()["rows"][0]
    journal=client.get(url(parts)+"/source-events",params={"source_item_id":source["source_item_id"],"release":third.release_id}).json()
    assert journal["binding"]["captured_occurrence_id"]==parts[3].sources[0].occurrence_id
    assert repo.figure_catalog(security_id=parts[2].security_id)["release"]["release_id"]==fourth.release_id
    assert client.get(url(parts)+"/selected",params={"handle":handle+"x"}).status_code==400
    assert client.get(url(parts)+"/selected",params={"handle":item["endpoint"]["supporting_records"]["inputs"]}).status_code==400
    assert client.get(url(parts)+"/selected",params={"handle":handle,"metric_path":"replacement"}).status_code==422
    assert client.get(url(parts)+"/selected",params={"handle":handle,"operation":"not-an-operation"}).status_code==400
    assert client.get(url(parts)+"/selected",params={"handle":handle,"limit":301}).status_code==422
    for changes in ({"security":999},{"pin":first.release_id},{"bundle":parts[4]},{"view":"ASSUMED"},{"root":"f"*64}):
        forged=repo.cursors.decode(handle)|changes
        response=client.get(url(parts)+"/selected",params={"handle":repo.cursors.encode(forged)})
        assert response.status_code in (400,404),response.text
        assert response.headers["cache-control"]=="no-store"


def test_real_catalog301_batch_coverage_seek_and_long_multibyte_selector(storage,monkeypatch):
    parts=setup(storage);original=parts[3].selections[0];metric="資産"*20000
    nodes=[original.model_copy(update={"node_key":f"output-{i:04}","metric_path":metric if i==0 else f"balance_sheet.fixture_{i}"}) for i in range(301)]
    intermediate=original.model_copy(update={"node_key":"intermediate","purpose":"INTERMEDIATE"})
    graph=validate_changed(parts[3],contract_revision=2,selections=(*nodes,intermediate),coverage=(parts[3].coverage[0].model_copy(update={"metric_paths":tuple(n.metric_path for n in nodes)}),),pipeline_run_key="catalog301")
    owner=FigureHistoryRepository(parts[0],clock=lambda:NOW);identifier=owner.store(parts[1],request_key="catalog301",bundle=graph)
    release=publish(parts,register(parts,exact_bundle_id=identifier,expected_exact_run_key=graph.pipeline_run_key))
    repo=reader(parts);monkeypatch.setattr(company_query_api,"query_repository",lambda:repo);client=TestClient(api.app)
    statements=[]
    def capture(c,cur,s,p,ctx,many):statements.append(s)
    event.listen(parts[0],"before_cursor_execute",capture)
    page=client.get(url(parts)+"/figures",params={"limit":300}).json();count=len(statements)
    event.remove(parts[0],"before_cursor_execute",capture)
    assert len(page["rows"])==300 and page["next_cursor"] and count<=10
    assert all(item["coverage"]["assessment"]=="FULL" for item in page["rows"])
    following=repo.figure_catalog(security_id=parts[2].security_id,limit=300,cursor=page["next_cursor"])
    assert len(following["rows"])==1 and following["next_cursor"] is None
    paths=[r["selector"]["metric_path"] for r in page["rows"]+following["rows"]];assert len(set(paths))==301 and original.metric_path not in paths
    selected=page["rows"][0];assert selected["selector"]["metric_path"]==metric
    result=client.get(url(parts)+"/selected",params={"handle":selected["selector_handle"]})
    assert result.status_code==200,result.text
    assert result.json()["selector"]["metric_path"]==metric
    assert len(str(result.request.url).encode())<1500
    for values in ({"limit":1,"view":"STRICT"},{"limit":300,"view":"ASSUMED"}):
        with pytest.raises(InvalidQuery):
            repo.figure_catalog(security_id=parts[2].security_id,cursor=page["next_cursor"],**values)
    forged=repo.cursors.decode(selected["selector_handle"]);forged["root"]=history_name_digest("intermediate")
    with pytest.raises(HistoryNotFound):repo.figure_selected(security_id=parts[2].security_id,handle=repo.cursors.encode(forged))
    with parts[0].connect() as c:
        plan=c.scalar(text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT node_key FROM figure_selection WHERE bundle_id=:bundle AND view='STRICT' AND COALESCE(record->>'purpose','OUTPUT')='OUTPUT' ORDER BY node_key COLLATE \"C\" LIMIT 301"),{"bundle":identifier})
    proof=dict(label="synthetic302-node-301OUTPUT-longmultibyte-coverage-batch;not-financial-acceptance",catalog_statement_count=count,response_bytes=len(json.dumps(page,ensure_ascii=False).encode()),selector_handle_bytes=len(selected["selector_handle"]),selected_request_bytes=len(str(result.request.url).encode()),plan=plan)
    Path("../local/figure-history-ui-catalog-plan-2026-10-04.json").write_text(json.dumps(proof,indent=2,default=str))


def test_legacy_gap_unknown_period_conservative_and_unbound_origin(storage):
    parts=setup(storage);node=parts[3].selections[0]
    from screener.figure_history import Period
    unknown=node.model_copy(update={"period":Period(kind="UNKNOWN",origin="UNKNOWN"),"view":"CONSERVATIVE","state":"NOT_ASSESSED","value_decimal_text":None,"observation_key":None,"reason":"fixture-no-native-binding"})
    coverage=parts[3].coverage[0].model_copy(update={"view":"CONSERVATIVE","assessment":"NOT_ASSESSED","reason":"fixture-no-native-binding"})
    graph=validate_changed(parts[3],selections=(unknown,),coverage=(coverage,),pipeline_run_key="unknown")
    identifier=FigureHistoryRepository(parts[0],clock=lambda:NOW).store(parts[1],request_key="unknown",bundle=graph)
    release=publish(parts,register(parts,exact_bundle_id=identifier,expected_exact_run_key=graph.pipeline_run_key));repo=reader(parts)
    row=repo.figure_catalog(security_id=parts[2].security_id,view="CONSERVATIVE")["rows"][0]
    assert row["selector"]["period"]["kind"]=="UNKNOWN" and row["endpoint"]["selection"]["value_decimal_text"] is None
    actual=repo.figure_selected(security_id=parts[2].security_id,handle=row["selector_handle"])
    assert actual["selector"]["period"]["kind"]=="UNKNOWN"
    assert repo.figure_support(security_id=parts[2].security_id,handle=row["endpoint"]["supporting_records"]["inputs"])["rows"]==[]
    assert repo.figure_catalog(security_id=parts[2].security_id,view="STRICT")["rows"]==[]
    # Published legacy member uses the existing explicit NULL bundle contract.
    legacy=register(parts,key="legacy",parent=release.release_id,exact_bundle_id=None,expected_exact_run_key=None)
    later=publish(parts,legacy,key="legacy-release",parent=release.release_id)
    page=repo.figure_catalog(security_id=parts[2].security_id)
    assert page["release"]["release_id"]==later.release_id and page["catalog_status"]=="legacy-no-bundle" and page["rows"]==[]


def test_actual_staged_foreign_and_unpublished_catalog_admission(storage):
    parts=setup(storage);repo=reader(parts)
    from screener.company_queries import QueryUnavailable, RestartQuery
    with pytest.raises(QueryUnavailable):repo.figure_catalog(security_id=parts[2].security_id)
    # The exact companion already exists and is sealed; publication is still required.
    candidate=register(parts)
    with pytest.raises(RestartQuery):repo.figure_catalog(security_id=parts[2].security_id,release_id=999999)
    release=publish(parts,candidate)
    page=repo.figure_catalog(security_id=parts[2].security_id)
    alternate=FigureHistoryRepository(parts[0],clock=lambda:NOW).store(parts[1],request_key="not-published",bundle=parts[3].model_copy(update={"pipeline_run_key":"other-staged-graph"}))
    forged=repo.cursors.decode(page["rows"][0]["selector_handle"])|{"bundle":alternate}
    with pytest.raises(HistoryNotFound):repo.figure_selected(security_id=parts[2].security_id,handle=repo.cursors.encode(forged))
    with pytest.raises(HistoryNotFound):repo.figure_catalog(security_id=999999,release_id=release.release_id)
