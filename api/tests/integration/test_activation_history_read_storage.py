"""Published synthetic graphs: read mechanics, not financial acceptance."""
import json
import subprocess
import sys
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from screener import api, company_query_api
from screener.company_queries import CompanyQueryRepository, HistoryQuery, InvalidQuery, RestartQuery, HistoryNotFound, QueryUnavailable
from test_publication_storage import storage, pytestmark
from test_publication_exact_binding_storage import setup, register, publish, record
from test_activation_history_storage import change
from test_publication_storage import NOW
from screener.figure_history import FigureHistoryRepository, CalculationDependency
from tests.test_figure_history import validate_changed
from test_figure_history_contract2_storage import prepared_bundle, objects

SECRET=b'history-read-test-secret-persist-process-123456'
SELECTOR=dict(metric_path='balance_sheet.total_assets',period_kind='INSTANT',period_end=date(2025,12,31))


def reader(parts): return CompanyQueryRepository(parts[0],cursor_secret=SECRET)
def timeline(parts,**kwargs): return reader(parts).figure_history(HistoryQuery(**SELECTOR,limit=kwargs.pop('limit',300)),security_id=parts[2].security_id,**kwargs)


def test_actual_publication_timeline_lookahead_live_pin_and_support(storage,monkeypatch):
    parts=setup(storage); first=publish(parts,register(parts))
    second,_,_=change(parts,'90',value='90.0000',parent=first.release_id)
    third,_,_=change(parts,'95',value='95.0000',parent=second.release_id)
    repository=reader(parts)
    monkeypatch.setattr(company_query_api,'query_repository',lambda:repository)
    client=TestClient(api.app)
    params=dict(SELECTOR,period_end='2025-12-31',limit=1)
    url=f'/api/v1/companies/{parts[2].security_id}/figure-history'
    response=client.get(url,params=params)
    assert response.status_code==200,response.text
    assert response.headers['cache-control']=='no-store'
    page=response.json(); assert page['release']['release_id']==third.release_id
    assert page['rows'][0]['previous']['release_id']==second.release_id
    assert page['rows'][0]['previous']['selection']['value_decimal_text']=='90.0000'
    assert page['rows'][0]['replacement']['selection']['value_decimal_text']=='95.0000'
    assert page['rows'][0]['delta']==dict(status='NOT_COMPUTED',value_decimal_text=None)
    fourth,_,_=change(parts,'96',value='96.0000',parent=third.release_id)
    collected=[third.release_id]
    while page['next_cursor']:
        page=client.get(url,params=dict(params,cursor=page['next_cursor'])).json()
        assert page['release']['release_id']==third.release_id
        collected.extend(row['release_id'] for row in page['rows'])
    assert collected==[third.release_id,second.release_id,first.release_id]
    assert timeline(parts)['release']['release_id']==fourth.release_id
    handle=page['endpoint']['supporting_records']['inputs']
    direct=client.get(url+'/dependencies',params=dict(handle=handle,limit=1))
    assert direct.status_code==200,direct.text
    item=direct.json()['rows'][0]
    assert item['observation']['value_decimal_text']=='95.0000' and item['role']=='reported-occurrence'
    assert item['observation']['reference']['filing_identity']['status']=='UNAVAILABLE'
    for kind,child in item['supporting_records'].items():
        proof=client.get(url+'/dependencies',params=dict(handle=child,limit=1))
        assert proof.status_code==200,proof.text
        assert proof.json()['endpoint_release_id']==third.release_id
    assert client.get(url+'/dependencies',params=dict(handle=handle+'x')).status_code==400
    assert client.get(url,params=dict(params,view='ASSUMED',cursor=response.json()['next_cursor'])).status_code==400
    assert client.get(url,params=dict(params,release=999999)).status_code==409
    assert client.get('/api/v1/companies/999999/figure-history',params=params).status_code==404
    assert client.get(url,params=dict(params,limit=301)).status_code==422


@pytest.mark.parametrize('value',['-0.0000','1E-20000','1E+200000','9007199254740993.1234567890123456789'])
def test_exact_strings_are_not_float_display_differences(storage,value):
    parts=setup(storage); first=publish(parts,register(parts))
    change(parts,'exact',value=value,parent=first.release_id)
    result=timeline(parts)
    assert result['endpoint']['selection']['value_decimal_text']==value
    assert result['rows'][0]['replacement']['selection']['value_decimal_text']==value
    assert result['rows'][0]['context_assessment']=='NOT_ASSESSED'


def test_source_journal_check_fence_and_graph_capture_are_distinct(storage):
    parts=setup(storage); release=publish(parts,register(parts)); repository=reader(parts)
    record(storage,'unchanged1',1); record(storage,'unchanged2',1)
    arguments=dict(security_id=parts[2].security_id,source_item_id=parts[3].sources[0].source_item_id,limit=1)
    first=repository.figure_source_events(**arguments)
    assert first['next_cursor'] and first['rows'][0]['capture_is_acceptance'] is False
    assert first['binding']['captured_occurrence_id']==parts[3].sources[0].occurrence_id
    assert first['binding']['accepted_occurrence_id']==parts[3].sources[0].occurrence_id
    assert first['occurrence_fence']>first['binding']['accepted_occurrence_id']
    record(storage,'unrelated',1,source_key='unrelated')
    assert repository.figure_source_events(**arguments,cursor=first['next_cursor'])['rows']
    record(storage,'same-current-later',1)
    with pytest.raises(RestartQuery): repository.figure_source_events(**arguments,cursor=first['next_cursor'])
    with pytest.raises(HistoryNotFound): repository.figure_source_events(**dict(arguments,source_item_id=999999))
    assert timeline(parts)['release']['release_id']==release.release_id


def test_fresh_process_handles_replay_and_constant_statement_batch(storage):
    parts=setup(storage); publish(parts,register(parts))
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany): statements.append(statement)
    event.listen(parts[0],'before_cursor_execute',capture)
    result=timeline(parts); handle=result['endpoint']['supporting_records']['inputs']
    assert len(statements)<=12
    statements.clear(); expected=reader(parts).figure_support(security_id=parts[2].security_id,handle=handle,limit=300)
    assert len(statements)<=12
    event.remove(parts[0],'before_cursor_execute',capture)
    process=subprocess.run([sys.executable,'-c','''
import json,sys
from sqlalchemy import create_engine
from screener.storage_config import StorageSettings
from screener.company_queries import CompanyQueryRepository
r=CompanyQueryRepository(create_engine(StorageSettings.from_env().database_url),cursor_secret=bytes.fromhex(sys.argv[1]))
print(json.dumps(r.figure_support(security_id=int(sys.argv[2]),handle=sys.argv[3]),default=str))
''',SECRET.hex(),str(parts[2].security_id),handle],capture_output=True,text=True,check=True,timeout=15)
    assert json.loads(process.stdout)==json.loads(json.dumps(expected,default=str))
    print('Fresh OS process: signed graph handle and exact direct records replayed; bounded SQL statements <=12.')


def test_sparse300_ancestor_window_preserves_lookahead_and_excludes_branch(storage):
    parts=setup(storage); first=publish(parts,register(parts)); parent=first.release_id
    # Labelled historical fixture: valid literal sealed parent/member rows, no hook markers.
    with parts[0].begin() as c:
        for i in range(304):
            new=c.scalar(text('INSERT INTO data_release(parent_release_id,request_key,publishing_job_id) VALUES(:parent,:key,:job) RETURNING release_id'),dict(parent=parent,key=f'legacy-{i}',job=parts[1].job_id))
            c.execute(text('INSERT INTO release_company(release_id,security_id,snapshot_id,exact_bundle_id) SELECT :new,security_id,snapshot_id,exact_bundle_id FROM release_company WHERE release_id=:parent'),dict(new=new,parent=parent))
            c.execute(text('UPDATE data_release SET published_at=:now WHERE release_id=:new'),dict(now=NOW,new=new))
            c.execute(text('SELECT release_id FROM active_data_release WHERE singleton_id=1 FOR UPDATE'))
            c.execute(text('UPDATE active_data_release SET release_id=:new WHERE singleton_id=1'),dict(new=new)); parent=new
        branch=c.scalar(text('INSERT INTO data_release(parent_release_id,request_key,publishing_job_id) VALUES(:parent,\'branch\',:job) RETURNING release_id'),dict(parent=first.release_id,job=parts[1].job_id))
        c.execute(text('UPDATE data_release SET published_at=:now WHERE release_id=:branch'),dict(now=NOW,branch=branch))
    page=timeline(parts,limit=1)
    assert page['scanned_releases']==300 and page['rows']==[] and page['next_cursor']
    page=timeline(parts,limit=1,cursor=page['next_cursor'])
    assert page['scanned_releases']==5 and [r['release_id'] for r in page['rows']]==[first.release_id]
    assert page['traversal_complete'] and page['next_cursor'] is None
    repository=reader(parts); query=HistoryQuery(**SELECTOR,limit=300)
    page=repository.figure_history(query,security_id=parts[2].security_id,activations=True)
    assert len(page['rows'])==300 and all(row['hook_revision'] is None for row in page['rows'])
    assert branch not in [r['release_id'] for r in page['rows']]
    remaining=repository.figure_history(query,security_id=parts[2].security_id,activations=True,cursor=page['next_cursor'])
    assert len(remaining['rows'])==5 and remaining['rows'][-1]['hook_revision']==1
    print('305 actual ancestors; first sparse300 window empty with continuation; 5 remaining; unrelated sealed branch excluded.')


def test_repeatable_read_pin_survives_publication_between_statements(storage,monkeypatch):
    parts=setup(storage); first=publish(parts,register(parts)); original=CompanyQueryRepository._history_window
    entered=Event(); resume=Event()
    def blocked(connection,start):
        assert connection.scalar(text('SHOW transaction_isolation'))=='repeatable read'
        entered.set(); assert resume.wait(8)
        return original(connection,start)
    monkeypatch.setattr(CompanyQueryRepository,'_history_window',staticmethod(blocked))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(timeline,parts)
        assert entered.wait(5)
        next_release,_,_=change(parts,'concurrent',value='90.0000',parent=first.release_id)
        resume.set(); page=future.result(8)
    assert page['release']['release_id']==first.release_id and len(page['rows'])==1
    monkeypatch.setattr(CompanyQueryRepository,'_history_window',staticmethod(original))
    assert timeline(parts)['release']['release_id']==next_release.release_id


def test_source_snapshot_ignores_uncommitted_and_mid_read_checks(storage,monkeypatch):
    parts=setup(storage); publish(parts,register(parts)); record(storage,'check-before',1)
    repository=reader(parts); args=dict(security_id=parts[2].security_id,source_item_id=parts[3].sources[0].source_item_id,limit=1)
    initial=repository.figure_source_events(**args)
    entered=Event(); resume=Event(); original=FigureHistoryRepository.read_journal_on_connection
    def blocked(connection,item_id,**kwargs): entered.set(); assert resume.wait(8); return original(connection,item_id,**kwargs)
    monkeypatch.setattr(FigureHistoryRepository,'read_journal_on_connection',staticmethod(blocked))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(repository.figure_source_events,**args)
        assert entered.wait(5); record(storage,'check-during',1); resume.set(); page=future.result(8)
    assert page['occurrence_fence']==initial['occurrence_fence'] and page['rows']==initial['rows']
    monkeypatch.setattr(FigureHistoryRepository,'read_journal_on_connection',staticmethod(original))
    with pytest.raises(RestartQuery): repository.figure_source_events(**args,cursor=page['next_cursor'])


@pytest.mark.parametrize('commit',[False,True])
def test_actual_source_owner_transaction_wait_keeps_uncommitted_journal_hidden(storage,monkeypatch,commit):
    from screener.source_observations import SourceObservationRepository
    parts=setup(storage); publish(parts,register(parts)); record(storage,'visible',1)
    repository=reader(parts); args=dict(security_id=parts[2].security_id,source_item_id=parts[3].sources[0].source_item_id,limit=1)
    first=repository.figure_source_events(**args)
    entered=Event(); resume=Event(); original=SourceObservationRepository._record_on_connection
    def pause(self,*arguments,**kwargs):
        result=original(self,*arguments,**kwargs); entered.set(); assert resume.wait(8)
        if not commit: raise RuntimeError('rollback after full actual source owner writes')
        return result
    monkeypatch.setattr(SourceObservationRepository,'_record_on_connection',pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(record,storage,'uncommitted',2)
        assert entered.wait(5)
        with parts[0].connect() as c:
            assert c.scalar(text("SELECT count(*) FROM pg_locks l JOIN pg_class t ON t.oid=l.relation WHERE t.relname='source_item_current' AND l.granted AND l.mode='RowExclusiveLock'"))>0
        assert repository.figure_source_events(**args)['rows']==first['rows']
        assert repository.figure_source_events(**args,cursor=first['next_cursor'])['rows']
        resume.set()
        if commit: future.result(8)
        else:
            with pytest.raises(RuntimeError,match='rollback after full'): future.result(8)
    if commit:
        with pytest.raises(RestartQuery): repository.figure_source_events(**args,cursor=first['next_cursor'])
    else:
        assert repository.figure_source_events(**args)['rows']==first['rows']
        assert repository.figure_source_events(**args,cursor=first['next_cursor'])['rows']


def test_recurrence_no_value_event_preserves_capture_and_each_release_acceptance(storage):
    parts=setup(storage); first=publish(parts,register(parts)); record(storage,'B',2); again=record(storage,'A-again',1)
    second=publish(parts,register(parts,key='A-current',parent=first.release_id),key='A-second-release',parent=first.release_id)
    assert [row['release_id'] for row in timeline(parts)['rows']]==[first.release_id]
    repository=reader(parts); args=dict(security_id=parts[2].security_id,source_item_id=parts[3].sources[0].source_item_id)
    old=repository.figure_source_events(**args,release_id=first.release_id); new=repository.figure_source_events(**args,release_id=second.release_id)
    assert old['binding']['accepted_occurrence_id']==old['binding']['captured_occurrence_id']
    assert new['binding']['accepted_occurrence_id']==again.delivery.occurrence_id>new['binding']['captured_occurrence_id']
    assert new['rows'][0]['source_observation_id']==new['rows'][-1]['source_observation_id']
    assert new['rows'][1]['source_observation_id']!=new['rows'][0]['source_observation_id']


@pytest.mark.parametrize('state',['removed','pending','unavailable'])
def test_source_only_state_does_not_claim_adjustment_or_rewrite_release_values(storage,state):
    parts=setup(storage); release=publish(parts,register(parts)); record(storage,'later-'+state,2,state=state)
    page=timeline(parts); assert page['endpoint']['selection']['value_decimal_text']=='100.0000'
    assert page['release']['release_id']==release.release_id and len(page['rows'])==1
    journal=reader(parts).figure_source_events(security_id=parts[2].security_id,source_item_id=parts[3].sources[0].source_item_id)
    assert journal['rows'][0]['state']==state and journal['affected_figures_assessed'] is False
    assert journal['cause']==dict(status='UNKNOWN') and journal['financial_readiness']=='NOT_ASSESSED'


def test_legacy_member_and_staged_catalog_are_explicit_gaps(storage):
    parts=setup(storage); repository=reader(parts)
    with pytest.raises(QueryUnavailable): timeline(parts)
    release=publish(parts,register(parts,exact_bundle_id=None,expected_exact_run_key=None))
    page=timeline(parts)
    assert page['rows']==[] and page['coverage']['assessment']=='NOT_ASSESSED'
    assert page['endpoint']['reason']=='legacy-no-bundle' and page['endpoint']['supporting_records'] is None
    events=repository.figure_source_events(security_id=parts[2].security_id,source_item_id=parts[3].sources[0].source_item_id)
    assert events['binding']['captured_occurrence_id'] is None and events['binding']['accepted_occurrence_id']
    forged=dict(domain='figure-history-record',revision=1,kind='inputs',pin=release.release_id,release=release.release_id,
        security=parts[2].security_id,bundle=parts[4],root='0'*64,node='0'*64,target='node',observation=None,parent=None,ordinal=None)
    with pytest.raises(HistoryNotFound): repository.figure_support(security_id=parts[2].security_id,handle=repository.cursors.encode(forged))


@pytest.mark.parametrize('kind',['UNKNOWN','DURATION'])
def test_actual_period_selector_does_not_match_same_amount_other_period(storage,kind):
    from screener.figure_history import Period
    from tests.test_activation_history import reported,partial_period
    parts=setup(storage); first=publish(parts,register(parts))
    period=Period(kind='UNKNOWN',origin='UNKNOWN') if kind=='UNKNOWN' else Period(kind='DURATION',start=date(2025,1,1),end=date(2025,12,31),origin='REPORTED')
    graph=partial_period(parts[3],period)
    graph=reported(graph,reference=graph.observations[0].reference.model_copy(update=dict(extraction_revision='different-period')))
    second,_,_=change(parts,'period-change',parent=first.release_id,bundle=graph)
    old=timeline(parts); assert old['endpoint']['reason']=='output-unavailable'
    assert old['rows'][0]['previous']['selection']['period']['kind']=='INSTANT'
    assert old['rows'][0]['replacement']['selection'] is None
    new=reader(parts).figure_history(HistoryQuery(metric_path=SELECTOR['metric_path'],period_kind=kind,period_start=period.start,period_end=period.end),security_id=parts[2].security_id)
    assert len(new['rows'])==1 and new['rows'][0]['release_id']==second.release_id
    assert new['rows'][0]['previous']['selection'] is None
    assert new['endpoint']['selection']['period']['kind']==kind
    assert new['rows'][0]['delta']['value_decimal_text'] is None


def test_read_timeout_returns503_no_store_without_database_error_details(storage,monkeypatch):
    parts=setup(storage); publish(parts,register(parts)); repository=reader(parts)
    monkeypatch.setattr(company_query_api,'query_repository',lambda:repository)
    def stalled(connection,start): connection.execute(text('SELECT pg_sleep(6)'))
    monkeypatch.setattr(CompanyQueryRepository,'_history_window',staticmethod(stalled))
    response=TestClient(api.app).get(f'/api/v1/companies/{parts[2].security_id}/figure-history',params=dict(SELECTOR,period_end='2025-12-31'))
    assert response.status_code==503 and response.headers['cache-control']=='no-store'
    assert response.json()['detail']['code']=='QUERY_STORE_UNAVAILABLE'
    assert 'pg_sleep' not in response.text and 'psycopg' not in response.text


def test_no_association_is_inferred_from_retained_catalog_and_amount(storage):
    parts=setup(storage)
    node=parts[3].selections[0].model_copy(update=dict(state='NOT_ASSESSED',value_decimal_text=None,observation_key=None,reason='native-context-not-retained'))
    coverage=parts[3].coverage[0].model_copy(update=dict(assessment='NOT_ASSESSED',reason='native-context-not-retained'))
    graph=validate_changed(parts[3],selections=(node,),coverage=(coverage,))
    identifier=FigureHistoryRepository(parts[0],clock=lambda:NOW).store(parts[1],request_key='unassociated',bundle=graph)
    publish(parts,register(parts,exact_bundle_id=identifier))
    page=timeline(parts); assert page['coverage']['assessment']=='NOT_ASSESSED'
    support=reader(parts).figure_support(security_id=parts[2].security_id,handle=page['endpoint']['supporting_records']['inputs'])
    assert support['rows']==[] and support['selected_reason']=='native-context-not-retained'


def test_documents_actions_and_intermediate_keep_endpoint_root_and_unproved_basis(storage):
    engine,lease,company,graph,owner,prepared=prepared_bundle(storage,graph='basis')
    identifier=owner.store(lease,request_key='basis',bundle=graph,prepared_documents=prepared)
    from screener.publication import PublicationRepository
    parts=(engine,lease,company,graph,identifier,PublicationRepository(engine,clock=lambda:NOW))
    release=publish(parts,register(parts)); page=timeline(parts); repository=reader(parts)
    assert page['coverage']['assessment']=='PARTIAL' and page['coverage']['reason']=='validator-unavailable'
    handles=page['endpoint']['supporting_records']
    actions=repository.figure_support(security_id=company.security_id,handle=handles['actions'],limit=1)
    assert actions['rows'][0]['old_shares_text']=='8' and actions['rows'][0]['new_shares_text']=='1'
    docs=repository.figure_support(security_id=company.security_id,handle=handles['documents'],limit=1)
    assert docs['rows'][0]['reference']['location']['start_byte']==0
    inputs=repository.figure_support(security_id=company.security_id,handle=handles['inputs'],limit=1)
    assert inputs['rows'][0]['selection']['purpose']=='INTERMEDIATE'
    child=inputs['rows'][0]['supporting_records']['inputs']
    inner=repository.figure_support(security_id=company.security_id,handle=child,limit=1)
    assert inner['root_identity']==inputs['root_identity'] and inner['endpoint_release_id']==release.release_id
    assert inner['rows'][0]['observation']['value_decimal_text']=='100.0000'
    forged=repository.cursors.decode(child); forged.update(parent=None,ordinal=None,root=forged['node'])
    with pytest.raises(HistoryNotFound): repository.figure_support(security_id=company.security_id,handle=repository.cursors.encode(forged))


def test_assumed_view_exposes_declared_assumption_without_strict_override(storage):
    engine,lease,company,graph,owner,prepared=prepared_bundle(storage,graph='assumed')
    identifier=owner.store(lease,request_key='assumed',bundle=graph,prepared_documents=prepared)
    from screener.publication import PublicationRepository
    parts=(engine,lease,company,graph,identifier,PublicationRepository(engine,clock=lambda:NOW)); publish(parts,register(parts))
    page=reader(parts).figure_history(HistoryQuery(**SELECTOR,view='ASSUMED'),security_id=company.security_id)
    rows=reader(parts).figure_support(security_id=company.security_id,handle=page['endpoint']['supporting_records']['assumptions'])['rows']
    assert rows[0]['affected_input_roles']==['strict-input']
    assert timeline(parts)['endpoint']['selection']['view']=='STRICT'


def test_long_http_names_deep_shared_dag_and_real_digest_scan_cost(storage,monkeypatch):
    parts=setup(storage); original=parts[3].selections[0]; metric='資産'*1000
    reported=original.model_copy(update=dict(node_key='raw-'+('根'*10000),metric_path=metric,purpose='INTERMEDIATE'))
    selections=[reported]; dependencies=[]
    for i in range(80):
        name=f'deep-{i}'
        selections.append(original.model_copy(update=dict(node_key=name,metric_path=metric,purpose='INTERMEDIATE',
            state='DERIVED',observation_key=None,definition='fixture.explicit-identity',definition_revision='synthetic-no-financial-proof',required_input_roles=('input',))))
        dependencies.append(CalculationDependency(parent_node_key=name,ordinal=0,role='input',selection_node_key=selections[-2].node_key))
    root=original.model_copy(update=dict(node_key='選択'*5000,metric_path=metric,state='DERIVED',observation_key=None,
        definition='fixture.multi-input',definition_revision='synthetic-no-financial-proof',required_input_roles=tuple(f'input-{i}' for i in range(301))))
    selections.append(root)
    dependencies.extend(CalculationDependency(parent_node_key=root.node_key,ordinal=i,role=f'input-{i}',selection_node_key=f'deep-{79 if i%2 else 49}') for i in range(301))
    selections.extend(reported.model_copy(update=dict(node_key=f'unreferenced-{i}')) for i in range(1500))
    coverage=parts[3].coverage[0].model_copy(update=dict(assessment='PARTIAL',reason='synthetic-extra-catalog',metric_paths=(metric,*(f'extra-{i}' for i in range(1000)))))
    graph=validate_changed(parts[3],contract_revision=2,selections=tuple(selections),dependencies=tuple(dependencies),coverage=(coverage,))
    identifier=FigureHistoryRepository(parts[0],clock=lambda:NOW).store(parts[1],request_key='large-names',bundle=graph)
    publish(parts,register(parts,exact_bundle_id=identifier))
    repository=reader(parts); monkeypatch.setattr(company_query_api,'query_repository',lambda:repository); client=TestClient(api.app)
    url=f'/api/v1/companies/{parts[2].security_id}/figure-history'
    params=dict(metric_path=metric,period_kind='INSTANT',period_end='2025-12-31',limit=1)
    page=client.get(url,params=params); assert page.status_code==200,page.text
    page=page.json(); assert page['selector']['metric_path']==metric
    handle=page['endpoint']['supporting_records']['inputs']; assert len(handle)<1500
    statements=[]
    def captured(conn,cursor,statement,parameters,context,executemany): statements.append(statement)
    event.listen(parts[0],'before_cursor_execute',captured); tracemalloc.start()
    try:
        response=client.get(url+'/dependencies',params=dict(handle=handle,limit=300))
        _,peak=tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop(); event.remove(parts[0],'before_cursor_execute',captured)
    assert response.status_code==200,response.text
    direct=response.json(); assert len(direct['rows'])==300 and len(statements)<=12
    assert direct['next_cursor'] and len(direct['next_cursor'])<1500
    remaining=client.get(url+'/dependencies',params=dict(handle=handle,limit=300,cursor=direct['next_cursor']))
    assert remaining.status_code==200 and len(remaining.json()['rows'])==1
    child=direct['rows'][0]['supporting_records']['inputs']
    for i in range(50):
        inner=client.get(url+'/dependencies',params=dict(handle=child,limit=1)); assert inner.status_code==200,inner.text
        inner=inner.json(); assert inner['root_identity']==direct['root_identity']
        child=inner['rows'][0]['supporting_records']['inputs']; assert len(child)<1500
    paths=client.get(url+'/dependencies',params=dict(handle=page['endpoint']['supporting_records']['coverage'],limit=1)).json()
    assert paths['next_cursor'] and len(paths['next_cursor'])<1500
    next_paths=client.get(url+'/dependencies',params=dict(handle=page['endpoint']['supporting_records']['coverage'],limit=1,cursor=paths['next_cursor']))
    assert next_paths.status_code==200,next_paths.text
    with parts[0].connect() as connection:
        plan=connection.scalar(text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT node_key FROM figure_selection WHERE bundle_id=:bundle AND encode(sha256(convert_to(node_key,'UTF8')),'hex')=:digest LIMIT 2"),dict(bundle=identifier,digest=repository.cursors.decode(handle)['node']))
        coverage_plan=connection.scalar(text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT p.key,c.record-'metric_paths' FROM figure_bundle_coverage c,LATERAL jsonb_array_elements_text(c.record->'metric_paths') p(key) WHERE c.bundle_id=:bundle ORDER BY p.key LIMIT 2"),dict(bundle=identifier))
    from pathlib import Path
    proof=dict(label='synthetic1582-node80-deep-shared-DAG-301-direct-inputs-1001-coveragepaths-longmultibytenames',
        returned_rows=300,statements=len(statements),response_bytes=len(response.content),peak_python_bytes=peak,digest_seek_plan=plan,coverage_array_plan=coverage_plan,
        ceiling='Digest lookup scans all nodes in the admitted bundle; metadata path projection scans the requested immutable family array; no constant per-bundle work claim.')
    Path('../local/history-read-resource-proof-2026-10-04.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({key:value for key,value in proof.items() if key not in {'digest_seek_plan','coverage_array_plan'}}))


def test_metadata_projection_paginates_actual_action_document_and_assumption_keys(storage):
    engine,lease,company,graph,owner,_=prepared_bundle(storage,graph='basis')
    documents=tuple(graph.document_evidence[0].model_copy(update=dict(key='資料'*2000+str(i))) for i in range(3))
    evidence=tuple(d.key for d in documents)
    actions=tuple(graph.actions[0].model_copy(update=dict(key='行動'*2000+str(i),legal_identity=f'legal-{i}',evidence_keys=evidence,
        date_witnesses=tuple(w.model_copy(update=dict(evidence_key=evidence[0])) for w in graph.actions[0].date_witnesses))) for i in range(3))
    raw=next(n for n in graph.selections if n.purpose=='INTERMEDIATE')
    output=next(n for n in graph.selections if n.purpose=='OUTPUT')
    before=raw.share_basis.model_copy(update=dict(evidence_keys=evidence)); after=before.model_copy(update=dict(incorporated_actions=tuple(a.key for a in actions)))
    raw=raw.model_copy(update=dict(share_basis=before))
    output=output.model_copy(update=dict(share_basis=after,basis_transition=output.basis_transition.model_copy(update=dict(action_keys=tuple(a.key for a in actions),evidence_keys=evidence))))
    manifest_witness=documents[0].model_copy(update=dict(key='unselected-manifest-witness'))
    unused=tuple(actions[0].model_copy(update=dict(key=f'unselected-{i}',legal_identity=f'unselected-legal-{i}',evidence_keys=(manifest_witness.key,),
        date_witnesses=tuple(w.model_copy(update=dict(evidence_key=manifest_witness.key)) for w in actions[0].date_witnesses))) for i in range(1000))
    graph=validate_changed(graph,document_evidence=(*documents,manifest_witness),actions=(*actions,*unused),selections=(raw,output),observations=(graph.observations[0].model_copy(update=dict(share_basis=before)),))
    prepared=owner.prepare_documents(graph,objects()); identifier=owner.store(lease,request_key='metadata-long',bundle=graph,prepared_documents=prepared)
    from screener.publication import PublicationRepository
    parts=(engine,lease,company,graph,identifier,PublicationRepository(engine,clock=lambda:NOW)); publish(parts,register(parts)); repository=reader(parts)
    handles=timeline(parts)['endpoint']['supporting_records']
    for kind in ('actions','documents'):
        page=repository.figure_support(security_id=company.security_id,handle=handles[kind],limit=1); collected=[]
        while True:
            collected.extend(page['rows'])
            if not page['next_cursor']: break
            assert len(page['next_cursor'])<1500
            page=repository.figure_support(security_id=company.security_id,handle=handles[kind],limit=1,cursor=page['next_cursor'])
        assert len(collected)==3
    with engine.connect() as c:
        plan=c.scalar(text("""EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT a.record FROM exact_figure_bundle b,
            LATERAL jsonb_array_elements(b.contract_details->'actions') a(record)
            WHERE b.bundle_id=:bundle AND a.record->>'key'=ANY(:keys) ORDER BY a.record->>'key' LIMIT 2"""),dict(bundle=identifier,keys=[a.key for a in actions]))
    from pathlib import Path
    Path('../local/history-read-action-array-proof-2026-10-04.json').write_text(json.dumps(dict(
        label='synthetic1003-action-manifest3-qualified-actions4-documents;longexactkeys',plan=plan,
        returned_records_per_page=1,ceiling='SQL scans action array per admitted bundle; supported-record rows are bounded, manifest work is not constant.'),indent=2),encoding='utf8')
    assert 'Rows Removed by Filter' in json.dumps(plan) and '1000' in json.dumps(plan)
