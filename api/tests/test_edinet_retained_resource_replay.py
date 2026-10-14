"""Historical resource delivery replays two actual retained Alpen annual sources."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from screener import evidence, sync
from screener.edinet_ingestion import EdinetResourceFetchHandler, _archive_metadata
from screener.shared_companies import SnapshotArtifact
from screener.sources.edinet_mapper import rebuild_companyfacts
from test_edinet_ingestion import (NOW, ResourceArtifacts, ResourceClient,
                                  ResourceCompanies, ResourceObjects, _resource_context)


_INPUTS = Path(__file__).resolve().parents[2] / "local/edinet-raw-export-observation-38442625807-2026-10-13/files"


def _actual_inputs():
    if not (_INPUTS / "edinet/S100WQEC.zip").is_file():
        pytest.skip("approved genuine older Alpen source unavailable")
    cached = json.loads((_INPUTS / "companyfacts_E03475.json").read_bytes())
    archives = {document: (_INPUTS / "edinet" / f"{document}.zip").read_bytes()
                for document in ("S100WQEC", "S100Z2YO")}
    assert hashlib.sha256(archives["S100WQEC"]).hexdigest() == "845a625d950b8d0798299ed07a73f2bbf0ac2e5b4ef3baa4f1761ab6dbdb927a"
    assert hashlib.sha256(archives["S100Z2YO"]).hexdigest() == "269373de7d467a2babdef75279b9e50c11d1bf55332d00de30acae8a089fc221"
    # Existing owner reconstructs legacy record fields; no invented publication time/basis.
    rebuilt = rebuild_companyfacts(cached, archives.__getitem__)
    records = {report["document"]: report["mapping_record"] for report in rebuilt["_adapter"]["reports"]}
    legacy = deepcopy(cached)
    legacy["_adapter"]["reports"] = [report for report in legacy["_adapter"]["reports"]
                                   if report["document"] == "S100Z2YO"]
    for data in legacy["facts"]["canonical"].values():
        for unit, entries in data["units"].items():
            data["units"][unit] = [entry for entry in entries if entry["accn"] == "S100Z2YO"]
    assert "accounting_standard" not in legacy["_adapter"]
    return legacy, archives, records


class AlpenAssociations:
    """Custody IDs are fake; records and source bytes are the actual retained reports."""
    def __init__(self, records, current_digest):
        self.parents = {}
        self.resources = {}
        self.by_digest = {}
        self.lookups = []
        for index, document in enumerate(("S100WQEC", "S100Z2YO")):
            parent = SimpleNamespace(item=SimpleNamespace(
                source_item_id=41 + index, source_system="EDINET", item_kind="filing",
                source_key=document, issuer_source_identifier="E03475"), observation=SimpleNamespace(
                    source_observation_id=51 + index, observation_sha256=str(5 + index) * 64,
                    state="present", canonical_metadata={"row": records[document]}))
            self.parents[51 + index] = parent
        current = self.parents[52]
        self._resource("S100Z2YO", current_digest, _archive_metadata(current,
            document_id="S100Z2YO", edinet_code="E03475", security_code="30280", observation_id=52))

    def _resource(self, document, artifact_digest, metadata, state="present"):
        observation_digest = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
        stored = SimpleNamespace(item=SimpleNamespace(source_item_id=61 if document == "S100WQEC" else 62,
            source_system="EDINET", item_kind="resource", source_key=f"{document}/xbrl",
            issuer_source_identifier="E03475", parent_source_item_id=metadata["parent_source_item_id"]),
            observation=SimpleNamespace(observation_sha256=observation_digest,
                artifact_sha256=artifact_digest, state=state, canonical_metadata=metadata))
        self.resources[document] = stored
        self.by_digest[observation_digest] = stored
        return stored

    def latest_edinet_filing(self, observation_id):
        return self.parents[observation_id]

    def latest_item(self, system, kind, source_key):
        assert (system, kind) == ("EDINET", "resource")
        self.lookups.append(source_key)
        return self.resources.get(source_key.removesuffix("/xbrl"))

    def by_sha256(self, digest):
        return self.by_digest[digest]

    def record(self, lease, **values):
        return self._resource(values["metadata"]["document_id"], values["artifact_sha256"],
                              values["metadata"], values["state"])


def _handler_case():
    legacy, archives, records = _actual_inputs()
    objects, artifacts = ResourceObjects(), ResourceArtifacts()
    raw = objects.put_verified(archives["S100Z2YO"], "application/zip")
    artifacts.add_verified(raw, "application/zip")
    canonical = objects.put_verified(json.dumps(legacy).encode(), "application/json")
    artifacts.add_verified(canonical, "application/json")
    current = SimpleNamespace(issuer_source="EDINET", issuer_identifier="E03475",
        security_identifier="edinet:E03475:primary-ordinary", artifacts=(
            SnapshotArtifact(raw.content_sha256, "raw_filing"),
            SnapshotArtifact(canonical.content_sha256, "canonical_edinet_facts")))
    observations = AlpenAssociations(records, raw.content_sha256)
    companies = ResourceCompanies(current)
    client = ResourceClient(archives["S100WQEC"])
    derived = []

    def derive(bundle):
        result = sync._derive_evidence_result(bundle)
        derived.append(result)
        return result.status, result.row

    handler = EdinetResourceFetchHandler(client=client, object_store=objects,
        artifacts=artifacts, observations=observations, companies=companies,
        listings={"3028": {"name": "Alpen"}}, derive=derive, clock=lambda: NOW)
    context, _ = _resource_context(document_id="S100WQEC", edinet_code="E03475", security_code="30280")
    return handler, context, observations, companies, client, derived


def test_historical_incoming_replays_actual_current_alpen_without_guessing_basis():
    handler, context, observations, companies, client, derived = _handler_case()
    handler(context)
    snap = derived[0].snapshot
    assert snap.common_equity_required
    assert snap.parent_equity.value == snap.common_equity.value == 126359000000
    assert snap.common_equity.operation_inputs[0].provenance.accession == "S100Z2YO"
    assert companies.calls[0]["source_accession"] == "S100Z2YO"
    assert set(derived[0].row["annual_net_income"]) >= {"2021", "2026"}
    assert client.calls == ["S100WQEC"]
    assert observations.lookups == ["S100Z2YO/xbrl"]


def test_same_document_replacement_uses_verified_incoming_without_old_archive_lookup():
    handler, _, observations, companies, client, derived = _handler_case()
    _, archives, _ = _actual_inputs()
    client.data = archives["S100Z2YO"]
    # An unused earlier raw input stays registered/history, not in the new active inputs.
    old = handler._object_store.put_verified(archives["S100WQEC"], "application/zip")
    handler._artifacts.add_verified(old, "application/zip")
    companies.current.artifacts += (SnapshotArtifact(old.content_sha256, "raw_filing"),)
    observations.resources.pop("S100Z2YO")
    context, _ = _resource_context(document_id="S100Z2YO", edinet_code="E03475",
                                   security_code="30280", observation_id=52)
    handler(context)
    assert client.calls == ["S100Z2YO"] and observations.lookups == []
    assert derived[0].snapshot.common_equity_required
    assert derived[0].snapshot.common_equity.value == 126359000000
    assert companies.calls[0]["source_accession"] == "S100Z2YO"
    assert {item.content_sha256 for item in companies.calls[0]["artifacts"]
            if item.role == "raw_filing"} == {hashlib.sha256(archives["S100Z2YO"]).hexdigest()}
    assert old.content_sha256 in handler._artifacts.rows


def test_container_replacement_keeps_older_report_but_drops_replaced_archive_input():
    handler, _, observations, companies, client, derived = _handler_case()
    _, archives, records = _actual_inputs()
    original = archives["S100Z2YO"]
    revised = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(revised, "w") as target:
        for info in source.infolist():
            target.writestr(info, source.read(info))
        target.comment = b"container custody revision; original member bytes unchanged"
    client.data = revised.getvalue()
    assert hashlib.sha256(client.data).digest() != hashlib.sha256(original).digest()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(io.BytesIO(client.data)) as target:
        assert {name: source.read(name) for name in source.namelist()} == {name: target.read(name) for name in target.namelist()}
    old = handler._object_store.put_verified(archives["S100WQEC"], "application/zip")
    handler._artifacts.add_verified(old, "application/zip")
    cached = (_INPUTS / "companyfacts_E03475.json").read_bytes()
    full = handler._object_store.put_verified(cached, "application/json")
    handler._artifacts.add_verified(full, "application/json")
    companies.current.artifacts = (
        SnapshotArtifact(full.content_sha256, "canonical_edinet_facts"),
        SnapshotArtifact(hashlib.sha256(original).hexdigest(), "raw_filing"),
        SnapshotArtifact(old.content_sha256, "raw_filing"))
    parent = observations.parents[51]
    observations._resource("S100WQEC", old.content_sha256, _archive_metadata(parent,
        document_id="S100WQEC", edinet_code="E03475", security_code="30280", observation_id=51))
    context, _ = _resource_context(document_id="S100Z2YO", edinet_code="E03475",
                                   security_code="30280", observation_id=52)
    handler(context)
    assert client.calls == ["S100Z2YO"]
    active = {item.content_sha256 for item in companies.calls[0]["artifacts"] if item.role == "raw_filing"}
    assert active == {old.content_sha256, hashlib.sha256(client.data).hexdigest()}
    assert observations.lookups == ["S100WQEC/xbrl"]
    assert derived[0].snapshot.common_equity_required
    assert hashlib.sha256(original).hexdigest() in handler._artifacts.rows


@pytest.mark.parametrize("fault", ["missing", "artifact", "withdrawn", "parent", "period"])
def test_legacy_replay_refuses_unbound_changed_or_withdrawn_original_source(fault):
    handler, context, observations, companies, client, derived = _handler_case()
    stored = observations.resources["S100Z2YO"]
    if fault == "missing":
        observations.resources.pop("S100Z2YO")
    elif fault == "artifact":
        stored.observation.artifact_sha256 = "9" * 64
    elif fault == "withdrawn":
        observations.parents[52].observation.state = "removed"
    elif fault == "parent":
        stored.item.parent_source_item_id = 999
    else:
        observations.parents[52].observation.canonical_metadata["row"]["periodEnd"] = "2025-06-30"
    with pytest.raises(ValueError):
        handler(context)
    assert not derived and not companies.calls
    assert client.calls == ["S100WQEC"]
