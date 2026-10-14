"""Structural history does not certify financial math or correction causes."""
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from screener.figure_history import (ActivationTransition, OutputGraphProjection, activation_inventory,
    Period, Unit, CalculationDependency)
from tests.test_figure_history import example_bundle, validate_changed
from tests.test_figure_history_contract2 import basis_graph, assumed_graph, document_bundle


def inventory(before, after):
    return activation_inventory(release_id=2, parent_release_id=1, security_id=1,
        previous_bundle_id=1 if before else None, previous=OutputGraphProjection(before) if before else None,
        replacement_bundle_id=2 if after else None, replacement=OutputGraphProjection(after) if after else None)


def reported(bundle, **changes):
    return validate_changed(bundle, observations=(bundle.observations[0].model_copy(update=changes),),
        selections=(bundle.selections[0].model_copy(update={k: v for k, v in changes.items()
                    if k in {"unit", "scope", "period", "value_decimal_text"}}),))


def partial_period(bundle, period):
    return reported(validate_changed(bundle, coverage=(bundle.coverage[0].model_copy(update={
        "assessment": "PARTIAL", "reason": "period-unavailable"}),)), period=period)


@pytest.mark.parametrize("before,after", [("100.0000", "90.0000"), ("90.0000", "95.0000"),
    ("0", "-0.0000"), ("1E-20000", "1E+200000"), ("9007199254740993.1234567890123456789", "9007199254740993.1234567890123456788")])
def test_exact_endpoint_change_has_no_delta_or_verified_cause(before, after):
    rows = inventory(example_bundle(value=before), example_bundle(value=after))
    assert len(rows) == 1 and rows[0].value_text_changed
    assert rows[0].context_assessment == "NOT_ASSESSED" and rows[0].declared_context_equal
    assert not {"delta", "delta_decimal_text", "verified_cause"} & ActivationTransition.model_fields.keys()
    assert rows[0].previous_bundle_id == 1 and rows[0].replacement_bundle_id == 2


def test_job_run_catalog_key_clock_noise_does_not_make_event():
    old = example_bundle()
    observed = old.observations[0].model_copy(update={"key": "renamed", "reference": old.observations[0].reference.model_copy(
        update={"source_published_at": datetime(2026, 10, 4, tzinfo=timezone.utc)})})
    node = old.selections[0].model_copy(update={"node_key": "renamed-node", "observation_key": observed.key})
    new = validate_changed(old, pipeline_run_key="another-job/run", snapshot_id=999, snapshot_sha256="c" * 64,
        observations=(observed,), selections=(node,), sources=(old.sources[0].model_copy(update={"occurrence_id": 4}),))
    assert inventory(old, new) == []


@pytest.mark.parametrize("field,value", [("source_lexical_text", "100.00000"), ("precision", "-6"), ("qname", "us-gaap:OtherAssets")])
def test_exact_reachable_observation_metadata_creates_provenance_change(field, value):
    old = example_bundle()
    new = reported(old, **{field: value})
    row, = inventory(old, new)
    assert row.provenance_changed and not row.value_text_changed


def test_source_location_and_extraction_changes_remain_meaningful():
    old = example_bundle()
    for change in (dict(extraction_revision="parser-v2"), dict(source_document="amendment"),
                   dict(artifact_sha256="c" * 64), dict(location=old.observations[0].reference.location.model_copy(update={"pointer": "/another/occurrence"}))):
        new = reported(old, reference=old.observations[0].reference.model_copy(update=change))
        row, = inventory(old, new)
        assert row.provenance_changed and row.context_assessment == "NOT_ASSESSED"


@pytest.mark.parametrize("change", ["currency", "scope", "dimensions"])
def test_known_conflict_is_incomparable_not_a_different_metric(change):
    old = example_bundle()
    if change == "currency":
        new = reported(old, unit=Unit(status="KNOWN", numerator=("iso4217:JPY",), currency="JPY"))
    elif change == "scope":
        new = reported(old, scope=old.selections[0].scope.model_copy(update={"consolidation": "NONCONSOLIDATED"}))
    else:
        from screener.figure_history import Dimension
        new = reported(old, scope=old.selections[0].scope.model_copy(update={"dimensions": (
            Dimension(axis="segment", member="other"),)}))
    row, = inventory(old, new)
    assert row.context_assessment == "INCOMPARABLE" and row.reason == "context-conflict"
    assert row.previous_node_key == row.replacement_node_key == "assets"


def test_unknown_descriptors_are_not_a_known_conflict():
    old = example_bundle()
    new = validate_changed(old, coverage=(old.coverage[0].model_copy(update={"assessment": "PARTIAL", "reason": "unknown-unit"}),))
    new = reported(new, unit=Unit(status="UNKNOWN"))
    row, = inventory(old, new)
    assert row.context_assessment == "NOT_ASSESSED" and not row.declared_context_equal


def test_actual_period_origin_is_provenance_not_output_identity():
    old = example_bundle()
    new = reported(old, period=old.selections[0].period.model_copy(update={"origin": "INFERRED"}))
    row, = inventory(old, new)
    assert row.provenance_changed and row.previous_node_key and row.replacement_node_key


@pytest.mark.parametrize("old_unknown,new_unknown", [(True, True), (True, False), (False, True)])
def test_unknown_inventory_and_known_periods_never_guessed(old_unknown, new_unknown):
    known = example_bundle()
    unknown = partial_period(known, Period(kind="UNKNOWN", origin="UNKNOWN"))
    rows = inventory(unknown if old_unknown else known, unknown if new_unknown else known)
    unknown_rows = [r for r in rows if r.period_kind == "UNKNOWN"]
    assert len(unknown_rows) == 1 and unknown_rows[0].reason == "unknown-period-unmatched"
    assert unknown_rows[0].context_assessment == "NOT_ASSESSED"
    if old_unknown and new_unknown:
        assert len(rows) == 1 and unknown_rows[0].previous_node_key and unknown_rows[0].replacement_node_key
    else:
        assert len(rows) == 2 and all(r.endpoint_presence_changed for r in rows)


def test_different_actual_durations_remain_distinct():
    old = reported(example_bundle(), period=Period(kind="DURATION", start=date(2025, 1, 1), end=date(2025, 12, 31), origin="REPORTED"))
    new = reported(old, period=old.selections[0].period.model_copy(update={"start": date(2025, 2, 1)}))
    rows = inventory(old, new)
    assert len(rows) == 2 and all(r.endpoint_presence_changed for r in rows)


@pytest.mark.parametrize("state", ["MISSING", "NOT_ASSESSED", "INVALIDATED"])
def test_no_value_state_keeps_endpoint_and_is_not_verified_withdrawal(state):
    old = example_bundle()
    selected = old.selections[0].model_copy(update={"state": state, "value_decimal_text": None,
        "observation_key": None, "reason": "original-owner-reason"})
    new = validate_changed(old, selections=(selected,), coverage=(old.coverage[0].model_copy(update={"assessment": "PARTIAL", "reason": "missing"}),))
    row, = inventory(old, new)
    assert row.state_changed and row.value_text_changed and not row.endpoint_presence_changed
    assert row.context_assessment == "NOT_ASSESSED"


def test_absent_graph_is_gap_and_no_graphs_cannot_invent_period():
    row, = inventory(example_bundle(), None)
    assert row.reason == "endpoint-unavailable" and row.endpoint_presence_changed
    assert row.replacement_node_key is None and not row.state_changed
    assert inventory(None, None) == []


def test_dependency_aliases_and_container_order_are_noise_but_roles_are_meaningful():
    old = basis_graph()
    raw = next(n for n in old.selections if n.purpose == "INTERMEDIATE")
    out = next(n for n in old.selections if n.purpose == "OUTPUT")
    new_raw = raw.model_copy(update={"node_key": "local-raw"})
    new_out = out.model_copy(update={"node_key": "local-output"})
    new = validate_changed(old, selections=(new_out, new_raw), dependencies=(old.dependencies[0].model_copy(update={
        "parent_node_key": new_out.node_key, "selection_node_key": new_raw.node_key}),))
    assert inventory(old, new) == []
    transition = new_out.basis_transition.model_copy(update={"input_roles": ("other-role",)})
    changed = validate_changed(new, selections=(new_out.model_copy(update={"required_input_roles": ("other-role",), "basis_transition": transition}), new_raw),
        dependencies=(new.dependencies[0].model_copy(update={"role": "other-role"}),))
    row, = inventory(old, changed)
    assert row.provenance_changed and not row.value_text_changed


@pytest.mark.parametrize("change", ["definition", "terms", "dates", "document", "class"])
def test_reachable_definition_action_or_evidence_is_not_hidden(change):
    old = basis_graph()
    if change == "definition":
        new = validate_changed(old, selections=tuple(n.model_copy(update={"definition_revision": "new"}) if n.purpose == "OUTPUT" else n for n in old.selections))
    elif change in {"terms", "dates"}:
        action = old.actions[0].model_copy(update={"new_shares_text": "2"} if change == "terms" else {
            "date_witnesses": (old.actions[0].date_witnesses[0].model_copy(update={"role": "DISCLOSED"}), *old.actions[0].date_witnesses[1:])})
        new = validate_changed(old, actions=(action,))
    elif change == "document":
        doc = old.document_evidence[0].model_copy(update={"reference": old.document_evidence[0].reference.model_copy(update={"source_document": "later-disclosure"})})
        new = validate_changed(old, document_evidence=(doc,))
    else:
        new = validate_changed(old, actions=(old.actions[0].model_copy(update={"security_class": "other-common"}),),
            observations=(old.observations[0].model_copy(update={"share_basis": old.observations[0].share_basis.model_copy(update={"security_class": "other-common"})}),),
            selections=tuple(n.model_copy(update={"share_basis": n.share_basis.model_copy(update={"security_class": "other-common"})}) for n in old.selections))
    row, = inventory(old, new)
    assert row.provenance_changed
    assert row.context_assessment == ("INCOMPARABLE" if change in {"terms", "class"} else "NOT_ASSESSED")


def test_unreachable_document_does_not_create_event_and_assumption_changes_do():
    old = document_bundle()
    extra = old.document_evidence[0].model_copy(update={"key": "unreachable", "reference": old.document_evidence[0].reference.model_copy(update={"source_document": "unused"})})
    assert inventory(old, validate_changed(old, document_evidence=(*old.document_evidence, extra))) == []
    old = assumed_graph()
    new = validate_changed(old, assumptions=(old.assumptions[0].model_copy(update={"reason": "another-declared-reason"}),))
    row, = inventory(old, new)
    assert row.view == "ASSUMED" and row.provenance_changed


def test_equivalent_positive_action_terms_preserve_raw_provenance_without_conflict():
    old = basis_graph()
    new = validate_changed(old, actions=(old.actions[0].model_copy(update={"old_shares_text": "8.00", "new_shares_text": "1.00"}),))
    row, = inventory(old, new)
    assert row.provenance_changed and row.context_assessment == "NOT_ASSESSED"
    assert not row.value_text_changed and not row.declared_context_equal
    assert old.actions[0].old_shares_text == "8" and new.actions[0].old_shares_text == "8.00"
    changed = validate_changed(old, actions=(old.actions[0].model_copy(update={"legal_identity": "different-actual-action"}),))
    row, = inventory(old, changed)
    assert row.context_assessment == "INCOMPARABLE"
    for field in ("old_shares_text", "new_shares_text"):
        with pytest.raises(ValidationError):
            validate_changed(old, actions=(old.actions[0].model_copy(update={field: None}),))


def test_shared_dag_nodes_are_projected_once():
    bundle = basis_graph()
    raw = next(n for n in bundle.selections if n.purpose == "INTERMEDIATE")
    output = next(n for n in bundle.selections if n.purpose == "OUTPUT")
    outputs = tuple(output.model_copy(update={"node_key": f"output-{i}", "metric_path": f"fixture.metric.{i}"}) for i in range(100))
    bundle = validate_changed(bundle, selections=(raw, *outputs),
        dependencies=tuple(bundle.dependencies[0].model_copy(update={"parent_node_key": n.node_key}) for n in outputs),
        coverage=(bundle.coverage[0].model_copy(update={"metric_paths": tuple(n.metric_path for n in outputs)}),))
    projection = OutputGraphProjection(bundle)
    for output in projection.outputs().values(): projection.provenance(output)
    alias = raw.model_copy(update={"node_key": "same-shared-input-new-key"})
    other = validate_changed(bundle, selections=(*reversed(outputs), alias),
        dependencies=tuple(d.model_copy(update={"selection_node_key": alias.node_key}) for d in bundle.dependencies))
    assert inventory(bundle, other) == []
    assert len(projection._nodes) == 101 and len(projection._observations) == 1


@pytest.mark.parametrize("changes", [dict(previous_bundle_id=1, previous_node_key="assets", parent_release_id=None),
    dict(previous_bundle_id=1, previous_node_key=None), dict(replacement_bundle_id=None, replacement_node_key="assets"),
    dict(previous_bundle_id=None, previous_node_key=None, replacement_bundle_id=None, replacement_node_key=None),
    dict(context_assessment="COMPARABLE"), dict(period_start=date(2025, 1, 1)),
    dict(period_kind="UNKNOWN", period_end=None, reason="claimed-match"),
    dict(period_kind="UNKNOWN", period_end=None, reason="unknown-period-unmatched", context_assessment="INCOMPARABLE")])
def test_transition_shape_rejects_forged_contract(changes):
    row, = inventory(None, example_bundle())
    with pytest.raises(ValidationError):
        ActivationTransition.model_validate(row.model_copy(update=changes))
