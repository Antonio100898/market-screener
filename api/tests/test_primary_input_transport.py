"""Synthetic registered transport identities; no local evidence or SQL needed."""
from dataclasses import replace
import hashlib
from types import SimpleNamespace

import pytest

from screener.artifacts import read_registered_artifact, store_evidence
from screener.models import CapturedSource, UnavailablePrimaryInput
from screener.primary_input_plan import PlanDependency, encode_primary_plan
from screener.primary_input_transport import PrimaryInputTransport, PLAN_ROLE, DEPENDENCY_ROLE
from screener.source_validity import SourceDeliveryRecord
from test_market_captures import Repository
from test_object_store import MemoryS3
from screener.object_store import ImmutableObjectStore, ObjectVerificationError
from test_primary_input_plan import representative
from test_primary_input_targets import company_facts
from test_native_primary_inputs import fixture


@pytest.fixture
def registered():
    artifacts, objects = Repository(), ImmutableObjectStore(MemoryS3(), 'private-plan-transport')
    plan = representative()
    raw, source, _, _ = company_facts('-0.0000')
    body, _ = fixture('2.06')
    stored = {}
    deliveries = {}
    payloads = {source.artifact_sha256: raw, hashlib.sha256(body).hexdigest(): body}

    def retain(payload):
        return store_evidence(payload, 'application/octet-stream', objects, artifacts).content_sha256

    for payload in payloads.values():
        retain(payload)
    request = plan.declarations[0].original
    filing_body = b'exact synthetic filing inventory row'
    filing_sha = retain(filing_body)
    filing_capture = CapturedSource('filing', filing_sha, 'https://www.sec.gov/synthetic-filing-inventory', 201, 202, 203)
    filing_delivery = SourceDeliveryRecord(203, 201, 202, 204, 1, 'filing:synthetic', plan.dependencies[0].delivery.checked_at)
    plan = replace(plan, dependencies=(*plan.dependencies, PlanDependency(filing_sha, len(filing_body),
        ('filing_witness',), filing_capture, 'b' * 64, filing_delivery)))
    for dependency in plan.dependencies:
        captured = dependency.captured_source
        item = SimpleNamespace(source_system='SEC', issuer_source_identifier=plan.cik,
            source_item_id=captured.source_item_id, item_kind='filing' if captured.role == 'filing' else 'resource',
            parent_source_item_id=None)
        metadata = dict(cik=plan.cik, role=captured.role)
        if captured.role == 'company_facts':
            metadata.update(capture_revision=2, capture_fidelity='RAW_HTTP_BYTES')
        if captured.role == 'primary_document':
            metadata.update(accession=request.source.accession)
        if captured.role == 'filing':
            metadata.update(accession=request.source.accession, form=request.source.form,
                            filing_date=request.source.filed.isoformat())
        observation = SimpleNamespace(source_observation_id=captured.source_observation_id,
            canonical_metadata=metadata, artifact_sha256=dependency.artifact_sha256,
            source_url=captured.source_document, state='present',
            source_published_at=plan.declarations[0].documents[0].reference.source_published_at
                                if captured.role == 'primary_document' else None)
        stored[dependency.observation_sha256] = SimpleNamespace(item=item, observation=observation)
        deliveries[dependency.delivery.occurrence_id] = dependency.delivery
    observations = SimpleNamespace(by_sha256=lambda digest: stored[digest],
        delivery=lambda occurrence, **kwargs: deliveries[occurrence])
    service = PrimaryInputTransport(artifacts, objects, observations)
    return SimpleNamespace(service=service, plan=plan, raw=raw, source=source,
        retain=retain, stored=stored, deliveries=deliveries, artifacts=artifacts, objects=objects)


def resolve(case, plan=None):
    digest = case.retain(encode_primary_plan(plan or case.plan))
    return case.service.resolve(digest, cik=case.plan.cik, security_id=case.plan.security_id,
        company_facts_source=case.source, company_facts_payload=case.raw)


def test_registered_plan_reaches_existing_copied_admission_with_all_dependencies(registered):
    result = resolve(registered)
    assert type(result.outcomes[0]).__name__ == 'AdmittedPrimaryInput'
    assert result.outcomes[0].mapping == registered.plan.declarations[0]
    assert result.outcomes[0].mapping is not registered.plan.declarations[0]
    assert len(result.captured_sources) == 3
    assert len(result.artifacts) == 4
    assert {link.role for link in result.artifacts} == {PLAN_ROLE, DEPENDENCY_ROLE}
    assert result.manifest['admission'] == [dict(key='mapping0', status='ADMITTED', reason=None)]
    assert result.manifest['financial_consumer'] == 'NOT_IMPLEMENTED'
    assert result.manifest['native_history'] == 'NOT_ASSESSED'


def test_relative_wire2_reaches_public_registered_transport_with_execution_contract1(registered):
    from datetime import date
    declaration = registered.plan.declarations[0]
    basis = declaration.share_basis.model_copy(update=dict(
        anchor_evidence_key=declaration.documents[0].key, anchor_as_of=date(2022, 10, 19)))
    declaration = replace(declaration, share_basis=basis)
    plan = replace(registered.plan, declarations=(declaration,))
    result = resolve(registered, plan)
    assert type(result.outcomes[0]).__name__ == 'AdmittedPrimaryInput'
    assert result.outcomes[0].mapping == declaration
    assert result.outcomes[0].mapping.share_basis.anchor_as_of == date(2022, 10, 19)
    assert result.manifest['primary_input_revision'] == 1
    assert result.manifest['financial_consumer'] == 'NOT_IMPLEMENTED'
    assert result.manifest['native_history'] == 'NOT_ASSESSED'


@pytest.mark.parametrize('problem', ['missing-anchor', 'wrong-document-role', 'wrong-action-class', 'changed-span'])
def test_relative_anchor_does_not_bypass_existing_primary_admission(registered, problem):
    from datetime import date
    declaration = registered.plan.declarations[0]
    basis = declaration.share_basis.model_copy(update=dict(
        anchor_evidence_key=declaration.documents[0].key, anchor_as_of=date(2022, 10, 19)))
    declaration = replace(declaration, share_basis=basis)
    if problem == 'missing-anchor':
        declaration = replace(declaration, documents=())
    elif problem == 'wrong-document-role':
        declaration = replace(declaration, documents=(declaration.documents[0].model_copy(update={
            'roles': ('CORRECTION_EXPLANATION', 'LEGAL_ACTION')}),))
    elif problem == 'wrong-action-class':
        declaration = replace(declaration, actions=(declaration.actions[0].model_copy(update={'security_class': 'preferred'}),))
    else:
        doc = declaration.documents[0]
        declaration = replace(declaration, documents=(doc.model_copy(update={'reference': doc.reference.model_copy(update={
            'location': doc.reference.location.model_copy(update={'span_sha256': 'f' * 64})})}),))
    result = resolve(registered, replace(registered.plan, declarations=(declaration,)))
    assert isinstance(result.outcomes[0], UnavailablePrimaryInput)
    assert result.manifest['financial_consumer'] == 'NOT_IMPLEMENTED'


def test_missing_filing_registration_stays_keyed_unavailable(registered):
    result = resolve(registered, replace(registered.plan, dependencies=registered.plan.dependencies[:-1]))
    unavailable, = result.outcomes
    assert isinstance(unavailable, UnavailablePrimaryInput)
    assert unavailable.reason == 'PINNED_REGISTERED_FILING_WITNESS_UNAVAILABLE'
    assert unavailable.original == registered.plan.declarations[0].original
    assert unavailable.target_origin == registered.plan.declarations[0].target_origin


def test_missing_native_delivery_stays_unavailable_without_fallback(registered):
    dependencies = list(registered.plan.dependencies)
    dependencies[1] = replace(dependencies[1], delivery=None)
    result = resolve(registered, replace(registered.plan, dependencies=tuple(dependencies)))
    assert isinstance(result.outcomes[0], UnavailablePrimaryInput)
    assert result.outcomes[0].reason == 'NATIVE_REGISTERED_SOURCE_DELIVERY_UNAVAILABLE'
    assert result.manifest['dependencies'][1]['status'] == 'REGISTERED_ARTIFACT_ONLY'


@pytest.mark.parametrize('field,wrong', [('state', 'removed'), ('artifact_sha256', 'f' * 64),
                                      ('source_url', 'https://www.sec.gov/other'), ('source_observation_id', 1000)])
def test_wrong_actual_observation_rejects(registered, field, wrong):
    value = registered.stored[registered.plan.dependencies[1].observation_sha256].observation
    setattr(value, field, wrong)
    with pytest.raises(ValueError, match='identity or role'):
        resolve(registered)


@pytest.mark.parametrize('field,wrong', [('source_system', 'EDINET'), ('issuer_source_identifier', '0000000001'),
                                      ('source_item_id', 1000)])
def test_wrong_actual_source_item_rejects(registered, field, wrong):
    value = registered.stored[registered.plan.dependencies[1].observation_sha256].item
    setattr(value, field, wrong)
    with pytest.raises(ValueError, match='identity or role'):
        resolve(registered)


@pytest.mark.parametrize('field,wrong', [('job_id', 1000), ('ownership_generation', 1000),
                                      ('item_key', 'another'), ('occurrence_id', 1000)])
def test_every_actual_delivery_field_is_checked(registered, field, wrong):
    delivery = registered.plan.dependencies[1].delivery
    registered.deliveries[delivery.occurrence_id] = replace(delivery, **{field: wrong})
    with pytest.raises(ValueError, match='delivery differs'):
        resolve(registered)


def test_wrong_parent_or_registered_filing_metadata_rejects(registered):
    native = registered.stored[registered.plan.dependencies[1].observation_sha256]
    native.item.parent_source_item_id = 999
    with pytest.raises(ValueError, match='metadata or parent'):
        resolve(registered)


def test_wrong_target_pinned_root_cannot_become_unavailable_or_fallback(registered):
    declaration = registered.plan.declarations[0]
    target = replace(declaration.target_origin, source=replace(declaration.target_origin.source, occurrence_id=999))
    with pytest.raises(ValueError, match='pinned Company Facts'):
        resolve(registered, replace(registered.plan, declarations=(replace(declaration, target_origin=target),)))


def test_registered_read_preserves_preparation_errors_and_bounds_before_object_io(registered, monkeypatch):
    digest = registered.retain(b'1234')
    monkeypatch.setattr(registered.objects, 'read_verified', lambda *args: pytest.fail('oversized object read'))
    with pytest.raises(ValueError, match='invalid preparation artifact metadata'):
        read_registered_artifact(digest, registered.objects, registered.artifacts, maximum=3, label='preparation')
    with pytest.raises(ValueError, match='invalid preparation artifact identity'):
        read_registered_artifact(True, registered.objects, registered.artifacts, label='preparation')


def test_request_local_read_cache_keeps_one_object_read_and_rechecks_bytes(registered, monkeypatch):
    digest = registered.retain(b'cached body')
    original = registered.objects.read_verified
    calls = []
    def observed(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(registered.objects, 'read_verified', observed)
    cache = {}
    for _ in range(2):
        _, payload = read_registered_artifact(digest, registered.objects, registered.artifacts, verified_payloads=cache)
        assert payload == b'cached body'
    assert len(calls) == 1
    cache[digest] = b'changed'
    with pytest.raises(ValueError, match='cached artifact bytes'):
        read_registered_artifact(digest, registered.objects, registered.artifacts, verified_payloads=cache)


@pytest.mark.parametrize('change', [dict(capture_revision=1), dict(capture_revision=True),
                                   dict(capture_fidelity='DECODED_DECIMAL')])
def test_company_facts_exact_fidelity_requires_actual_capture_flags(registered, change):
    observation = registered.stored[registered.plan.dependencies[0].observation_sha256].observation
    observation.canonical_metadata.update({'capture_revision': 2, 'capture_fidelity': 'RAW_HTTP_BYTES', **change})
    with pytest.raises(ValueError, match='raw HTTP admission'):
        resolve(registered)


def test_distinct_registered_annual_wrapper_does_not_replace_native_source_parent(registered):
    declaration = registered.plan.declarations[0]
    wrapper = replace(declaration.original.source, annual_accession='0001213900-24-022083',
                      annual_form='10-K', annual_filed=declaration.original.source.filed.replace(year=2024))
    capture = CapturedSource('filing', registered.plan.dependencies[-1].artifact_sha256,
        'https://www.sec.gov/synthetic-wrapper-inventory', 301, 302, 303)
    delivery = replace(registered.plan.dependencies[-1].delivery, occurrence_id=303, source_item_id=301,
                       source_observation_id=302, item_key='filing:wrapper')
    dependency = replace(registered.plan.dependencies[-1], captured_source=capture,
                         observation_sha256='c' * 64, delivery=delivery)
    source_record = registered.stored[registered.plan.dependencies[1].observation_sha256]
    source_record.item.parent_source_item_id = 201
    registered.stored[dependency.observation_sha256] = SimpleNamespace(
        item=SimpleNamespace(source_system='SEC', issuer_source_identifier=registered.plan.cik,
            source_item_id=301, item_kind='filing', parent_source_item_id=None),
        observation=SimpleNamespace(source_observation_id=302, state='present', artifact_sha256=capture.artifact_sha256,
            source_url=capture.source_document, source_published_at=None, canonical_metadata=dict(cik=registered.plan.cik,
            accession=wrapper.annual_accession, form=wrapper.annual_form, filing_date=wrapper.annual_filed.isoformat())))
    registered.deliveries[303] = delivery
    plan = replace(registered.plan, declarations=(replace(declaration, original=replace(declaration.original, source=wrapper)),),
                   dependencies=(*registered.plan.dependencies, dependency))
    assert resolve(registered, plan).manifest['admission'][0]['status'] == 'ADMITTED'
