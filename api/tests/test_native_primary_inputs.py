"""Private requested native bytes. All inputs are self-contained fixtures."""
import copy
import hashlib
import pickle
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext

import pytest

from screener import evidence, store, sync
from screener.figure_history import (ActionEvidence, DateWitness, DocumentEvidence,
                                     DocumentSpan, ShareBasis, SourceOccurrenceRef)
from screener.models import (AdmittedPrimaryInput, CapturedSource, Fact, NativeContext, NativeDimension,
                             NativeInlineOrdinalLocation, NativeNumericRequest, NativeSource,
                             NativeXmlOrdinalLocation, PrimaryInputMapping, Provenance,
                             UnavailablePrimaryInput)
from screener.sources.inline_xbrl import read_native_numeric

CIK = "0001066923"
ACCESSION = "0001213900-23-022083"
URL = "https://www.sec.gov/Archives/edgar/data/1066923/000121390023022083/annual.htm"
IX = "http://www.xbrl.org/2013/inlineXBRL"
TRANSFORM = "http://www.xbrl.org/inlineXBRL/transformation/2020-02-12"
XBRLI = "http://www.xbrl.org/2003/instance"
USD = "{http://www.xbrl.org/2003/iso4217}USD"
CONTEXT = NativeContext(CIK, "http://www.sec.gov/CIK", None, date(2020, 1, 1), date(2020, 12, 31))


def fixture(text="2.06", *, attrs="", resource="", qname="co:TotalDiluted", inline=True):
    context = f'''<x:context id="c"><x:entity><x:identifier scheme="http://www.sec.gov/CIK">{CIK}</x:identifier></x:entity><x:period><x:startDate>2020-01-01</x:startDate><x:endDate>2020-12-31</x:endDate></x:period></x:context>'''
    unit = '<x:unit id="u"><x:measure>iso:USD</x:measure></x:unit>'
    if inline:
        numeric = f'''<ix:nonFraction name="{qname}" contextRef="c" unitRef="u" decimals="2" format="t:num-dot-decimal" {attrs}>{text}</ix:nonFraction>'''
        numeric = '<ix:nonFraction name="co:Unrequested" xsi:nil="true"/>' + numeric
        tag = "html"
    else:
        numeric = f'<co:TotalDiluted contextRef="c" unitRef="u" decimals="2" {attrs}>{text}</co:TotalDiluted>'
        tag = "x:xbrl"
    body = (f'''<{tag} xmlns:x="{XBRLI}" xmlns:ix="{IX}" xmlns:t="{TRANSFORM}" xmlns:co="urn:custom" xmlns:iso="http://www.xbrl.org/2003/iso4217" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:d="http://xbrl.org/2006/xbrldi" xmlns:a="urn:axis">{context}{unit}{resource}{numeric}<p>Verified role explanation</p></{tag}>''').encode()
    source = NativeSource(hashlib.sha256(body).hexdigest(), len(body), CIK, ACCESSION,
                          "10-K/A", date(2023, 3, 24), URL, ACCESSION, "10-K/A", date(2023, 3, 24))
    location = (NativeInlineOrdinalLocation(1, "{urn:custom}TotalDiluted", "c", "u") if inline
                else NativeXmlOrdinalLocation(2, "{urn:custom}TotalDiluted", "c", "u"))
    request = NativeNumericRequest(source, location, CONTEXT, (USD,), decimals="2",
                                   transformation=f"{{{TRANSFORM}}}num-dot-decimal" if inline else None)
    return body, request


def bind(body, request):
    return replace(request, source=replace(request.source, artifact_sha256=hashlib.sha256(body).hexdigest(), byte_size=len(body)))


def period_scalar_fixture(scalar, text, *, inline=True, expected_missing=False):
    body, request = fixture(inline=inline)
    if scalar == "instant":
        body = body.replace(
            b"<x:startDate>2020-01-01</x:startDate><x:endDate>2020-12-31</x:endDate>",
            f"<x:instant>{text}</x:instant>".encode())
        request = replace(request, context=replace(request.context, start=None, end=None,
                          instant=None if expected_missing else date(2020, 12, 31)))
    else:
        original = "2020-01-01" if scalar == "startDate" else "2020-12-31"
        body = body.replace(f"<x:{scalar}>{original}</x:{scalar}>".encode(),
                            f"<x:{scalar}>{text}</x:{scalar}>".encode())
        if expected_missing:
            request = replace(request, context=replace(request.context,
                              **{"start" if scalar == "startDate" else "end": None}))
    return body, bind(body, request)


def document(body, request, *, roles=("CORRECTION_EXPLANATION",), key="explanation"):
    text = b"Verified role explanation"
    offset = body.index(text)
    return DocumentEvidence(key=key, roles=roles, reference=SourceOccurrenceRef(
        artifact_sha256=request.source.artifact_sha256, extraction_revision="fixture-v1",
        source_system="SEC", issuer_source_identifier=CIK, source_document=URL,
        annual_wrapper=ACCESSION, fidelity="EXACT_LEXICAL",
        location=DocumentSpan(start_byte=offset, end_byte=offset + len(text), span_sha256=hashlib.sha256(text).hexdigest())))


def mapping(body, request, **kwargs):
    return PrimaryInputMapping("diluted", "us-gaap", "EarningsPerShareDiluted", request,
                               documents=(document(body, request),), **kwargs)


@pytest.mark.parametrize("inline", [False, True])
def test_exact_original_locator_and_copied_input(inline):
    body, request = fixture(inline=inline)
    result = read_native_numeric(body, request)
    assert result.value == Decimal("2.06") and result.lexical_text == "2.06"
    assert result.decoded_text == "2.06" and result.request == request
    assert result.request is not request and result.request.source is not request.source
    assert pickle.loads(pickle.dumps(result)) == result
    assert copy.deepcopy(result) == result


@pytest.mark.parametrize("text,sign,scale,expected", [
    ("123456789012345678901234567890.123456789", "-", 2, "-12345678901234567890123456789012.3456789"),
    ("0.000", "-", 2, "-0.0"), ("0.000", None, -2, "0.00000"),
    ("1,234.5600", None, 0, "1234.5600"),
])
def test_sign_scale_exact_digits_and_signed_zero(text, sign, scale, expected):
    attrs = f'scale="{scale}"' + (f' sign="{sign}"' if sign else "")
    body, request = fixture(text, attrs=attrs)
    request = replace(request, sign=sign, scale=scale)
    with localcontext() as context:
        context.prec = 3
        actual = read_native_numeric(body, request)
    assert actual.lexical_text == text
    assert actual.value.as_tuple() == Decimal(expected).as_tuple()
    assert actual.decoded_text == expected


@pytest.mark.parametrize("text", ["-0.000", "123456789012345678901234567890.987654321", "1e-10000", "1e+10000"])
def test_xml_exact_precision_and_extreme_exponent(text):
    body, request = fixture(text, inline=False)
    assert read_native_numeric(body, request).value.as_tuple() == Decimal(text).as_tuple()


@pytest.mark.parametrize("field,value", [
    ("artifact_sha256", "0" * 64), ("byte_size", 1), ("byte_size", True),
    ("cik", "0000000001"), ("accession", "0000000001-23-000001"),
    ("source_document", "file:///private/source"), ("annual_form", "8-K"),
    ("filed", "2023-03-24"),
])
def test_wrong_source_identity_rejected(field, value):
    body, request = fixture()
    with pytest.raises(ValueError):
        read_native_numeric(body, replace(request, source=replace(request.source, **{field: value})))


@pytest.mark.parametrize("field,value", [
    ("element_ordinal", 0), ("element_ordinal", 100), ("element_ordinal", -1),
    ("element_ordinal", True), ("fact_qname", "{urn:other}TotalDiluted"),
    ("context_ref", "missing"), ("unit_ref", "missing"), ("fact_id", "invented"),
])
def test_wrong_locator_even_equal_value_rejected(field, value):
    body, request = fixture()
    with pytest.raises(ValueError):
        read_native_numeric(body, replace(request, location=replace(request.location, **{field: value})))


@pytest.mark.parametrize("field,value", [
    ("entity_identifier", "0000000001"), ("entity_scheme", "urn:untrusted"),
    ("start", date(2019, 1, 1)), ("end", date(2021, 12, 31)),
    ("instant", date(2020, 12, 31)),
    ("dimensions", (NativeDimension("{urn:axis}Axis", "explicit", "{urn:axis}Member"),)),
])
def test_expected_context_must_match_actual(field, value):
    body, request = fixture()
    with pytest.raises(ValueError):
        read_native_numeric(body, replace(request, context=replace(CONTEXT, **{field: value})))


@pytest.mark.parametrize("old,new", [
    ("http://www.sec.gov/CIK", "urn:untrusted"),
    ("2020-12-31", "2021-12-31"),
    ("http://www.xbrl.org/2003/iso4217", "urn:wrong-currency"),
    ("t:num-dot-decimal", "t:fixed-zero"),
    ('decimals="2"', 'decimals="bad"'),
    ('xmlns:co="urn:custom"', 'xmlns:co="urn:changed"'),
])
def test_changed_verified_body_still_must_match_expected_context(old, new):
    body, request = fixture()
    body = body.replace(old.encode(), new.encode())
    with pytest.raises(ValueError):
        read_native_numeric(body, bind(body, request))


@pytest.mark.parametrize("attrs", ['xsi:nil="true"', 'continuedAt="later"', 'sign="+"',
                                   'scale="1.5"', 'precision="3"', 'id="unexpected"'])
def test_unsupported_numeric_shapes(attrs):
    body, request = fixture(attrs=attrs)
    with pytest.raises(ValueError):
        read_native_numeric(body, request)


@pytest.mark.parametrize("text", ["1,23", "NaN", "Infinity", "-2.06", "1e2", "<b>2.06</b>", ""])
def test_unsupported_native_transformation_text(text):
    body, request = fixture(text)
    with pytest.raises(ValueError):
        read_native_numeric(body, request)


@pytest.mark.parametrize("resource", [
    '<x:unit id="u"><x:measure>iso:USD</x:measure></x:unit>',
    f'<x:context id="c"><x:entity><x:identifier scheme="http://www.sec.gov/CIK">{CIK}</x:identifier></x:entity><x:period><x:instant>2020-12-31</x:instant></x:period></x:context>',
])
def test_duplicate_resource_identity_rejected_even_if_equal(resource):
    body, request = fixture(resource=resource)
    with pytest.raises(ValueError, match="duplicate"):
        read_native_numeric(body, request)


def test_namespace_bindings_scoped_at_occurrence_and_resources():
    body, request = fixture()
    body = body.replace(b'<ix:nonFraction name="co:TotalDiluted"', b'<ix:nonFraction xmlns:co="urn:local" name="co:TotalDiluted"')
    request = bind(body, replace(request, location=replace(request.location, fact_qname="{urn:local}TotalDiluted")))
    assert read_native_numeric(body, request).value == Decimal("2.06")


@pytest.mark.parametrize("kind", ["explicit", "typed"])
def test_actual_dimensions_and_changed_member_namespace(kind):
    body, request = fixture()
    member = ('<d:explicitMember dimension="a:Axis">a:Member</d:explicitMember>' if kind == "explicit"
              else '<d:typedMember dimension="a:Axis"><a:Domain>actual</a:Domain></d:typedMember>')
    body = body.replace(b'</x:entity>', f'<x:segment>{member}</x:segment></x:entity>'.encode())
    value = "{urn:axis}Member" if kind == "explicit" else '<ns0:Domain xmlns:ns0="urn:axis">actual</ns0:Domain>'
    request = bind(body, replace(request, context=replace(CONTEXT, dimensions=(NativeDimension("{urn:axis}Axis", kind, value),))))
    assert read_native_numeric(body, request).value == Decimal("2.06")
    changed = body.replace(b'xmlns:a="urn:axis"', b'xmlns:a="urn:changed"')
    with pytest.raises(ValueError, match="context"):
        read_native_numeric(changed, bind(changed, request))


def test_actual_fact_id_unique_and_missing_resource_negatives():
    body, request = fixture(attrs='id="actual"')
    request = replace(request, location=replace(request.location, fact_id="actual"))
    assert read_native_numeric(body, request).value == Decimal("2.06")
    duplicate = body.replace(b'<p>', b'<p id="actual">')
    with pytest.raises(ValueError, match="duplicate"):
        read_native_numeric(duplicate, bind(duplicate, request))
    missing = body.replace(b'id="c"', b'id="other"')
    with pytest.raises(ValueError, match="missing"):
        read_native_numeric(missing, bind(missing, request))


@pytest.mark.parametrize("old,new", [
    ("<x:endDate>2020-12-31</x:endDate>", "<x:endDate>bad</x:endDate>"),
    ("<x:startDate>2020-01-01</x:startDate>", "<x:startDate>2021-01-01</x:startDate>"),
    ("<x:measure>iso:USD</x:measure>", "<x:measure>iso:USD</x:measure><x:divide/>"),
    ("</x:entity>", "<x:segment><co:UnknownScope>unknown</co:UnknownScope></x:segment></x:entity>"),
    ("<ix:nonFraction name=\"co:TotalDiluted\"", "<ix:nonFraction name=\"unbound:TotalDiluted\""),
])
def test_malformed_native_resources_and_scoped_names_are_unavailable(old, new):
    body, request = fixture()
    body = body.replace(old.encode(), new.encode())
    with pytest.raises(ValueError):
        read_native_numeric(body, bind(body, request))


def test_wrong_transform_namespace_cannot_be_declared_as_supported():
    body, request = fixture()
    body = body.replace(TRANSFORM.encode(), b"urn:wrong-transform")
    request = bind(body, replace(request, transformation="{urn:wrong-transform}num-dot-decimal"))
    with pytest.raises(ValueError, match="unsupported"):
        read_native_numeric(body, request)


def test_divided_currency_shares_unit_requires_actual_measure_namespaces():
    body, request = fixture()
    body = body.replace(b'<x:measure>iso:USD</x:measure>',
        b'<x:divide><x:unitNumerator><x:measure>iso:USD</x:measure></x:unitNumerator><x:unitDenominator><x:measure>x:shares</x:measure></x:unitDenominator></x:divide>')
    request = bind(body, replace(request, unit_denominator=(f"{{{XBRLI}}}shares",)))
    assert read_native_numeric(body, request).value == Decimal("2.06")
    changed = body.replace(b'<x:measure>x:shares</x:measure>', b'<x:measure>co:shares</x:measure>')
    with pytest.raises(ValueError, match="unit"):
        read_native_numeric(changed, bind(changed, request))


def test_fraction_or_non_xml_body_cannot_repair_requested_original():
    body, request = fixture()
    for changed in (body.replace(b'ix:nonFraction', b'ix:fraction'), b'<html><malformed>'):
        with pytest.raises(ValueError):
            read_native_numeric(changed, bind(changed, request))


def test_declared_role_admission_does_not_apply_or_change_taxonomy():
    body, request = fixture()
    plan = mapping(body, request, unary_factor_text="1000")
    before = copy.deepcopy(plan)
    result, = evidence.admit_primary_inputs(CIK, (plan,), {request.source.artifact_sha256: body})
    assert isinstance(result, AdmittedPrimaryInput)
    assert result.original.value == Decimal("2.06") and result.mapping.unary_factor_text == "1000"
    assert plan == before and result.mapping == plan and result.mapping is not plan
    assert result.mapping.documents[0] is not plan.documents[0]


def test_replacement_keeps_both_occurrences_and_actual_custom_qname():
    body, request = fixture()
    replacement_body, replacement = fixture("2.33")
    plan = mapping(body, request, replacement=replacement)
    result, = evidence.admit_primary_inputs(CIK, (plan,), {request.source.artifact_sha256: body,
                                                        replacement.source.artifact_sha256: replacement_body})
    assert isinstance(result, AdmittedPrimaryInput)
    assert result.original.value == Decimal("2.06") and result.replacement.value == Decimal("2.33")


@pytest.mark.parametrize("problem", ["missing-bytes", "wrong-bytes", "wrong-span", "missing-doc", "wrong-issuer",
                                    "bad-factor", "duplicate-key", "duplicate-occurrence", "replacement-context"])
def test_declared_invalid_mapping_explicitly_unavailable(problem):
    body, request = fixture()
    plan = mapping(body, request)
    payloads = {request.source.artifact_sha256: body}
    plans = (plan,)
    if problem == "missing-bytes":
        payloads = {}
    elif problem == "wrong-bytes":
        payloads[request.source.artifact_sha256] = body + b" "
    elif problem == "wrong-span":
        doc = plan.documents[0]
        doc = doc.model_copy(update={"reference": doc.reference.model_copy(update={"location": doc.reference.location.model_copy(update={"span_sha256": "0" * 64})})})
        plans = (replace(plan, documents=(doc,)),)
    elif problem == "missing-doc":
        plans = (replace(plan, documents=()),)
    elif problem == "wrong-issuer":
        doc = plan.documents[0]
        plans = (replace(plan, documents=(doc.model_copy(update={"reference": doc.reference.model_copy(update={"issuer_source_identifier": "1"})}),)),)
    elif problem == "bad-factor":
        plans = (replace(plan, unary_factor_text="0"),)
    elif problem == "duplicate-key":
        plans = (plan, plan)
    elif problem == "duplicate-occurrence":
        plans = (plan, replace(plan, key="other"))
    elif problem == "replacement-context":
        plans = (replace(plan, replacement=replace(request, context=replace(CONTEXT, end=date(2021, 12, 31)))),)
    results = evidence.admit_primary_inputs(CIK, plans, payloads)
    assert len(results) == len(plans)
    assert all(isinstance(result, UnavailablePrimaryInput) and result.reason for result in results)
    assert all(result.original == plan.original and result.original is not plan.original
               for result, plan in zip(results, plans, strict=True))


def test_repeated_witnesses_one_action_and_unknown_incorporation_retained():
    body, request = fixture()
    docs = tuple(document(body, request, roles=("CORRECTION_EXPLANATION", "LEGAL_ACTION", "INCORPORATED_BASIS"), key=k) for k in ("march10", "march16"))
    action = ActionEvidence(key="action16", legal_identity="legal16", security_class="ordinary",
                            old_shares_text="8", new_shares_text="1", evidence_keys=("march10", "march16"),
                            date_witnesses=(DateWitness(role="FILED", date=date(2016, 3, 10), evidence_key="march10"),
                                            DateWitness(role="EFFECTIVE", date=date(2016, 3, 16), evidence_key="march16")))
    basis = ShareBasis(kind="ORDINARY", security_class="ordinary", incorporation="UNKNOWN", evidence_keys=("march10",))
    plan = replace(mapping(body, request), documents=docs, actions=(action,), share_basis=basis)
    result, = evidence.admit_primary_inputs(CIK, (plan,), {request.source.artifact_sha256: body})
    assert isinstance(result, AdmittedPrimaryInput)
    assert result.mapping.share_basis.incorporation == "UNKNOWN"
    assert result.mapping.actions[0].date_witnesses == action.date_witnesses
    duplicate, = evidence.admit_primary_inputs(CIK, (replace(plan, actions=(action, action.model_copy(update={"key": "again"}))),), {request.source.artifact_sha256: body})
    assert isinstance(duplicate, UnavailablePrimaryInput)


def test_negative_unary_factor_is_retained_without_applying_sign_math():
    body, request = fixture()
    result, = evidence.admit_primary_inputs(CIK, (mapping(body, request, unary_factor_text="-1"),),
                                           {request.source.artifact_sha256: body})
    assert isinstance(result, AdmittedPrimaryInput)
    assert result.mapping.unary_factor_text == "-1" and result.original.value == Decimal("2.06")


def test_unrelated_duplicate_html_ids_do_not_invalidate_requested_occurrence():
    body, request = fixture(resource='<p id="html-only"/><p id="html-only"/>')
    assert read_native_numeric(body, request).value == Decimal("2.06")


@pytest.mark.parametrize("field,value", [
    ("role", ["mutable"]), ("fidelity", ["mutable"]),
    ("artifact_sha256", ["mutable"]), ("source_document", ["mutable"]),
    ("source_item_id", [1]), ("source_observation_id", True), ("occurrence_id", 3.0),
    ("role", ""), ("fidelity", " "),
])
def test_requested_captured_reference_requires_complete_immutable_fields(field, value):
    body, request = fixture()
    captured = CapturedSource("primary", request.source.artifact_sha256,
                              request.source.source_document, 1, 2, 3, "EXACT_LEXICAL")
    request = replace(request, source=replace(request.source, captured_source=replace(captured, **{field: value})))
    with pytest.raises(ValueError, match="captured"):
        read_native_numeric(body, request)
    unavailable, = evidence.admit_primary_inputs(CIK, (mapping(body, request),),
                                                {request.source.artifact_sha256: body})
    assert isinstance(unavailable, UnavailablePrimaryInput) and unavailable.original == request


def test_structural_captured_reference_valid_neighbor_without_sql_claim():
    body, request = fixture()
    captured = CapturedSource("primary", request.source.artifact_sha256,
                              request.source.source_document, 1, 2, 3, "EXACT_LEXICAL")
    request = replace(request, source=replace(request.source, captured_source=captured))
    actual = read_native_numeric(body, request)
    assert actual.request.source.captured_source == captured
    assert actual.request.source.captured_source is not captured
    assert pickle.loads(pickle.dumps(actual)) == copy.deepcopy(actual) == actual


@pytest.mark.parametrize("old,new", [
    (b'2020-12-31</x:endDate>', b'2020-12-31<co:Unexpected>2021-12-31</co:Unexpected></x:endDate>'),
    (b'0001066923</x:identifier>', b'0001066923<co:Unexpected>other</co:Unexpected></x:identifier>'),
    (b'2020-01-01</x:startDate>', b'2020-01-01<co:Unexpected>2019-01-01</co:Unexpected></x:startDate>'),
    (b'</x:endDate></x:period>', b'</x:endDate>2021-12-31</x:period>'),
])
def test_native_scalar_context_cannot_hide_conflicting_nested_content(old, new):
    body, request = fixture()
    body = body.replace(old, new)
    request = bind(body, request)
    with pytest.raises(ValueError, match="scalar"):
        read_native_numeric(body, request)
    unavailable, = evidence.admit_primary_inputs(CIK, (mapping(body, request),),
                                                {request.source.artifact_sha256: body})
    assert isinstance(unavailable, UnavailablePrimaryInput) and unavailable.original == request


def test_instant_scalar_neighbor_and_nested_conflict():
    body, request = fixture()
    body = body.replace(b'<x:startDate>2020-01-01</x:startDate><x:endDate>2020-12-31</x:endDate>',
                        b'<x:instant>2020-12-31</x:instant>')
    request = bind(body, replace(request, context=replace(CONTEXT, start=None, end=None, instant=date(2020, 12, 31))))
    assert read_native_numeric(body, request).value == Decimal("2.06")
    changed = body.replace(b'2020-12-31</x:instant>', b'2020-12-31<co:Unexpected>other</co:Unexpected></x:instant>')
    with pytest.raises(ValueError, match="scalar"):
        read_native_numeric(changed, bind(changed, request))


@pytest.mark.parametrize("old,new", [
    (b"</x:identifier>", b"</x:identifier>0000000001"),
    (b"<x:entity>", b"<x:entity>0000000001"),
    (b'<x:context id="c">', b'<x:context id="c">0000000001'),
    (b"</x:entity>", b"</x:entity>0000000001"),
    (b"</x:period>", b"</x:period>2021-12-31"),
])
def test_requested_structural_containers_cannot_discard_mixed_content(old, new):
    body, request = fixture()
    body = body.replace(old, new)
    request = bind(body, request)
    with pytest.raises(ValueError, match="scalar"):
        read_native_numeric(body, request)
    unavailable, = evidence.admit_primary_inputs(CIK, (mapping(body, request),),
                                                {request.source.artifact_sha256: body})
    assert isinstance(unavailable, UnavailablePrimaryInput) and unavailable.original == request


@pytest.mark.parametrize("container", ["segment", "scenario"])
@pytest.mark.parametrize("mixed", [None, "leading", "tail"])
def test_dimension_container_whitespace_neighbor_and_mixed_conflicts(container, mixed):
    body, request = fixture()
    member = '<d:explicitMember dimension="a:Axis">a:Member</d:explicitMember>'
    fragment = f'<x:{container}> \n\t{member} \n\t</x:{container}>'.encode()
    close = b'</x:entity>' if container == "segment" else b'</x:period>'
    body = body.replace(close, fragment + close if container == "segment" else close + fragment)
    request = bind(body, replace(request, context=replace(CONTEXT,
        dimensions=(NativeDimension("{urn:axis}Axis", "explicit", "{urn:axis}Member"),))))
    if mixed:
        old = f'<x:{container}>' if mixed == "leading" else '</d:explicitMember>'
        body = body.replace(old.encode(), (old + "unexpected").encode())
        request = bind(body, request)
        with pytest.raises(ValueError, match="scalar"):
            read_native_numeric(body, request)
        result, = evidence.admit_primary_inputs(CIK, (mapping(body, request),),
                                               {request.source.artifact_sha256: body})
        assert isinstance(result, UnavailablePrimaryInput) and result.original == request
    else:
        result = read_native_numeric(body, request)
        assert result.request.context == request.context
        admitted, = evidence.admit_primary_inputs(CIK, (mapping(body, request),),
                                                 {request.source.artifact_sha256: body})
        assert isinstance(admitted, AdmittedPrimaryInput)


@pytest.mark.parametrize("mixed", [None, "leading", "tail"])
def test_typed_dimension_wrapper_and_actual_direct_period_leaves(mixed):
    body, request = fixture()
    # Typed domain values can themselves contain date-named elements; these are not context dates.
    fragment = '<x:segment><d:typedMember dimension="a:Axis"><a:Domain><x:endDate>1990-12-31</x:endDate></a:Domain></d:typedMember></x:segment>'
    body = body.replace(b'</x:entity>', fragment.encode() + b'</x:entity>')
    member = '<ns0:Domain xmlns:ns0="urn:axis" xmlns:ns1="http://www.xbrl.org/2003/instance"><ns1:endDate>1990-12-31</ns1:endDate></ns0:Domain>'
    request = bind(body, replace(request, context=replace(CONTEXT,
        dimensions=(NativeDimension("{urn:axis}Axis", "typed", member),))))
    if mixed:
        old = '<d:typedMember dimension="a:Axis">' if mixed == "leading" else '</a:Domain>'
        body = body.replace(old.encode(), (old + "unexpected").encode())
        request = bind(body, request)
        with pytest.raises(ValueError, match="scalar"):
            read_native_numeric(body, request)
        result, = evidence.admit_primary_inputs(CIK, (mapping(body, request),),
                                               {request.source.artifact_sha256: body})
        assert isinstance(result, UnavailablePrimaryInput) and result.original == request
    else:
        result = read_native_numeric(body, request)
        assert result.request.context.end == date(2020, 12, 31)
        admitted, = evidence.admit_primary_inputs(CIK, (mapping(body, request),),
                                                 {request.source.artifact_sha256: body})
        assert isinstance(admitted, AdmittedPrimaryInput)


@pytest.mark.parametrize("label,old,new", [
    ("context-leading", b'<x:context id="c">', b'<x:context id="c">unrepresented-scope'),
    ("entity-tail", b'</x:entity>', b'</x:entity>unrepresented-entity'),
    ("period-tail", b'</x:period>', b'</x:period>unrepresented-period'),
    ("unit-leading", b'<x:unit id="u">', b'<x:unit id="u">CNY'),
    ("measure-tail", b'</x:measure>', b'</x:measure>CNY'),
    ("segment-leading", b'</x:entity>', b'<x:segment>unrepresented-scope</x:segment></x:entity>'),
    ("scenario-leading", b'</x:context>', b'<x:scenario>unrepresented-scope</x:scenario></x:context>'),
])
def test_requested_adjacent_resource_text_and_whitespace_neighbors(label, old, new):
    body, request = fixture()
    changed = body.replace(old, new)
    invalid = bind(changed, request)
    with pytest.raises(ValueError, match="scalar"):
        read_native_numeric(changed, invalid)
    unavailable, = evidence.admit_primary_inputs(CIK, (mapping(changed, invalid),),
                                                {invalid.source.artifact_sha256: changed})
    assert isinstance(unavailable, UnavailablePrimaryInput) and unavailable.original == invalid
    whitespace = (b'<x:segment> \n\t</x:segment></x:entity>' if label == "segment-leading" else
                  b'<x:scenario> \n\t</x:scenario></x:context>' if label == "scenario-leading" else old + b' \n\t')
    neighbor = body.replace(old, whitespace)
    valid = bind(neighbor, request)
    assert read_native_numeric(neighbor, valid).value == Decimal("2.06")
    admitted, = evidence.admit_primary_inputs(CIK, (mapping(neighbor, valid),),
                                             {valid.source.artifact_sha256: neighbor})
    assert isinstance(admitted, AdmittedPrimaryInput)


@pytest.mark.parametrize("boundary", ["<x:unit id=\"u\">", "<x:divide>", "<x:unitNumerator>",
                                    "<x:unitDenominator>", "</x:measure>", "</x:unitNumerator>",
                                    "</x:unitDenominator>", "</x:divide>"])
def test_divided_unit_structural_text_and_whitespace_neighbors(boundary):
    body, request = fixture()
    body = body.replace(b'<x:measure>iso:USD</x:measure>',
        b'<x:divide><x:unitNumerator><x:measure>iso:USD</x:measure></x:unitNumerator><x:unitDenominator><x:measure>x:shares</x:measure></x:unitDenominator></x:divide>')
    request = bind(body, replace(request, unit_denominator=(f"{{{XBRLI}}}shares",)))
    invalid_body = body.replace(boundary.encode(), (boundary + "CNY").encode())
    invalid = bind(invalid_body, request)
    with pytest.raises(ValueError, match="scalar"):
        read_native_numeric(invalid_body, invalid)
    unavailable, = evidence.admit_primary_inputs(CIK, (mapping(invalid_body, invalid),),
                                                {invalid.source.artifact_sha256: invalid_body})
    assert isinstance(unavailable, UnavailablePrimaryInput) and unavailable.original == invalid
    neighbor = body.replace(boundary.encode(), (boundary + " \n\t").encode())
    valid = bind(neighbor, request)
    assert read_native_numeric(neighbor, valid).value == Decimal("2.06")
    admitted, = evidence.admit_primary_inputs(CIK, (mapping(neighbor, valid),),
                                             {valid.source.artifact_sha256: neighbor})
    assert isinstance(admitted, AdmittedPrimaryInput)


def test_unselected_malformed_resources_and_requested_whitespace_stay_valid():
    resource = ('<x:context id="unselected">other<x:entity>other<x:identifier scheme="http://www.sec.gov/CIK">other<co:Unexpected>nested</co:Unexpected></x:identifier>other</x:entity><x:period>other<x:instant>2021-12-31</x:instant></x:period></x:context>'
                '<x:unit id="other">CNY<x:measure>iso:CNY</x:measure>other</x:unit>')
    body, request = fixture(resource=resource)
    for boundary in ('<x:context id="c">', '<x:entity>', '</x:entity>', '</x:identifier>',
                     '<x:period>', '</x:startDate>', '</x:endDate>', '</x:period>'):
        body = body.replace(boundary.encode(), (boundary + " \n\t").encode())
    request = bind(body, request)
    actual = read_native_numeric(body, request)
    assert actual.request.context == request.context and actual.value == Decimal("2.06")
    admitted, = evidence.admit_primary_inputs(CIK, (mapping(body, request),),
                                             {request.source.artifact_sha256: body})
    assert isinstance(admitted, AdmittedPrimaryInput)


def test_list_annotations_are_copied_to_immutable_tuples():
    body, request = fixture()
    dimensions, numerator, documents, actions = [], [USD], [document(body, request)], []
    request = replace(request, context=replace(CONTEXT, dimensions=dimensions), unit_numerator=numerator)
    plan = replace(mapping(body, request), documents=documents, actions=actions)
    dimensions.append(NativeDimension("{urn:axis}Axis", "explicit", "{urn:axis}Member"))
    numerator.clear()
    documents.clear()
    actions.append("invalid")
    result, = evidence.admit_primary_inputs(CIK, (plan,), {request.source.artifact_sha256: body})
    assert isinstance(result, AdmittedPrimaryInput)
    assert result.mapping.documents and result.mapping.actions == ()
    assert result.original.request.context.dimensions == ()
    assert result.original.request.unit_numerator == (USD,)


def test_loader_absent_default_and_supplied_result_do_not_mutate_facts(tmp_path):
    class Edgar:
        cache_dir = tmp_path
        def company_facts(self, cik):
            raise AssertionError("source fetch forbidden")
    connection = store.connect(tmp_path / "test.db")
    loader = evidence.EvidenceLoader(connection, Edgar())
    facts = {"cik": int(CIK), "facts": {}}
    before = copy.deepcopy(facts)
    absent = loader.load(CIK, "FTFT", facts)
    assert absent.primary_inputs == () and facts == before
    body, request = fixture()
    result = loader.load(CIK, "FTFT", facts, primary_mappings=(mapping(body, request),),
                         primary_payloads={request.source.artifact_sha256: body})
    assert isinstance(result.primary_inputs[0], AdmittedPrimaryInput)
    assert result.facts == absent.facts and facts == before
    assert pickle.loads(pickle.dumps(result)).primary_inputs == result.primary_inputs
    connection.close()


def test_existing_fact_serialization_is_unchanged_and_has_no_private_metadata():
    provenance = Provenance("EPS", "co:TotalDiluted", 2020, "10-K/A", ACCESSION,
                            date(2023, 3, 24), date(2020, 12, 31))
    fact = Fact(Decimal("2.06"), provenance)
    actual = sync._source(fact)
    assert actual == sync._provenance_source(provenance)
    assert not any("native" in key or "primary" in key for key in actual)
    assert fact == pickle.loads(pickle.dumps(fact))


def test_absent_bundle_preserves_old_positional_repr_and_explicit_serializers():
    bundle = evidence.EvidenceBundle(CIK, "FTFT", {}, None, None)
    assert bundle.primary_inputs == ()
    assert repr(bundle) == ("EvidenceBundle(cik='0001066923', ticker='FTFT', facts={}, dimensioned=None, "
                           "receipt=None, supplement_state=None, captured_sources=(), original_company_facts=None)")
    assert bundle == pickle.loads(pickle.dumps(bundle)) == copy.deepcopy(bundle)
    from screener.figure_history import SourceOccurrenceRef
    body, request = fixture()
    with pytest.raises(ValueError):
        SourceOccurrenceRef(artifact_sha256=request.source.artifact_sha256, extraction_revision="native-private",
            source_system="SEC", issuer_source_identifier=CIK, source_document=URL,
            location=request.location, fidelity="EXACT_LEXICAL")


@pytest.mark.parametrize("problem", ["missing-action-doc", "missing-basis-doc", "wrong-class", "wrong-document-url",
                                    "wrong-document-hash", "reverse-span", "duplicate-doc", "nonfinite-factor"])
def test_explanatory_graph_admission_rejects_forged_or_missing_binding(problem):
    body, request = fixture()
    doc = document(body, request, roles=("CORRECTION_EXPLANATION", "LEGAL_ACTION", "INCORPORATED_BASIS"))
    action = ActionEvidence(key="action", legal_identity="legal", security_class="ordinary",
                            old_shares_text="8", new_shares_text="1", evidence_keys=(doc.key,))
    basis = ShareBasis(kind="ORDINARY", security_class="ordinary", incorporation="KNOWN",
                       incorporated_actions=("action",), evidence_keys=(doc.key,))
    plan = replace(mapping(body, request), documents=(doc,), actions=(action,), share_basis=basis)
    if problem == "missing-action-doc":
        plan = replace(plan, actions=(action.model_copy(update={"evidence_keys": ("absent",)}),))
    elif problem == "missing-basis-doc":
        plan = replace(plan, share_basis=basis.model_copy(update={"evidence_keys": ("absent",)}))
    elif problem == "wrong-class":
        plan = replace(plan, share_basis=basis.model_copy(update={"security_class": "other"}))
    elif problem == "wrong-document-url":
        plan = replace(plan, documents=(doc.model_copy(update={"reference": doc.reference.model_copy(update={"source_document": URL.replace("1066923", "1")})}),))
    elif problem == "wrong-document-hash":
        plan = replace(plan, documents=(doc.model_copy(update={"reference": doc.reference.model_copy(update={"artifact_sha256": "0" * 64})}),))
    elif problem == "reverse-span":
        plan = replace(plan, documents=(doc.model_copy(update={"reference": doc.reference.model_copy(update={"location": doc.reference.location.model_copy(update={"start_byte": doc.reference.location.end_byte})})}),))
    elif problem == "duplicate-doc":
        plan = replace(plan, documents=(doc, doc))
    elif problem == "nonfinite-factor":
        plan = replace(plan, unary_factor_text="Infinity")
    result, = evidence.admit_primary_inputs(CIK, (plan,), {request.source.artifact_sha256: body})
    assert isinstance(result, UnavailablePrimaryInput)
    assert result.original == request


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("scalar", ["startDate", "endDate", "instant"])
@pytest.mark.parametrize("text", ["", " \n\t", "2020", "2020-12", "2020-02-30", "0000-01-01",
                                 "20201231", "2020-W53-4", "2020-12-31Z", "2020-12-31T00:00:00",
                                 "\u00a02020-12-31\u00a0", "\u202f2020-12-31\u202f", "２０２０-１２-３１",
                                 "2020- 12-31", "2020-12-31 2020-12-31", "prefix2020-12-31"])
def test_requested_period_requires_actual_supported_calendar_scalar(inline, scalar, text):
    body, request = period_scalar_fixture(scalar, text, inline=inline, expected_missing=not text.strip())
    with pytest.raises(ValueError):
        read_native_numeric(body, request)
    result, = evidence.admit_primary_inputs(CIK, (mapping(body, request),),
                                            {request.source.artifact_sha256: body})
    assert isinstance(result, UnavailablePrimaryInput)
    assert result.original == request and result.original is not request


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("scalar", ["startDate", "endDate", "instant"])
def test_requested_period_self_closing_scalar_cannot_be_absent(inline, scalar):
    body, request = period_scalar_fixture(scalar, "", inline=inline, expected_missing=True)
    body = body.replace(f"<x:{scalar}></x:{scalar}>".encode(), f"<x:{scalar}/>".encode())
    request = bind(body, request)
    with pytest.raises(ValueError):
        read_native_numeric(body, request)
    result, = evidence.admit_primary_inputs(CIK, (mapping(body, request),),
                                            {request.source.artifact_sha256: body})
    assert isinstance(result, UnavailablePrimaryInput) and result.original == request


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("scalar", ["startDate", "endDate", "instant"])
def test_requested_calendar_scalar_keeps_structural_whitespace(inline, scalar):
    text = "2020-01-01" if scalar == "startDate" else "2020-12-31"
    body, request = period_scalar_fixture(scalar, text, inline=inline)
    body = body.replace(f"<x:{scalar}>".encode(), f" \n<x:{scalar}>".encode())
    body = body.replace(f"</x:{scalar}>".encode(), f"</x:{scalar}>\t\n".encode())
    request = bind(body, request)
    assert read_native_numeric(body, request).request.context == request.context
    result, = evidence.admit_primary_inputs(CIK, (mapping(body, request),),
                                            {request.source.artifact_sha256: body})
    assert isinstance(result, AdmittedPrimaryInput) and result.original.request.context == request.context


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("scalar", ["startDate", "endDate", "instant"])
@pytest.mark.parametrize("wrapper", [" ", "\t", "\n", "&#xD;", " \t\n&#xD; "])
def test_requested_calendar_scalar_collapses_only_surrounding_xml_whitespace(inline, scalar, wrapper):
    text = "2020-01-01" if scalar == "startDate" else "2020-12-31"
    body, request = period_scalar_fixture(scalar, wrapper + text + wrapper, inline=inline)
    result = read_native_numeric(body, request)
    assert result.value == Decimal("2.06") and result.request.context == request.context
    admitted, = evidence.admit_primary_inputs(CIK, (mapping(body, request),),
                                             {request.source.artifact_sha256: body})
    assert isinstance(admitted, AdmittedPrimaryInput) and admitted.original.request.context == request.context


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("scalar", ["startDate", "endDate", "instant"])
def test_requested_valid_wrapped_date_cannot_change_supplied_period(inline, scalar):
    wrong_date = "2021-12-31" if scalar == "endDate" else "2019-12-31"
    body, request = period_scalar_fixture(scalar, " \n" + wrong_date + "\t ", inline=inline)
    with pytest.raises(ValueError, match="context identity"):
        read_native_numeric(body, request)
    admitted, = evidence.admit_primary_inputs(CIK, (mapping(body, request),),
                                             {request.source.artifact_sha256: body})
    assert isinstance(admitted, UnavailablePrimaryInput) and admitted.original == request
