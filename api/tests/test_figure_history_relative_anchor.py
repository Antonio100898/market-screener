"""Relative basis is documentary scope, never a financial-operation qualifier."""
from datetime import date
import hashlib

import pytest

from screener.figure_history import ExactFigureBundle, OutputGraphProjection, ShareBasis
from tests.test_figure_history import validate_changed
from tests.test_figure_history_contract2 import basis_graph
from tests.test_activation_history import inventory


def relative_graph(*, incorporation="KNOWN"):
    graph = basis_graph(reason="unknown-incorporation" if incorporation == "UNKNOWN" else "validator-unavailable")
    observed = graph.observations[0]
    before = observed.share_basis.model_copy(update=dict(anchor_evidence_key="disclosure",
        anchor_as_of=date(2022, 10, 19), incorporation=incorporation))
    nodes = tuple(n.model_copy(update={"share_basis": n.share_basis.model_copy(update=dict(
        anchor_evidence_key="disclosure", anchor_as_of=date(2022, 10, 19),
        **({"incorporation": incorporation} if n.purpose == "INTERMEDIATE" else {})))}) for n in graph.selections)
    return validate_changed(graph, observations=(observed.model_copy(update={"share_basis": before}),), selections=nodes)


def rename_document(graph, key):
    document = graph.document_evidence[0].model_copy(update={"key": key})
    def basis(value):
        return value.model_copy(update={"anchor_evidence_key": key, "evidence_keys": (key,)})
    actions = tuple(a.model_copy(update={"evidence_keys": (key,), "date_witnesses": tuple(
        w.model_copy(update={"evidence_key": key}) for w in a.date_witnesses)}) for a in graph.actions)
    return validate_changed(graph, document_evidence=(document,), actions=actions,
        observations=tuple(o.model_copy(update={"share_basis": basis(o.share_basis)}) for o in graph.observations),
        selections=tuple(n.model_copy(update={"share_basis": basis(n.share_basis),
            **({"basis_transition": n.basis_transition.model_copy(update={"evidence_keys": (key,)})}
               if n.basis_transition is not None else {})}) for n in graph.selections))


def test_pre_edit_absolute_bundle_bytes_remain_exact():
    raw = basis_graph().model_dump_json().encode()
    assert len(raw) == 4698
    assert hashlib.sha256(raw).hexdigest() == "df267bbcd9e903b37fb481d03a11c77d102e2f472a73de68609e0468513370c5"
    assert b"anchor_evidence_key" not in raw and b"anchor_as_of" not in raw


@pytest.mark.parametrize("update", [{"anchor_evidence_key": "doc"}, {"anchor_as_of": date(2022, 10, 19)},
    {"anchor_evidence_key": "missing", "anchor_as_of": date(2022, 10, 19)},
    {"anchor_evidence_key": "doc", "anchor_as_of": date(2022, 10, 19), "security_class": None}])
def test_anchor_pair_actual_class_and_member_document_required(update):
    fields = dict(kind="ORDINARY", security_class="common", incorporation="UNKNOWN", evidence_keys=("doc",))
    with pytest.raises(ValueError):
        ShareBasis.model_validate({**fields, **update})


@pytest.mark.parametrize("incorporation", ["KNOWN", "UNKNOWN"])
def test_relative_partial_roundtrip_and_resolved_projection(incorporation):
    graph = relative_graph(incorporation=incorporation)
    restored = ExactFigureBundle.model_validate_json(graph.model_dump_json())
    assert restored == graph and restored.coverage[0].assessment == "PARTIAL"
    projection = OutputGraphProjection(restored)
    output = next(n for n in restored.selections if n.purpose == "OUTPUT")
    anchor = projection.context(output)["share_basis"]["anchor"]
    assert anchor["as_of"] == "2022-10-19"
    assert anchor["evidence"]["reference"]["location"] == graph.document_evidence[0].reference.location.model_dump(mode="json")
    with pytest.raises(ValueError):
        validate_changed(graph, coverage=(graph.coverage[0].model_copy(update={"assessment": "FULL"}),))


@pytest.mark.parametrize("problem", ["missing-document", "wrong-role", "absolute", "as-of", "class", "different-span"])
def test_invalid_relative_crossings_fail_closed(problem):
    graph = relative_graph()
    before, output = sorted(graph.selections, key=lambda n: n.purpose == "OUTPUT")
    changes = {}
    if problem == "missing-document":
        changes["document_evidence"] = ()
    elif problem == "wrong-role":
        changes["document_evidence"] = (graph.document_evidence[0].model_copy(update={"roles": ("LEGAL_ACTION",)}),)
    else:
        update = {"anchor_evidence_key": None, "anchor_as_of": None} if problem == "absolute" else (
            {"anchor_as_of": date(2022, 10, 20)} if problem == "as-of" else {"security_class": "preferred"})
        if problem == "different-span":
            second = graph.document_evidence[0].model_copy(update={"key": "different", "reference":
                graph.document_evidence[0].reference.model_copy(update={"source_document": "https://www.sec.gov/another"})})
            changes["document_evidence"] = (*graph.document_evidence, second)
            update = {"anchor_evidence_key": "different", "evidence_keys": ("disclosure", "different")}
        output = output.model_copy(update={"share_basis": output.share_basis.model_copy(update=update)})
        changes["selections"] = (before, output)
    with pytest.raises(ValueError):
        validate_changed(graph, **changes)


def test_local_anchor_alias_is_same_authenticated_basis_and_digest():
    graph = relative_graph()
    renamed = rename_document(graph, "local-alias")
    output = next(n for n in graph.selections if n.purpose == "OUTPUT")
    assert OutputGraphProjection(graph).provenance(output) == OutputGraphProjection(renamed).provenance(output)
    assert inventory(graph, renamed) == []


@pytest.mark.parametrize("problem", ["absolute", "as-of", "same-key-different-source", "different-span"])
def test_incompatible_anchors_are_incomparable_without_numeric_delta(problem):
    old = relative_graph()
    if problem == "absolute":
        new = basis_graph()
    elif problem == "as-of":
        new = validate_changed(old,
            observations=tuple(o.model_copy(update={"share_basis": o.share_basis.model_copy(update={"anchor_as_of": date(2022, 10, 20)})}) for o in old.observations),
            selections=tuple(n.model_copy(update={"share_basis": n.share_basis.model_copy(update={"anchor_as_of": date(2022, 10, 20)})}) for n in old.selections))
    else:
        ref = old.document_evidence[0].reference
        ref = ref.model_copy(update={"source_document": "https://www.sec.gov/another"} if problem == "same-key-different-source" else {
            "location": ref.location.model_copy(update={"span_sha256": "f" * 64})})
        new = validate_changed(old, document_evidence=(old.document_evidence[0].model_copy(update={"reference": ref}),))
    row, = inventory(old, new)
    assert row.context_assessment == "INCOMPARABLE" and row.reason == "context-conflict"
    assert not row.declared_context_equal
    assert "delta_decimal_text" not in type(row).model_fields


def test_same_physical_anchor_alias_can_support_relative_partial_crossing():
    graph = relative_graph()
    alias = graph.document_evidence[0].model_copy(update={"key": "alias"})
    output = next(n for n in graph.selections if n.purpose == "OUTPUT")
    nodes = tuple(n.model_copy(update={"share_basis": n.share_basis.model_copy(update={
        "anchor_evidence_key": "alias", "evidence_keys": ("disclosure", "alias")})})
        if n.node_key == output.node_key else n for n in graph.selections)
    restored = validate_changed(graph, document_evidence=(*graph.document_evidence, alias), selections=nodes)
    assert restored.coverage[0].assessment == "PARTIAL"


def test_missing_input_basis_cannot_claim_a_relative_crossing_even_without_actions():
    graph = relative_graph(incorporation="UNKNOWN")
    before, output = sorted(graph.selections, key=lambda n: n.purpose == "OUTPUT")
    before = before.model_copy(update={"share_basis": None})
    output = output.model_copy(update={"share_basis": output.share_basis.model_copy(update={"incorporated_actions": ()}),
        "basis_transition": output.basis_transition.model_copy(update={"action_keys": ()})})
    with pytest.raises(ValueError, match="same authenticated anchor"):
        validate_changed(graph, observations=(graph.observations[0].model_copy(update={"share_basis": None}),),
            selections=(before, output))


def test_required_basis_role_alias_crosses_without_treating_other_roles_as_identity():
    graph = relative_graph()
    alias = graph.document_evidence[0].model_copy(update={"key": "basis-only", "roles": ("INCORPORATED_BASIS",)})
    output = next(n for n in graph.selections if n.purpose == "OUTPUT")
    nodes = tuple(n.model_copy(update={"share_basis": n.share_basis.model_copy(update={
        "anchor_evidence_key": alias.key, "evidence_keys": ("disclosure", alias.key)})})
        if n.node_key == output.node_key else n for n in graph.selections)
    accepted = validate_changed(graph, document_evidence=(*graph.document_evidence, alias), selections=nodes)
    assert accepted.coverage[0].assessment == "PARTIAL"
    assert alias.roles == ("INCORPORATED_BASIS",)


def test_role_change_preserves_provenance_without_making_same_anchor_incomparable():
    old = relative_graph()
    doc = old.document_evidence[0].model_copy(update={"roles": ("LEGAL_ACTION", "INCORPORATED_BASIS")})
    new = validate_changed(old, document_evidence=(doc,))
    row, = inventory(old, new)
    assert row.provenance_changed and row.declared_context_equal
    assert row.context_assessment == "NOT_ASSESSED"
    assert "roles" not in OutputGraphProjection(new).context(new.selections[-1])["share_basis"]["anchor"]["evidence"]
    assert OutputGraphProjection(new)._basis(new.selections[-1].share_basis)["evidence"][0]["roles"] == sorted(doc.roles)
