"""Synthetic association mechanics. No Snapshot, SQL, HTTP or financial proof."""
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import threading
from types import SimpleNamespace as NS

import pytest

from screener import complete_input_plan as owner
from screener.artifacts import store_evidence
from screener.source_validity import SourceDeliveryRecord
from test_artifacts import MemoryRepository
from screener.object_store import content_address, VerifiedObject

NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


class Objects:
    def __init__(self): self.rows = {}; self.reads = {}
    def put_verified(self, raw, media_type):
        digest, key = content_address(raw); self.rows[digest] = raw
        return VerifiedObject(digest, key, len(raw), NOW)
    def read_verified(self, object_key, digest, byte_size):
        assert object_key == f"raw/sha256/{digest}"
        self.reads[digest] = self.reads.get(digest, 0) + 1
        raw = self.rows[digest]
        assert len(raw) == byte_size and hashlib.sha256(raw).hexdigest() == digest
        return raw


class Artifacts(MemoryRepository):
    def get(self, sha): return self.rows[sha]


@pytest.fixture
def fixture(monkeypatch):
    monkeypatch.setattr(owner, 'resolver_identity', lambda: 'b'*64)
    monkeypatch.setattr(owner.captured_assembly, 'engine_identity', lambda: 'a'*64)
    objects, artifacts = Objects(), Artifacts()
    def put(value):
        raw = value if isinstance(value, bytes) else owner.encode(value)
        return store_evidence(raw, 'application/json', objects, artifacts).content_sha256
    body = put(b'retained synthetic input')
    delivery = SourceDeliveryRecord(11, 12, 13, 14, 15, 'root:company_facts', NOW)
    source = dict(observation_sha256='c'*64, item_kind='aggregate', source_key='companyfacts/0000000001',parent_source_item_id=None,role='company_facts',artifact_byte_size=len(objects.rows[body]),captured_source=dict(role='company_facts',artifact_sha256=body,source_document='https://data.sec.gov/fixture',source_item_id=12,source_observation_id=13,occurrence_id=11,fidelity='EXACT_LEXICAL'), artifact_sha256=body, source_url='https://data.sec.gov/fixture', state='present',
        delivery=dict(occurrence_id=11, source_item_id=12, source_observation_id=13, job_id=14,
            ownership_generation=15, item_key='root:company_facts', checked_at=NOW.isoformat()))
    stored = NS(item=NS(source_system='SEC', issuer_source_identifier='0000000001', source_item_id=12,item_kind='aggregate',source_key='companyfacts/0000000001',parent_source_item_id=None),
        observation=NS(source_observation_id=13, source_item_id=12, observation_sha256='c'*64,
            artifact_sha256=body, state='present', source_url=source['source_url'], canonical_metadata={'role':'company_facts','capture_revision':2,'capture_fidelity':'RAW_HTTP_BYTES'}))
    observations = NS(by_sha256=lambda sha: stored if sha == 'c'*64 else (_ for _ in ()).throw(ValueError('missing source')),
        delivery=lambda *a, **kw: delivery)
    company = NS(issuer_source='SEC', issuer_identifier='0000000001', artifacts=(NS(content_sha256=body),),
        snapshot=NS(snapshot_sha256='d'*64))
    companies = NS(snapshot=lambda id: company if id == 2 else None,
        current=lambda security_id, *, release_id: NS(snapshot_id=2))
    binding = dict(security_id=1, snapshot_id=2, exact_bundle_id=None)
    accounting = dict(accounting_revision=1, expected_parent_release_id=None, engine_identity_sha256='a'*64,
        engine_identity_status='DECLARED_NOT_VERIFIED', universe=[dict(key='input', artifact_sha256s=[body])],
        outcomes=[dict(input_key='input', status='SELECTED', selection=binding)], parent_members=[],
        selected_members=[dict(**binding, disposition='NEW', candidate_id=3)], retirements=[])
    recipe_value = dict(recipe_revision=1, engine_revision=191, engine_identity_sha256='a'*64,
        members=[dict(security_id=1, snapshot_id=2)])
    recipe = put(recipe_value)
    member = dict(security_id=1, snapshot_id=2, snapshot_sha256='d'*64,
        source_observations=[dict(source_item_id=12, source_observation_id=13)])
    prep_value = dict(preparation_revision=1, engine_revision=191, engine_identity_sha256='a'*64, members=[member])
    prep = put(prep_value)
    verifies = []
    canonical = NS(verify=lambda p, r: verifies.append((p,r)) or prep_value,
        prepare=lambda *a: pytest.fail('canonical prepare forbidden'))
    candidate = NS(security_id=1,snapshot_id=2,expected_parent_release_id=None,exact_bundle_id=None,source_observation_ids=(13,))
    publications = NS(candidate=lambda id: candidate if id==3 else None, release=lambda id:NS(member_count=1))
    resolver = owner.CompleteInputResolver(canonical, artifacts, objects, observations, companies, NS(), publications)
    record = owner.load(owner.InputRecord, owner.encode(dict(ordinal=0,input_key='input',issuer=dict(source='SEC',identifier='0000000001'),
        sources=[source],canonical=dict(recipe_sha256=recipe,preparation_sha256=prep,member_ordinal=0),
        exact_bundle=None,submissions=None,parent_state=None,gaps=['SYNTHETIC_INPUT_NOT_FINANCIAL_PROOF'])))
    def plan(record_value=None, accounting_value=None, preparations=None):
        return resolver.record_plan(accounting_sha256=put(accounting_value or accounting), records=[record_value or record],
            preparations=preparations or [dict(recipe_sha256=recipe,preparation_sha256=prep)],
            operation_at=NOW.isoformat(), export_mode='FULL',global_listing=dict(status='UNAVAILABLE',artifact_sha256=None,reason='No registered listing capture'),global_indexes=[dict(name=n,status='UNAVAILABLE',artifact_sha256=None,reason='No registered index capture') for n in ('S&P 500','DJIA','Nasdaq 100')],gaps=['TEST_ONLY'])
    return NS(**locals())


def test_integrated_producer_resolve_and_immutable_recovery(fixture, monkeypatch):
    f=fixture; plan=f.plan(); resolution=f.resolver.resolve(plan)
    monkeypatch.setattr(owner, 'resolve_submissions', lambda *a, **k:pytest.fail('metadata reselection'))
    f.observations.latest_sec_filing=lambda *a:pytest.fail('current lookup')
    output=f.resolver.verify(resolution,plan)
    assert output.scope=='RESOLVER_ONLY' and output.financial_status=='NOT_ASSESSED'
    assert output.engine_identity_status=='DECLARED_NOT_VERIFIED' and output.display_status=='NOT_BUILT'
    assert output.inputs[0].input.sources[0].delivery.occurrence_id==11
    assert f.verifies==[(f.prep,f.recipe)]*3
    assert f.objects.reads[f.body]==3 # once per SHA per producer/resolve/replay


@pytest.mark.parametrize('change',[
    lambda r:r.update(ordinal=0.0),lambda r:r.update(ordinal=False),lambda r:r.update(extra='bad'),
    lambda r:r['sources'][0]['delivery'].update(job_id=14.0),lambda r:r['sources'][0]['delivery'].update(ownership_generation=True),
    lambda r:r['canonical'].update(member_ordinal=True),lambda r:r['issuer'].update(source='YAHOO'),
    lambda r:r.update(sources=r['sources']*2),lambda r:r.update(input_key='bad\0key'),
])
def test_strict_record_admission(fixture,change):
    r=fixture.record.model_dump(mode='json');change(r)
    with pytest.raises(ValueError):owner.load(owner.InputRecord,owner.encode(r))


@pytest.mark.parametrize('field',['occurrence_id','source_item_id','source_observation_id','job_id','ownership_generation','item_key','checked_at'])
def test_all_seven_delivery_fields_bind_actual_owner(fixture,field):
    f=fixture;r=f.record.model_dump(mode='json');d=r['sources'][0]['delivery']
    d[field]=d[field]+1 if isinstance(d[field],int) else ('different' if field=='item_key' else '2026-10-06T00:00:00Z')
    if field in ('occurrence_id','source_item_id','source_observation_id'):r['sources'][0]['captured_source'][field]=d[field]
    with pytest.raises(ValueError,match='source|delivery'):
        f.plan(owner.load(owner.InputRecord,owner.encode(r)))
    assert f.verifies == [] # exact delivery admission precedes canonical parsing


@pytest.mark.parametrize('change',[
    lambda f:setattr(f.candidate,'snapshot_id',99),lambda f:setattr(f.candidate,'security_id',99),
    lambda f:setattr(f.candidate,'expected_parent_release_id',99),lambda f:setattr(f.candidate,'exact_bundle_id',99),
    lambda f:setattr(f.candidate,'source_observation_ids',(99,)),lambda f:setattr(f.company,'issuer_identifier','2'),
    lambda f:setattr(f.company.snapshot,'snapshot_sha256','f'*64),
])
def test_actual_candidate_snapshot_source_associations(fixture,change):
    change(fixture)
    with pytest.raises(ValueError):fixture.plan()


def test_omitted_extra_inputs_and_unconsumed_members(fixture):
    f=fixture;a=json.loads(json.dumps(f.accounting));a['universe'].append(dict(key='extra',artifact_sha256s=[f.body]));a['outcomes'].append(dict(input_key='extra',status='UNRESOLVED',reason='fixture unavailable'))
    with pytest.raises(ValueError,match='universe'):f.plan(accounting_value=a)
    f.prep_value['members'].append(f.member)
    f.canonical.verify=lambda *a:f.prep_value
    with pytest.raises(ValueError,match='unconsumed'):f.plan()


@pytest.mark.parametrize('field',['recipe_revision','engine_revision'])
@pytest.mark.parametrize('bad',[True,1.0])
def test_new_consumer_does_not_inherit_equal_scalar_recipe_admission(fixture,field,bad):
    f=fixture;recipe=dict(f.recipe_value);recipe[field]=bad;sha=f.put(recipe)
    r=f.record.model_dump(mode='json');r['canonical']['recipe_sha256']=sha
    with pytest.raises(ValueError,match='integer'):
        f.plan(owner.load(owner.InputRecord,owner.encode(r)),preparations=[dict(recipe_sha256=sha,preparation_sha256=f.prep)])


def test_chunk_ranges_hash_size_and_root_types(fixture):
    f=fixture;sha=f.plan();raw=f.objects.rows[sha];plan=json.loads(raw)
    for key,bad in [('start',0.0),('count',True),('byte_size',1),('ordinal',1)]:
        v=json.loads(raw);v['chunks'][0][key]=bad
        with pytest.raises(ValueError):f.resolver.resolve(f.put(v))
    with pytest.raises(ValueError,match='duplicate'):owner.load(owner.CompleteInputPlan,raw.replace(b'"contract":',b'"contract":"COMPLETE_INPUT_PLAN1","contract":',1))
    with pytest.raises(ValueError):owner.load(owner.CompleteInputPlan,b'{"x":"\xff"}')


def test_large_complete_byte_packing_preserves_7259_and_distinct_deliveries(fixture):
    f=fixture;records=[];universe=[];outcomes=[]
    for i in range(7259):
        r=f.record.model_dump(mode='json');r.update(ordinal=i,input_key=f'{i}:雪'*8,canonical=None)
        r['sources'][0]['delivery']['occurrence_id']=i+100
        r['sources'][0]['captured_source']['occurrence_id']=i+100
        records.append(owner.load(owner.InputRecord,owner.encode(r)))
        universe.append(dict(key=r['input_key'],artifact_sha256s=[f.body]))
        outcomes.append(dict(input_key=r['input_key'],status='UNRESOLVED',reason='Synthetic preserved unavailable evidence'))
    f.observations.delivery=lambda id,**kw:replace(f.delivery,occurrence_id=id)
    accounting=dict(f.accounting,universe=universe,outcomes=outcomes,selected_members=[])
    root=f.resolver.record_plan(accounting_sha256=f.put(accounting),records=records,preparations=[],operation_at=NOW.isoformat(),export_mode='FULL',global_listing=dict(status='UNAVAILABLE',artifact_sha256=None,reason='fixture'),global_indexes=[dict(name=n,status='UNAVAILABLE',artifact_sha256=None,reason='fixture') for n in ('S&P 500','DJIA','Nasdaq 100')],gaps=['SYNTHETIC'])
    plan=owner.load(owner.CompleteInputPlan,f.objects.rows[root]);assert len(plan.chunks)>1
    assert all(c.byte_size<=owner.MAX_ENVELOPE_BYTES for c in plan.chunks)
    resolution=f.resolver.resolve(root);output=f.resolver.verify(resolution,root)
    assert len(output.inputs)==7259
    assert len({r.input.sources[0].delivery.occurrence_id for r in output.inputs})==7259
    assert f.objects.reads[f.body]==3*7259 # bounded per-input reads, no cohort raw cache
    print(json.dumps(dict(synthetic_inputs=7259, chunks=len(plan.chunks),
        root_bytes=len(f.objects.rows[root]), chunk_bytes=sum(c.byte_size for c in plan.chunks),
        resolution_bytes=len(f.objects.rows[resolution]), shared_source_body_bytes=len(f.objects.rows[f.body]),
        source_reads=f.objects.reads[f.body], cohort_raw_cache_entries=0,
        financial_capacity="NOT_ASSESSED"),sort_keys=True))


def test_one_oversized_unicode_record_refuses_without_truncation(fixture):
    r=fixture.record.model_dump(mode='json');r['input_key']='雪'*(owner.MAX_ENVELOPE_BYTES//3+1)
    with pytest.raises(ValueError,match='1MiB'):list(owner.pack_chunks([owner.load(owner.InputRecord,owner.encode(r))]))


def test_build_drift_before_and_after_resolution(fixture,monkeypatch):
    f=fixture;plan=f.plan();monkeypatch.setattr(owner,'resolver_identity',lambda:'f'*64)
    with pytest.raises(owner.JobDeferred):f.resolver.resolve(plan)
    monkeypatch.setattr(owner,'resolver_identity',lambda:'b'*64)
    f.canonical.verify=lambda *a:monkeypatch.setattr(owner,'resolver_identity',lambda:'f'*64) or f.prep_value
    with pytest.raises(owner.JobDeferred,match='changed'):f.resolver.resolve(plan)


def test_actual_resolver_identity_includes_helper_sources(monkeypatch):
    monkeypatch.undo()
    first=owner.resolver_identity();assert len(first)==64
    monkeypatch.setattr(owner.captured_assembly,'engine_identity',lambda:'f'*64)
    with pytest.raises(owner.JobDeferred,match='fresh'):owner.resolver_identity()


def test_resolution_replay_rejects_altered_same_key_identity(fixture):
    f=fixture;p=f.plan();r=f.resolver.resolve(p);v=json.loads(f.objects.rows[r])
    for field in ['complete_plan_sha256','accounting_sha256','resolver_build_sha256','canonical_engine_sha256']:
        altered=dict(v);altered[field]='f'*64
        with pytest.raises(ValueError):f.resolver.verify(f.put(altered),p)


def test_actual_handler_checkpoint_recovery_and_final_fences(fixture):
    f=fixture;p=f.plan();r=f.resolver.resolve(p);events=[]
    context=NS(job=NS(parameters={'complete_plan_sha256':p},checkpoint={'complete_plan_sha256':p,'resolution_sha256':r,'resolver_build_sha256':'b'*64}),
        stopping=threading.Event(),lease='test',_clock=lambda:NOW,
        _repository=NS(engine=NS(begin=lambda:nullcontext('connection')),
            _lock_live_lease=lambda *a:events.append('lease')),
        save_checkpoint=lambda value:events.append('checkpoint'),record_item_outcome=lambda **kw:events.append(kw['item_key']))
    f.resolver.resolve=lambda *a:pytest.fail('completed recovery resolved again')
    assert owner.CompleteInputResolutionHandler(f.resolver)(context)==context.job.checkpoint
    assert events==['lease','checkpoint','complete-input-resolution']
    context.save_checkpoint=lambda value:(_ for _ in ()).throw(owner.JobDeferred('late lease failure'))
    with pytest.raises(owner.JobDeferred):owner.CompleteInputResolutionHandler(f.resolver)(context)
    assert events[-1]=='lease'


def test_plan_finite_kind_and_actual_runtime_registration(monkeypatch,tmp_path):
    from screener import automatic_ingestion as automatic
    from test_automatic_ingestion import assembled, request, write_plan
    current,events,resources=assembled(monkeypatch,tmp_path)
    r=request('local-complete-input-resolution');r['parameters']={'complete_plan_sha256':'a'*64}
    write_plan(tmp_path,[r]);assembly=automatic.start_automatic_ingestion(current)
    try:
        handler=assembly.runtime.handlers['local-complete-input-resolution']
        assert isinstance(handler,owner.CompleteInputResolutionHandler)
        assert handler.resolver.canonical is assembly.runtime.handlers['local-canonical-preparation'].preparer
        assert assembly.runtime.source(NOW)[0].parameters==r['parameters']
    finally:automatic.stop_automatic_ingestion(assembly)
    r['parameters']={'recipe_sha256':'a'*64};write_plan(tmp_path,[r])
    with pytest.raises(ValueError):automatic.read_plan(current.plan_path)


@pytest.mark.parametrize('change',[
    lambda r:r['sources'][0].update(role='different'),lambda r:r['sources'][0].update(source_key='different'),
    lambda r:r['sources'][0].update(item_kind='filing'),lambda r:r['sources'][0].update(parent_source_item_id=9),
    lambda r:r['sources'][0].update(artifact_byte_size=1),
    lambda r:r['sources'][0]['captured_source'].update(fidelity='EXACT_LEXICAL'),
])
def test_full_source_item_role_size_binding(fixture,change):
    f=fixture;r=f.record.model_dump(mode='json');change(r)
    if r['sources'][0]['role']=='different':r['sources'][0]['captured_source']['role']='different'
    if r['sources'][0]['captured_source']['fidelity']=='EXACT_LEXICAL' and r==f.record.model_dump(mode='json'):
        f.stored.observation.canonical_metadata['capture_fidelity']='DECODED_CACHE'
    with pytest.raises(ValueError):f.plan(owner.load(owner.InputRecord,owner.encode(r)))


def test_empty_source_provenance_never_becomes_exact(fixture):
    f=fixture;r=f.record.model_dump(mode='json');r['sources'][0]['captured_source']=None
    with pytest.raises(ValueError,match='explicit gap'):f.plan(owner.load(owner.InputRecord,owner.encode(r)))
    r['gaps'].append('CAPTURED_SOURCE_UNAVAILABLE');plan=f.plan(owner.load(owner.InputRecord,owner.encode(r)))
    assert f.resolver.verify(f.resolver.resolve(plan),plan).financial_status=='NOT_ASSESSED'


@pytest.mark.parametrize('members',[
    [(1,2,None),(9,10,None)],[],[(1,99,None)],[(1,2,9)],
])
def test_full_parent_exact_membership_and_initial_no_active_lookup(fixture,members):
    f=fixture;a=json.loads(json.dumps(f.accounting));a['expected_parent_release_id']=8
    a['parent_members']=[dict(security_id=1,snapshot_id=2,exact_bundle_id=None)]
    a['selected_members'][0]['disposition']='REPLACED';f.candidate.expected_parent_release_id=8
    f.publications.retained_members=lambda id:members
    r=f.record.model_dump(mode='json');r['parent_state']=dict(status='UNAVAILABLE',artifact_sha256=None,reason='No accepted state',parent_release_id=8,security_id=1,snapshot_id=2)
    with pytest.raises(ValueError,match='parent'):f.plan(owner.load(owner.InputRecord,owner.encode(r)),a)
    # Initial None must not consult an active/default release or parent reader.
    f.publications.retained_members=lambda *a:pytest.fail('initial active lookup')
    f.candidate.expected_parent_release_id=None
    f.plan()


def test_valid_inherited_parent_and_unavailable_prior_state(fixture):
    f=fixture;a=json.loads(json.dumps(f.accounting));a['expected_parent_release_id']=8
    a['parent_members']=[a['outcomes'][0]['selection']]
    a['selected_members'][0].update(disposition='RETAINED',candidate_id=None)
    f.publications.retained_members=lambda id:((1,2,None),)
    f.publications.candidate=lambda *a:pytest.fail('inherited candidate lookup')
    r=f.record.model_dump(mode='json');r['parent_state']=dict(status='UNAVAILABLE',artifact_sha256=None,reason='No accepted market/history state',parent_release_id=8,security_id=1,snapshot_id=2)
    p=f.plan(owner.load(owner.InputRecord,owner.encode(r)),a)
    output=f.resolver.verify(f.resolver.resolve(p),p)
    assert output.inputs[0].input.parent_state.status=='UNAVAILABLE'


def test_root_modes_and_global_associations_are_retained_not_reconstructed(fixture):
    f=fixture;p=f.plan();v=json.loads(f.objects.rows[p]);v['export_mode']='QUOTE_ONLY'
    v['global_listing']=dict(status='REGISTERED_ARTIFACT_ONLY',artifact_sha256=f.body,reason='No source delivery/global authority')
    p=f.put(v);output=f.resolver.verify(f.resolver.resolve(p),p)
    assert output.complete_plan_sha256==p and output.financial_status=='NOT_ASSESSED'
    v['global_indexes']=v['global_indexes'][:2]
    with pytest.raises(ValueError,match='index'):f.resolver.resolve(f.put(v))


def test_raw_cache_is_released_between_different_inputs(fixture,monkeypatch):
    f=fixture;observed=[];real=owner.read_registered_artifact
    def bounded(*args,**kwargs):
        cache=kwargs.get('verified_payloads');observed.append(len(cache) if cache is not None else 0)
        return real(*args,**kwargs)
    monkeypatch.setattr(owner,'read_registered_artifact',bounded)
    f.resolver.resolve(f.plan())
    assert max(observed)<=1 # root only; no accumulated financial/source bodies


def test_exact_bundle_delivery_recurrence_binding(fixture):
    f=fixture;a=json.loads(json.dumps(f.accounting));a['outcomes'][0]['selection']['exact_bundle_id']=7;a['selected_members'][0]['exact_bundle_id']=7
    f.candidate.exact_bundle_id=7
    f.resolver.history=NS(engine=NS(connect=lambda:nullcontext('connection')),
        publication_identity_on_connection=lambda *a,**kw:dict(exact_bundle_sha256='e'*64,source_observation_ids=(13,)),
        load_on_connection=lambda *a:NS(sources=(NS(source_item_id=12,source_observation_id=13,occurrence_id=11),)))
    r=f.record.model_dump(mode='json');r['exact_bundle']=dict(exact_bundle_id=7,exact_bundle_sha256='e'*64,expected_exact_run_key='actual-run')
    assert f.plan(owner.load(owner.InputRecord,owner.encode(r)),a)
    f.resolver.history.load_on_connection=lambda *a:NS(sources=(NS(source_item_id=12,source_observation_id=13,occurrence_id=99),))
    with pytest.raises(ValueError,match='occurrence'):f.plan(owner.load(owner.InputRecord,owner.encode(r)),a)


def test_publication_accessors_use_exact_identity_no_active_or_write():
    from screener.publication import PublicationRepository
    from sqlalchemy.dialects import postgresql
    calls=[]
    class Result:
        def mappings(self):return self
        def one_or_none(self):return None
        def __iter__(self):return iter([(1,2,None)])
    connection=NS(execute=lambda stmt:calls.append(str(stmt.compile(dialect=postgresql.dialect()))) or Result(),scalar=lambda stmt:8)
    repo=PublicationRepository(NS(connect=lambda:nullcontext(connection)))
    assert repo.candidate(9) is None
    assert repo.retained_members(8)==((1,2,None),)
    assert len(calls)==2 and all('active_data_release' not in c and 'INSERT' not in c for c in calls)
    for method in (repo.candidate,repo.retained_members):
        for bad in (True,8.0,0):
            with pytest.raises(ValueError):method(bad)
    assert len(calls)==2



@pytest.mark.parametrize('role,kind,metadata',[
    ('filing','filing',{}),('primary_document','resource',{'role':'primary_document'}),
])
def test_existing_native_source_fidelity_is_distinct_from_company_facts_http_admission(fixture,role,kind,metadata):
    f=fixture;r=f.record.model_dump(mode='json');r['sources'][0].update(role=role,item_kind=kind)
    r['sources'][0]['captured_source']['role']=role
    f.stored.item.item_kind=kind;f.stored.observation.canonical_metadata=metadata
    assert f.plan(owner.load(owner.InputRecord,owner.encode(r)))
