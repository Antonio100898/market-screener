import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from screener import api, automatic_ingestion as owner
from screener.sec_ingestion import SecRecentDiscoveryHandler, SecQuarterlyReconciliationHandler
from screener.storage_config import AutomaticIngestionSettings

NOW=datetime(2026,10,4,tzinfo=timezone.utc)


def request(kind="sec-recent-discovery-v2"):
    return dict(schedule_key=f"automatic-ingestion:v1:{kind}:fixture",scheduled_for=NOW.isoformat(),
                kind=kind,parameters=dict(start_date="2026-10-04",end_date="2026-10-04"),due_at=NOW.isoformat())


def write_plan(tmp_path, entries=None):
    path=tmp_path/'plan.json'
    path.write_text(json.dumps(dict(plan_revision=1,occurrences=entries or [request()])))
    return path


def settings(tmp_path):
    return AutomaticIngestionSettings(True,write_plan(tmp_path),tmp_path/'dedicated-cache')


@pytest.fixture
def registry(monkeypatch):
    assert owner._active is None
    yield
    if owner._active:
        owner.stop_automatic_ingestion(owner._active)


def test_disabled_actual_lifespan_preserves_quotes_without_durable_constructors(monkeypatch):
    monkeypatch.delenv("SCREENER_AUTOMATIC_INGESTION",raising=False)
    monkeypatch.setenv("SCREENER_INGESTION_PLAN","unused-invalid")
    monkeypatch.setattr(owner,"AutomaticIngestion",lambda *a,**k:pytest.fail("disabled durable construction"))
    events=[]
    monkeypatch.setattr(api.jobs,"start_hourly_quotes",lambda:events.append("start"))
    monkeypatch.setattr(api.jobs,"stop_hourly_quotes",lambda:events.append("stop"))
    with TestClient(api.app):assert events==["start"]
    assert events==["start","stop"]


@pytest.mark.parametrize("enabled",["yes","false","2",""])
def test_invalid_enable_is_explicit_before_construction(monkeypatch,enabled):
    monkeypatch.setenv("SCREENER_AUTOMATIC_INGESTION",enabled)
    with pytest.raises(ValueError):owner.start_automatic_ingestion()


def test_enabled_requires_plan_and_dedicated_cache(monkeypatch,tmp_path):
    monkeypatch.setenv("SCREENER_AUTOMATIC_INGESTION","1")
    monkeypatch.delenv("SCREENER_INGESTION_PLAN",raising=False)
    monkeypatch.delenv("SCREENER_INGESTION_CACHE",raising=False)
    with pytest.raises(ValueError):AutomaticIngestionSettings.from_env()
    with pytest.raises(ValueError):AutomaticIngestionSettings(True,tmp_path/'p',Path.home()/'.cache/graham-screener/nested').validate()


@pytest.mark.parametrize("change",[
    lambda r:r.update(kind="private-agent"),lambda r:r.update(schedule_key="old-key"),
    lambda r:r.update(schedule_key=r['schedule_key']+' '),lambda r:r.update(parameters={"capture_revision":2}),
    lambda r:r.update(parameters={"start_date":"2026-01-01","end_date":"2026-03-01"}),
    lambda r:r.update(scheduled_for="2026-10-04T00:00:00"),lambda r:r.update(max_attempts=True),
    lambda r:r.update(priority=0.5),lambda r:r.update(deadline_at=r['due_at']),lambda r:r.update(extra="unused"),
    lambda r:r.update(schedule_key=r['schedule_key']+'\0bad'),
    lambda r:r.update(scheduled_for='0001-01-01T00:00:00+01:00'),
    lambda r:r.update(due_at='9999-12-31T23:59:59-01:00'),
    lambda r:r.update(deadline_at='9999-12-31T23:59:59-01:00'),
    lambda r:r.update(schedule_key=r['schedule_key']+'x'*1024),
])
def test_plan_invalid_admission_precedes_factories(tmp_path,monkeypatch,change,registry):
    current=settings(tmp_path);r=request();change(r);write_plan(tmp_path,[r])
    monkeypatch.setattr(owner,"create_postgres_engine",lambda *a:pytest.fail("invalid plan opened database"))
    with pytest.raises(ValueError):owner.start_automatic_ingestion(current)
    assert owner._active is None


def test_duplicate_occurrence_revision_and_size_bounds(tmp_path):
    path=write_plan(tmp_path,[request(),request()])
    with pytest.raises(ValueError,match="duplicate"):owner.read_plan(path)
    path.write_text(json.dumps(dict(plan_revision=True,occurrences=[request()])))
    with pytest.raises(ValueError):owner.read_plan(path)
    path.write_bytes(b' '* (owner.MAX_PLAN_BYTES+1))
    with pytest.raises(ValueError,match="exceeds"):owner.read_plan(path)


def test_finite_plan_has_no_today_or_new_recipe_inference(tmp_path):
    quarter=request("sec-index-reconciliation-v2");quarter['parameters']={'year':2026,'quarter':4}
    recipe=request("local-canonical-preparation");recipe['parameters']={'recipe_sha256':'a'*64}
    items=owner.read_plan(write_plan(tmp_path,[request(),quarter,recipe]))
    assert len(items)==3 and items[0].scheduled_for==NOW
    recipe['parameters']['recipe_revision']=2
    with pytest.raises(ValueError):owner.read_plan(write_plan(tmp_path,[recipe]))


def assembled(monkeypatch,tmp_path,*,borrowed=False,start_error=None,drained=True):
    events=[]
    engine=SimpleNamespace(dispose=lambda:events.append('engine-close'))
    s3=SimpleNamespace(close=lambda:events.append('s3-close'))
    edgar=SimpleNamespace(close=lambda:events.append('edgar-close'))
    monkeypatch.setattr(owner,'create_postgres_engine',lambda *a:engine)
    monkeypatch.setattr(owner,'create_s3_client',lambda *a:s3)
    monkeypatch.setattr(owner,'EdgarClient',lambda **kw:edgar)
    monkeypatch.setattr(owner,'preflight',lambda *a:events.append('preflight'))
    class Runtime:
        def __init__(self,repository,source,handlers,**kw):
            self.repository=repository;self.source=source;self.handlers=handlers;self.worker_count=kw['worker_count']
        def start(self):
            events.append('start')
            if start_error:raise start_error
            return True
        def stop(self,timeout):events.append('drain');return drained
    monkeypatch.setattr(owner,'DurableJobRuntime',Runtime)
    resources=dict(engine=engine,s3=s3,edgar=edgar) if borrowed else {}
    return settings(tmp_path),events,resources


def test_registry_reuses_handlers_same_engine_versioned_routing_and_finite_ticks(monkeypatch,tmp_path,registry):
    current,events,resources=assembled(monkeypatch,tmp_path)
    assembly=owner.start_automatic_ingestion(current)
    runtime=assembly.runtime
    assert runtime.worker_count==1 and runtime.source(NOW)==runtime.source(NOW.replace(year=2030))
    assert runtime.handlers['sec-recent-discovery']._resource_kind=='sec-resource-fetch'
    assert runtime.handlers['sec-recent-discovery-v2']._resource_kind=='sec-resource-fetch-v2'
    assert runtime.handlers['sec-index-reconciliation-v2']._resource_kind=='sec-resource-fetch-v2'
    assert not runtime.handlers['sec-resource-fetch']._annotate_annual
    assert runtime.handlers['sec-resource-fetch-v2']._annotate_annual
    candidate=runtime.handlers['sec-candidate-stage']
    assert candidate._companies.engine is candidate._history.engine is runtime.repository.engine
    assert all('private' not in name and 'research' not in name for name in runtime.handlers)
    with pytest.raises(RuntimeError,match='still owns'):owner.start_automatic_ingestion(current)
    owner.stop_automatic_ingestion(assembly)
    assert events==['preflight','start','drain','edgar-close','s3-close','engine-close']


def test_borrowed_resources_are_not_closed(monkeypatch,tmp_path,registry):
    current,events,resources=assembled(monkeypatch,tmp_path,borrowed=True)
    assembly=owner.start_automatic_ingestion(current,**resources)
    owner.stop_automatic_ingestion(assembly)
    assert events==['preflight','start','drain']


def test_preflight_and_later_factory_failures_close_acquired_owners(monkeypatch,tmp_path,registry):
    current,events,_=assembled(monkeypatch,tmp_path)
    def fail(*a,**kw):raise RuntimeError('known startup failure')
    monkeypatch.setattr(owner,'preflight',fail)
    with pytest.raises(RuntimeError,match='known startup'):owner.start_automatic_ingestion(current)
    assert events==['s3-close','engine-close'] and owner._active is None
    events.clear();monkeypatch.setattr(owner,'preflight',lambda *a:None)
    monkeypatch.setattr(owner,'EdgarClient',fail)
    with pytest.raises(RuntimeError):owner.start_automatic_ingestion(current)
    assert events==['s3-close','engine-close'] and owner._active is None


def test_failed_start_drains_before_disposal(monkeypatch,tmp_path,registry):
    current,events,_=assembled(monkeypatch,tmp_path,start_error=RuntimeError('thread failure'))
    with pytest.raises(RuntimeError,match='thread failure'):owner.start_automatic_ingestion(current)
    assert events==['preflight','start','drain','edgar-close','s3-close','engine-close']
    assert owner._active is None


@pytest.mark.parametrize('startup',[False,True])
def test_failed_drain_keeps_singleton_and_ownership_until_real_drain(monkeypatch,tmp_path,registry,startup):
    current,events,_=assembled(monkeypatch,tmp_path,drained=False,start_error=RuntimeError('startup') if startup else None)
    if startup:
        with pytest.raises(owner.IngestionDrainError):owner.start_automatic_ingestion(current)
        assembly=owner._active
    else:
        assembly=owner.start_automatic_ingestion(current)
        with pytest.raises(owner.IngestionDrainError):owner.stop_automatic_ingestion(assembly)
    assert owner._active is assembly and not any('close' in event for event in events)
    with pytest.raises(RuntimeError):owner.start_automatic_ingestion(current)
    monkeypatch.setattr(assembly.runtime,'stop',lambda timeout:True)
    owner.stop_automatic_ingestion(assembly)
    assert owner._active is None and events[-3:]==['edgar-close','s3-close','engine-close']


def test_lifespan_construction_and_drain_are_off_async_thread(monkeypatch):
    async_thread=threading.get_ident();events=[]
    def start():events.append(('start',threading.get_ident()));return object()
    def stop(assembly):events.append(('stop',threading.get_ident()))
    monkeypatch.setattr(api,'start_automatic_ingestion',start)
    monkeypatch.setattr(api,'stop_automatic_ingestion',stop)
    monkeypatch.setattr(api.jobs,'start_hourly_quotes',lambda:None)
    monkeypatch.setattr(api.jobs,'stop_hourly_quotes',lambda:None)
    async def exercise():
        async with api._lifespan(api.app):pass
    asyncio.run(exercise())
    assert [e[0] for e in events]==['start','stop'] and all(e[1]!=async_thread for e in events)


@pytest.mark.parametrize('handler',[SecRecentDiscoveryHandler,SecQuarterlyReconciliationHandler])
@pytest.mark.parametrize('kind',['private-handler',None,[],2])
def test_producer_rejects_unregistered_resource_kind(handler,kind):
    with pytest.raises(ValueError):handler(edgar=None,object_store=None,artifacts=None,observations=None,jobs=None,resource_kind=kind)


def test_edgar_public_close_delegates_without_fetch(tmp_path,monkeypatch):
    from screener.sources.edgar import EdgarClient
    calls=[]
    monkeypatch.setattr('screener.sources.edgar.httpx.Client',lambda **k:SimpleNamespace(close=lambda:calls.append('closed')))
    edgar=EdgarClient(cache_dir=tmp_path/'owned');edgar.close()
    assert calls==['closed']


@pytest.mark.parametrize('field,value',[('priority',-2147483649),('priority',2147483648),('max_attempts',2147483648)])
def test_plan_rejects_unrepresentable_storage_integers(tmp_path,field,value):
    r=request();r[field]=value
    with pytest.raises(ValueError):owner.read_plan(write_plan(tmp_path,[r]))


@pytest.mark.parametrize('priority',[-2147483648,2147483647])
def test_plan_preserves_legitimate_storage_integer_boundaries(tmp_path,priority):
    r=request();r.update(priority=priority,max_attempts=2147483647)
    assert owner.read_plan(write_plan(tmp_path,[r]))[0].priority==priority


@pytest.mark.parametrize('multibyte',[False,True])
def test_plan_schedule_key_boundary_uses_exact_utf8_bytes(tmp_path,multibyte):
    r=request();prefix=f'automatic-ingestion:v1:{r["kind"]}:'
    remaining=1024-len(prefix.encode('utf-8'))
    suffix=('株'*(remaining//3)+'x'*(remaining%3)) if multibyte else 'x'*remaining
    r['schedule_key']=prefix+suffix
    assert len(r['schedule_key'].encode('utf-8'))==1024
    assert owner.read_plan(write_plan(tmp_path,[r]))[0].schedule_key==r['schedule_key']
    r['schedule_key']+='x'
    with pytest.raises(ValueError,match='UTF-8'):owner.read_plan(write_plan(tmp_path,[r]))


def test_partial_legacy_start_still_registers_both_cleanup_owners(monkeypatch):
    events=[]
    monkeypatch.setattr(api,'start_automatic_ingestion',lambda:events.append('automatic-start'))
    monkeypatch.setattr(api,'stop_automatic_ingestion',lambda _:events.append('automatic-stop'))
    def legacy_start():events.append('legacy-start');raise RuntimeError('partial legacy start')
    monkeypatch.setattr(api.jobs,'start_hourly_quotes',legacy_start)
    monkeypatch.setattr(api.jobs,'stop_hourly_quotes',lambda:events.append('legacy-stop'))
    async def run():
        async with api._lifespan(api.app):pytest.fail('failed startup yielded')
    with pytest.raises(RuntimeError,match='partial legacy'):asyncio.run(run())
    assert events==['automatic-start','legacy-start','automatic-stop','legacy-stop']


@pytest.mark.parametrize('drain_error',[False,True])
def test_repeated_startup_cancellation_waits_for_native_owner_and_exposes_drain_failure(monkeypatch,drain_error):
    entered,release,stopped=threading.Event(),threading.Event(),threading.Event()
    assembly=object()
    def start():entered.set();assert release.wait(3);return assembly
    def stop(value):
        assert value is assembly;stopped.set()
        if drain_error:raise owner.IngestionDrainError('live ownership retained')
    monkeypatch.setattr(api,'start_automatic_ingestion',start)
    monkeypatch.setattr(api,'stop_automatic_ingestion',stop)
    monkeypatch.setattr(api.jobs,'start_hourly_quotes',lambda:pytest.fail('cancelled startup launched legacy'))
    async def scenario():
        async def lifetime():
            async with api._lifespan(api.app):pytest.fail('cancelled startup yielded')
        task=asyncio.create_task(lifetime())
        assert await asyncio.to_thread(entered.wait,3)
        task.cancel();await asyncio.sleep(0);task.cancel();release.set()
        with pytest.raises(owner.IngestionDrainError if drain_error else asyncio.CancelledError):await task
        assert stopped.is_set()
    asyncio.run(scenario())




@pytest.mark.parametrize('drain_error',[False,True])
def test_shutdown_finishes_legacy_cleanup_and_preserves_known_drain_failure(monkeypatch,drain_error):
    entered,release,finished=threading.Event(),threading.Event(),threading.Event()
    monkeypatch.setattr(api,'start_automatic_ingestion',object)
    monkeypatch.setattr(api.jobs,'start_hourly_quotes',lambda:None)
    def stop(_):
        if drain_error:raise owner.IngestionDrainError('live automatic ownership retained')
    def legacy_stop():
        entered.set();assert release.wait(3);finished.set()
    monkeypatch.setattr(api,'stop_automatic_ingestion',stop)
    monkeypatch.setattr(api.jobs,'stop_hourly_quotes',legacy_stop)
    async def run():
        async def lifetime():
            async with api._lifespan(api.app):pass
        task=asyncio.create_task(lifetime())
        assert await asyncio.to_thread(entered.wait,3)
        task.cancel();release.set()
        with pytest.raises(owner.IngestionDrainError if drain_error else asyncio.CancelledError):await task
        assert finished.is_set()
    asyncio.run(run())
