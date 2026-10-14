"""Planned caller contracts over synthetic registered bodies; no local caches or SQL."""
import copy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from helpers import GAAP
from screener import sync, store
from screener.captured_assembly import CapturedCanonicalPreparer, engine_identity
from screener.company_import import _canonical_json
from screener.evidence import EvidenceBundle
from screener.primary_input_plan import encode_primary_plan
from screener.sec_ingestion import SecResourceFetchHandler, _candidate_parameters
from screener.shared_companies import SnapshotArtifact
from screener.sources.sec_json import decode_company_facts, ORIGIN_KEY
from test_primary_input_transport import registered


@pytest.fixture
def planned_preparation(registered):
    case = registered
    value = json.loads(case.raw)
    for tag, data in GAAP.items():
        if tag != 'EarningsPerShareDiluted':
            value['facts']['us-gaap'][tag] = copy.deepcopy(data)
    value['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares'].extend(copy.deepcopy(
        GAAP['EarningsPerShareDiluted']['units']['USD/shares']))
    raw = json.dumps(value).encode()
    source = replace(case.source, artifact_sha256=case.retain(raw))
    facts = decode_company_facts(raw, source=source)
    target = facts['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares'][99][ORIGIN_KEY]
    dependencies = (replace(case.plan.dependencies[0], artifact_sha256=source.artifact_sha256,
                            byte_size=len(raw), captured_source=source), *case.plan.dependencies[1:])
    plan = replace(case.plan, dependencies=dependencies,
                   declarations=(replace(case.plan.declarations[0], target_origin=target),))
    case.stored[dependencies[0].observation_sha256].observation.artifact_sha256 = source.artifact_sha256
    plan_sha = case.retain(encode_primary_plan(plan))
    resolved = case.service.resolve(plan_sha, cik=plan.cik, security_id=plan.security_id,
        company_facts_source=source, company_facts_payload=raw)
    result = sync._derive_evidence_result(EvidenceBundle(plan.cik, 'TEST', resolved.original_company_facts, None, None,
        'missing', resolved.captured_sources, resolved.original_company_facts, resolved.outcomes))
    assert result.status == 'ok'
    links = (SnapshotArtifact(source.artifact_sha256, 'official_api_facts'),
        SnapshotArtifact(case.retain(_canonical_json({'0': {'cik_str': int(plan.cik), 'ticker': 'TEST'}})),
                         'official_sec_ticker_mapping'), *resolved.artifacts)
    snapshot = SimpleNamespace(snapshot_id=1, security_id=plan.security_id, ticker='TEST',
        engine_revision=store.ENGINE_VERSION, payload_sha256=hashlib.sha256(_canonical_json(result.row)).hexdigest(),
        canonical_payload=result.row, snapshot_sha256='c' * 64, security_basis='SEC_TICKER_MAPPING_ONLY',
        quote_currency='USD', source_accession=None, security_title=None, receipt_ratio=None)
    company = SimpleNamespace(issuer_source='SEC', issuer_identifier=plan.cik, snapshot=snapshot, artifacts=links)
    service = CapturedCanonicalPreparer(SimpleNamespace(snapshot=lambda sid: company if sid == 1 else None),
        case.artifacts, case.objects, case.service.observations)
    recipe = dict(recipe_revision=2, engine_revision=store.ENGINE_VERSION, engine_identity_sha256=engine_identity(),
        members=[dict(snapshot_id=1, security_id=plan.security_id, market_recipe_sha256=None,
                      primary_input_plan_sha256=plan_sha, company_facts_occurrence_id=source.occurrence_id)])
    return SimpleNamespace(case=case, plan=plan, service=service, recipe=recipe, company=company)


def test_actual_planned_preparation_supplies_copied_inputs_and_builds_once(planned_preparation, monkeypatch):
    fixture = planned_preparation
    calls, builds = [], []
    derive, build = sync._derive_evidence_result, sync.build_snapshot
    def observe(bundle, *args, **kwargs):
        calls.append(bundle)
        return derive(bundle, *args, **kwargs)
    def observe_build(*args, **kwargs):
        builds.append(kwargs['assume_absent_zero'])
        return build(*args, **kwargs)
    monkeypatch.setattr(sync, '_derive_evidence_result', observe)
    monkeypatch.setattr(sync, 'build_snapshot', observe_build)
    digest = fixture.service.prepare(fixture.case.retain(_canonical_json(fixture.recipe)))
    assert len(calls) == 1 and builds == [False, True]
    bundle, = calls
    assert bundle.primary_inputs[0].mapping == fixture.plan.declarations[0]
    assert bundle.primary_inputs[0].mapping is not fixture.plan.declarations[0]
    assert bundle.original_company_facts is bundle.facts
    assert len(bundle.captured_sources) == 3
    output = json.loads(fixture.service._read(digest))
    assert output['preparation_revision'] == 2
    row, = output['members']
    assert row['primary_input_transport']['admission'][0]['status'] == 'ADMITTED'
    assert 'PRIMARY_INPUT_FINANCIAL_CONSUMER_NOT_IMPLEMENTED' in row['upstream_gaps']
    assert 'UPSTREAM_SOURCE_OBSERVATIONS_ABSENT' not in row['upstream_gaps']
    assert json.loads(fixture.service._read(row['canonical_artifact_sha256'])) == json.loads(_canonical_json(fixture.company.snapshot.canonical_payload))


def test_planned_preparation_reads_each_unique_artifact_once_and_recovers_without_build(planned_preparation, monkeypatch):
    fixture = planned_preparation
    recipe_sha = fixture.case.retain(_canonical_json(fixture.recipe))
    reads, original = [], fixture.case.objects.read_verified
    def observe(digest, *args):
        reads.append(digest)
        return original(digest, *args)
    monkeypatch.setattr(fixture.case.objects, 'read_verified', observe)
    output = fixture.service.prepare(recipe_sha)
    for digest in {link.content_sha256 for link in fixture.company.artifacts}:
        assert reads.count('raw/sha256/' + digest) == 1
    monkeypatch.setattr(sync, '_derive_evidence_result', lambda *args, **kwargs: pytest.fail('recovery built snapshot'))
    monkeypatch.setattr(sync, '_derive_evidence', lambda *args, **kwargs: pytest.fail('recovery ran selector'))
    assert fixture.service.verify(output, recipe_sha)['preparation_revision'] == 2


@pytest.mark.parametrize('field,value', [('company_facts_occurrence_id', 999), ('security_id', 999),
    ('primary_input_plan_sha256', 'f' * 64)])
def test_planned_preparation_wrong_pinned_inputs_refuse(planned_preparation, field, value):
    fixture = planned_preparation
    fixture.recipe['members'][0][field] = value
    with pytest.raises((ValueError, KeyError)):
        fixture.service.prepare(fixture.case.retain(_canonical_json(fixture.recipe)))


def test_old_recipe_cannot_accept_new_private_semantics(planned_preparation):
    fixture = planned_preparation
    fixture.recipe['recipe_revision'] = 1
    with pytest.raises(ValidationError):
        fixture.service.prepare(fixture.case.retain(_canonical_json(fixture.recipe)))


def test_planned_checkpoint_admission_and_occurrence_are_sealed(planned_preparation):
    fixture = planned_preparation
    recipe_sha = fixture.case.retain(_canonical_json(fixture.recipe))
    output = json.loads(fixture.service._read(fixture.service.prepare(recipe_sha)))
    output['members'][0]['primary_input_transport']['admission'][0]['status'] = 'UNAVAILABLE'
    with pytest.raises(ValueError, match='private plan'):
        fixture.service.verify(fixture.case.retain(_canonical_json(output)), recipe_sha)


@pytest.mark.parametrize('field', ['revision', 'dependency_id'])
def test_planned_preparation_manifest_rejects_integer_to_boolean_substitution(planned_preparation, field):
    fixture = planned_preparation
    recipe_sha = fixture.case.retain(_canonical_json(fixture.recipe))
    output = json.loads(fixture.service._read(fixture.service.prepare(recipe_sha)))
    manifest = output['members'][0]['primary_input_transport']
    if field == 'revision':
        manifest['primary_input_revision'] = True
    else:
        manifest['dependencies'][0]['source_item_id'] = True
    with pytest.raises(ValueError, match='private plan'):
        fixture.service.verify(fixture.case.retain(_canonical_json(output)), recipe_sha)


@pytest.mark.parametrize('path,change', [
    (('preparation_revision',), float), (('engine_revision',), float),
    (('members', 0, 'snapshot_id'), float), (('members', 0, 'snapshot_id'), bool),
    (('members', 0, 'security_id'), float), (('members', 0, 'company_facts_occurrence_id'), float),
    (('members', 0, 'source_observations', 0, 'source_item_id'), float),
    (('members', 0, 'source_observations', 0, 'source_observation_id'), float),
    (('members', 0, 'source_observations', 0, 'source_item_id'), bool),
    (('members', 0, 'input_artifacts', 0, 'role'), lambda value: None),
    (('members', 0, 'input_artifacts', 0, 'content_sha256'), lambda value: True),
    (('members', 0, 'source_observations', 0, 'artifact_sha256'), lambda value: True),
    (('members', 0, 'source_observations', 0, 'observation_sha256'), lambda value: None),
])
def test_planned_preparation_replay_requires_exact_identity_types(planned_preparation, path, change):
    fixture = planned_preparation
    fixture.recipe['members'][0]['source_observation_sha256'] = [fixture.plan.dependencies[0].observation_sha256]
    recipe_sha = fixture.case.retain(_canonical_json(fixture.recipe))
    output = json.loads(fixture.service._read(fixture.service.prepare(recipe_sha)))
    assert fixture.service.verify(fixture.case.retain(_canonical_json(output)), recipe_sha) == output
    owner = output
    for part in path[:-1]:
        owner = owner[part]
    owner[path[-1]] = change(owner[path[-1]])
    with pytest.raises(ValueError):
        fixture.service.verify(fixture.case.retain(_canonical_json(output)), recipe_sha)


def planned_resource(case):
    parameters = dict(accession='0001066923-23-000001', cik=case.plan.cik, form='10-K',
        filename='edgar/data/1066923/0001066923-23-000001.txt', observation_id=10)
    roles = ('complete_submission', 'accession_inventory', 'submissions', 'company_facts')
    delivery = case.plan.dependencies[0].delivery
    deliveries = {role: dict(occurrence_id=delivery.occurrence_id, source_item_id=delivery.source_item_id,
        source_observation_id=delivery.source_observation_id, job_id=delivery.job_id,
        ownership_generation=delivery.ownership_generation, item_key=f'root:{role}',
        checked_at=delivery.checked_at.isoformat()) for role in roles}
    checkpoint = dict(accession=parameters['accession'], cik=case.plan.cik, filing_observation_id=10,
        capture_revision=2, roots={role: 'a' * 64 for role in roles}, root_deliveries=deliveries)
    calls = []
    jobs = SimpleNamespace(enqueue_child=lambda lease, **kwargs: calls.append((lease, kwargs)))
    handler = SecResourceFetchHandler(edgar=None, object_store=case.objects, artifacts=case.artifacts,
        observations=case.service.observations, jobs=jobs, clock=lambda: delivery.checked_at)
    context = SimpleNamespace(job=SimpleNamespace(parameters=parameters, due_at=delivery.checked_at, priority=4),
                              lease='synthetic live parent')
    return handler, context, checkpoint, calls


def test_explicit_planned_enqueue_pins_new_request_while_default_remains_old(registered):
    handler, context, checkpoint, calls = planned_resource(registered)
    plan_sha = registered.retain(encode_primary_plan(registered.plan))
    handler._enqueue_candidate(context, checkpoint)
    handler.enqueue_planned_candidate(context, checkpoint, primary_input_plan_sha256=plan_sha)
    old, planned = (call[1] for call in calls)
    assert 'primary_input_revision' not in old['parameters']
    assert planned['parameters']['primary_input_revision'] == 1
    assert planned['parameters']['primary_input_plan_sha256'] == plan_sha
    assert old['child_key'] != planned['child_key']
    assert _candidate_parameters(planned['parameters'])[-1] == checkpoint['roots']


@pytest.mark.parametrize('change', [dict(capture_revision=1), dict(capture_revision=True), dict(roots={}),
    dict(filing_observation_id=999), dict(unexpected=True)])
def test_explicit_planned_enqueue_refuses_wrong_capture_before_child_write(registered, change):
    handler, context, checkpoint, calls = planned_resource(registered)
    checkpoint.update(change)
    with pytest.raises(ValueError):
        handler.enqueue_planned_candidate(context, checkpoint,
            primary_input_plan_sha256=registered.retain(encode_primary_plan(registered.plan)))
    assert calls == []


@pytest.mark.parametrize('change', [dict(primary_input_revision=True), dict(primary_input_revision=2),
    dict(primary_input_plan_sha256=True), dict(primary_input_plan_sha256='wrong')])
def test_stage_contract_rejects_bad_new_header_without_reinterpreting_old_literals(registered, change):
    _, context, checkpoint, _ = planned_resource(registered)
    parameters = {**context.job.parameters, **{key: checkpoint[key] for key in ('capture_revision', 'roots', 'root_deliveries')},
        'primary_input_revision': 1, 'primary_input_plan_sha256': 'f' * 64, **change}
    with pytest.raises(ValueError):
        _candidate_parameters(parameters)
