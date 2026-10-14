import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from helpers import GAAP, facts_doc
from test_market_captures import Repository
from test_object_store import MemoryS3
from test_shared_companies import repository, _company
from screener import store, sync
from screener.artifacts import store_evidence
from screener.captured_assembly import CapturedCanonicalPreparer, PreparationUnavailable, engine_identity, RECIPE_MEDIA_TYPE
from screener.company_import import _canonical_json
from screener.evidence import EvidenceBundle
from screener.object_store import ImmutableObjectStore, ObjectVerificationError
from screener.shared_companies import SnapshotArtifact


@pytest.fixture
def preparation():
    artifacts = Repository(); objects = ImmutableObjectStore(MemoryS3(), 'canonical-tests')
    def retain(value):
        return store_evidence(_canonical_json(value), 'application/json', objects, artifacts).content_sha256
    facts = {**facts_doc(GAAP), 'cik': 1234}
    status, strict = sync._derive_evidence(EvidenceBundle('0000001234', 'TEST', facts, None, None))
    assert status == 'ok'
    links = (SnapshotArtifact(retain(facts), 'official_api_facts'),
        SnapshotArtifact(retain({'0':{'cik_str':1234,'ticker':'TEST'}}), 'official_sec_ticker_mapping'))
    snapshot = SimpleNamespace(snapshot_id=1, security_id=2, ticker='TEST', engine_revision=store.ENGINE_VERSION,
        payload_sha256=hashlib.sha256(_canonical_json(strict)).hexdigest(), canonical_payload=strict,
        snapshot_sha256='b'*64, security_basis='SEC_TICKER_MAPPING_ONLY', quote_currency='USD',
        source_accession=None, security_title=None, receipt_ratio=None)
    company = SimpleNamespace(issuer_source='SEC', issuer_identifier='0000001234', snapshot=snapshot, artifacts=links)
    companies = SimpleNamespace(snapshot=lambda sid: company if sid == 1 else None)
    observations = SimpleNamespace(by_sha256=lambda _: (_ for _ in ()).throw(ValueError('missing source')))
    service = CapturedCanonicalPreparer(companies, artifacts, objects, observations)
    recipe = dict(recipe_revision=1, engine_revision=store.ENGINE_VERSION, engine_identity_sha256=engine_identity(),
        members=[dict(snapshot_id=1, security_id=2, market_recipe_sha256=None)])
    return service, retain, recipe, company


def test_exact_snapshot_lookup_does_not_follow_current(repository):
    old = repository.store_and_select(**_company(canonical_payload={'ticker':'ABT','assets':100}))
    new = repository.store_and_select(**_company(canonical_payload={'ticker':'ABT','assets':90}))
    assert repository.current(old.security_id).snapshot_id == new.snapshot_id
    exact = repository.snapshot(old.snapshot_id)
    assert exact.snapshot.canonical_payload['assets'] == 100 and exact.release_id is None
    assert exact.snapshot.security_id == old.security_id
    assert repository.snapshot(99999) is None
    for invalid in (True,0,-1,'1'):
        with pytest.raises(ValueError): repository.snapshot(invalid)


def test_offline_preparation_repeat_is_identical_and_preserves_gaps(preparation):
    service, retain, recipe, company = preparation
    original = json.loads(json.dumps(company.snapshot.canonical_payload))
    digest = service.prepare(retain(recipe))
    assert service.prepare(retain(recipe)) == digest
    output = json.loads(service._read(digest)); row=output['members'][0]
    assert output['display_projection_status'] == 'NOT_BUILT'
    assert output['official_financial_readiness'] == 'NOT_ASSESSED'
    assert row['detail_projection_status'] == 'NOT_BUILT'
    for gap in ('DERA_COVERAGE_UNASSESSED','MARKET_CAPTURE_ABSENT','UPSTREAM_SOURCE_OBSERVATIONS_ABSENT'):
        assert gap in row['upstream_gaps']
    assert json.loads(service._read(row['canonical_artifact_sha256'])) == original
    assert json.loads(_canonical_json(company.snapshot.canonical_payload)) == original
    assert row['assumed_engine_artifact_sha256'] is not None
    assert not any('release_id' in key for key in output)


@pytest.mark.parametrize('changes', [dict(recipe_revision=True),dict(engine_revision=0),
    dict(strict_row={'price':1}),dict(members=[]),dict(engine_identity_sha256='bad')])
def test_recipe_rejects_output_overrides_and_invalid_contract(preparation, changes):
    service, retain, recipe, _ = preparation; recipe.update(changes)
    with pytest.raises(ValidationError): service.prepare(retain(recipe))


@pytest.mark.parametrize('changes', [dict(security_id=True),dict(snapshot_id=0),dict(price=10),
    dict(assumed_row={}),dict(market_recipe_sha256='bad')])
def test_member_rejects_financial_or_invalid_identity_inputs(preparation, changes):
    service, retain, recipe, _ = preparation; recipe['members'][0].update(changes)
    with pytest.raises(ValidationError): service.prepare(retain(recipe))


@pytest.mark.parametrize('field,value', [('engine_revision',1),('engine_identity_sha256','0'*64)])
def test_wrong_engine_is_unavailable(preparation, field, value):
    service, retain, recipe, _ = preparation; recipe[field]=value
    with pytest.raises(PreparationUnavailable, match='engine'):service.prepare(retain(recipe))


@pytest.mark.parametrize('field,value', [('security_id',3),('snapshot_id',999)])
def test_wrong_snapshot_security_is_not_replaced_by_current(preparation, field, value):
    service, retain, recipe, _ = preparation; recipe['members'][0][field]=value
    with pytest.raises((ValueError,PreparationUnavailable)):service.prepare(retain(recipe))


def test_duplicate_members_and_missing_graph_rejected(preparation):
    service, retain, recipe, company = preparation; recipe['members']*=2
    with pytest.raises(ValueError, match='duplicate'):service.prepare(retain(recipe))
    recipe['members']=recipe['members'][:1]
    company.artifacts=tuple(link for link in company.artifacts if link.role != 'official_api_facts')
    with pytest.raises(PreparationUnavailable, match='role'):service.prepare(retain(recipe))


def test_wrong_facts_are_not_accepted(preparation):
    service, retain, recipe, company = preparation
    wrong=retain({'cik':999,'facts':{}})
    company.artifacts=(SnapshotArtifact(wrong,'official_api_facts'),company.artifacts[1])
    with pytest.raises(ValueError,match='issuer'):service.prepare(retain(recipe))


def test_unexplained_snapshot_mismatch_is_unavailable(preparation):
    service, retain, recipe, company = preparation
    company.snapshot.canonical_payload={**company.snapshot.canonical_payload,'shares':1}
    with pytest.raises(PreparationUnavailable,match='reproduce'):service.prepare(retain(recipe))


def test_artifact_corruption_cannot_become_missing_market(preparation):
    service, retain, recipe, _ = preparation
    digest=retain(recipe); key=service.artifacts.get(digest).object_key
    service.objects.client.objects[key]=b'corrupted'
    with pytest.raises(ObjectVerificationError):service.prepare(digest)


def test_duplicate_json_fields_fail_before_derivation(preparation):
    service, _, _, _=preparation
    digest=store_evidence(b'{"recipe_revision":1,"recipe_revision":1}',RECIPE_MEDIA_TYPE,
        service.objects,service.artifacts).content_sha256
    with pytest.raises(ValueError,match='duplicate'):service.prepare(digest)


def test_source_observation_must_bind_same_company_and_artifact(preparation):
    service, retain, recipe, company=preparation
    recipe['members'][0]['source_observation_sha256']=['c'*64]
    source=SimpleNamespace(item=SimpleNamespace(source_system='SEC',issuer_source_identifier='wrong',source_item_id=1),
        observation=SimpleNamespace(state='present',artifact_sha256=company.artifacts[0].content_sha256,source_observation_id=1))
    service.observations.by_sha256=lambda _:source
    with pytest.raises(ValueError,match='bind'):service.prepare(retain(recipe))


def test_market_missing_recipe_cannot_fall_back_to_network(preparation,monkeypatch):
    from screener.market_captures import MarketCaptureError
    service,retain,recipe,_=preparation
    recipe['members'][0]['market_recipe_sha256']='e'*64
    monkeypatch.setattr('httpx.Client.get',lambda *_args,**_kwargs:(_ for _ in ()).throw(AssertionError('live HTTP')))
    with pytest.raises(MarketCaptureError):service.prepare(retain(recipe))


def test_checkpoint_verification_rejects_missing_output_and_wrong_recipe(preparation):
    service,retain,recipe,_=preparation;recipe_sha=retain(recipe);output=service.prepare(recipe_sha)
    assert service.verify(output,recipe_sha)['recipe_sha256']==recipe_sha
    with pytest.raises(ValueError,match='contract'):service.verify(retain({'recipe_sha256':recipe_sha}),recipe_sha)
    modified={**recipe,'engine_identity_sha256':'0'*64}
    with pytest.raises(ValueError,match='different inputs'):service.verify(output,retain(modified))


def test_changed_code_during_prepare_is_unavailable(preparation,monkeypatch):
    service,retain,recipe,_=preparation
    identities=iter([recipe['engine_identity_sha256'],'0'*64])
    monkeypatch.setattr('screener.captured_assembly.engine_identity',lambda:next(identities))
    with pytest.raises(PreparationUnavailable,match='changed'):service.prepare(retain(recipe))


def test_retained_sidecar_is_not_omitted_or_replaced(preparation):
    service,retain,recipe,company=preparation
    # A linked second DERA revision is ambiguous, not permission to select whichever comes first.
    company.artifacts+= (SnapshotArtifact(retain({'facts':{'us-gaap':{}}}),'dimensioned_facts'),
        SnapshotArtifact(retain({'facts':{'ifrs-full':{}}}),'dimensioned_facts'))
    with pytest.raises(PreparationUnavailable,match='ambiguous'):service.prepare(retain(recipe))


def test_structured_receipt_identity_is_checked_before_financial_work(preparation):
    from decimal import Decimal
    service,retain,recipe,company=preparation
    company.snapshot.security_basis='PRIMARY_DEPOSITARY_RECEIPT'
    company.snapshot.security_title='American Depositary Shares, each representing two ordinary shares'
    company.snapshot.source_accession='0000001234-26-000001'; company.snapshot.receipt_ratio=Decimal('2')
    identity=dict(title=company.snapshot.security_title,symbol='TEST',accn=company.snapshot.source_accession,ratio='5')
    company.artifacts=(company.artifacts[0],SnapshotArtifact(retain(identity),'structured_cover_identity'))
    with pytest.raises(ValueError,match='ratio'):service.prepare(retain(recipe))


def test_unsupported_inline_contract_is_not_parsed_as_success(preparation):
    service,retain,recipe,company=preparation
    company.artifacts+=(SnapshotArtifact(retain({'schema':'invented'}),'sec_inline_current_manifest'),)
    with pytest.raises(PreparationUnavailable,match='foreign-inline'):service.prepare(retain(recipe))


def test_checkpoint_refuses_changed_canonical_artifact(preparation):
    service,retain,recipe,_=preparation;recipe_sha=retain(recipe)
    output=json.loads(service._read(service.prepare(recipe_sha)))
    output['members'][0]['canonical_artifact_sha256']=retain({'shares':1})
    with pytest.raises(ValueError,match='canonical artifact'):service.verify(retain(output),recipe_sha)


def test_missing_foreign_manifest_does_not_make_partial_leaves_usable(preparation):
    service,retain,recipe,company=preparation
    company.artifacts+=(SnapshotArtifact(retain({'ignored':'leaf'}),'sec_inline_direct_annual_index'),)
    with pytest.raises(PreparationUnavailable,match='foreign-inline'):service.prepare(retain(recipe))


@pytest.mark.parametrize('present', [True, False])
def test_raw_cover_uses_exact_filing_form_or_stays_unavailable(preparation, monkeypatch, present):
    service, retain, recipe, company = preparation
    company.snapshot.security_basis = 'PRIMARY_COMMON_EQUITY'
    company.snapshot.source_accession = '0000001234-26-000001'
    company.snapshot.exchange_code = 'NYSE'
    recent = dict(accessionNumber=[company.snapshot.source_accession], form=['40-F'],
        filingDate=['2026-01-02'], reportDate=['2025-12-31'], primaryDocument=['annual.htm'])
    if not present:
        recent = {key: [] for key in recent}
    company.artifacts = (company.artifacts[0], SnapshotArtifact(retain(dict(
        tickers=['TEST'], exchanges=['NYSE'], filings=dict(recent=recent))), 'sec_root_submissions'))
    seen = []
    def identity(_submissions, **kwargs):
        seen.append(kwargs['form'])
        return 'TEST', 'NYSE', None
    monkeypatch.setattr('screener.captured_assembly._security_identity', identity)
    if present:
        service.prepare(retain(recipe))
        assert seen == ['40-F']
    else:
        with pytest.raises(PreparationUnavailable, match='filing metadata'):
            service.prepare(retain(recipe))
        assert seen == []


def test_completed_checkpoint_cannot_cross_running_engine_change(preparation, monkeypatch):
    service, retain, recipe, _ = preparation
    recipe_sha = retain(recipe); output = service.prepare(recipe_sha)
    monkeypatch.setattr('screener.captured_assembly.engine_identity', lambda: '0'*64)
    with pytest.raises(PreparationUnavailable, match='checkpoint engine'):
        service.verify(output, recipe_sha)


@pytest.mark.parametrize('field,value', [('snapshot_sha256','0'*64),
    ('canonical_payload_sha256','0'*64),('input_artifacts',[]),('source_observations',[{}])])
def test_checkpoint_cannot_replace_immutable_graph_metadata(preparation, field, value):
    service, retain, recipe, _ = preparation
    recipe_sha = retain(recipe); output = json.loads(service._read(service.prepare(recipe_sha)))
    output['members'][0][field] = value
    with pytest.raises(ValueError, match='immutable snapshot graph'):
        service.verify(retain(output), recipe_sha)


@pytest.mark.parametrize('changed_file', ['captured_assembly.py', 'sources/sec_json.py', 'sources/sec_submission.py', 'exact_annual.py',
                                        'artifacts.py', 'primary_input_plan.py', 'primary_input_transport.py',
                                        'figure_history.py', 'source_observations.py', 'source_validity.py',
                                        'durable_jobs.py', 'postgres.py', 'shared_companies.py'])
def test_engine_identity_binds_reconstruction_source_known_manifest(monkeypatch, changed_file):
    from pathlib import Path
    from screener import captured_assembly
    root = Path(captured_assembly.__file__).parent
    names = ('artifacts.py','primary_input_plan.py','primary_input_transport.py',
        'figure_history.py','source_observations.py','source_validity.py',
        'durable_jobs.py','postgres.py','shared_companies.py',
        'ch13.py','company_import.py','evidence.py','market_captures.py','models.py',
        'normalize.py','sec_ingestion.py','store.py','sync.py','captured_assembly.py','exact_annual.py',
        'sources/cover.py','sources/edinet.py','sources/edinet_mapper.py',
        'sources/inline_xbrl.py','sources/jpx.py','sources/prices.py','sources/sec_json.py','sources/sec_submission.py')
    paths = [root / name for name in names] + sorted((root / 'screens').glob('*.py'))
    expected = hashlib.sha256(_canonical_json({str(path.relative_to(root)):
        hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})).hexdigest()
    assert engine_identity() == expected
    read_bytes = Path.read_bytes
    own = root / changed_file
    monkeypatch.setattr(Path, 'read_bytes', lambda path:
        read_bytes(path) + b'\n# changed reconstruction ordering\n' if path == own else read_bytes(path))
    changed = hashlib.sha256(_canonical_json({str(path.relative_to(root)):
        hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})).hexdigest()
    assert changed != expected
    assert engine_identity() == changed
