"""Prepared readonly current-build reconstruction over owned real staged fixtures."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from screener.artifacts import store_evidence
from screener.captured_assembly import engine_identity
from screener.company_import import _canonical_json
from screener import sync, store
from test_primary_input_transport_storage import planned_flow, storage, database_url, pytestmark
from test_sec_ingestion_storage import _context
from test_sec_completed_capture_storage import rows


def recorded(storage,key):
    flow=planned_flow(storage,key)
    checkpoint=flow.stage(_context(flow.planned,flow.jobs))
    recipe=dict(recipe_revision=2,engine_revision=store.ENGINE_VERSION,engine_identity_sha256=engine_identity(),
        members=[dict(security_id=checkpoint["security_id"],snapshot_id=checkpoint["snapshot_id"],
            market_recipe_sha256=None,primary_input_plan_sha256=flow.plan_artifact.content_sha256,
            company_facts_occurrence_id=flow.plan.dependencies[0].delivery.occurrence_id)])
    artifact=store_evidence(_canonical_json(recipe),"application/json",flow.objects,flow.artifacts)
    return flow,checkpoint,recipe,artifact.content_sha256


def test_actual_registered_recipe_reconstructs_existing_snapshot_readonly(storage,monkeypatch):
    flow,checkpoint,recipe,sha=recorded(storage,"native-reconstruction")
    before=rows(flow.engine)
    def forbidden(*args,**kwargs):pytest.fail("readonly reconstruction wrote/selected current/fetched")
    for obj,name in ((flow.objects,"put_verified"),(flow.source,"record"),(flow.source,"latest_sec_filing"),
                     (flow.stage._companies,"current"),(flow.stage._edgar,"fetch")):
        monkeypatch.setattr(obj,name,forbidden)
    proof=flow.stage._history.prepare_registered_native_reconstruction(sha,
        expected_engine_identity_sha256=engine_identity(),object_store=flow.objects)
    value=flow.stage._history.verify_native_reconstruction(proof)
    assert proof.snapshot_id==checkpoint["snapshot_id"] and value["canonical_row"]
    assert value["financial_status"]==value["currentness_status"]=="NOT_ASSESSED"
    assert all(node.state=="NOT_ASSESSED" and node.value_decimal_text is None for node in proof.selections)
    assert rows(flow.engine)==before


def test_actual_wrong_registered_recipe_identity_refuses_without_mutation(storage):
    flow,checkpoint,recipe,sha=recorded(storage,"native-wrong-reconstruction")
    wrong=deepcopy(recipe);wrong["members"][0]["security_id"]+=1000
    artifact=store_evidence(_canonical_json(wrong),"application/json",flow.objects,flow.artifacts)
    before=rows(flow.engine)
    from screener.captured_assembly import PreparationUnavailable
    with pytest.raises((ValueError,PreparationUnavailable)):
        flow.stage._history.prepare_registered_native_reconstruction(artifact.content_sha256,
            expected_engine_identity_sha256=engine_identity(),object_store=flow.objects)
    assert rows(flow.engine)==before


def test_actual_separate_readonly_seal_does_not_enter_native_writer(storage):
    flow,checkpoint,recipe,sha=recorded(storage,"native-proof-mode")
    repository=flow.stage._history
    proof=repository.prepare_registered_native_reconstruction(sha,
        expected_engine_identity_sha256=engine_identity(),object_store=flow.objects)
    before=rows(flow.engine)
    with pytest.raises(ValueError):repository.verify_native_reconstruction(replace(proof,verification_seal=b"counterfeit"))
    graph=repository.load(checkpoint["exact_bundle_id"])
    with pytest.raises(ValueError):repository._require_storage_contract(graph,proof)
    assert rows(flow.engine)==before
