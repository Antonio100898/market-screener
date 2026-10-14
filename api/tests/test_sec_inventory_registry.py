"""Finite producer registry and admission through the existing automatic assembly."""
from copy import deepcopy
import json

import pytest

from screener import automatic_ingestion as automatic
from screener import sec_inventory_plan as owner
from test_automatic_ingestion import assembled, registry
from test_sec_inventory_plan import parameters, NOW


def occurrence():
    return dict(schedule_key=owner.NAMESPACE + "finite", scheduled_for=NOW.isoformat(),
        kind=owner.KIND, parameters=parameters(), due_at=NOW.isoformat())


def test_finite_registry_reuses_same_stores_single_worker_and_retains_supported21(monkeypatch, tmp_path, registry):
    settings, events, _ = assembled(monkeypatch, tmp_path)
    settings.plan_path.write_text(json.dumps(dict(plan_revision=1, occurrences=[occurrence()])))
    assembly = automatic.start_automatic_ingestion(settings)
    try:
        handler = assembly.runtime.handlers[owner.KIND]
        assert isinstance(handler, owner.SecFiniteInventoryHandler)
        assert handler.jobs is assembly.runtime.repository
        assert handler.discovery._resource_kind == "sec-resource-fetch-v2"
        assert handler.discovery._occurrence_bound is True
        assert assembly.runtime.worker_count == 1
        assert "20261009_0021" in automatic.SUPPORTED_SCHEMA_REVISIONS
        assert "sec-recent-discovery-v3" in assembly.runtime.handlers
        assert assembly.runtime.source(NOW)[0].parameters == parameters()
    finally:
        automatic.stop_automatic_ingestion(assembly)


@pytest.mark.parametrize("mutation", [
    lambda item: item["parameters"].update(request_revision=True),
    lambda item: item["parameters"].update(expected_parent_release_id=1.0),
    lambda item: item["parameters"].update(unknown=True),
    lambda item: item.update(schedule_key=owner.NAMESPACE + "different"),
    lambda item: item.update(scheduled_for="2026-09-29T12:01:00+00:00"),
])
def test_finite_plan_refuses_changed_identity_before_services(monkeypatch, tmp_path, mutation, registry):
    settings, events, _ = assembled(monkeypatch, tmp_path)
    item = occurrence()
    mutation(item)
    settings.plan_path.write_text(json.dumps(dict(plan_revision=1, occurrences=[item])))
    monkeypatch.setattr(automatic, "create_postgres_engine", lambda *a: pytest.fail("invalid plan reached database factory"))
    with pytest.raises(ValueError):
        automatic.start_automatic_ingestion(settings)


def test_finite_plan_duplicate_json_keys_refuse_before_registry(tmp_path):
    path = tmp_path / "plan.json"
    raw = json.dumps(dict(plan_revision=1, occurrences=[occurrence()]))
    raw = raw.replace('"request_revision": 1', '"request_revision": 1, "request_revision": 1')
    path.write_text(raw)
    with pytest.raises(ValueError, match="duplicate"):
        automatic.read_plan(path)
