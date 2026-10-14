"""Fresh CF root ownership. All source/delivery identities are synthetic."""
from dataclasses import replace
import hashlib
import json

import pytest

from screener import evidence
from screener.models import AdmittedPrimaryInput, UnavailablePrimaryInput
from screener.sources import sec_json
from test_native_primary_inputs import CIK, fixture, mapping
from test_primary_input_targets import company_facts


def inputs():
    raw, source, _, target = company_facts()
    body, request = fixture()
    declaration = replace(mapping(body, request), target_origin=target)
    return raw, source, declaration, {source.artifact_sha256: raw, request.source.artifact_sha256: body}


def observed_decoder(monkeypatch):
    original = sec_json.decode_company_facts
    calls = []
    def observe(raw, *, source):
        calls.append((raw, source))
        return original(raw, source=source)
    monkeypatch.setattr(sec_json, 'decode_company_facts', observe)
    return calls


def test_new_entry_owns_one_fresh_root_for_multiple_actual_targets(monkeypatch):
    raw, source, declaration, payloads = inputs()
    other_body, other_request = fixture('3.07')
    decoded = sec_json.decode_company_facts(raw, source=source)
    other_target = decoded['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares'][98][sec_json.ORIGIN_KEY]
    other = replace(mapping(other_body, other_request), key='other', target_origin=other_target)
    payloads[other_request.source.artifact_sha256] = other_body
    calls = observed_decoder(monkeypatch)
    original, outcomes = evidence.decode_and_admit_primary_inputs(CIK, (declaration, other), payloads,
        company_facts_source=source)
    assert len(calls) == 1 and calls[0] == (raw, source)
    assert all(isinstance(outcome, AdmittedPrimaryInput) for outcome in outcomes)
    assert original == decoded
    assert original is not decoded and outcomes[0].mapping is not declaration
    assert payloads[source.artifact_sha256] == raw


def test_empty_old_entry_keeps_zero_decode_new_entry_returns_one_root(monkeypatch):
    raw, source, _, payloads = inputs()
    calls = observed_decoder(monkeypatch)
    assert evidence.admit_primary_inputs(CIK, (), payloads) == () and calls == []
    tree, outcomes = evidence.decode_and_admit_primary_inputs(CIK, (), payloads, company_facts_source=source)
    assert outcomes == () and tree['cik'] == int(CIK) and len(calls) == 1


def test_full_captured_source_cache_key_does_not_borrow_another_delivery(monkeypatch):
    _, source, declaration, payloads = inputs()
    changed_source = replace(source, occurrence_id=999)
    declaration = replace(declaration, target_origin=replace(declaration.target_origin, source=changed_source))
    calls = observed_decoder(monkeypatch)
    tree, outcomes = evidence.decode_and_admit_primary_inputs(CIK, (declaration,), payloads, company_facts_source=source)
    assert isinstance(outcomes[0], AdmittedPrimaryInput)
    assert [captured for _, captured in calls] == [source, changed_source]
    assert tree['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares'][99][sec_json.ORIGIN_KEY].source == source
    assert outcomes[0].mapping.target_origin.source == changed_source


def test_separate_calls_return_fresh_trees_and_copied_outcomes():
    _, source, declaration, payloads = inputs()
    first_tree, first = evidence.decode_and_admit_primary_inputs(CIK, (declaration,), payloads, company_facts_source=source)
    first_tree['facts'].clear()
    second_tree, second = evidence.decode_and_admit_primary_inputs(CIK, (declaration,), payloads, company_facts_source=source)
    assert second_tree['facts'] and second == first and second[0] is not first[0]


@pytest.mark.parametrize('field,value', [('source_item_id', True), ('source_observation_id', 0),
    ('occurrence_id', None), ('occurrence_id', False), ('role', []), ('fidelity', {}),
    ('artifact_sha256', 'F' * 64), ('source_document', [])])
def test_new_root_uses_existing_strict_source_guard(field, value):
    _, source, _, payloads = inputs()
    with pytest.raises(ValueError, match='exact captured delivery'):
        evidence.decode_and_admit_primary_inputs(CIK, (), payloads,
            company_facts_source=replace(source, **{field: value}))


@pytest.mark.parametrize('kind', ['corrupt', 'rehashed', 'duplicate', 'reserved', 'wrong_issuer'])
def test_invalid_root_refuses_before_bundle_or_unavailable_mapping(kind):
    raw, source, declaration, payloads = inputs()
    if kind == 'corrupt':
        modified = raw + b'x'
    elif kind == 'duplicate':
        modified = b'{"cik":1066923,"cik":1066923}'
    elif kind == 'reserved':
        modified = b'{"cik":1066923,"_exact_sec_origin":{}}'
    elif kind == 'wrong_issuer':
        tree = json.loads(raw); tree['cik'] = 999; modified = json.dumps(tree).encode()
    else:
        modified = b'not JSON'
    if kind != 'corrupt':
        source = replace(source, artifact_sha256=hashlib.sha256(modified).hexdigest())
    payloads[source.artifact_sha256] = modified
    with pytest.raises(ValueError):
        evidence.decode_and_admit_primary_inputs(CIK, (declaration,), payloads, company_facts_source=source)


@pytest.mark.parametrize('field,value', [('lexical_text', 'another'), ('pointer', '/missing'),
                                      ('end', 'another'), ('unit', 'another')])
def test_mapping_specific_wrong_origin_remains_unavailable(field, value):
    _, source, declaration, payloads = inputs()
    declaration = replace(declaration, target_origin=replace(declaration.target_origin, **{field: value}))
    _, outcomes = evidence.decode_and_admit_primary_inputs(CIK, (declaration,), payloads, company_facts_source=source)
    assert isinstance(outcomes[0], UnavailablePrimaryInput)
    assert evidence.admit_primary_inputs(CIK, (declaration,), payloads) == outcomes
