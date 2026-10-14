"""Self-contained declaration codec. All security/source/delivery IDs are synthetic."""
import copy
from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
import json
import hashlib

import pytest

from screener import evidence, primary_input_plan as codec
from screener.figure_history import ActionEvidence, DateWitness, ShareBasis
from screener.models import CapturedSource, NativeDimension, NativeXmlOrdinalLocation, UnavailablePrimaryInput
from screener.source_validity import SourceDeliveryRecord
from screener.sources.sec_json import decode_company_facts, ORIGIN_KEY
from screener.models import AdmittedPrimaryInput
from test_native_primary_inputs import CIK, document, fixture, mapping
from test_primary_input_targets import company_facts


def representative(count=1):
    raw, cf_source, tree, target = company_facts("-0.0000")
    originals = tree['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares']
    declarations, dependencies = [], [codec.PlanDependency(cf_source.artifact_sha256, len(raw),
        ('company_facts', 'target'), cf_source, 'a' * 64,
        SourceDeliveryRecord(3, 1, 2, 4, 5, 'root:company_facts', datetime(2023, 3, 24, 8, 0, tzinfo=timezone.utc)))]
    for index in range(count):
        body, request = fixture(str(index + 2) + '.06', inline=index % 2 == 0)
        captured = CapturedSource('primary_document', request.source.artifact_sha256,
            request.source.source_document, 20, 40 + index, 60 + index, 'EXACT_LEXICAL')
        request = replace(request, source=replace(request.source, captured_source=captured))
        doc = document(body, request, key=f'explanation{index}',
                       roles=('CORRECTION_EXPLANATION', 'LEGAL_ACTION', 'INCORPORATED_BASIS'))
        doc = doc.model_copy(update={'reference': doc.reference.model_copy(update={
            'source_published_at': datetime(2023, 3, 22, 9, 12, 13, 123456, tzinfo=timezone(timedelta(hours=2)))})})
        action = ActionEvidence(key=f'action{index}', legal_identity=f'legal{index}', security_class='ordinary',
            old_shares_text='8.0000', new_shares_text='1e+0', evidence_keys=(doc.key,),
            date_witnesses=(DateWitness(role='EFFECTIVE', date=date(2023, 1, 17), evidence_key=doc.key),
                            DateWitness(role='DISCLOSED', date=date(2023, 3, 22), evidence_key=doc.key)))
        basis = ShareBasis(kind='ORDINARY', security_class='ordinary', incorporation='KNOWN',
                           incorporated_actions=(action.key,), evidence_keys=(doc.key,))
        target = originals[99 - index]['_exact_sec_origin']
        declarations.append(replace(mapping(body, request), key=f'mapping{index}', documents=(doc,),
                                    actions=(action,), share_basis=basis, unary_factor_text='-1.0000', target_origin=target))
        dependencies.append(codec.PlanDependency(request.source.artifact_sha256, len(body),
            ('native_original', 'explanation'), captured, f'{index + 1:064x}',
            SourceDeliveryRecord(60 + index, 20, 40 + index, 80, 2, f'leaf:{index}',
                                datetime(2023, 3, 24, 9, index, tzinfo=timezone.utc))))
    return codec.PrimaryInputPlan(CIK, 91, tuple(declarations), tuple(dependencies))


def wire(plan=None):
    return json.loads(codec.encode_primary_plan(plan or representative()))


def decode(value):
    return codec.decode_primary_plan(json.dumps(value).encode())


def test_full_fields_canonical_roundtrip_and_immutable_copies():
    plan = representative(16)
    before = copy.deepcopy(plan)
    raw = codec.encode_primary_plan(plan)
    restored = codec.decode_primary_plan(raw)
    assert restored == plan == before and restored is not plan
    assert codec.encode_primary_plan(restored) == raw == codec.encode_primary_plan(plan)
    assert type(restored.declarations[0].original.location).__name__ == 'NativeInlineOrdinalLocation'
    assert isinstance(restored.declarations[1].original.location, NativeXmlOrdinalLocation)
    assert restored.declarations[0].target_origin.lexical_text == '-0.0000'
    for original, decoded in zip(plan.declarations, restored.declarations, strict=True):
        for field in fields(original):
            assert getattr(original, field.name) == getattr(decoded, field.name)
        assert original is not decoded and original.original is not decoded.original
        assert decoded.documents[0].reference.source_published_at.isoformat() == original.documents[0].reference.source_published_at.isoformat()
    assert len(raw) < codec.MAXIMUM_BYTES


def test_replacement_fields_and_private_xml_kind_roundtrip():
    plan = representative()
    _, replacement = fixture('3.01', inline=False)
    declaration = replace(plan.declarations[0], unary_factor_text=None, replacement=replacement)
    decoded = codec.decode_primary_plan(codec.encode_primary_plan(replace(plan, declarations=(declaration,))))
    assert decoded.declarations[0] == declaration
    assert isinstance(decoded.declarations[0].replacement.location, NativeXmlOrdinalLocation)


def test_unknown_and_unavailable_declarations_not_replaced_with_outcomes():
    body, request = fixture()
    declaration = mapping(body, request)
    plan = codec.PrimaryInputPlan(CIK, 1, (declaration,),
                                 (codec.PlanDependency(request.source.artifact_sha256, len(body), ('native',)),))
    decoded = codec.decode_primary_plan(codec.encode_primary_plan(plan))
    assert decoded.declarations[0] == declaration and decoded.dependencies[0].delivery is None
    assert decoded.dependencies[0].captured_source is None and decoded.dependencies[0].observation_sha256 is None
    result, = evidence.admit_primary_inputs(CIK, decoded.declarations, {})
    assert isinstance(result, UnavailablePrimaryInput) and result.original == request
    with pytest.raises(codec.PlanCodecError):
        codec.encode_primary_plan(replace(plan, declarations=(result,)))


def test_incomplete_old_captured_binding_is_preserved_without_success_claim():
    plan = representative()
    declaration = plan.declarations[0]
    declaration = replace(declaration, target_origin=replace(declaration.target_origin,
        source=replace(declaration.target_origin.source, occurrence_id=None)))
    dependency = replace(plan.dependencies[0], captured_source=replace(plan.dependencies[0].captured_source, occurrence_id=None), delivery=None)
    decoded = codec.decode_primary_plan(codec.encode_primary_plan(replace(plan, declarations=(declaration,), dependencies=(dependency,))))
    assert decoded.declarations[0].target_origin.source.occurrence_id is None
    assert decoded.dependencies[0].delivery is None


@pytest.mark.parametrize('token', ['-0.0000', '9007199254740993.1234567890123456789', '-1e-10000', '1e+10000'])
def test_exact_lexical_terms_are_never_float_or_normalized(token):
    plan = representative()
    declaration = replace(plan.declarations[0], unary_factor_text=token,
        target_origin=replace(plan.declarations[0].target_origin, lexical_text=token))
    decoded = codec.decode_primary_plan(codec.encode_primary_plan(replace(plan, declarations=(declaration,))))
    assert decoded.declarations[0].unary_factor_text == token
    assert decoded.declarations[0].target_origin.lexical_text == token


@pytest.mark.parametrize('path', [[], ['declarations', 0], ['declarations', 0, 'original'],
    ['declarations', 0, 'original', 'source'], ['declarations', 0, 'original', 'source', 'captured_source'],
    ['declarations', 0, 'original', 'context'], ['declarations', 0, 'original', 'location'],
    ['declarations', 0, 'target_origin'], ['declarations', 0, 'documents', 0],
    ['declarations', 0, 'documents', 0, 'reference'], ['declarations', 0, 'actions', 0],
    ['declarations', 0, 'share_basis'], ['dependencies', 0], ['dependencies', 0, 'delivery']])
@pytest.mark.parametrize('mutation', ['extra', 'missing'])
def test_exact_keys_at_all_owned_structural_boundaries(path, mutation):
    value = wire()
    node = value
    for token in path:
        node = node[token]
    if mutation == 'extra':
        node['unexpected'] = 1
    else:
        del node[next(iter(node))]
    with pytest.raises(codec.PlanCodecError):
        decode(value)


@pytest.mark.parametrize('path', [['security_id'], ['revision'], ['declarations', 0, 'original', 'location', 'element_ordinal'],
    ['declarations', 0, 'original', 'scale'], ['declarations', 0, 'original', 'source', 'byte_size'],
    ['dependencies', 0, 'byte_size'], ['dependencies', 0, 'captured_source', 'source_item_id'],
    ['dependencies', 0, 'delivery', 'occurrence_id'], ['dependencies', 0, 'delivery', 'ownership_generation']])
@pytest.mark.parametrize('bad', [True, 1.0, '1'])
def test_integer_identity_never_coerces(path, bad):
    value = wire()
    node = value
    for token in path[:-1]:
        node = node[token]
    node[path[-1]] = bad
    with pytest.raises(codec.PlanCodecError):
        decode(value)


@pytest.mark.parametrize('bad', ['20200101', '2020-W01-3', '2020-02-30', ' 2020-01-01 ', '', True])
@pytest.mark.parametrize('path', [['declarations', 0, 'original', 'source', 'filed'],
    ['declarations', 0, 'original', 'context', 'start'],
    ['declarations', 0, 'actions', 0, 'date_witnesses', 0, 'date']])
def test_calendar_dates_require_exact_supported_calendar(path, bad):
    value = wire()
    node = value
    for token in path[:-1]:
        node = node[token]
    node[path[-1]] = bad
    with pytest.raises(codec.PlanCodecError):
        decode(value)


@pytest.mark.parametrize('bad', ['2023-03-24T08:00:00', '20230324T08:00:00Z',
    '2023-03-24T08:00:00.1234567Z', '2023-02-30T08:00:00Z', '2023-03-24T25:00:00Z', '', True])
def test_exact_aware_delivery_timestamp(bad):
    value = wire()
    value['dependencies'][0]['delivery']['checked_at'] = bad
    with pytest.raises(codec.PlanCodecError):
        decode(value)


@pytest.mark.parametrize('bad', ['INLINE_XBRL', 'XBRL_XML', 'DOCUMENT_BYTES', 'other', None])
def test_unknown_public_or_private_locator_branch_rejects(bad):
    value = wire()
    value['declarations'][0]['original']['location']['kind'] = bad
    with pytest.raises(codec.PlanCodecError):
        decode(value)


@pytest.mark.parametrize('field,bad', [('kind', 'InputRecipe'), ('revision', 2), ('cik', '1066923'),
    ('cik', 1066923), ('security_id', 0)])
def test_header_is_private_and_exact(field, bad):
    value = wire()
    value[field] = bad
    with pytest.raises(codec.PlanCodecError):
        decode(value)


@pytest.mark.parametrize('kind', ['key', 'original', 'target', 'dependency', 'roles', 'operation'])
def test_duplicate_and_ambiguous_declarations_reject(kind):
    value = wire(representative(2))
    if kind == 'key':
        value['declarations'][1]['key'] = value['declarations'][0]['key']
    elif kind == 'original':
        value['declarations'][1]['original'] = value['declarations'][0]['original']
    elif kind == 'target':
        value['declarations'][1]['target_origin'] = value['declarations'][0]['target_origin']
    elif kind == 'dependency':
        value['dependencies'].append(copy.deepcopy(value['dependencies'][0]))
    elif kind == 'roles':
        value['dependencies'][0]['roles'] *= 2
    else:
        value['declarations'][0]['replacement'] = value['declarations'][0]['original']
    with pytest.raises(codec.PlanCodecError):
        decode(value)


@pytest.mark.parametrize('field', ['source_item_id', 'source_observation_id', 'occurrence_id'])
def test_dependency_source_and_delivery_tuple_must_agree(field):
    value = wire()
    value['dependencies'][0]['delivery'][field] += 1
    with pytest.raises(codec.PlanCodecError):
        decode(value)


def test_same_artifact_distinct_occurrences_preserve_all_delivery_fields():
    plan = representative()
    original = plan.dependencies[0]
    another = replace(original, captured_source=replace(original.captured_source, occurrence_id=11),
        delivery=replace(original.delivery, occurrence_id=11, job_id=12, ownership_generation=13,
                         item_key='root:second', checked_at=original.delivery.checked_at + timedelta(seconds=2)))
    restored = codec.decode_primary_plan(codec.encode_primary_plan(replace(plan, dependencies=(original, another))))
    assert restored.dependencies == (original, another)
    for before, after in zip((original, another), restored.dependencies, strict=True):
        for field in fields(before.delivery):
            assert getattr(before.delivery, field.name) == getattr(after.delivery, field.name)


@pytest.mark.parametrize('raw', [b'{"kind":1,"kind":2}', b'{"_exact_sec_origin":{}}', b'{"n":NaN}',
                               b'{"n":Infinity}', b'\xff', b'[]', b'[' * 2000 + b']' * 2000])
def test_strict_json_and_deep_malformed_return_codec_errors(raw):
    with pytest.raises(codec.PlanCodecError):
        codec.decode_primary_plan(raw)


def test_decode_byte_ceiling_before_tree_expansion(monkeypatch):
    raw = codec.encode_primary_plan(codec.PrimaryInputPlan(CIK, 1, ()))
    maximum = raw + b' ' * (codec.MAXIMUM_BYTES - len(raw))
    assert codec.decode_primary_plan(maximum).declarations == ()
    monkeypatch.setattr(codec, 'decode_object', lambda *a: pytest.fail('oversized bytes reached decoder'))
    with pytest.raises(codec.PlanCodecError):
        codec.decode_primary_plan(maximum + b' ')


def test_encode_byte_ceiling_and_mutable_field_errors():
    plan = representative()
    declaration = plan.declarations[0]
    request = replace(declaration.original, context=replace(declaration.original.context,
        dimensions=(NativeDimension('axis', 'typed', 'x' * codec.MAXIMUM_BYTES),)))
    with pytest.raises(codec.PlanCodecError):
        codec.encode_primary_plan(replace(plan, declarations=(replace(declaration, original=request),)))
    with pytest.raises(codec.PlanCodecError):
        codec.encode_primary_plan(replace(plan, declarations=(replace(declaration, key=['mutable']),)))


@pytest.mark.parametrize('field', ['start', 'end', 'filed'])
@pytest.mark.parametrize('raw_metadata', ['20201231', '2020-W53-4', '2020-02-30', '', 'unknown', None])
def test_raw_cf_metadata_preserves_existing_identity_guard_contract(field, raw_metadata):
    raw, source, _, _ = company_facts()
    tree = json.loads(raw)
    tree['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares'][99][field] = raw_metadata
    raw = json.dumps(tree).encode()
    source = replace(source, artifact_sha256=hashlib.sha256(raw).hexdigest())
    decoded = decode_company_facts(raw, source=source)
    origin = decoded['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares'][99][ORIGIN_KEY]
    body, request = fixture()
    declaration = replace(mapping(body, request), target_origin=origin)
    admitted, = evidence.admit_primary_inputs(CIK, (declaration,),
        {source.artifact_sha256: raw, request.source.artifact_sha256: body})
    assert isinstance(admitted, AdmittedPrimaryInput)
    plan = codec.PrimaryInputPlan(CIK, 1, (declaration,))
    restored = codec.decode_primary_plan(codec.encode_primary_plan(plan))
    assert getattr(restored.declarations[0].target_origin, field) == raw_metadata


@pytest.mark.parametrize('field', ['start', 'end', 'filed'])
@pytest.mark.parametrize('invalid', [True, 20201231, [], {}])
def test_raw_cf_metadata_still_requires_immutable_text(field, invalid):
    value = wire()
    value['declarations'][0]['target_origin'][field] = invalid
    with pytest.raises(codec.PlanCodecError):
        decode(value)


@pytest.mark.parametrize('collection', ['declarations', 'dependencies'])
def test_encoded_byte_budget_stops_before_serializing_unbounded_tail(monkeypatch, collection):
    plan = representative()
    declaration = replace(plan.declarations[0], key='x' * (codec.MAXIMUM_BYTES // 2))
    dependency = replace(plan.dependencies[0], roles=('x' * (codec.MAXIMUM_BYTES // 2),))
    plan = replace(plan, **{collection: (declaration, declaration, declaration) if collection == 'declarations'
                           else (dependency, dependency, dependency)})
    adapter = codec._MAPPING if collection == 'declarations' else codec._CAPTURED
    original = adapter.dump_python
    visited = []

    def counted(item, **kwargs):
        visited.append(item)
        return original(item, **kwargs)

    monkeypatch.setattr(adapter, 'dump_python', counted)
    with pytest.raises(codec.PlanCodecError, match='1MiB'):
        codec.encode_primary_plan(plan)
    assert len(visited) == 2


def anchored_plan():
    plan = representative()
    declaration = plan.declarations[0]
    basis = declaration.share_basis.model_copy(update=dict(
        anchor_evidence_key=declaration.documents[0].key, anchor_as_of=date(2022, 10, 19)))
    return replace(plan, declarations=(replace(declaration, share_basis=basis),))


@pytest.mark.parametrize('incorporation,expected_sha,expected_bytes', [
    ('KNOWN', '55219d72559c9e136e468757889f44fd2f974af9c5886638a8a083c21e9ad4ca', 4717),
    ('UNKNOWN', '651675e735d0753b58043fe7427dd5f55a07824d0206510850a52954b177a6a9', 4696)])
def test_pre_edit_revision1_known_unknown_exact_bytes(incorporation, expected_sha, expected_bytes):
    plan = representative()
    if incorporation == 'UNKNOWN':
        plan = replace(plan, declarations=(replace(plan.declarations[0], share_basis=ShareBasis(
            kind='ORDINARY', security_class='ordinary', incorporation='UNKNOWN')),))
    raw = codec.encode_primary_plan(plan)
    assert len(raw) == expected_bytes and hashlib.sha256(raw).hexdigest() == expected_sha
    assert json.loads(raw)['revision'] == 1 and b'anchor_' not in raw
    assert codec.encode_primary_plan(codec.decode_primary_plan(raw)) == raw


def test_explicit_anchor_uses_revision2_and_preserves_mixed_legacy_bases():
    anchored = anchored_plan()
    second = representative(2).declarations[1]
    plan = replace(anchored, declarations=(*anchored.declarations, second))
    raw = codec.encode_primary_plan(plan)
    value = json.loads(raw)
    assert value['revision'] == 2
    assert value['declarations'][0]['share_basis']['anchor_as_of'] == '2022-10-19'
    assert 'anchor_as_of' not in value['declarations'][1]['share_basis']
    assert codec.decode_primary_plan(raw) == plan
    assert codec.encode_primary_plan(codec.decode_primary_plan(raw)) == raw


@pytest.mark.parametrize('problem', ['revision1-anchor', 'revision2-without-anchor', 'one-sided', 'null-anchor', 'bad-date', 'unknown-field'])
def test_revision_and_anchor_wire_contract_refuses_ambiguous_shapes(problem):
    value = wire(anchored_plan())
    basis = value['declarations'][0]['share_basis']
    if problem == 'revision1-anchor':
        value['revision'] = 1
    elif problem == 'revision2-without-anchor':
        del basis['anchor_evidence_key']; del basis['anchor_as_of']
    elif problem == 'one-sided':
        del basis['anchor_as_of']
    elif problem == 'null-anchor':
        basis['anchor_evidence_key'] = None; basis['anchor_as_of'] = None
    elif problem == 'bad-date':
        basis['anchor_as_of'] = '20221019'
    else:
        basis['anchor_guess'] = 'invented'
    with pytest.raises(codec.PlanCodecError):
        decode(value)


def test_anchor_obeys_existing_native_text_and_byte_fences_before_mapping(monkeypatch):
    value = wire(anchored_plan())
    value['declarations'][0]['share_basis']['anchor_evidence_key'] = 'é' * (codec.NATIVE_PREPARATION_LIMITS['name_bytes'] // 2 + 1)
    monkeypatch.setattr(codec, '_mapping', lambda *args: pytest.fail('oversized anchor reached mapping'))
    with pytest.raises(codec.PlanCodecError, match='text ceiling'):
        codec.decode_native_preparation_plan(json.dumps(value).encode())
    with pytest.raises(codec.PlanCodecError, match='1MiB'):
        codec.decode_primary_plan(b' ' * (codec.MAXIMUM_BYTES + 1))
