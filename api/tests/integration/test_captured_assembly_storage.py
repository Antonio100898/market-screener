"""Retained canonical inputs and genuine market bodies through disposable PG/S3."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from threading import Event
from time import sleep
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, update

from test_publication_storage import storage, pytestmark
from screener import store
from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.captured_assembly import CapturedCanonicalPreparer, CapturedPreparationHandler, PreparationUnavailable, engine_identity
from screener.company_import import CompanyImporter, _canonical_json, _read_only_sqlite
from screener.durable_jobs import DurableJobRepository, LeaseLost, RetryPolicy
from screener.job_runtime import JobContext
from screener.market_captures import retain_recipe, retain_response
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import active_data_release, durable_job, durable_job_item, data_release
from screener.shared_companies import SharedCompanyRepository
from screener.source_observations import SourceObservationRepository
from screener.storage_config import StorageSettings

ROOT=Path(__file__).parents[3]


def _owners(engine):
    settings=StorageSettings.from_env()
    objects=ImmutableObjectStore(create_s3_client(settings),settings.s3_bucket)
    artifacts=SqlArtifactRepository(engine)
    companies=SharedCompanyRepository(engine)
    return CapturedCanonicalPreparer(companies,artifacts,objects,SourceObservationRepository(engine))


def _capture(service, stamp):
    path=ROOT/f'tmp/verification-runtime/market-captures/manifest-{stamp}.json'
    assert path.exists(), 'genuine market capture fixture unavailable'
    records=[]
    for item in json.loads(path.read_bytes())['captures']:
        body=(path.parent/item['body_file']).read_bytes()
        assert len(body)==item['byte_size'] and hashlib.sha256(body).hexdigest()==item['sha256']
        records.append(retain_response(requested_url=item['requested_url'],response_url=item['response_url'],
            started_at=datetime.fromisoformat(item['started_at']),captured_at=datetime.fromisoformat(item['captured_at']),
            status_code=item['status_code'],media_type=item['media_type'],etag=item['etag'],last_modified=item['last_modified'],
            body=body,objects=service.objects,repository=service.artifacts))
    return retain_recipe(records,service.objects,service.artifacts).content_sha256


def _retain(service, value):
    return store_evidence(_canonical_json(value),'application/json',service.objects,service.artifacts).content_sha256


def _recipe(service, imported, *, market=None, listing=None):
    return _retain(service,dict(recipe_revision=1,engine_revision=store.ENGINE_VERSION,
        engine_identity_sha256=engine_identity(),members=[dict(security_id=imported.stored.security_id,
        snapshot_id=imported.stored.snapshot_id,market_recipe_sha256=market,jpx_listing_sha256=listing)]))


def _import(service, ticker):
    baseline=ROOT/'tmp/verification-runtime/baseline/screener.db'
    assert baseline.exists(), 'retained read-only baseline unavailable'
    with _read_only_sqlite(baseline) as conn:
        return CompanyImporter(conn,Path.home()/'.cache/graham-screener',service.objects,
            service.artifacts,service.companies).import_ticker(ticker)


def _context(engine, digest, *, key=None, duration=timedelta(minutes=5)):
    jobs=DurableJobRepository(engine,retry_policy=RetryPolicy(base_delay=timedelta(milliseconds=1),max_delay=timedelta(seconds=1),jitter_seconds=lambda _:0)); now=datetime.now(timezone.utc);key=key or uuid4().hex
    jobs.enqueue_scheduled(schedule_key=key,scheduled_for=now,kind='local-canonical-preparation',
        parameters={'recipe_sha256':digest},due_at=now)
    claimed=jobs.claim_next(owner=key,lease_duration=duration,allowed_kinds={'local-canonical-preparation'})
    assert claimed is not None
    return JobContext(claimed.job,Event(),jobs,claimed.lease,lambda:datetime.now(timezone.utc))


def test_genuine_seven_company_offline_preparation_repository_restart(storage,monkeypatch):
    engine=storage[0]; service=_owners(engine)
    us=_capture(service,'20261004T081958687901Z'); jp=_capture(service,'20261004T084347905669Z')
    jpx_meta=json.loads((ROOT/'local/captured-assembly-source-probes-2026-10-04/jpx-capture.json').read_text())
    workbook=(ROOT/'local/captured-assembly-source-probes-2026-10-04'/jpx_meta['body_file']).read_bytes()
    assert hashlib.sha256(workbook).hexdigest()==jpx_meta['sha256']
    listing=store_evidence(workbook,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',service.objects,service.artifacts).content_sha256
    imported={ticker:_import(service,ticker) for ticker in ('ABT','MFG','TRP','3028.T','6920.T','BRK-B','UHAL')}
    recipes={ticker:_recipe(service,item,market=None if ticker in ('BRK-B','UHAL') else jp if ticker.endswith('.T') else us,
        listing=listing if ticker.endswith('.T') else None) for ticker,item in imported.items()}
    def no_network(*args,**kwargs):raise AssertionError('offline preparation attempted live HTTP')
    monkeypatch.setattr('httpx.Client.get',no_network);monkeypatch.setattr('httpx.get',no_network)
    outputs={ticker:service.prepare(recipe) for ticker,recipe in recipes.items()}
    service.objects.client.close(); engine.dispose()
    restarted=_owners(engine)
    assert {ticker:restarted.prepare(recipe) for ticker,recipe in recipes.items()}==outputs
    report={'scope':'retained canonical-only preparation with explicit upstream gaps; no source-current/display publication claim',
        'engine':store.ENGINE_VERSION,'engine_identity_sha256':engine_identity(),'companies':{}}
    for ticker,digest in outputs.items():
        output=json.loads(restarted._read(digest));member=output['members'][0]
        exact=restarted.companies.snapshot(imported[ticker].stored.snapshot_id)
        strict=json.loads(restarted._read(member['canonical_artifact_sha256']))
        assert strict==exact.snapshot.canonical_payload
        assert output['official_financial_readiness']=='NOT_ASSESSED' and output['display_projection_status']=='NOT_BUILT'
        assert member['detail_projection_status']=='NOT_BUILT' and member['assumed_engine_artifact_sha256'] is not None
        if ticker.endswith('.T'):
            assert 'EDINET_CANONICAL_ONLY_ORIGINAL_LIST_AND_REMAP_UNVERIFIED' in member['upstream_gaps']
            assert 'JPX_CAPTURE_ASOF_IS_NOT_A_CURRENTNESS_GUARANTEE' in member['upstream_gaps']
        market=json.loads(restarted._read(member['market_artifact_sha256']))
        if ticker in ('BRK-B','UHAL'):
            assert market['status']=='UNAVAILABLE' and 'DERA_RAW_QUARTER_BINDING_UNVERIFIED' in member['upstream_gaps']
            assert strict['ttm_eps']==pytest.approx(39.75920686175043 if ticker=='BRK-B' else 0.24)
            assert next(c for c in strict['criteria'] if c['n']==4)['status']==('FAIL' if ticker=='BRK-B' else 'PASS')
        else:
            assert len(market['history']['closes'])==262
            if ticker in ('MFG','TRP'):assert len(market['fx']['closes'])==524
        report['companies'][ticker]={'preparation_sha256':digest,'recipe_sha256':recipes[ticker],
            'canonical_payload_sha256':exact.snapshot.payload_sha256,'upstream_gaps':member['upstream_gaps'],
            'market_requests':market['requests'],'closes':len((market.get('history') or {}).get('closes',[])),
            'repeat_after_new_clients_equal':True}
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(data_release))==0
        assert conn.scalar(select(active_data_release.c.release_id)) is None
    (ROOT/'local/captured-canonical-preparation-storage-proof-2026-10-04.json').write_text(json.dumps(report,indent=2)+'\n')


def test_durable_preparation_checkpoint_restart_and_input_conflict(storage,monkeypatch):
    engine=storage[0]; service=_owners(engine); imported=_import(service,'ABT');digest=_recipe(service,imported)
    context=_context(engine,digest);handler=CapturedPreparationHandler(service)
    first=handler(context)
    fresh=context._repository.save_checkpoint(context.lease,first)
    resumed=JobContext(fresh,Event(),context._repository,context.lease,context._clock)
    monkeypatch.setattr(service,'prepare',lambda _:(_ for _ in ()).throw(AssertionError('repeated completed preparation')))
    assert handler(resumed)==first
    bad=replace(fresh,checkpoint={**first,'recipe_sha256':'0'*64})
    with pytest.raises(ValueError,match='different inputs'):handler(replace(resumed,job=bad))
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(durable_job_item))==1


@pytest.mark.parametrize('transition',['expire','replace','stop'])
def test_expired_replaced_and_stopped_worker_cannot_checkpoint(storage,monkeypatch,transition):
    engine=storage[0];service=_owners(engine);item=_import(service,'ABT');digest=_recipe(service,item)
    context=_context(engine,digest)
    if transition=='stop':context.stopping.set()
    else:
        with engine.begin() as conn:
            conn.execute(update(durable_job).where(durable_job.c.job_id==context.job.job_id).values(
                lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
        if transition=='replace':
            context._repository.recover_expired()
            sleep(0.01)
            replacement=context._repository.claim_next(owner='replacement',lease_duration=timedelta(minutes=5),
                allowed_kinds={'local-canonical-preparation'})
            assert replacement.lease.generation>context.lease.generation
    monkeypatch.setattr(service,'prepare',lambda _:(_ for _ in ()).throw(AssertionError('expired worker prepared')))
    with pytest.raises(PreparationUnavailable if transition=='stop' else LeaseLost):CapturedPreparationHandler(service)(context)
    with engine.connect() as conn:
        assert conn.scalar(select(durable_job.c.checkpoint).where(durable_job.c.job_id==context.job.job_id)) is None
        assert conn.scalar(select(func.count()).select_from(durable_job_item))==0


def test_checkpoint_rejects_real_lock_wait_expiry_after_preparation(storage,monkeypatch):
    engine=storage[0];service=_owners(engine);item=_import(service,'ABT');digest=_recipe(service,item)
    context=_context(engine,digest);prepared=service.prepare(digest);entered=Event();reached=Event();release_prepared=Event()
    def prepared_then_wait(_):entered.set();assert release_prepared.wait(5);return prepared
    monkeypatch.setattr(service,'prepare',prepared_then_wait)
    blocker=engine.connect();transaction=None
    def observer(conn,cursor,statement,parameters,execution,many):
        if 'FROM durable_job ' in statement and 'FOR UPDATE' in statement:reached.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(CapturedPreparationHandler(service),context)
        assert entered.wait(5)
        with engine.begin() as conn:
            conn.execute(update(durable_job).where(durable_job.c.job_id==context.job.job_id).values(
                lease_expires_at=datetime.now(timezone.utc)+timedelta(seconds=0.5)))
        transaction=blocker.begin();blocker.execute(select(durable_job).where(durable_job.c.job_id==context.job.job_id).with_for_update()).one()
        event.listen(engine,'before_cursor_execute',observer)
        # Release the preparation hook so save_checkpoint reaches the locked job.
        release_prepared.set()
        assert reached.wait(5), "checkpoint did not reach blocked durable job lock"
        sleep(0.7);assert not future.done();transaction.commit()
        with pytest.raises(LeaseLost):future.result(timeout=5)
    event.remove(engine,'before_cursor_execute',observer);blocker.close()
    with engine.connect() as conn:
        assert conn.scalar(select(durable_job.c.checkpoint).where(durable_job.c.job_id==context.job.job_id)) is None


def test_missing_retained_class_sidecar_cannot_reproduce_selected_snapshot(storage):
    engine=storage[0];service=_owners(engine);original=_import(service,'BRK-B');old=service.companies.snapshot(original.stored.snapshot_id)
    context=_context(engine,'0'*64)
    snap=old.snapshot
    staged=service.companies.store_candidate(context.lease,issuer_source=old.issuer_source,
        issuer_identifier=old.issuer_identifier,security_identifier=old.security_identifier,
        ticker=snap.ticker,exchange_code=snap.exchange_code,quote_currency=snap.quote_currency,
        security_title=snap.security_title,source_accession=snap.source_accession,security_basis=snap.security_basis,
        receipt_ratio=snap.receipt_ratio,engine_revision=snap.engine_revision,canonical_payload=snap.canonical_payload,
        artifacts=tuple(link for link in old.artifacts if link.role!='dimensioned_facts'))
    damaged=replace(original,stored=staged)
    with pytest.raises(PreparationUnavailable,match='reproduce'):service.prepare(_recipe(service,damaged))
    assert service.companies.current(snap.security_id).snapshot_id==original.stored.snapshot_id


def test_replacement_after_preparation_cannot_commit_old_checkpoint(storage,monkeypatch):
    engine=storage[0];service=_owners(engine);item=_import(service,'ABT');digest=_recipe(service,item)
    context=_context(engine,digest); prepared=service.prepare(digest);replacement=[]
    def interrupted(_):
        with engine.begin() as conn:
            conn.execute(update(durable_job).where(durable_job.c.job_id==context.job.job_id)
                .values(lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
        context._repository.recover_expired();sleep(0.01)
        replacement.append(context._repository.claim_next(owner='new-worker',lease_duration=timedelta(minutes=5),
            allowed_kinds={'local-canonical-preparation'}))
        return prepared
    monkeypatch.setattr(service,'prepare',interrupted)
    with pytest.raises(LeaseLost):CapturedPreparationHandler(service)(context)
    assert replacement[0].lease.generation>context.lease.generation
    with engine.connect() as conn:
        assert conn.scalar(select(durable_job.c.checkpoint).where(durable_job.c.job_id==context.job.job_id)) is None
    fresh=JobContext(replacement[0].job,Event(),context._repository,replacement[0].lease,context._clock)
    monkeypatch.setattr(service,'prepare',lambda _:prepared)
    assert CapturedPreparationHandler(service)(fresh)['preparation_sha256']==prepared


def test_existing_durable_runtime_prepares_completes_and_does_not_reclaim_terminal(storage):
    from screener.job_runtime import DurableJobRuntime
    engine=storage[0];service=_owners(engine);item=_import(service,'ABT');digest=_recipe(service,item)
    jobs=DurableJobRepository(engine);now=datetime.now(timezone.utc)
    jobs.enqueue_scheduled(schedule_key='actual-runtime-preparation',scheduled_for=now,
        kind='local-canonical-preparation',parameters={'recipe_sha256':digest},due_at=now)
    runtime=DurableJobRuntime(jobs,lambda _now:(),{'local-canonical-preparation':CapturedPreparationHandler(service)},
        worker_count=1,lease_duration=timedelta(seconds=20),heartbeat_interval=timedelta(seconds=5))
    claimed=runtime._claim('fixture-runtime')
    assert claimed is not None
    runtime._run_claimed(claimed)
    with engine.connect() as conn:
        job=conn.execute(select(durable_job).where(durable_job.c.job_id==claimed.job.job_id)).mappings().one()
        assert job['status']=='succeeded' and job['checkpoint']['recipe_sha256']==digest
        assert conn.scalar(select(func.count()).select_from(durable_job_item))==1
    assert runtime._claim('fixture-runtime') is None
    assert not runtime.errors
