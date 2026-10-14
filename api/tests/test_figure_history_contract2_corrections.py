"""Known supplied graphs cannot hide incomplete nodes or contradictory actions."""
import pytest
from screener.figure_history import CalculationDependency, Period, Unit
from tests.test_figure_history import validate_changed
from tests.test_figure_history_contract2 import document_bundle, basis_graph


@pytest.mark.parametrize("field,value", [("unit", Unit(status="UNKNOWN")),
    ("period", Period(kind="UNKNOWN", origin="UNKNOWN"))])
@pytest.mark.parametrize("depth", [1, 3])
def test_full_checks_every_reachable_intermediate(field, value, depth):
    bundle = document_bundle()
    base = bundle.selections[0]
    nodes, edges = [], []
    for i in range(depth + 1):
        node = base.model_copy(update={"node_key": f"stage-{i}", "purpose": "OUTPUT" if i == depth else "INTERMEDIATE",
            "state": "DERIVED", "observation_key": None, "definition": "existing.identity", "definition_revision": "1",
            "required_input_roles": ("input",), **({field: value} if i == 0 else {})})
        nodes.append(node)
        edges.append(CalculationDependency(parent_node_key=node.node_key, ordinal=0, role="input",
            **({"observation_key": bundle.observations[0].key} if i == 0 else {"selection_node_key": f"stage-{i-1}"})))
    with pytest.raises(ValueError, match="unknown unit/actual period"):
        validate_changed(bundle, selections=tuple(nodes), dependencies=tuple(edges))
    partial = bundle.coverage[0].model_copy(update={"assessment": "PARTIAL", "reason": "unassessed-input-basis"})
    assert validate_changed(bundle, selections=tuple(nodes), dependencies=tuple(edges), coverage=(partial,)).coverage[0].assessment == "PARTIAL"


@pytest.mark.parametrize("origin", ["INFERRED", "UNKNOWN"])
def test_origin_cannot_create_a_second_output(origin):
    bundle = document_bundle()
    output = bundle.selections[0]
    duplicate = output.model_copy(update={"node_key": "another", "state": "NOT_ASSESSED", "observation_key": None,
        "value_decimal_text": None, "reason": "supplied-gap", "period": output.period.model_copy(update={"origin": origin})})
    with pytest.raises(ValueError, match="duplicate metric/period/view"):
        validate_changed(bundle, selections=(output, duplicate))


@pytest.mark.parametrize("problem", ["input-class", "output-class", "omitted-incorporation", "already-incorporated", "unnamed-change"])
def test_transition_binds_actual_classes_and_incorporation(problem):
    bundle = basis_graph()
    raw, output = sorted(bundle.selections, key=lambda n: n.purpose == "OUTPUT")
    changes = {}
    if problem == "input-class":
        basis = raw.share_basis.model_copy(update={"security_class": "preferred"})
        raw = raw.model_copy(update={"share_basis": basis})
        changes["observations"] = (bundle.observations[0].model_copy(update={"share_basis": basis}),)
    elif problem == "output-class":
        output = output.model_copy(update={"share_basis": output.share_basis.model_copy(update={"security_class": "preferred", "incorporated_actions": ()})})
    elif problem == "omitted-incorporation":
        output = output.model_copy(update={"share_basis": output.share_basis.model_copy(update={"incorporated_actions": ()})})
    elif problem == "already-incorporated":
        raw = raw.model_copy(update={"share_basis": output.share_basis})
        changes["observations"] = (bundle.observations[0].model_copy(update={"share_basis": output.share_basis}),)
    else:
        output = output.model_copy(update={"basis_transition": output.basis_transition.model_copy(update={"action_keys": ()})})
    with pytest.raises(ValueError, match="actual .*share class|actual incorporation change"):
        validate_changed(bundle, selections=(raw, output), **changes)


def test_unknown_incorporation_does_not_invent_a_known_action_delta():
    bundle = basis_graph(reason="unknown-incorporation")
    raw, output = sorted(bundle.selections, key=lambda n: n.purpose == "OUTPUT")
    unknown = raw.share_basis.model_copy(update={"incorporation": "UNKNOWN", "evidence_keys": ()})
    raw = raw.model_copy(update={"share_basis": unknown})
    valid = validate_changed(bundle, selections=(raw, output),
        observations=(bundle.observations[0].model_copy(update={"share_basis": unknown}),))
    assert valid.coverage[0].assessment == "PARTIAL"
    with pytest.raises(ValueError, match="actual input/output share class"):
        validate_changed(valid, actions=(valid.actions[0].model_copy(update={"security_class": "preferred"}),),
            selections=(raw, output.model_copy(update={"share_basis": output.share_basis.model_copy(update={"incorporated_actions": ()})})))
