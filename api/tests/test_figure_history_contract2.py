import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from screener.figure_history import (ActionEvidence, Assumption, BasisTransition, CalculationDependency,
    DateWitness, DocumentEvidence, DocumentSpan, ExactFigureBundle, ReportedObservation, ShareBasis)
from tests.test_figure_history import example_bundle, validate_changed


def document_bundle(bundle=None, *, body=b'Legal action and basis disclosure'):
    bundle = bundle or example_bundle()
    reference = bundle.observations[0].reference.model_copy(update={
        'location': DocumentSpan(start_byte=0, end_byte=len(body), span_sha256=hashlib.sha256(body).hexdigest()),
        'extraction_revision': 'document-exact-v2', 'source_document':'https://www.sec.gov/filing-1'})
    document = DocumentEvidence(key='disclosure', reference=reference,
        roles=('LEGAL_ACTION', 'INCORPORATED_BASIS', 'RECEIPT_TERMS', 'CORRECTION_EXPLANATION'))
    return validate_changed(bundle, contract_revision=2, document_evidence=(document,))


def basis_graph(bundle=None, *, reason='validator-unavailable'):
    bundle = bundle if bundle is not None and bundle.document_evidence else document_bundle(bundle)
    before = ShareBasis(kind='ORDINARY', security_class='common', incorporation='KNOWN', evidence_keys=('disclosure',))
    after = before.model_copy(update={'incorporated_actions': ('split',)})
    observed = bundle.observations[0].model_copy(update={'share_basis': before})
    raw = bundle.selections[0].model_copy(update={'node_key': 'before', 'purpose': 'INTERMEDIATE',
        'share_basis': before, 'security_basis': 'ORDINARY_SOURCE_SHARE'})
    output = bundle.selections[0].model_copy(update={'state': 'DERIVED', 'observation_key': None,
        'definition': 'normalize._split_adjust', 'definition_revision': 'unimplemented-crossing-validator',
        'required_input_roles': ('reported',), 'share_basis': after,
        'basis_transition': BasisTransition(input_roles=('reported',), action_keys=('split',),
            evidence_keys=('disclosure',), reason=reason)})
    action = ActionEvidence(key='split', legal_identity='issuer:common:legal-event-1', security_class='common',
        old_shares_text='8', new_shares_text='1', evidence_keys=('disclosure',),
        date_witnesses=(DateWitness(role='FILED', date=date(2016, 3, 10), evidence_key='disclosure'),
                       DateWitness(role='EFFECTIVE', date=date(2016, 3, 16), evidence_key='disclosure')))
    coverage = bundle.coverage[0].model_copy(update={'assessment': 'PARTIAL', 'reason': reason})
    return validate_changed(bundle, observations=(observed,), selections=(raw, output), actions=(action,),
        dependencies=(CalculationDependency(parent_node_key=output.node_key, ordinal=0, role='reported', selection_node_key=raw.node_key),),
        coverage=(coverage,))


def assumed_graph(bundle=None):
    bundle = bundle if bundle is not None and bundle.document_evidence else document_bundle(bundle)
    strict = bundle.selections[0]
    assumed = strict.model_copy(update={'node_key': 'assumed', 'view': 'ASSUMED', 'state': 'ASSUMED',
        'observation_key': None, 'definition': 'existing.assumption', 'definition_revision': '1',
        'required_input_roles': ('strict-input',), 'assumption_keys': ('absence-policy',)})
    assumption = Assumption(key='absence-policy', definition=assumed.definition, definition_revision='1',
        reason='Explicit existing missing-input assumption', affected_input_roles=('strict-input',))
    coverage = bundle.coverage[0].model_copy(update={'view': 'ASSUMED'})
    return validate_changed(bundle, selections=(strict, assumed), assumptions=(assumption,),
        dependencies=(CalculationDependency(parent_node_key='assumed', ordinal=0, role='strict-input', selection_node_key=strict.node_key),),
        coverage=(*bundle.coverage, coverage))


def test_genuine_pre_extension_canonical_bytes_preserved():
    proof = json.loads((Path(__file__).parent/'fixtures/figure_history_contract1.json').read_text())
    bundle = ExactFigureBundle.model_validate_json(proof['canonical_json'])
    actual = json.dumps(bundle.model_dump(mode='json'), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    assert actual == proof['canonical_json']
    assert hashlib.sha256(actual.encode()).hexdigest() == proof['bundle_sha256']
    record = bundle.observations[0].model_dump(mode='json')
    record.pop('key'); record.pop('value_decimal_text')
    assert record == proof['observation_record']


@pytest.mark.parametrize('field', ['documents', 'observed-basis', 'intermediate', 'selected-basis', 'actions', 'assumptions'])
def test_contract1_rejects_new_semantics_instead_of_discarding(field):
    original = example_bundle()
    changes = {'documents': {'document_evidence': document_bundle().document_evidence},
        'observed-basis': {'observations': basis_graph().observations},
        'intermediate': {'selections': (original.selections[0].model_copy(update={'purpose': 'INTERMEDIATE'}),)},
        'selected-basis': {'selections': (original.selections[0].model_copy(update={'share_basis': ShareBasis(kind='NONE', incorporation='KNOWN')}),)},
        'actions': {'actions': basis_graph().actions}, 'assumptions': {'assumptions': assumed_graph().assumptions}}
    with pytest.raises(ValidationError, match='contract revision2'):
        validate_changed(original, **changes[field])


def test_document_cannot_be_numeric_even_if_nil():
    bundle = document_bundle()
    for nil, value in ((False, '100.0000'), (True, None)):
        with pytest.raises(ValidationError, match='nonnumeric document'):
            ReportedObservation.model_validate(bundle.observations[0].model_copy(update={
                'reference': bundle.document_evidence[0].reference, 'nil': nil, 'value_decimal_text': value}))


def test_partial_intermediate_actual_basis_and_ambiguous_dates_roundtrip():
    bundle = basis_graph()
    assert ExactFigureBundle.model_validate_json(bundle.model_dump_json()) == bundle
    assert len({n.metric_path for n in bundle.selections}) == 1
    assert bundle.actions[0].date_witnesses[0].date != bundle.actions[0].date_witnesses[1].date
    assert [n for n in bundle.selections if n.purpose == 'OUTPUT'][0].security_basis == 'PRIMARY_COMMON_SHARE'


@pytest.mark.parametrize('problem', ['FULL', 'wrong-reason', 'no-transition', 'wrong-role', 'duplicate-output',
    'duplicate-event', 'duplicate-operation', 'missing-witness', 'wrong-class', 'reported-basis'])
def test_unsupported_basis_graph_adversaries(problem):
    bundle = basis_graph()
    raw, output = sorted(bundle.selections, key=lambda n:n.purpose == 'OUTPUT')
    changes = {
        'FULL': {'coverage': (bundle.coverage[0].model_copy(update={'assessment':'FULL'}),)},
        'wrong-reason': {'coverage': (bundle.coverage[0].model_copy(update={'reason':'claimed-approved'}),)},
        'no-transition': {'selections': (raw, output.model_copy(update={'basis_transition':None}))},
        'wrong-role': {'selections': (raw, output.model_copy(update={'basis_transition':output.basis_transition.model_copy(update={'input_roles':('unrelated',)})}))},
        'duplicate-output': {'selections': (raw, output, output.model_copy(update={'node_key':'duplicate'}))},
        'duplicate-event': {'actions': (*bundle.actions, bundle.actions[0].model_copy(update={'key':'second-disclosure'}))},
        'duplicate-operation': {'selections': (raw, output.model_copy(update={'basis_transition':output.basis_transition.model_copy(update={'action_keys':('split','split')})}))},
        'missing-witness': {'document_evidence': ()},
        'wrong-class': {'actions': (bundle.actions[0].model_copy(update={'security_class':'preferred'}),)},
        'reported-basis': {'observations': (bundle.observations[0].model_copy(update={'share_basis':output.share_basis}),)},
    }
    with pytest.raises(ValidationError):
        validate_changed(bundle, **changes[problem])


def test_repeated_witnesses_are_one_action_and_unknown_incorporation_is_partial():
    bundle = basis_graph()
    second = bundle.document_evidence[0].model_copy(update={'key':'later-disclosure', 'reference':bundle.document_evidence[0].reference.model_copy(update={'source_document':'later-report'})})
    action = bundle.actions[0].model_copy(update={'evidence_keys':('disclosure','later-disclosure')})
    nodes = tuple(n.model_copy(update={'basis_transition':n.basis_transition.model_copy(update={'evidence_keys':action.evidence_keys})}) if n.basis_transition else n for n in bundle.selections)
    assert len(validate_changed(bundle, document_evidence=(*bundle.document_evidence,second), actions=(action,), selections=nodes).actions) == 1
    b = document_bundle()
    unknown = ShareBasis(kind='ORDINARY', security_class='common', incorporation='UNKNOWN')
    coverage = b.coverage[0].model_copy(update={'assessment':'PARTIAL', 'reason':'unknown-incorporation'})
    graph = validate_changed(b, observations=(b.observations[0].model_copy(update={'share_basis':unknown}),),
        selections=(b.selections[0].model_copy(update={'share_basis':unknown}),), coverage=(coverage,))
    with pytest.raises(ValidationError):
        validate_changed(graph, coverage=(coverage.model_copy(update={'assessment':'FULL'}),))


def test_explicit_strict_to_assumed_keeps_strict_output():
    bundle = assumed_graph()
    assert ExactFigureBundle.model_validate_json(bundle.model_dump_json()) == bundle
    assert next(n for n in bundle.selections if n.view=='STRICT').state == 'REPORTED'


def test_full_per_share_requires_actual_share_basis():
    bundle = document_bundle()
    unit = bundle.selections[0].unit.model_copy(update={'denominator':('xbrli:shares',)})
    with pytest.raises(ValidationError, match='explicit PARTIAL'):
        validate_changed(bundle, selections=(bundle.selections[0].model_copy(update={'unit':unit}),),
            observations=(bundle.observations[0].model_copy(update={'unit':unit}),))


@pytest.mark.parametrize('problem', ['missing', 'wrong-role', 'wrong-definition', 'undeclared-node', 'reverse', 'conservative', 'transitive'])
def test_assumption_adversaries(problem):
    bundle = assumed_graph()
    strict = next(n for n in bundle.selections if n.view=='STRICT')
    assumed = next(n for n in bundle.selections if n.view=='ASSUMED')
    changes = {}
    if problem in ('missing','wrong-role','wrong-definition'):
        changes['assumptions'] = () if problem=='missing' else (bundle.assumptions[0].model_copy(update=
            {'affected_input_roles':('invented',)} if problem=='wrong-role' else {'definition':'other'}),)
    elif problem=='undeclared-node': changes['selections']=(strict,assumed.model_copy(update={'assumption_keys':()}))
    else:
        middle_view='CONSERVATIVE' if problem in ('conservative','transitive') else 'STRICT'
        middle=assumed.model_copy(update={'node_key':'launder','view':middle_view,'state':'DERIVED','assumption_keys':(), 'metric_path':'other'})
        dep=CalculationDependency(parent_node_key='launder',ordinal=0,role='strict-input',selection_node_key='assumed')
        nodes=(*bundle.selections,middle); deps=(*bundle.dependencies,dep)
        coverage=(*bundle.coverage,bundle.coverage[0].model_copy(update={'view':middle_view,'metric_family':'other-family','metric_paths':('other',)}))
        middle=middle.model_copy(update={'metric_family':'other-family'}); nodes=(*bundle.selections,middle)
        if problem=='transitive':
            final=middle.model_copy(update={'node_key':'final','view':'STRICT','metric_path':'final'})
            nodes=(*nodes,final); deps=(*deps,dep.model_copy(update={'parent_node_key':'final','selection_node_key':'launder'}))
        changes=dict(selections=nodes,dependencies=deps,coverage=coverage)
    with pytest.raises(ValidationError): validate_changed(bundle,**changes)
