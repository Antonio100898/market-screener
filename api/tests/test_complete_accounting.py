import json

import pytest

from screener.complete_accounting import load_accounting
from screener.display_publication import DisplayReleasePreparer, canonical_bytes


def ledger():
    binding = dict(security_id=1, snapshot_id=11, exact_bundle_id=None)
    return dict(accounting_revision=1, expected_parent_release_id=10,
                engine_identity_sha256="a" * 64,
                universe=[dict(key="retained-A", artifact_sha256s=["b" * 64]),
                          dict(key="unresolved-root", artifact_sha256s=["c" * 64])],
                outcomes=[dict(input_key="retained-A", status="SELECTED", selection=binding),
                          dict(input_key="unresolved-root", status="UNRESOLVED", reason="Original context unavailable")],
                parent_members=[binding], selected_members=[dict(**binding, disposition="RETAINED")],
                retirements=[])


def read(value):
    return load_accounting(canonical_bytes(value))


def test_exact_input_parent_and_selected_sets_and_unresolved_reason():
    value = read(ledger())
    value.validate_publication(10, [dict(security_id=1, snapshot_id=11, exact_bundle_id=None)], [])
    value.validate_display([dict(security_id=1, snapshot_id=11, exact_bundle_id=None)])
    assert value.outcomes[1].reason == "Original context unavailable"
    assert value.outcomes[1].selection is None
    assert read(json.loads(value.model_dump_json())) == value


@pytest.mark.parametrize("mutation", [
    lambda v: v["retirements"].append(1),
    lambda v: v["universe"].append(v["universe"][0]),
    lambda v: v["outcomes"].pop(),
    lambda v: v["outcomes"].append(v["outcomes"][0]),
    lambda v: v["outcomes"][1].update(input_key="extra"),
    lambda v: v["outcomes"][1].update(selection=v["parent_members"][0]),
    lambda v: v["outcomes"][1].update(reason=" "),
    lambda v: v["selected_members"].clear(),
    lambda v: v["selected_members"][0].update(snapshot_id=12),
    lambda v: v["parent_members"].append(v["parent_members"][0]),
    lambda v: v["selected_members"][0].update(candidate_id=9),
    lambda v: v["selected_members"][0].update(disposition="NEW", candidate_id=9),
    lambda v: v.update(expected_parent_release_id=None),
    lambda v: v.update(accounting_revision=True),
    lambda v: v.update(accounting_revision=2),
    lambda v: v["parent_members"][0].update(security_id=True),
    lambda v: v["universe"][0].update(key="A\x00B"),
    lambda v: v["universe"][0].update(artifact_sha256s=[]),
    lambda v: v.update(extra_count=2),
])
def test_bad_accounting_refuses_omissions_conflicts_and_fake_security(mutation):
    value = ledger()
    mutation(value)
    with pytest.raises(ValueError):
        read(value)


def test_registered_replacement_is_exact_and_retains_actual_locked_parent():
    value = ledger()
    replacement = dict(security_id=1, snapshot_id=12, exact_bundle_id=8)
    value["selected_members"] = [dict(**replacement, disposition="REPLACED", candidate_id=3)]
    value["outcomes"][0]["selection"] = replacement
    accounting = read(value)
    candidate = dict(candidate_id=3, **replacement)
    accounting.validate_publication(10, value["parent_members"], [candidate])
    with pytest.raises(ValueError, match="registered"):
        accounting.validate_publication(10, value["parent_members"], [{**candidate, "snapshot_id": 13}])
    with pytest.raises(ValueError, match="locked parent"):
        accounting.validate_publication(10, [], [candidate])
    with pytest.raises(ValueError, match="expected parent"):
        accounting.validate_publication(9, value["parent_members"], [candidate])


def test_selected_input_covers_actual_retained_artifacts():
    accounting = read(ledger())
    accounting.validate_artifacts([dict(security_id=1, canonical_input_artifacts=[dict(content_sha256="b" * 64)])])
    with pytest.raises(ValueError, match="snapshot artifacts"):
        accounting.validate_artifacts([dict(security_id=1, canonical_input_artifacts=[dict(content_sha256="d" * 64)])])


def test_diagnostic_empty_selection_is_retained_without_activation(monkeypatch):
    value = ledger()
    value.update(expected_parent_release_id=None, parent_members=[], selected_members=[])
    value["outcomes"][0] = dict(input_key="retained-A", status="DEFERRED", reason="Configured security not assessed")
    accounting = read(value)
    preparer = object.__new__(DisplayReleasePreparer)
    reads, stored = [], []
    preparer._read = lambda sha: reads.append(sha)
    preparer._store = lambda record: stored.append(record) or "e" * 64
    assert preparer.record_accounting(accounting) == "e" * 64
    assert set(reads) == {"b" * 64, "c" * 64}
    assert stored[0]["kind"] == "COMPLETE_ACCOUNTING_DIAGNOSTIC"
    with pytest.raises(ValueError, match="nonempty"):
        preparer.prepare_complete([], accounting=accounting, capture_mode="regression_import", limitations=["Fixture"])


def test_same_artifact_can_belong_to_distinct_inputs_but_read_once():
    value = ledger()
    value["universe"][1]["artifact_sha256s"] = ["b" * 64]
    preparer = object.__new__(DisplayReleasePreparer)
    reads = []
    preparer._read = lambda sha: reads.append(sha)
    result = preparer._verify_accounting(read(value))
    assert len(result.universe) == 2
    assert reads == ["b" * 64]


def test_internal_model_copy_cannot_bypass_validation_at_artifact_boundary():
    preparer = object.__new__(DisplayReleasePreparer)
    preparer._read = lambda sha: pytest.fail("invalid accounting reached artifact read")
    with pytest.raises(ValueError):
        preparer._verify_accounting(read(ledger()).model_copy(update={"retirements": (1,)}))


def complete_prepared():
    from screener.display_publication import PreparedDisplayRelease, digest, typed_projection
    from test_display_publication import projected_row
    row = projected_row()
    entry = dict(security_id=1, snapshot_id=11, canonical_input_artifacts=[dict(content_sha256="b" * 64)],
                 strict_row_sha256=digest(row), assumed_row_sha256=None, source_observation_ids=[4], captures={})
    bundle = digest(entry)
    member = {**entry, "input_bundle_sha256": bundle}
    manifest = dict(contract_revision=3, projection_revision=1, complete_accounting=ledger(), members=[member])
    projected = typed_projection(row, ordinal=0, security_id=1, input_bundle_sha256=bundle)
    preparer = object.__new__(DisplayReleasePreparer)
    retained = {bundle: canonical_bytes(entry), "b" * 64: b"B", "c" * 64: b"C"}

    def prepared_for(value, rows=None, revision=1):
        from hashlib import sha256
        raw = canonical_bytes(value)
        sha = sha256(raw).hexdigest()
        retained[sha] = raw
        return PreparedDisplayRelease(revision, sha, raw, canonical_bytes(rows or [projected]))

    preparer._read = retained.__getitem__
    return preparer, manifest, projected, prepared_for


@pytest.mark.parametrize("target", ["contract", "projection", "prepared", "security", "snapshot", "source", "row_security", "ordinal"])
def test_complete_display_rejects_equal_scalar_identity_tampering(target):
    preparer, manifest, row, make = complete_prepared()
    assert preparer.verify(make(manifest))[0] == manifest
    revision = 1
    if target == "contract":
        manifest["contract_revision"] = 3.0
    elif target == "projection":
        manifest["projection_revision"] = 1.0
    elif target == "prepared":
        revision = 1.0
    elif target in ("security", "snapshot"):
        key = target + "_id"
        manifest["members"][0][key] = float(manifest["members"][0][key])
    elif target == "source":
        manifest["members"][0]["source_observation_ids"] = [4.0]
    elif target == "row_security":
        row["security_id"] = 1.0
    else:
        row["universe_ordinal"] = False
    with pytest.raises(ValueError):
        preparer.verify(make(manifest, [row], revision))


def test_accounting_rejects_duplicate_json_key_instead_of_last_winner():
    raw = canonical_bytes(ledger()).replace(b'"accounting_revision":1', b'"accounting_revision":2,"accounting_revision":1')
    with pytest.raises(ValueError):
        load_accounting(raw)
