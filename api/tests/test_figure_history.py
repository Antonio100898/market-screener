from datetime import date

import pytest
from pydantic import ValidationError

from screener.figure_history import (BundleSource, CalculationDependency, Coverage, DeraLocation, ExactFigureBundle,
    FigureSelection, Period, ReportedObservation, Scope, SecJsonLocation, SourceOccurrenceRef, Unit, XmlLocation, decimal_text)


def example_bundle(*, security_id=1, snapshot_id=1, snapshot_sha256="a" * 64,
                   artifact_sha256="b" * 64, source_item_id=1, source_observation_id=1,
                   occurrence_id=1, value="100.0000", engine_revision=187):
    unit = Unit(status="KNOWN", numerator=("iso4217:USD",), currency="USD")
    period = Period(kind="INSTANT", end=date(2025, 12, 31), origin="REPORTED")
    scope = Scope(entity_identifier="0000001800", entity_scheme="http://www.sec.gov/CIK",
                  consolidation="CONSOLIDATED", accounting_basis="US_GAAP", dimensions_assessed=True)
    observation = ReportedObservation(key="assets-source", reference=SourceOccurrenceRef(
        artifact_sha256=artifact_sha256, extraction_revision="sec-json-exact-v1", source_system="SEC", issuer_source_identifier="0000001800",
        source_document="filing-1", location=SecJsonLocation(pointer="/facts/us-gaap/Assets/units/USD/0"),
        fidelity="EXACT_LEXICAL"), qname="us-gaap:Assets", value_decimal_text=value,
        source_lexical_text=value, unit=unit, period=period, scope=scope)
    selected = FigureSelection(node_key="assets", metric_family="balance_sheet", metric_path="balance_sheet.total_assets",
        view="STRICT", state="REPORTED", value_decimal_text=value, observation_key=observation.key,
        unit=unit, period=period, scope=scope, security_basis="PRIMARY_COMMON_SHARE")
    return ExactFigureBundle(security_id=security_id, snapshot_id=snapshot_id, snapshot_sha256=snapshot_sha256,
        engine_revision=engine_revision, pipeline_run_key="derive:test", observations=(observation,), selections=(selected,),
        coverage=(Coverage(metric_family="balance_sheet", view="STRICT", assessment="FULL",
            metric_paths=(selected.metric_path,), source_fidelity="EXACT_LEXICAL", earlier_history="CAPTURED_ONLY"),),
        sources=(BundleSource(source_item_id=source_item_id, source_observation_id=source_observation_id, occurrence_id=occurrence_id),))


@pytest.mark.parametrize("value", ["9007199254740993.1234567890123456789", "1E-20000", "1E+200000", "-0.0000", "+0001.2300"])
def test_lossless_decimal_text(value):
    assert decimal_text(value) == value
    bundle = example_bundle(value=value)
    assert ExactFigureBundle.model_validate_json(bundle.model_dump_json()) == bundle
    assert bundle.selections[0].value_decimal_text == value


@pytest.mark.parametrize("value", [float("inf"), 100.0, "NaN", "Infinity", " 1 ", "1_000", "0x1", ""])
def test_nonexact_values_rejected(value):
    with pytest.raises((ValueError, ValidationError)):
        example_bundle(value=value)


def validate_changed(bundle, **changes):
    return ExactFigureBundle.model_validate(bundle.model_copy(update=changes))


def test_false_full_missing_scope_fidelity_witness_and_metric_rejected():
    bundle = example_bundle()
    for change in [
        dict(sources=()),
        dict(sources=(bundle.sources[0].model_copy(update={"occurrence_id": None}),)),
        dict(coverage=(bundle.coverage[0].model_copy(update={"metric_paths": ("missing",)}),)),
        dict(coverage=(bundle.coverage[0].model_copy(update={"source_fidelity": "UNKNOWN"}),)),
        dict(observations=(bundle.observations[0].model_copy(update={"reference": bundle.observations[0].reference.model_copy(update={"fidelity": "UNKNOWN"})}),)),
    ]:
        with pytest.raises(ValidationError):
            validate_changed(bundle, **change)
    scope = bundle.selections[0].scope.model_copy(update={"consolidation": "UNKNOWN"})
    with pytest.raises(ValidationError, match="unknown selection basis"):
        validate_changed(bundle, selections=(bundle.selections[0].model_copy(update={"scope": scope}),),
            observations=(bundle.observations[0].model_copy(update={"scope": scope}),))


def test_reported_mismatch_and_assumption_cannot_replace_strict():
    bundle = example_bundle()
    with pytest.raises(ValidationError, match="preserve its exact observation"):
        validate_changed(bundle, selections=(bundle.selections[0].model_copy(update={"value_decimal_text": "100"}),))
    with pytest.raises(ValidationError, match="assumed output"):
        FigureSelection.model_validate(bundle.selections[0].model_copy(update={"state": "ASSUMED", "observation_key": None,
            "definition": "existing.definition", "definition_revision": "187"}))


def derived_graph():
    bundle = example_bundle()
    selected = bundle.selections[0]
    derived = selected.model_copy(update={"node_key": "derived", "metric_path": "balance_sheet.component_sum",
        "state": "DERIVED", "observation_key": None, "definition": "normalize._component_sum",
        "definition_revision": "187", "required_input_roles": ("component",)})
    coverage = bundle.coverage[0].model_copy(update={"metric_paths": (selected.metric_path, derived.metric_path)})
    return validate_changed(bundle, selections=(selected, derived), coverage=(coverage,), dependencies=(
        CalculationDependency(parent_node_key="derived", ordinal=0, role="component", selection_node_key="assets"),))


def test_dependency_graph_requires_complete_roles_same_view_and_acyclic_inputs():
    bundle = derived_graph()
    with pytest.raises(ValidationError, match="input roles"):
        validate_changed(bundle, dependencies=())
    for target in ("unknown", "derived"):
        with pytest.raises(ValidationError, match="unknown selection|cyclic"):
            validate_changed(bundle, dependencies=(bundle.dependencies[0].model_copy(update={"selection_node_key": target}),))
    for field, value in (("view", "CONSERVATIVE"), ("security_basis", "OTHER")):
        changed = tuple(n.model_copy(update={field: value}) if n.node_key == "derived" else n for n in bundle.selections)
        with pytest.raises(ValidationError, match="basis differs"):
            validate_changed(bundle, selections=changed)


def test_unassessed_coverage_is_honest_and_extra_functions_rejected():
    bundle = example_bundle()
    unknown = bundle.selections[0].model_copy(update={"state": "NOT_ASSESSED", "value_decimal_text": None,
        "observation_key": None, "reason": "exact propagation unavailable"})
    coverage = bundle.coverage[0].model_copy(update={"assessment": "NOT_ASSESSED", "reason": "legacy pipeline"})
    assert validate_changed(bundle, selections=(unknown,), coverage=(coverage,), observations=(), sources=()).coverage[0].assessment == "NOT_ASSESSED"
    record = unknown.model_dump()
    record["formula"] = "eval(source)"
    with pytest.raises(ValidationError, match="Extra inputs"):
        FigureSelection.model_validate(record)


def test_full_rejects_different_reporting_scope_and_unknown_dependency():
    bundle = derived_graph()
    child = bundle.selections[0]
    changed_scope = child.scope.model_copy(update={"consolidation": "NONCONSOLIDATED"})
    changed_observation = bundle.observations[0].model_copy(update={"scope": changed_scope})
    changed_child = child.model_copy(update={"scope": changed_scope})
    with pytest.raises(ValidationError, match="reporting scope differs"):
        validate_changed(bundle, observations=(changed_observation,), selections=(changed_child, bundle.selections[1]))


def test_currency_and_source_system_must_match_typed_reference():
    bundle = example_bundle()
    with pytest.raises(ValidationError, match="currency differs"):
        Unit.model_validate(bundle.selections[0].unit.model_copy(update={"currency": "EUR"}))
    with pytest.raises(ValidationError, match="SEC location"):
        SourceOccurrenceRef.model_validate(bundle.observations[0].reference.model_copy(update={"source_system": "EDINET"}))


def test_xml_lexical_whitespace_retained_and_dera_start_not_invented():
    observed = example_bundle().observations[0]
    xml = XmlLocation(member="PublicDoc/example.xbrl", element_ordinal=647, qname="{http://example.test/taxonomy}Assets",
                      context_ref="CurrentYearInstant", unit_ref="JPY")
    xml_observation = observed.model_copy(update={"qname": xml.qname, "source_lexical_text": "\n 100.0000 \n",
        "reference": observed.reference.model_copy(update={"location": xml})})
    assert ReportedObservation.model_validate(xml_observation).source_lexical_text == "\n 100.0000 \n"
    with pytest.raises(ValidationError, match="QName differs"):
        ReportedObservation.model_validate(xml_observation.model_copy(update={"qname": "other"}))
    dera = DeraLocation(member="num.txt", physical_row=2, accession="0000001961-26-000014", tag="Assets",
        taxonomy="us-gaap/2025", ddate=date(2025, 12, 31), qtrs=4, coreg="", segments="")
    duration = Period(kind="DURATION", start=date(2025, 1, 1), end=date(2025, 12, 31), origin="INFERRED")
    observation = observed.model_copy(update={"period": duration, "reference": observed.reference.model_copy(update={"location": dera})})
    assert ReportedObservation.model_validate(observation).period.origin == "INFERRED"
    with pytest.raises(ValidationError, match="must remain inferred"):
        ReportedObservation.model_validate(observation.model_copy(update={"period": duration.model_copy(update={"origin": "REPORTED"})}))


@pytest.mark.parametrize("qtrs,kind", [(0, "DURATION"), (4, "INSTANT")])
def test_dera_quarter_count_cannot_contradict_period_kind(qtrs, kind):
    observed = example_bundle().observations[0]
    location = DeraLocation(member="num.txt", physical_row=2, accession="filing-1", tag="Assets",
        taxonomy="us-gaap/2025", ddate=date(2025, 12, 31), qtrs=qtrs, coreg="", segments="")
    period = Period(kind=kind, end=date(2025, 12, 31), start=date(2025, 1, 1) if kind == "DURATION" else None,
                    origin="INFERRED" if kind == "DURATION" else "REPORTED")
    with pytest.raises(ValidationError, match="DERA qtrs"):
        ReportedObservation.model_validate(observed.model_copy(update={"period": period,
            "reference": observed.reference.model_copy(update={"location": location})}))
