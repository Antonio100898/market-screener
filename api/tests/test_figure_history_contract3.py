import hashlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
import sys
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from screener import figure_history as owner
from tests.test_figure_history import example_bundle, validate_changed
from tests.test_figure_history_contract2 import document_bundle, basis_graph, assumed_graph


def declared_bundle(bundle=None, *, document=False):
    original = bundle or example_bundle()
    if document and not original.document_evidence:
        original = document_bundle(original)
    source = original.sources[0]
    links = [owner.SourceWitness(ordinal=0, association_status="DECLARED_NOT_VERIFIED",
        role="ORIGINAL_COMPANY_FACTS", observation_key=original.observations[0].key, source=source)]
    if document:
        links.append(owner.SourceWitness(ordinal=1, association_status="DECLARED_NOT_VERIFIED",
            role="CORRECTION_EXPLANATION", evidence_key=original.document_evidence[0].key, source=source))
    node = original.selections[0].model_copy(update=dict(state="NOT_ASSESSED", value_decimal_text=None,
        observation_key=None, reason="selected-association-unassessed", source_witnesses=tuple(links)))
    coverage = original.coverage[0].model_copy(update=dict(assessment="PARTIAL", reason="selected-association-unassessed"))
    return validate_changed(original, contract_revision=3, selections=(node,), coverage=(coverage,))


def native_bundle(*, inline=False, value="-0.0000"):
    original = declared_bundle()
    location = dict(element_ordinal=144 if inline else 384, qname="{urn:issuer}Assets",
                    fact_id=None, context_ref="c1", unit_ref="u1", decimals="INF")
    if inline:
        location = owner.NativeInlineHistoryLocation(**location, transformation=owner.NATIVE_TRANSFORMATION, sign="-", scale=0)
    else:
        location = owner.NativeXmlHistoryLocation(**location)
    observed = original.observations[0].model_copy(update=dict(qname=location.qname,
        value_decimal_text=value, source_lexical_text="0.0000" if inline else value,
        reference=original.observations[0].reference.model_copy(update=dict(location=location,
            extraction_revision=owner.NATIVE_EXTRACTION_REVISION)),
        unit=original.observations[0].unit.model_copy(update=dict(source_unit_ref="u1"))))
    return validate_changed(original, observations=(observed,), selections=(
        original.selections[0].model_copy(update=dict(source_witnesses=())),))


@pytest.fixture(scope="module")
def old_owner():
    path = Path(__file__).resolve().parents[2] / "local/native-contract3-before-2026-10-09/api/screener/figure_history.py"
    spec = importlib.util.spec_from_file_location("screener._native_contract3_before", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("factory", [example_bundle, document_bundle, basis_graph, assumed_graph])
def test_old_canonical_records_and_activation_meanings_stay_exact(old_owner, factory):
    current = factory()
    baseline = old_owner.ExactFigureBundle.model_validate_json(current.model_dump_json())
    assert current.model_dump(mode="json") == baseline.model_dump(mode="json")
    assert owner._digest(current.model_dump(mode="json")) == old_owner._digest(baseline.model_dump(mode="json"))
    new_projection, old_projection = owner.OutputGraphProjection(current), old_owner.OutputGraphProjection(baseline)
    for key in new_projection.nodes:
        assert new_projection.node(key) == old_projection.node(key)
        assert new_projection.context(new_projection.nodes[key]) == old_projection.context(old_projection.nodes[key])
    assert all("source_witnesses" not in node.model_dump(mode="json") for node in current.selections)


@pytest.mark.parametrize("revision", [1, 2, 3])
def test_true_json_admission_preserves_tuples_dates_and_python_strictness(revision):
    bundle = declared_bundle(document=True) if revision == 3 else document_bundle() if revision == 2 else example_bundle()
    reference = bundle.observations[0].reference.model_copy(update=dict(source_published_at=datetime(2026, 10, 9, tzinfo=timezone.utc)))
    bundle = validate_changed(bundle, observations=(bundle.observations[0].model_copy(update=dict(reference=reference)),))
    raw = bundle.model_dump_json()
    restored = owner.ExactFigureBundle.model_validate_json(raw)
    assert restored == bundle and isinstance(restored.observations, tuple)
    assert restored.observations[0].reference.source_published_at == datetime(2026, 10, 9, tzinfo=timezone.utc)
    with pytest.raises(ValidationError, match="tuple|date|datetime"):
        owner.ExactFigureBundle.model_validate(json.loads(raw))
    with pytest.raises(ValidationError, match="Extra inputs"):
        owner.ExactFigureBundle.model_validate_json(raw[:-1] + ',"unknown":1}')


def test_caps_precede_json_leaf_factories(monkeypatch):
    raw = declared_bundle().model_dump(mode="json")
    raw["selections"] *= 129
    def unavailable(cls):
        raise AssertionError("oversize input reached JSON leaf factories")
    monkeypatch.setattr(owner, "_json_field_adapters", unavailable)
    with pytest.raises(ValidationError, match="count exceeds"):
        owner.ExactFigureBundle.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("field,value", [("selections", None), ("selections", [None]), ("selections", [False]), ("sources", None), ("observations", "unstructured")])
def test_raw_contract3_invalid_arrays_refuse_at_admission(field, value):
    record = declared_bundle().model_dump(mode="json")
    record[field] = value
    with pytest.raises(ValidationError, match="structured arrays"):
        owner.ExactFigureBundle.model_validate_json(json.dumps(record))


def test_invalid_scope_refuses_before_nested_factory():
    record = declared_bundle().model_dump(mode="json")
    record["observations"][0]["scope"] = None
    with pytest.raises(ValidationError, match="structured record"):
        owner.ExactFigureBundle.model_validate_json(json.dumps(record))


def test_wrong_python_record_type_refuses_without_attribute_error():
    record = declared_bundle().model_dump()
    record["selections"] = (owner.Unit(status="KNOWN", numerator=("pure",)),)
    with pytest.raises(ValidationError, match="structured arrays"):
        owner.ExactFigureBundle.model_validate(record)


@pytest.mark.parametrize("reference", [None, [], {"artifact_sha256": {}}])
def test_raw_document_reference_refuses_before_hash_set_or_factory(reference):
    record = declared_bundle(document=True).model_dump(mode="json")
    record["document_evidence"][0]["reference"] = reference
    with pytest.raises(ValidationError, match="structured record|artifact digest"):
        owner.ExactFigureBundle.model_validate_json(json.dumps(record))


def test_witness_raw_count_refuses_before_leaf_factories(monkeypatch):
    record = declared_bundle().model_dump(mode="json")
    record["selections"][0]["source_witnesses"] *= 301
    def unavailable(cls):
        raise AssertionError("oversize witness list reached leaf factories")
    monkeypatch.setattr(owner, "_json_field_adapters", unavailable)
    with pytest.raises(ValidationError, match="witness count"):
        owner.ExactFigureBundle.model_validate_json(json.dumps(record))


@pytest.mark.parametrize("inline", [False, True])
def test_original_ordinal_domains_actual_absent_id_and_signed_text_roundtrip(inline):
    bundle = native_bundle(inline=inline)
    restored = owner.ExactFigureBundle.model_validate_json(bundle.model_dump_json())
    locator = restored.observations[0].reference.location
    assert locator.fact_id is None and locator.element_ordinal == (144 if inline else 384)
    assert locator.ordinal_domain == ("ORIGINAL_NAMESPACE_RESOLVED_NONFRACTION_START_ORDER" if inline else "ORIGINAL_INSTANCE_DIRECT_CHILD_ORDER")
    assert restored.observations[0].value_decimal_text == "-0.0000"
    assert restored.selections[0].state == "NOT_ASSESSED" and restored.selections[0].value_decimal_text is None


@pytest.mark.parametrize("value", ["9007199254740993.1234567890123456789", "1E-20000", "-0.0000"])
def test_native_xml_exact_text_has_no_float_conversion(value):
    assert owner.ExactFigureBundle.model_validate_json(native_bundle(value=value).model_dump_json()).observations[0].value_decimal_text == value


@pytest.mark.parametrize("field,value", [("element_ordinal", True), ("element_ordinal", 1.0), ("element_ordinal", -1),
    ("qname", "prefix:Assets"), ("fact_id", ""), ("unit_ref", ""), ("decimals", "1.5"),
    ("ordinal_domain", "ALL_DESCENDANTS"), ("scale", 1), ("continuedAt", "next")])
def test_xml_locator_structural_negatives(field, value):
    record = native_bundle().observations[0].reference.location.model_dump()
    record[field] = value
    with pytest.raises(ValidationError):
        owner.NativeXmlHistoryLocation.model_validate(record)


@pytest.mark.parametrize("field,value", [("scale", True), ("scale", 1.0), ("sign", "+"),
    ("transformation", "{urn:wrong}num-dot-decimal"), ("continuation_ids", ("next",))])
def test_inline_locator_does_not_widen_reader_capability(field, value):
    record = native_bundle(inline=True).observations[0].reference.location.model_dump()
    record[field] = value
    with pytest.raises(ValidationError):
        owner.NativeInlineHistoryLocation.model_validate(record)


@pytest.mark.parametrize("revision", [True, False, 1.0, 2.0, 3.0, 4, "3"])
def test_exact_bundle_revision_rejects_coercible_values(revision):
    record = declared_bundle().model_dump(mode="json")
    record["contract_revision"] = revision
    for raw in (record, json.dumps(record)):
        with pytest.raises(ValidationError):
            owner.ExactFigureBundle.model_validate(raw) if isinstance(raw, dict) else owner.ExactFigureBundle.model_validate_json(raw)


@pytest.mark.parametrize("revision", [1, 2])
@pytest.mark.parametrize("kind", ["native", "witness"])
def test_old_contracts_refuse_new_meanings(revision, kind):
    with pytest.raises(ValidationError, match="contract revision3"):
        validate_changed(native_bundle() if kind == "native" else declared_bundle(), contract_revision=revision)


def test_declared_links_do_not_claim_selected_view_or_numerical_authority():
    bundle = declared_bundle(document=True)
    node = bundle.selections[0]
    assert all(link.association_status == "DECLARED_NOT_VERIFIED" for link in node.source_witnesses)
    assert node.value_decimal_text is None and node.observation_key is None and not bundle.dependencies
    public = owner.history_selection_record(dict(value_decimal_text=None, record=node.model_dump(mode="json", exclude={"value_decimal_text"})))
    assert "source_witnesses" not in public and public["state"] == "NOT_ASSESSED"
    assumed = node.model_copy(update=dict(node_key="assumed", view="ASSUMED"))
    coverage = bundle.coverage[0].model_copy(update=dict(view="ASSUMED"))
    admitted = validate_changed(bundle, selections=(assumed,), coverage=(coverage,))
    assert admitted.selections[0].source_witnesses[0].association_status == "DECLARED_NOT_VERIFIED"


@pytest.mark.parametrize("problem", ["state", "intermediate", "noncontiguous", "duplicate", "unknown-target",
    "source-outside", "wrong-role", "no-occurrence", "both-targets", "verified-status"])
def test_declared_link_identity_and_unavailable_output_guards(problem):
    bundle = declared_bundle(document=True)
    node, first = bundle.selections[0], bundle.selections[0].source_witnesses[0]
    changed = {
        "state": node.model_copy(update=dict(state="REPORTED", value_decimal_text="100.0000", observation_key=first.observation_key)),
        "intermediate": node.model_copy(update=dict(purpose="INTERMEDIATE")),
        "noncontiguous": node.model_copy(update=dict(source_witnesses=(first.model_copy(update=dict(ordinal=1)),))),
        "duplicate": node.model_copy(update=dict(source_witnesses=(first, first.model_copy(update=dict(ordinal=1))))),
        "unknown-target": node.model_copy(update=dict(source_witnesses=(first.model_copy(update=dict(observation_key="missing")),))),
        "source-outside": node.model_copy(update=dict(source_witnesses=(first.model_copy(update=dict(source=first.source.model_copy(update=dict(source_item_id=2)))),))),
        "wrong-role": node.model_copy(update=dict(source_witnesses=(first.model_copy(update=dict(role="ORIGINAL_NATIVE")),))),
        "no-occurrence": node.model_copy(update=dict(source_witnesses=(first.model_copy(update=dict(source=first.source.model_copy(update=dict(occurrence_id=None)))),))),
        "both-targets": node.model_copy(update=dict(source_witnesses=(first.model_copy(update=dict(evidence_key="disclosure")),))),
        "verified-status": node.model_copy(update=dict(source_witnesses=(first.model_copy(update=dict(association_status="VERIFIED")),))),
    }[problem]
    with pytest.raises(ValidationError):
        validate_changed(bundle, selections=(changed,))


def test_witness_meaning_resolves_keys_and_changes_only_provenance():
    bundle = declared_bundle(document=True)
    renamed = validate_changed(bundle,
        observations=(bundle.observations[0].model_copy(update=dict(key="different-local-key")),),
        selections=(bundle.selections[0].model_copy(update=dict(source_witnesses=(
            bundle.selections[0].source_witnesses[0].model_copy(update=dict(observation_key="different-local-key")),
            bundle.selections[0].source_witnesses[1]))),))
    assert owner.OutputGraphProjection(bundle).node("assets") == owner.OutputGraphProjection(renamed).node("assets")
    without = validate_changed(bundle, selections=(bundle.selections[0].model_copy(update=dict(source_witnesses=())),))
    assert owner.OutputGraphProjection(bundle).provenance(bundle.selections[0]) != owner.OutputGraphProjection(without).provenance(without.selections[0])
    assert owner.OutputGraphProjection(bundle).context(bundle.selections[0]) == owner.OutputGraphProjection(without).context(without.selections[0])
    assert bundle.selections[0].value_decimal_text == without.selections[0].value_decimal_text is None


@pytest.mark.parametrize("change", ["name", "numeric", "outputs", "sources", "documents", "dimensions", "record"])
def test_contract3_limits_reject_without_limiting_old_valid_records(change):
    bundle = declared_bundle()
    record = bundle.model_dump(mode="json")
    if change == "name":
        record["pipeline_run_key"] = "é" * 2049
    elif change == "numeric":
        record["observations"][0]["source_lexical_text"] = "0" * 16385
    elif change == "outputs":
        record["selections"] *= 129
    elif change == "sources":
        record["sources"] *= 65
    elif change == "documents":
        record = declared_bundle(document=True).model_dump(mode="json")
        record["document_evidence"] *= 65
    elif change == "dimensions":
        record["observations"][0]["scope"]["dimensions"] = [dict(axis="axis", member="member", typed_member_xml=None)] * 33
    else:
        record["selections"][0]["source_witnesses"] *= 300
    with pytest.raises(ValidationError):
        owner.ExactFigureBundle.model_validate_json(json.dumps(record))
    assert validate_changed(example_bundle(), pipeline_run_key="é" * 2049).contract_revision == 1


class MetadataRows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self.rows


class MetadataEngine:
    def __init__(self, rows):
        self.rows, self.reads = rows, 0

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, statement):
        self.reads += 1
        return MetadataRows(self.rows)

    def begin(self):
        raise AssertionError("guard reached writer transaction")


class Bodies:
    def __init__(self, bodies):
        self.bodies, self.reads = bodies, []

    def read_verified(self, key, digest, size):
        self.reads.append(digest)
        return self.bodies[digest]


def prepared_case():
    body = b"Actual retained documentary bytes"
    digest = hashlib.sha256(body).hexdigest()
    bundle = declared_bundle(example_bundle(artifact_sha256=digest), document=True)
    document = bundle.document_evidence[0].model_copy(update=dict(reference=bundle.document_evidence[0].reference.model_copy(update=dict(
        location=owner.DocumentSpan(start_byte=0, end_byte=len(body), span_sha256=digest)))))
    bundle = validate_changed(bundle, document_evidence=(document,))
    rows = [dict(content_sha256=digest, object_key=f"raw/sha256/{digest}", byte_size=len(body))]
    return bundle, MetadataEngine(rows), Bodies({digest: body})


def test_bounded_document_tuple_dispatch_and_legacy_seal_identity(old_owner):
    bundle, engine, objects = prepared_case()
    repository = owner.FigureHistoryRepository(engine)
    token = repository.prepare_documents(bundle, objects)
    tuple_token = repository.prepare_document_records(bundle.document_evidence, objects, contract_revision=3)
    assert token == tuple_token and isinstance(token, owner.PreparedContract3DocumentEvidence)
    assert token.limits_sha256 == owner.CONTRACT3_DOCUMENT_LIMITS_SHA256
    assert len(objects.reads) == 2
    repository._require_prepared(bundle, token)
    old_bundle = validate_changed(bundle, contract_revision=2, selections=(bundle.selections[0].model_copy(update=dict(source_witnesses=())),))
    baseline_bundle = old_owner.ExactFigureBundle.model_validate_json(old_bundle.model_dump_json())
    baseline_repo = old_owner.FigureHistoryRepository(engine)
    baseline_repo._preparation_key = repository._preparation_key
    old_token = baseline_repo.prepare_documents(baseline_bundle, objects)
    current_token = repository.prepare_documents(old_bundle, objects)
    assert current_token.record_sha256 == old_token.record_sha256 and current_token.verification_seal == old_token.verification_seal
    with pytest.raises(ValueError, match="bounded preparation"):
        repository.store(None, request_key="old-token", bundle=bundle, prepared_documents=current_token)


@pytest.mark.parametrize("fault", ["record", "limits", "seal", "other-repository"])
def test_bounded_proof_tampering_refuses_before_write(fault):
    bundle, engine, objects = prepared_case()
    repository = owner.FigureHistoryRepository(engine)
    token = repository.prepare_documents(bundle, objects)
    if fault == "other-repository":
        token = owner.FigureHistoryRepository(engine).prepare_documents(bundle, objects)
    else:
        token = replace(token, **{"record": dict(record_sha256="a" * 64), "limits": dict(limits_sha256="a" * 64), "seal": dict(verification_seal=b"forged")}[fault])
    with pytest.raises(ValueError, match="bounded preparation"):
        repository.store_on_connection(engine, None, request_key="tamper", bundle=bundle, prepared_documents=token)


@pytest.mark.parametrize("fault", ["body", "aggregate", "object-key", "false-size"])
def test_document_registered_metadata_refuses_before_read_even_with_small_span(fault):
    bundle, engine, objects = prepared_case()
    if fault == "aggregate":
        engine.rows = [dict(content_sha256=str(i) * 64, object_key=f"raw/sha256/{str(i) * 64}", byte_size=8 * 1024 * 1024) for i in range(1, 10)]
        documents = tuple(bundle.document_evidence[0].model_copy(update=dict(key=f"doc{i}", reference=bundle.document_evidence[0].reference.model_copy(update=dict(artifact_sha256=str(i) * 64)))) for i in range(1, 10))
    else:
        documents = bundle.document_evidence
        engine.rows[0].update({"body": dict(byte_size=8 * 1024 * 1024 + 1), "object-key": dict(object_key="wrong"), "false-size": dict(byte_size=True)}[fault])
    repository = owner.FigureHistoryRepository(engine)
    with pytest.raises(ValueError, match="ceiling|identity"):
        repository.prepare_document_records(documents, objects, contract_revision=3)
    assert not objects.reads


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("wrapper", ["store", "store_on_connection"])
def test_native_persistence_is_closed_even_without_links_or_with_fake_proof(inline, wrapper):
    engine = MetadataEngine([])
    repository = owner.FigureHistoryRepository(engine)
    bundle = native_bundle(inline=inline)
    token = owner.PreparedContract3DocumentEvidence("a" * 64, owner.CONTRACT3_DOCUMENT_LIMITS_SHA256, b"fake")
    kwargs = dict(request_key="native", bundle=bundle, prepared_documents=token)
    with pytest.raises(ValueError, match="native persistence is closed"):
        repository.store(None, **kwargs) if wrapper == "store" else repository.store_on_connection(engine, None, **kwargs)
    assert engine.reads == 0


def test_history_publication_header_and_replay_contract_refuse3_before_source_reads():
    row = dict(bundle_id=1, security_id=1, snapshot_id=1, pipeline_run_key="run", contract_revision=3)

    class Result:
        def mappings(self): return self
        def one_or_none(self): return row

    class Connection:
        def execute(self, statement): return Result()
        def scalars(self, statement): raise AssertionError("contract3 reached source inventory")
        def scalar(self, statement): return 3

    with pytest.raises(ValueError, match="not publication qualified"):
        owner.FigureHistoryRepository.publication_identity_on_connection(Connection(), 1,
            security_id=1, snapshot_id=1, expected_run_key="run")
    with pytest.raises(ValueError, match="not publication qualified"):
        owner.FigureHistoryRepository.require_release_contract_on_connection(Connection(), 1)


def test_final_membership_contract_gate_precedes_all_activation_writes():
    from screener.publication import PublicationRepository
    class Connection:
        def scalar(self, statement): return 3
        def execute(self, statement): raise AssertionError("contract3 reached activation write")
    publisher = PublicationRepository(MetadataEngine([]))
    with pytest.raises(ValueError, match="not publication qualified"):
        publisher._activate(Connection(), None, "new", 1, [dict(security_id=1, snapshot_id=1, exact_bundle_id=3)], ())
    owner.FigureHistoryRepository.require_membership_contract_on_connection(None, [])
