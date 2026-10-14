"""Composed local runtime over labelled synthetic SEC transport; no native acceptance."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from screener import api, automatic_ingestion as owner
from screener.durable_jobs import DurableJobRepository
from screener.figure_history import FigureHistoryRepository
from screener.postgres import (active_data_release, company_snapshot, current_company_snapshot,
                              data_release, durable_job, exact_figure_bundle, source_observation)
from screener.sources.edgar import EdgarTransportResult, NoXbrlDataError
from screener.storage_config import AutomaticIngestionSettings
from test_publication_storage import storage, pytestmark
from test_sec_exact_annotation import annotated
from test_captured_assembly import preparation
from test_sec_ingestion_storage import (FixtureEdgar, RESOURCE_ACCESSION, _index, _row,
                                       _url, _resource_responses, _resource_urls)

def proof_directory(tmp_path):
    directory=Path(os.environ.get('SCREENER_INGESTION_TEST_OUTPUT_DIR',str(tmp_path/'proof')))
    directory.mkdir(parents=True,exist_ok=True)
    return directory


def fixture_responses():
    responses=_resource_responses()
    raw,_,_=annotated();facts=json.loads(raw)
    for taxonomy in facts['facts'].values():
        for definition in taxonomy.values():
            for entries in definition['units'].values():
                for entry in entries:
                    if entry['accn']=='k25':entry.update(accn=RESOURCE_ACCESSION,filed='2026-09-28')
    url=_resource_urls()['company_facts']
    responses[url]=EdgarTransportResult(json.dumps(facts).encode(),url,'application/json','synthetic-annotation',None)
    daily=_url(date(2026,9,28))
    raw=_index(_row('10-K','FIXTURE CORP','1234','2026-09-28',RESOURCE_ACCESSION))
    responses[daily]=EdgarTransportResult(raw,daily,'text/plain','synthetic-daily',None)
    quarterly='https://www.sec.gov/Archives/edgar/full-index/2026/QTR3/form.idx'
    responses[quarterly]=EdgarTransportResult(raw,quarterly,'text/plain','synthetic-quarterly',None)
    base='https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/'
    for name,body in [('annual.htm',b'<html>annual synthetic fixture</html>'),
                      ('R1.htm',b'<html>Title of each class Common Stock Trading Symbol FIX Name of each exchange Nasdaq</html>')]:
        responses[base+name]=EdgarTransportResult(body,base+name,'text/html',None,None)
    return responses


class RetainedTransport(FixtureEdgar):
    def __init__(self,responses):super().__init__(responses);self.closed=False
    def close(self):self.closed=True


def plan(tmp_path,kind='sec-recent-discovery-v2',key='first'):
    now=datetime.now(timezone.utc)
    entry=dict(schedule_key=f'automatic-ingestion:v1:{kind}:{key}',scheduled_for=now.isoformat(),
               due_at=now.isoformat(),kind=kind,parameters=({'year':2026,'quarter':3} if kind.startswith('sec-index-reconciliation')
                                                        else {'start_date':'2026-09-28','end_date':'2026-09-28'}))
    path=tmp_path/f'{key}.json';path.write_text(json.dumps({'plan_revision':1,'occurrences':[entry]}))
    return AutomaticIngestionSettings(True,path,tmp_path/'cache'),entry


def wait(engine,predicate,timeout=12):
    deadline=monotonic()+timeout
    while monotonic()<deadline:
        with engine.connect() as connection:
            rows=connection.execute(select(durable_job)).mappings().all()
        failed=[dict(row) for row in rows if row['status']=='failed']
        assert not failed,[(r['kind'],r['error_summary']) for r in failed]
        if predicate(rows):return rows
        sleep(.03)
    raise AssertionError([(r['kind'],r['status'],r['error_summary']) for r in rows])


def pointers(engine):
    with engine.connect() as connection:
        return dict(current=connection.scalar(select(func.count()).select_from(current_company_snapshot)),
                    releases=connection.scalar(select(func.count()).select_from(data_release)),
                    active=connection.scalar(select(active_data_release.c.release_id)))


@pytest.mark.parametrize('kind',['sec-recent-discovery-v2','sec-index-reconciliation-v2'])
def test_actual_lifespan_no_requests_runs_capture2_same_run_pair_and_repeated_ticks(storage,tmp_path,monkeypatch,kind):
    engine=storage[0];current,entry=plan(tmp_path,kind=kind)
    edgar=RetainedTransport(fixture_responses())
    monkeypatch.setenv('SCREENER_AUTOMATIC_INGESTION','1')
    monkeypatch.setenv('SCREENER_INGESTION_PLAN',str(current.plan_path))
    monkeypatch.setenv('SCREENER_INGESTION_CACHE',str(current.cache_dir))
    monkeypatch.setattr(owner,'EdgarClient',lambda **kw:edgar)
    monkeypatch.setattr(api.jobs,'start_hourly_quotes',lambda:None)
    monkeypatch.setattr(api.jobs,'stop_hourly_quotes',lambda:None)
    before=pointers(engine)
    with TestClient(api.app):
        rows=wait(engine,lambda rows:len(rows)==3 and all(row['status']=='succeeded' for row in rows))
        assembly=owner._active
        sleep(1.1)
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(durable_job))==3
            assert connection.scalar(select(func.count()).select_from(company_snapshot))==1
            assert connection.scalar(select(func.count()).select_from(exact_figure_bundle))==1
        resource=next(row for row in rows if row['kind']=='sec-resource-fetch-v2')
        candidate=next(row for row in rows if row['kind']=='sec-candidate-stage')
        assert resource['checkpoint']['capture_revision']==2 and resource['checkpoint']['root_deliveries']
        checkpoint=candidate['checkpoint']
        graph=FigureHistoryRepository(engine).load(checkpoint['exact_bundle_id'])
        assert graph.snapshot_id==checkpoint['snapshot_id'] and graph.snapshot_sha256==checkpoint['snapshot_sha256']
        assert graph.coverage and all(row.assessment=='PARTIAL' for row in graph.coverage)
        assert pointers(engine)==before
        assert assembly.runtime.running
    assert edgar.closed and owner._active is None
    assert pointers(engine)==before
    (proof_directory(tmp_path)/f'automatic-ingestion-lifespan-flow-{kind}-{uuid4().hex}.json').write_text(json.dumps({
        'scope':'Synthetic raw-HTTP transport and real SQL/S3/runtime; no financial acceptance',
        'job_kinds':[row['kind'] for row in rows],'checkpoint':checkpoint,
        'transport_count':len(edgar.urls),'response_bytes':sum(len(edgar.responses[url].data) for url in edgar.urls),
        'pointers_before_after':before,'owned_closed':edgar.closed,
        'threads_at_drain':len(assembly.runtime._threads),'errors':assembly.runtime.errors},indent=2,default=str))


def test_legacy_resource_queue_keeps_unannotated_shape_in_composed_registry(storage,tmp_path):
    from test_sec_ingestion_storage import _seed_resource_parent, _enqueue_resource_job
    engine=storage[0];objects=owner.ImmutableObjectStore(owner.create_s3_client(),owner.StorageSettings.from_env().s3_bucket)
    jobs=DurableJobRepository(engine)
    parent=_seed_resource_parent(engine,objects,jobs,'legacy-auto')
    claim=_enqueue_resource_job(jobs,parent,'legacy-auto')
    # Existing owner reclaims its already-running job, preserving legacy parameters and key.
    with engine.begin() as connection:
        connection.execute(durable_job.update().where(durable_job.c.job_id==claim.job.job_id)
                           .values(lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
    jobs.recover_expired()
    with engine.begin() as connection:
        connection.execute(durable_job.update().where(durable_job.c.job_id==claim.job.job_id)
                           .values(next_retry_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
    current,entry=plan(tmp_path,kind='sec-recent-discovery',key='legacy')
    responses=_resource_responses();responses[_url(date(2026,9,28))]=NoXbrlDataError('synthetic absence')
    edgar=RetainedTransport(responses)
    assembly=owner.start_automatic_ingestion(current,engine=engine,s3=objects.client,edgar=edgar)
    try:
        rows=wait(engine,lambda rows:any(r['job_id']==claim.job.job_id and r['status']=='succeeded' for r in rows))
        replay=next(row for row in rows if row['job_id']==claim.job.job_id)
        assert 'capture_revision' not in replay['checkpoint'] and 'root_deliveries' not in replay['checkpoint']
        assert replay['parameters']==claim.job.parameters
        assert pointers(engine)==dict(current=0,releases=0,active=None)
    finally:
        owner.stop_automatic_ingestion(assembly);objects.client.close()
    assert not edgar.closed


def test_composed_blocked_fetch_drain_retains_resources_until_worker_returns(storage,tmp_path,monkeypatch):
    engine=storage[0];current,_=plan(tmp_path)
    entered,release=Event(),Event();edgar=RetainedTransport(fixture_responses())
    fetch=edgar.fetch
    def blocked(url):
        if '/daily-index/' in url:
            entered.set();assert release.wait(5)
        return fetch(url)
    edgar.fetch=blocked
    objects=owner.ImmutableObjectStore(owner.create_s3_client(),owner.StorageSettings.from_env().s3_bucket)
    assembly=owner.start_automatic_ingestion(current,engine=engine,s3=objects.client,edgar=edgar)
    try:
        assert entered.wait(3)
        stop=assembly.stop
        monkeypatch.setattr(assembly,'stop',lambda timeout=30:stop(.01))
        with pytest.raises(owner.IngestionDrainError):owner.stop_automatic_ingestion(assembly)
        assert owner._active is assembly and assembly.runtime.running and not edgar.closed
        with pytest.raises(RuntimeError):owner.start_automatic_ingestion(current)
        release.set();monkeypatch.setattr(assembly,'stop',stop)
        owner.stop_automatic_ingestion(assembly)
        assert not assembly.runtime.running and owner._active is None and not edgar.closed
        assert pointers(engine)==dict(current=0,releases=0,active=None)
    finally:
        release.set()
        if owner._active is assembly:
            monkeypatch.setattr(assembly,'stop',stop);owner.stop_automatic_ingestion(assembly)
        objects.client.close()


@pytest.mark.parametrize('point',['capture','pair'])
@pytest.mark.skipif(os.name!='nt',reason='Native process ownership proof requires Windows CIM/PowerShell')
def test_actual_composed_process_kill_and_reclaim_keeps_exact_checkpoint(storage,tmp_path,point,
                                                                       producer_kind='sec-recent-discovery-v2'):
    engine=storage[0];current,_=plan(tmp_path,kind=producer_kind,key=f'process-{point}')
    directory=proof_directory(tmp_path)/f'automatic-ingestion-process-{point}-{uuid4().hex}'
    directory.mkdir();before=directory/'before.json';after=directory/'after.json'
    env={**os.environ,'SCREENER_AUTOMATIC_INGESTION':'1','SCREENER_INGESTION_PLAN':str(current.plan_path),
         'SCREENER_INGESTION_CACHE':str(current.cache_dir)}
    script=Path(__file__).with_name('automatic_ingestion_process_fixture.py')
    child=None
    try:
        with (directory/'killed.log').open('w') as output:
            child=subprocess.Popen([sys.executable,str(script),f'kill-{point}',str(before)],
                env=env,stdout=output,stderr=subprocess.STDOUT)
            deadline=monotonic()+12
            while not before.exists() and monotonic()<deadline:
                assert child.poll() is None,(directory/'killed.log').read_text()
                sleep(.03)
            assert before.exists(),'child did not reach actual committed boundary'
            original=json.loads(before.read_text())
            memory=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',
                f'Get-Process -Id {original["process_id"]} | Select-Object WorkingSet64,PeakWorkingSet64 | ConvertTo-Json -Compress'],
                capture_output=True,text=True,check=True)
            memory=json.loads(memory.stdout)
            native_pid=original['process_id']
            subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',
                f"$owned = Get-CimInstance Win32_Process -Filter 'ProcessId={native_pid}'; "
                "if ($owned.CommandLine -notlike '*automatic_ingestion_process_fixture.py*') {throw 'Wrong native process'}; "
                f'Stop-Process -Id {native_pid} -Force'],check=True,capture_output=True,text=True)
            child.wait(timeout=5)
            assert child.returncode is not None
        with engine.begin() as connection:
            saved=connection.execute(select(durable_job).where(durable_job.c.job_id==original['job_id'])).mappings().one()
            assert saved['status']=='running' and saved['checkpoint']==original['checkpoint']
            connection.execute(durable_job.update().where(durable_job.c.job_id==original['job_id'])
                .values(lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
        with (directory/'recovered.log').open('w') as output:
            restarted=subprocess.run([sys.executable,str(script),f'recover-{point}',str(after)],env=env,
                stdout=output,stderr=subprocess.STDOUT,timeout=20)
        assert restarted.returncode==0,(directory/'recovered.log').read_text()
        recovered=json.loads(after.read_text())
        with engine.connect() as connection:
            final=connection.execute(select(durable_job).where(durable_job.c.job_id==original['job_id'])).mappings().one()
            assert final['ownership_generation']>original['generation']
            assert final['checkpoint']==original['checkpoint']
            assert connection.scalar(select(func.count()).select_from(company_snapshot))==1
            assert connection.scalar(select(func.count()).select_from(exact_figure_bundle))==1
        if point=='pair':assert recovered['transport_urls']==[] and recovered['checkpoint']==original['checkpoint']
        assert not recovered['runtime_errors']
        assert pointers(engine)==dict(current=0,releases=0,active=None)
        (directory/'receipt.json').write_text(json.dumps({'scope':'Synthetic transports; actual composed OS-process recovery',
            'launcher_process_id':child.pid,'killed_process_id':native_pid,'kill_exit':child.returncode,'recover_exit':restarted.returncode,
            'memory':memory,'boundary':point,'before':original,'after':recovered},indent=2))
    finally:
        if child is not None and child.poll() is None:child.kill();child.wait(timeout=5)


def test_unavailable_root_defers_without_snapshot_or_zero(storage,tmp_path):
    engine=storage[0];current,_=plan(tmp_path,key='pending-root')
    responses=fixture_responses();responses[_resource_urls()['company_facts']]=NoXbrlDataError('known unavailable fixture root')
    edgar=RetainedTransport(responses)
    s3=owner.create_s3_client()
    assembly=owner.start_automatic_ingestion(current,engine=engine,s3=s3,edgar=edgar)
    try:
        rows=wait(engine,lambda rows:any(r['kind']=='sec-resource-fetch-v2' and r['status']=='retry_wait' for r in rows))
        pending=next(r for r in rows if r['kind']=='sec-resource-fetch-v2')
        assert 'pending' in pending['error_summary'].lower()
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(company_snapshot))==0
            assert connection.scalar(select(func.count()).select_from(exact_figure_bundle))==0
            assert connection.scalar(select(func.count()).select_from(source_observation).where(source_observation.c.state=='pending'))==1
        assert pointers(engine)==dict(current=0,releases=0,active=None)
    finally:owner.stop_automatic_ingestion(assembly);s3.close()


def test_pre_retained_synthetic_recipe1_uses_composed_handler_with_no_live_http(storage,tmp_path,monkeypatch,preparation):
    from screener.artifacts import store_evidence
    from screener.company_import import _canonical_json
    from screener.shared_companies import SnapshotArtifact
    from test_captured_assembly_storage import _owners
    engine=storage[0];seed,_,recipe,company=preparation
    service=_owners(engine);objects=service.objects;artifacts=service.artifacts;companies=service.companies
    links=tuple(SnapshotArtifact(store_evidence(seed._read(link.content_sha256),'application/json',objects,artifacts).content_sha256,link.role)
                for link in company.artifacts)
    snap=company.snapshot
    stored=companies.store_and_select(issuer_source=company.issuer_source,issuer_identifier=company.issuer_identifier,
        security_identifier='sec:0000001234:primary-common',ticker=snap.ticker,exchange_code=None,
        quote_currency=snap.quote_currency,security_title=snap.security_title,source_accession=snap.source_accession,
        security_basis=snap.security_basis,engine_revision=snap.engine_revision,canonical_payload=snap.canonical_payload,artifacts=links)
    recipe['members'][0].update(security_id=stored.security_id,snapshot_id=stored.snapshot_id)
    digest=store_evidence(_canonical_json(recipe),'application/json',objects,artifacts).content_sha256
    before=pointers(engine)
    current,entry=plan(tmp_path,kind='local-canonical-preparation',key='synthetic-recipe1')
    entry['parameters']={'recipe_sha256':digest}
    current.plan_path.write_text(json.dumps({'plan_revision':1,'occurrences':[entry]}))
    def no(*a,**k):raise AssertionError('canonical recipe1 attempted live HTTP')
    monkeypatch.setattr('httpx.Client.get',no);monkeypatch.setattr('httpx.get',no)
    edgar=RetainedTransport({})
    assembly=owner.start_automatic_ingestion(current,engine=engine,s3=service.objects.client,edgar=edgar)
    try:
        rows=wait(engine,lambda rows:len(rows)==1 and rows[0]['status']=='succeeded')
        result=json.loads(service._read(rows[0]['checkpoint']['preparation_sha256']))
        assert result['official_financial_readiness']=='NOT_ASSESSED'
        assert result['display_projection_status']=='NOT_BUILT'
        assert not edgar.urls and pointers(engine)==before
    finally:owner.stop_automatic_ingestion(assembly)
    replay=owner.start_automatic_ingestion(current,engine=engine,s3=objects.client,edgar=edgar)
    try:
        replayed=wait(engine,lambda rows:len(rows)==1 and rows[0]['status']=='succeeded')
        assert replayed[0]['checkpoint']==rows[0]['checkpoint'] and pointers(engine)==before
        assert not edgar.urls
    finally:owner.stop_automatic_ingestion(replay);objects.client.close()


def test_exact_versioned_occurrence_identity_and_integer_sql_neighbors(storage,tmp_path):
    from screener.durable_jobs import JobIdentityConflict
    engine=storage[0];jobs=DurableJobRepository(engine)
    current,old=plan(tmp_path,kind='sec-recent-discovery',key='identity')
    old=owner.read_plan(current.plan_path)[0]
    saved=jobs.enqueue_scheduled(**vars(old))
    from dataclasses import replace
    with pytest.raises(JobIdentityConflict):jobs.enqueue_scheduled(**vars(replace(old,kind='sec-recent-discovery-v2')))
    newer=replace(old,kind='sec-recent-discovery-v2',schedule_key=old.schedule_key.replace('discovery:','discovery-v2:'))
    assert jobs.enqueue_scheduled(**vars(newer)).job_id!=saved.job_id
    for number in (-2147483648,2147483647):
        bounded=replace(newer,schedule_key=newer.schedule_key+str(number),priority=number,max_attempts=2147483647)
        accepted=jobs.enqueue_scheduled(**vars(bounded))
        assert accepted.priority==number and accepted.max_attempts==2147483647
    assert pointers(engine)==dict(current=0,releases=0,active=None)


def test_finite_discoveries_record_index_A_B_A_and_preserve_completed_child_dedup(storage,tmp_path):
    engine=storage[0];s3=owner.create_s3_client();observed=[]
    try:
        for number,company in enumerate(['FIXTURE CORP','FIXTURE CORP REVISION','FIXTURE CORP']):
            current,_=plan(tmp_path,key=f'recurrence-{number}')
            responses=fixture_responses();url=_url(date(2026,9,28))
            raw=_index(_row('10-K',company,'1234','2026-09-28',RESOURCE_ACCESSION))
            responses[url]=EdgarTransportResult(raw,url,'text/plain',f'synthetic-{company}',None)
            edgar=RetainedTransport(responses)
            assembly=owner.start_automatic_ingestion(current,engine=engine,s3=s3,edgar=edgar)
            try:
                rows=wait(engine,lambda rows:len(rows)==[3,6,7][number] and all(r['status']=='succeeded' for r in rows))
                if number<2:
                    latest=max((r for r in rows if r['kind']=='sec-resource-fetch-v2'),key=lambda r:r['job_id'])
                    observed.append(latest['checkpoint'])
            finally:owner.stop_automatic_ingestion(assembly)
        from screener.postgres import source_item,source_check_occurrence
        with engine.connect() as connection:
            checks=connection.execute(select(source_check_occurrence).join(source_item)
                .where(source_item.c.item_kind=='filing',source_item.c.source_key==RESOURCE_ACCESSION)
                .order_by(source_check_occurrence.c.occurrence_id)).mappings().all()
        assert len(checks)==3
        assert checks[0]['source_observation_id']==checks[2]['source_observation_id']!=checks[1]['source_observation_id']
        from screener.source_observations import SourceObservationRepository
        source=SourceObservationRepository(engine)
        deliveries=[]
        for checkpoint in observed:
            role=checkpoint['root_deliveries']['company_facts']
            # Existing DTO reload owns check-to-source binding; no latest lookup.
            delivered=source.delivery(role['occurrence_id'],source_item_id=role['source_item_id'],
                                      source_observation_id=role['source_observation_id'])
            deliveries.append(delivered.occurrence_id)
        assert len(set(deliveries))==2
        (proof_directory(tmp_path)/f'automatic-ingestion-index-recurrence-ceiling-{uuid4().hex}.json').write_text(json.dumps({
            'scope':'Index recurrence; completed resource children reused, not three captured roots',
            'jobs':len(rows),'filing_checks':[dict(row) for row in checks],
            'captured_companyfacts_delivery_occurrences':deliveries,
            'required_followup':'Independent mutable-resource check occurrences and retained-input rederivation'},indent=2,default=str))
        assert pointers(engine)==dict(current=0,releases=0,active=None)
    finally:s3.close()


def test_actual0006_schema_and_missing_bucket_reject_before_scheduling(storage,tmp_path,monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    name='ms_auto_incompatible_'+uuid4().hex
    settings=owner.StorageSettings.from_env()
    admin=create_engine(settings.database_url,isolation_level='AUTOCOMMIT')
    url=make_url(settings.database_url).set(database=name)
    with admin.connect() as connection:connection.execute(text(f'CREATE DATABASE "{name}"'))
    old=create_engine(url);calls=[]
    s3=type('Missing',(),{'head_bucket':lambda self,**kw:calls.append('bucket')})()
    current,_=plan(tmp_path,key='unready')
    try:
        with monkeypatch.context() as patch:
            patch.setenv('SCREENER_DATABASE_URL',url.render_as_string(hide_password=False))
            command.upgrade(Config('alembic.ini'),'20260929_0006')
            with pytest.raises(ValueError,match='supported migrated'):
                owner.start_automatic_ingestion(current,engine=old,s3=s3,edgar=RetainedTransport({}))
            assert owner._active is None and not calls
            with old.connect() as connection:assert connection.scalar(select(func.count()).select_from(durable_job))==0
        def missing_bucket(**kw):raise RuntimeError('labelled missing bucket')
        s3.head_bucket=missing_bucket
        with pytest.raises(RuntimeError,match='missing bucket'):
            owner.start_automatic_ingestion(current,engine=storage[0],s3=s3,edgar=RetainedTransport({}))
        assert owner._active is None
        with storage[0].connect() as connection:assert connection.scalar(select(func.count()).select_from(durable_job))==0
    finally:
        old.dispose()
        with admin.connect() as connection:connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
