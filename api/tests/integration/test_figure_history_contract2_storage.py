import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from time import monotonic, sleep

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.durable_jobs import DurableJobRepository, LeaseLost, LeaseToken
from screener.figure_history import (DocumentEvidence, DocumentSpan, ExactFigureBundle,
    FigureHistoryConflict, FigureHistoryRepository, PreparedDocumentEvidence, SourceOccurrenceRef, Period, Scope, Unit, _digest)
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import (document_evidence, exact_figure_bundle, figure_bundle_evidence,
    figure_observation, figure_selection, company_snapshot, source_item_current, source_check_occurrence)
from screener.shared_companies import SharedCompanyRepository, SnapshotArtifact
from screener.source_observations import SourceItemIdentity, SourceObservationRepository
from screener.storage_config import StorageSettings
from tests.test_figure_history import example_bundle
from tests.test_figure_history_contract2 import document_bundle, basis_graph, assumed_graph
from test_figure_history_storage import bound_bundle
from test_publication_storage import NOW, _claim, _company, _source, storage

pytestmark = pytest.mark.skipif(os.getenv('RUN_STORAGE_INTEGRATION') != '1', reason='requires local PostgreSQL/S3')


def objects():
    settings=StorageSettings.from_env()
    return ImmutableObjectStore(create_s3_client(settings),settings.s3_bucket)


def prepared_bundle(storage, *, graph='document'):
    engine, artifact=storage
    body=b'publication source bytes: 100'
    engine, lease, company, bundle=bound_bundle(storage)
    bundle=document_bundle(bundle,body=body)
    if graph=='basis': bundle=basis_graph(bundle)
    if graph=='assumed': bundle=assumed_graph(bundle)
    repository=FigureHistoryRepository(engine,clock=lambda:NOW)
    prepared=repository.prepare_documents(bundle,objects())
    return engine,lease,company,bundle,repository,prepared


def test_genuinely_pre_extension_sealed_contract1_upgrade_and_replay(monkeypatch):
    proof_path=Path('../local/contract1-pre-extension-sealed-proof-2026-10-04.json')
    if not proof_path.exists():
        pytest.skip('Historical author-created0013 proof DB unavailable; portable literal fixture is independently tested')
    proof=json.loads(proof_path.read_text())
    name=proof['database_name']
    assert name.startswith('ms_contract1_legacy_') and name.isidentifier()
    url=make_url(StorageSettings.from_env().database_url).set(database=name)
    assert url.host=='127.0.0.1' and url.port==55432
    engine=create_engine(url)
    try:
        monkeypatch.setenv('SCREENER_DATABASE_URL',url.render_as_string(hide_password=False))
        command.upgrade(Config('alembic.ini'),'head')
        command.check(Config('alembic.ini'))
        repository=FigureHistoryRepository(engine,clock=lambda:NOW)
        bundle=repository.load(proof['bundle_id'])
        assert json.dumps(bundle.model_dump(mode='json'),sort_keys=True,separators=(',',':'),ensure_ascii=False)==proof['canonical_json']
        lease=LeaseToken(**proof['lease_token'])
        assert repository.store(lease,request_key='original-contract1',bundle=bundle)==proof['bundle_id']
        with engine.connect() as connection:
            assert connection.scalar(select(exact_figure_bundle.c.bundle_sha256))==proof['bundle_sha256']
            assert connection.scalar(select(figure_observation.c.record))==proof['observation_record']
            assert connection.scalar(select(exact_figure_bundle.c.contract_details))=={}
            assert connection.scalar(text('SELECT version_num FROM alembic_version'))==ScriptDirectory.from_config(Config('alembic.ini')).get_current_head()
    finally: engine.dispose()


@pytest.mark.parametrize('graph',['document','basis','assumed'])
def test_contract2_real_replay_restart_and_immutable_rows(storage,graph):
    engine,lease,_,bundle,repository,prepared=prepared_bundle(storage,graph=graph)
    identifier=repository.store(lease,request_key='v2',bundle=bundle,prepared_documents=prepared)
    assert repository.load(identifier)==bundle
    assert repository.store(lease,request_key='v2',bundle=bundle,prepared_documents=prepared)==identifier
    process=subprocess.run([sys.executable,'-c', '''
import sys
from screener.figure_history import FigureHistoryRepository
from screener.postgres import create_postgres_engine
engine=create_postgres_engine()
print(FigureHistoryRepository(engine).load(int(sys.argv[1])).model_dump_json())
engine.dispose()
''',str(identifier)],capture_output=True,text=True,check=True,timeout=15)
    assert ExactFigureBundle.model_validate_json(process.stdout)==bundle
    for statement in (document_evidence.delete(),update(document_evidence).values(record={}),
                      figure_bundle_evidence.delete(),update(exact_figure_bundle).values(contract_details={})):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection: connection.execute(statement)
    with pytest.raises(IntegrityError,match='sealed exact bundle'):
        with engine.begin() as connection:
            connection.execute(figure_bundle_evidence.insert().values(bundle_id=identifier,evidence_key='late',evidence_id=1))


@pytest.mark.parametrize('problem',['offset','hash','unregistered','foreign-artifact','issuer','system','source-document','missing-source','missing-doc-source','forged-prepared','changed-after-prepare'])
def test_document_preparation_and_actual_binding_adversaries(storage,problem):
    engine,lease,_,bundle,repository,prepared=prepared_bundle(storage)
    document=bundle.document_evidence[0]
    reference=document.reference
    if problem in ('offset','hash'):
        span=reference.location.model_copy(update={'end_byte':99999} if problem=='offset' else {'span_sha256':'c'*64})
        reference=reference.model_copy(update={'location':span})
    elif problem=='unregistered': reference=reference.model_copy(update={'artifact_sha256':'c'*64})
    elif problem=='foreign-artifact':
        body=b'publication source bytes: 200'
        artifact=store_evidence(body,'text/html',objects(),SqlArtifactRepository(engine))
        reference=reference.model_copy(update={'artifact_sha256':artifact.content_sha256,
            'location':reference.location.model_copy(update={'span_sha256':hashlib.sha256(body).hexdigest()})})
    elif problem=='issuer': reference=reference.model_copy(update={'issuer_source_identifier':'9999'})
    elif problem=='system': reference=reference.model_copy(update={'source_system':'EDINET'})
    elif problem=='source-document': reference=reference.model_copy(update={'source_document':'https://example.test/forged'})
    elif problem=='changed-after-prepare': reference=reference.model_copy(update={'extraction_revision':'changed'})
    bundle=ExactFigureBundle.model_validate(bundle.model_copy(update={'document_evidence':(document.model_copy(update={'reference':reference}),)}))
    if problem=='missing-source': bundle=bundle.model_copy(update={'coverage':(bundle.coverage[0].model_copy(update={'assessment':'PARTIAL','reason':'test-gap'}),),'sources':()})
    if problem=='missing-doc-source':
        selected=bundle.selections[0].model_copy(update={'state':'NOT_ASSESSED','observation_key':None,
            'value_decimal_text':None,'reason':'document-only'})
        bundle=bundle.model_copy(update={'observations':(), 'selections':(selected,), 'sources':(),
            'coverage':(bundle.coverage[0].model_copy(update={'assessment':'NOT_ASSESSED','reason':'document-only'}),)})
    with pytest.raises(ValueError):
        if problem in ('offset','hash','unregistered'):
            repository.prepare_documents(bundle,objects())
        else:
            if problem not in ('forged-prepared','changed-after-prepare'): prepared=repository.prepare_documents(bundle,objects())
            if problem=='forged-prepared': prepared=PreparedDocumentEvidence(prepared.record_sha256,b'forged')
            repository.store(lease,request_key='forged',bundle=bundle,prepared_documents=prepared)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(exact_figure_bundle))==0
        assert connection.scalar(select(func.count()).select_from(document_evidence))==0


def test_preparation_has_no_sql_connection_during_s3_read_and_cannot_skip_verification(storage):
    engine,lease,_,bundle,repository,_=prepared_bundle(storage)
    backing=objects()
    class CheckOutsideSQL:
        def read_verified(self,*args):
            assert engine.pool.checkedout()==0
            return backing.read_verified(*args)
    prepared=repository.prepare_documents(bundle,CheckOutsideSQL())
    with pytest.raises(ValueError,match='outside SQL'):
        repository.store(lease,request_key='missing',bundle=bundle)
    assert repository.store(lease,request_key='verified',bundle=bundle,prepared_documents=prepared)>0


def test_catalog_replay_conflict_and_atomic_caller_rollback(storage):
    engine,lease,_,bundle,repository,prepared=prepared_bundle(storage)
    first=repository.store(lease,request_key='one',bundle=bundle,prepared_documents=prepared)
    with pytest.raises(RuntimeError,match='rollback'):
        with engine.begin() as connection:
            repository.store_on_connection(connection,lease,request_key='rolledback',bundle=bundle,prepared_documents=prepared)
            raise RuntimeError('rollback')
    changed=bundle.model_copy(update={'document_evidence':(bundle.document_evidence[0].model_copy(update={'roles':('RECEIPT_TERMS',)}),)})
    verified=repository.prepare_documents(changed,objects())
    with pytest.raises(FigureHistoryConflict,match='different evidence'):
        repository.store(lease,request_key='conflict',bundle=changed,prepared_documents=verified)
    with pytest.raises(FigureHistoryConflict,match='different exact bundle'):
        repository.store(lease,request_key='one',bundle=changed,prepared_documents=verified)
    with engine.connect() as connection:
        for table in (exact_figure_bundle,document_evidence,figure_bundle_evidence):
            assert connection.scalar(select(func.count()).select_from(table))==1
    assert repository.load(first)==bundle


def test_output_share_kind_cannot_contradict_actual_snapshot(storage):
    engine,lease,_,bundle,repository,_=prepared_bundle(storage,graph='basis')
    selected=tuple(n.model_copy(update={'share_basis':n.share_basis.model_copy(update={'kind':'RECEIPT'})})
                   if n.purpose=='OUTPUT' else n for n in bundle.selections)
    bundle=ExactFigureBundle.model_validate(bundle.model_copy(update={'selections':selected}))
    prepared=repository.prepare_documents(bundle,objects())
    with pytest.raises(ValueError,match='output share kind'):
        repository.store(lease,request_key='wrong-final-kind',bundle=bundle,prepared_documents=prepared)


def test_document_a_b_a_keeps_catalog_identity_and_check3_witness(storage):
    engine,lease,_,bundle,repository,prepared=prepared_bundle(storage)
    first=repository.store(lease,request_key='A1',bundle=bundle,prepared_documents=prepared)
    owner=SourceObservationRepository(engine,clock=lambda:NOW)
    item=SourceItemIdentity('SEC','filing','filing-1','0000001800')
    owner.record(lease,item=item,item_outcome_key='B',state='pending',metadata={'revision':2},
        source_url='https://www.sec.gov/filing-1',detected_at=NOW+timedelta(seconds=1))
    owner.record(lease,item=item,item_outcome_key='A3',state='present',metadata={'revision':1},
        artifact_sha256=bundle.document_evidence[0].reference.artifact_sha256,
        source_url='https://www.sec.gov/filing-1',detected_at=NOW+timedelta(seconds=2))
    with engine.connect() as connection: pointer=connection.execute(select(source_item_current)).mappings().one()
    assert pointer['source_observation_id']==bundle.sources[0].source_observation_id
    assert pointer['occurrence_id']!=bundle.sources[0].occurrence_id
    again=bundle.model_copy(update={'sources':(bundle.sources[0].model_copy(update={'occurrence_id':pointer['occurrence_id']}),)})
    third=repository.store(lease,request_key='A3',bundle=again,prepared_documents=prepared)
    assert repository.load(first).sources!=repository.load(third).sources
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(document_evidence))==1
        assert connection.scalar(select(func.count()).select_from(source_check_occurrence))==3


def test_two_live_workers_replay_one_document_bundle(storage):
    engine,lease,_,bundle,repository,prepared=prepared_bundle(storage)
    other=_claim(engine,'second-document-worker')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(repository.store,lease,request_key='concurrent',bundle=bundle,prepared_documents=prepared)
        second=pool.submit(repository.store,other,request_key='concurrent',bundle=bundle,prepared_documents=prepared)
        assert first.result(timeout=10)==second.result(timeout=10)
    with engine.connect() as connection:
        for table in (document_evidence,exact_figure_bundle,figure_bundle_evidence):
            assert connection.scalar(select(func.count()).select_from(table))==1


def test_many_document_bindings_load_in_one_batch(storage):
    engine,lease,_,bundle,repository,_=prepared_bundle(storage)
    document=bundle.document_evidence[0]
    body=b'publication source bytes: 100'
    docs=tuple(document.model_copy(update={'key':f'doc-{i:03}', 'reference':document.reference.model_copy(update={
        'extraction_revision':f'annotated-{i}'})}) for i in range(200))
    bundle=ExactFigureBundle.model_validate(bundle.model_copy(update={'document_evidence':docs}))
    prepared=repository.prepare_documents(bundle,objects())
    identifier=repository.store(lease,request_key='batch',bundle=bundle,prepared_documents=prepared)
    statements=[]
    def observe(*args): statements.append(args[2])
    event.listen(engine,'before_cursor_execute',observe)
    try: assert repository.load(identifier)==bundle
    finally: event.remove(engine,'before_cursor_execute',observe)
    assert len(statements)==7


def test_database_rejects_duplicate_output_period_view_without_blocking_intermediate(storage):
    engine,lease,_,bundle,repository,prepared=prepared_bundle(storage)
    identifier=repository.store(lease,request_key='original-output',bundle=bundle,prepared_documents=prepared)
    with engine.connect() as connection:
        parent=dict(connection.execute(select(exact_figure_bundle).where(exact_figure_bundle.c.bundle_id==identifier)).mappings().one())
        node=dict(connection.execute(select(figure_selection).where(figure_selection.c.bundle_id==identifier)).mappings().one())
    for k in ('bundle_id','created_at','sealed_at'):parent.pop(k)
    parent['request_key']='duplicate-output-fixture'
    with pytest.raises(IntegrityError,match='uq_figure_selection_output'):
        with engine.begin() as connection:
            new_id=connection.scalar(exact_figure_bundle.insert().values(**parent).returning(exact_figure_bundle.c.bundle_id))
            node['bundle_id']=new_id
            connection.execute(figure_selection.insert().values(**node))
            node['node_key']='other';node['record']={**node['record'],'node_key':'other'}
            connection.execute(figure_selection.insert().values(**node))


@pytest.mark.parametrize('key',['2016_PRE_ACTION','2023_ACTION','2025_2026_ACTIONS','2026_JULY_ACTION','2026_ALL_INCORPORATED','NVO_RECEIPT_COVER'])
def test_retained_genuine_ftft_nvo_spans_via_existing_s3_source_snapshot_owners(storage,key):
    manifest=Path('../local/exact-history-contract-anchor-evidence-2026-10-04.json')
    if not manifest.exists(): pytest.skip('Retained machine-local filing proof unavailable')
    anchor=next(a for a in json.loads(manifest.read_text(encoding='utf-8'))['anchors'] if a['key']==key)
    body=Path(anchor['path']).read_bytes()
    assert hashlib.sha256(body).hexdigest()==anchor['artifact_sha256']
    engine,_=storage; lease=_claim(engine,'real-document')
    artifact=store_evidence(body,'text/html',objects(),SqlArtifactRepository(engine))
    cik='0000353278' if key=='NVO_RECEIPT_COVER' else '0001066923'
    ticker='NVO' if key=='NVO_RECEIPT_COVER' else 'FTFT'
    company=SharedCompanyRepository(engine,clock=lambda:NOW).store_candidate(lease,issuer_source='SEC',issuer_identifier=cik,
        security_identifier=f'sec:{cik}:primary',ticker=ticker,security_title='Captured documentary proof',
        security_basis='PRIMARY_COMMON_SHARE',source_accession=anchor['source_document'],engine_revision=187,
        canonical_payload={'proof_scope':'document anchors only; financial emission NOT_ASSESSED'},
        artifacts=(SnapshotArtifact(artifact.content_sha256,'raw_filing'),))
    source=SourceObservationRepository(engine,clock=lambda:NOW).record(lease,item=SourceItemIdentity('SEC','filing',anchor['source_document'],cik),
        item_outcome_key='document',state='present',metadata={},artifact_sha256=artifact.content_sha256,source_url=anchor['source_document'])
    with engine.connect() as connection:
        pointer=connection.execute(select(source_item_current)).mappings().one()
        snapshot_hash=connection.scalar(select(company_snapshot.c.snapshot_sha256))
    template=example_bundle(security_id=company.security_id,snapshot_id=company.snapshot_id,snapshot_sha256=snapshot_hash,
        artifact_sha256=artifact.content_sha256,source_item_id=pointer['source_item_id'],
        source_observation_id=source.observation.source_observation_id,occurrence_id=pointer['occurrence_id'])
    role='RECEIPT_TERMS' if key=='NVO_RECEIPT_COVER' else 'INCORPORATED_BASIS' if key in ('2016_PRE_ACTION','2026_ALL_INCORPORATED') else 'LEGAL_ACTION'
    reference=SourceOccurrenceRef(artifact_sha256=artifact.content_sha256,extraction_revision='raw-document-proof-v2',source_system='SEC',
        issuer_source_identifier=cik,source_document=anchor['source_document'],fidelity='EXACT_LEXICAL',
        location=DocumentSpan(start_byte=anchor['byte_start'],end_byte=anchor['byte_end'],span_sha256=anchor['span_sha256']))
    selected=template.selections[0].model_copy(update={'state':'NOT_ASSESSED','observation_key':None,'value_decimal_text':None,
        'reason':'Financial source companion not emitted', 'unit':Unit(status='UNKNOWN'),
        'period':Period(kind='UNKNOWN',origin='UNKNOWN'),
        'scope':Scope(entity_identifier=cik,entity_scheme='http://www.sec.gov/CIK',consolidation='UNKNOWN')})
    bundle=ExactFigureBundle.model_validate(template.model_copy(update={'contract_revision':2,'observations':(), 'selections':(selected,),
        'coverage':(template.coverage[0].model_copy(update={'assessment':'NOT_ASSESSED','reason':'document-only proof'}),),
        'document_evidence':(DocumentEvidence(key=key,reference=reference,roles=(role,)),)}))
    repository=FigureHistoryRepository(engine,clock=lambda:NOW)
    prepared=repository.prepare_documents(bundle,objects())
    identifier=repository.store(lease,request_key=key,bundle=bundle,prepared_documents=prepared)
    assert repository.load(identifier)==bundle
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(figure_observation))==0


def test_real_document_catalog_wait_expiry_rolls_back_entire_bundle(storage):
    engine,artifact=storage
    now=datetime.now(timezone.utc); jobs=DurableJobRepository(engine)
    jobs.enqueue_scheduled(schedule_key='doc-expiry',scheduled_for=now,kind='doc-expiry',parameters={},due_at=now)
    claim=jobs.claim_next(owner='doc-expiry',lease_duration=timedelta(seconds=2.5),allowed_kinds={'doc-expiry'})
    assert claim
    company=_company(engine,artifact,lease=claim.lease); source_id=_source(engine,artifact,claim.lease)
    with engine.connect() as connection:
        pointer=connection.execute(select(source_item_current)).mappings().one()
        snapshot_hash=connection.scalar(select(company_snapshot.c.snapshot_sha256))
    bundle=document_bundle(example_bundle(security_id=company.security_id,snapshot_id=company.snapshot_id,snapshot_sha256=snapshot_hash,
        artifact_sha256=artifact.content_sha256,source_item_id=pointer['source_item_id'],source_observation_id=source_id,occurrence_id=pointer['occurrence_id']),
        body=b'publication source bytes: 100')
    repository=FigureHistoryRepository(engine); prepared=repository.prepare_documents(bundle,objects())
    blocker=engine.connect(); tx=blocker.begin()
    doc=bundle.document_evidence[0]; record=doc.model_dump(mode='json');record.pop('key');span=doc.reference.location
    blocker.execute(document_evidence.insert().values(issuer_id=company.issuer_id,artifact_sha256=artifact.content_sha256,
        extraction_revision=doc.reference.extraction_revision,location_sha256=_digest(span.model_dump(mode='json')),
        start_byte=span.start_byte,end_byte=span.end_byte,span_sha256=span.span_sha256,record=record))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(repository.store,claim.lease,request_key='expired',bundle=bundle,prepared_documents=prepared)
            deadline=monotonic()+1.5
            with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as observer:
                while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
                    assert not future.done() and monotonic()<deadline
                    sleep(.01)
            while datetime.now(timezone.utc)<=claim.job.lease_expires_at+timedelta(seconds=.1):
                assert not future.done();sleep(.02)
            tx.rollback()
            with pytest.raises(LeaseLost): future.result(timeout=4)
    finally:
        if tx.is_active:tx.rollback()
        blocker.close()
    with engine.connect() as connection:
        for table in (exact_figure_bundle,document_evidence,figure_bundle_evidence,figure_observation,figure_selection):
            assert connection.scalar(select(func.count()).select_from(table))==0
