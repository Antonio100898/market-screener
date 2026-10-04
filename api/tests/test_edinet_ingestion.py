import json
import hashlib
import io
import threading
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from screener.artifacts import EvidenceArtifact
from screener.durable_jobs import DurableJobRecord, LeaseToken
from screener.edinet_ingestion import (
    EdinetListDiscoveryHandler,
    EdinetResourcePending,
    EdinetResourceFetchHandler,
)
from screener.job_runtime import JobContext
from screener.object_store import VerifiedObject
from screener.shared_companies import SnapshotArtifact
from screener.sources.edinet import (
    EdinetClient,
    EdinetError,
    EdinetHttpError,
    EdinetTransportResult,
)


NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)


def _row(document="S100TEST", **overrides):
    row = {
        "docID": document,
        "edinetCode": "E01772",
        "secCode": "67520",
        "docTypeCode": "120",
        "submitDateTime": "2026-09-29 10:30",
        "periodStart": "2025-04-01",
        "periodEnd": "2026-03-31",
        "opeDateTime": None,
        "parentDocID": None,
        "withdrawalStatus": "0",
        "docInfoEditStatus": "0",
        "disclosureStatus": "0",
        "legalStatus": "1",
        "xbrlFlag": "1",
    }
    row.update(overrides)
    return row


def _payload(*rows):
    rows = [
        {**row, "seqNumber": row.get("seqNumber", index)}
        for index, row in enumerate(rows, start=1)
    ]
    return json.dumps({
        "metadata": {
            "status": "200",
            "parameter": {"date": "2026-09-29", "type": "2"},
            "processDateTime": "2026-09-29 21:00",
            "resultset": {"count": len(rows)},
        },
        "results": rows,
    }).encode()


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.dates = []

    def fetch_documents_on(self, day):
        self.dates.append(day)
        return EdinetTransportResult(
            self.payload,
            f"https://api.edinet-fsa.go.jp/api/v2/documents.json?date={day}&type=2",
            "application/json",
            '"v1"',
            None,
        )


class FakeObjects:
    def put_verified(self, data, _media_type):
        return VerifiedObject("a" * 64, "raw/sha256/" + "a" * 64, len(data), NOW)


class FakeArtifacts:
    def add_verified(self, verified, media_type):
        return EvidenceArtifact(
            verified.content_sha256, verified.object_key, verified.byte_size,
            media_type, NOW, NOW,
        )


class FakeObservations:
    def __init__(self, prior=()):
        self.calls = []
        self.prior = tuple(prior)
        self.latest = {}

    def record(self, lease, **values):
        assert lease == LeaseToken(1, "worker", 1)
        self.calls.append(values)
        item_id = len(self.calls)
        stored = SimpleNamespace(
            item=SimpleNamespace(
                source_item_id=item_id,
                source_key=values["item"].source_key,
                issuer_source_identifier=values["item"].issuer_source_identifier,
            ),
            observation=SimpleNamespace(
                source_observation_id=item_id,
                observation_sha256=f"{item_id % 10}" * 64,
                artifact_sha256=values.get("artifact_sha256"),
                state=values["state"],
                canonical_metadata=values["metadata"],
            ),
        )
        if values["item"].item_kind == "filing":
            self.latest[values["item"].source_key] = stored
        return stored

    def record_edinet_list(
        self, lease, *, index_date, process_at, item_outcome_key,
        artifact_sha256, source_url, **_values,
    ):
        return self.record(
            lease,
            item=SimpleNamespace(
                source_system="EDINET", item_kind="aggregate",
                source_key=f"documents/{index_date}",
                issuer_source_identifier=None, parent_source_item_id=None,
            ),
            item_outcome_key=item_outcome_key,
            state="present",
            metadata={"index_date": index_date, "process_at": process_at.isoformat()},
            artifact_sha256=artifact_sha256,
            source_url=source_url,
        )

    def latest_edinet_filings(self, _day):
        return self.prior

    def latest_item(self, _system, _kind, key):
        return self.latest.get(key) or next(
            (item for item in self.prior if item.item.source_key == key), None
        )

    def latest_edinet_event(self, _key, _event_types=None):
        return None


class FakeJobs:
    def __init__(self):
        self.children = []

    def enqueue_child(self, lease, **values):
        assert lease == LeaseToken(1, "worker", 1)
        self.children.append(values)
        return object()


class FakeRepository:
    def __init__(self):
        self.checkpoints = []

    def save_checkpoint(self, _lease, checkpoint, *, now):
        assert now == NOW
        self.checkpoints.append(dict(checkpoint))
        return object()

    def record_item_outcome(self, *_args, **_kwargs):
        return object()


def _context():
    repository = FakeRepository()
    job = DurableJobRecord(
        1, 1, "edinet-list-discovery",
        {"start_date": "2026-09-29", "end_date": "2026-09-29"},
        "running", 20, NOW, 1, 3, None, None, "worker", None, 1, NOW,
        None, None, NOW, NOW, None,
    )
    return JobContext(
        job, threading.Event(), repository, LeaseToken(1, "worker", 1),
        lambda: NOW,
    ), repository


def _handler(payload, *, observations=None):
    observations = observations or FakeObservations()
    jobs = FakeJobs()
    return EdinetListDiscoveryHandler(
        client=FakeClient(payload),
        object_store=FakeObjects(),
        artifacts=FakeArtifacts(),
        observations=observations,
        jobs=jobs,
        clock=lambda: NOW,
    ), observations, jobs


def test_client_returns_exact_bytes_without_subscription_key():
    response = httpx.Response(
        200,
        content=b'{"metadata":{"status":"200"},"results":[]}',
        headers={"Content-Type": "application/json", "ETag": '"v1"'},
        request=httpx.Request(
            "GET",
            "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=2026-09-29&type=2&Subscription-Key=secret",
        ),
    )

    class Http:
        def get(self, *_args, **_kwargs):
            return response

    result = EdinetClient(api_key="secret", http=Http()).fetch_documents_on("2026-09-29")

    assert result.data == response.content
    assert "secret" not in result.url
    assert result.etag == '"v1"'


def test_client_returns_exact_archive_bytes_without_subscription_key():
    response = httpx.Response(
        200,
        content=b"PK\x03\x04archive",
        headers={"Content-Type": "application/zip", "ETag": '"zip-v1"'},
        request=httpx.Request(
            "GET",
            "https://api.edinet-fsa.go.jp/api/v2/documents/S100TEST?type=1&Subscription-Key=secret",
        ),
    )

    class Http:
        def get(self, *_args, **_kwargs):
            return response

    client = EdinetClient(api_key="secret", http=Http())
    result = client.fetch_xbrl_archive("S100TEST")

    assert result.data == response.content
    assert result.url.endswith("/documents/S100TEST?type=1")
    assert "secret" not in result.url
    assert result.etag == '"zip-v1"'
    assert client.xbrl_archive("S100TEST") == response.content


def test_discovery_records_annual_and_enqueues_future_fetch():
    handler, observations, jobs = _handler(_payload(_row()))
    context, repository = _context()

    checkpoint = handler(context)

    assert [call["item"].item_kind for call in observations.calls] == [
        "aggregate", "filing"
    ]
    assert observations.calls[1]["state"] == "present"
    assert observations.calls[1]["source_published_at"] == datetime(
        2026, 9, 29, 1, 30, tzinfo=timezone.utc
    )
    assert jobs.children[0]["kind"] == "edinet-resource-fetch"
    assert jobs.children[0]["parameters"]["document_id"] == "S100TEST"
    assert repository.checkpoints == [checkpoint]


def test_discovery_does_not_enqueue_an_invalid_security_code():
    handler, observations, jobs = _handler(_payload(_row(secCode="67521")))
    context, repository = _context()

    checkpoint = handler(context)

    assert observations.calls[1]["state"] == "present"
    assert jobs.children == []
    assert repository.checkpoints == [checkpoint]


def test_discovery_enqueues_an_alphanumeric_jpx_security_code():
    handler, _observations, jobs = _handler(_payload(_row(secCode="130A0")))
    context, _repository = _context()

    handler(context)

    assert jobs.children[0]["parameters"]["security_code"] == "130A0"


def test_lifecycle_events_and_targets_remain_distinct():
    prior = SimpleNamespace(
        item=SimpleNamespace(
            source_item_id=90, source_key="S100OLD1",
            issuer_source_identifier="E01772",
        ),
        observation=SimpleNamespace(
            state="present",
            canonical_metadata={"index_date": "2026-09-29", "row": _row("S100OLD1")},
        ),
    )
    disclosed = SimpleNamespace(
        item=SimpleNamespace(
            source_item_id=91, source_key="S100STOP",
            issuer_source_identifier="E01772",
        ),
        observation=SimpleNamespace(
            state="present",
            canonical_metadata={"index_date": "2026-09-29", "row": _row("S100STOP")},
        ),
    )
    rows = (
        _row("S100WITH", parentDocID="S100OLD1", withdrawalStatus="1"),
        _row("S100STOP", disclosureStatus="1", opeDateTime="2026-09-29 11:05"),
        _row("S100HIDE", disclosureStatus="2"),
        _row("S100EXPR", legalStatus="0"),
    )
    handler, observations, jobs = _handler(
        _payload(*rows), observations=FakeObservations((prior, disclosed))
    )
    context, _repository = _context()

    handler(context)

    states = [(call["item"].item_kind, call["state"], call["metadata"].get("reason"))
              for call in observations.calls]
    assert ("resource", "present", None) in states
    assert ("filing", "removed", "withdrawn") in states
    assert ("filing", "unavailable", "not_disclosed") in states
    assert ("filing", "unavailable", "viewing_period_expired") in states
    assert ("filing", "unavailable", "absent_from_reconciled_list") not in states
    assert jobs.children == []


def test_invalid_result_count_retains_no_checkpoint_or_filing():
    payload = json.loads(_payload(_row()))
    payload["metadata"]["resultset"]["count"] = 2
    handler, observations, jobs = _handler(json.dumps(payload).encode())
    context, repository = _context()

    with pytest.raises(ValueError, match="result count"):
        handler(context)

    assert [call["item"].item_kind for call in observations.calls] == ["aggregate"]
    assert jobs.children == []
    assert repository.checkpoints == []


def test_edit_and_disclosure_events_have_separate_stable_items():
    rows = (
        _row("S100EDIT", docInfoEditStatus="1", opeDateTime="2026-09-29 11:00"),
        _row("S100STOP", disclosureStatus="1", opeDateTime="2026-09-29 11:05"),
        _row("S100OPEN", disclosureStatus="3", opeDateTime="2026-09-29 11:10"),
    )
    handler, observations, jobs = _handler(_payload(*rows))
    context, _repository = _context()

    handler(context)

    assert {
        call["metadata"].get("event")
        for call in observations.calls
        if call["item"].item_kind == "resource"
    } == {"metadata_edit", "disclosure_stop", "disclosure_release"}
    assert jobs.children == []


def test_complete_reconciliation_marks_unexplained_absence_unavailable():
    prior = SimpleNamespace(
        item=SimpleNamespace(
            source_item_id=90, source_key="S100OLD1",
            issuer_source_identifier="E01772",
        ),
        observation=SimpleNamespace(
            state="present",
            canonical_metadata={"index_date": "2026-09-29", "row": _row("S100OLD1")},
        ),
    )
    handler, observations, _jobs = _handler(
        _payload(), observations=FakeObservations((prior,))
    )
    context, _repository = _context()

    handler(context)

    missing = observations.calls[-1]
    assert missing["state"] == "unavailable"
    assert missing["metadata"]["reason"] == "absent_from_reconciled_list"


def _archive_bytes(payload=b"placeholder"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL/PublicDoc/annual.xbrl", payload)
    return buffer.getvalue()


class ResourceClient:
    def __init__(self, data=None):
        self.data = _archive_bytes() if data is None else data
        self.calls = []

    def fetch_xbrl_archive(self, document_id):
        self.calls.append(document_id)
        return EdinetTransportResult(
            self.data,
            f"https://api.edinet-fsa.go.jp/api/v2/documents/{document_id}?type=1",
            "application/zip",
            '"zip-v1"',
            None,
        )


class ResourceObjects:
    def __init__(self):
        self.data = {}

    def put_verified(self, data, _media_type):
        digest = hashlib.sha256(data).hexdigest()
        self.data[digest] = data
        return VerifiedObject(digest, f"raw/sha256/{digest}", len(data), NOW)

    def read_verified(self, _key, digest, size):
        data = self.data[digest]
        assert len(data) == size
        return data


class ResourceArtifacts:
    def __init__(self):
        self.rows = {}

    def add_verified(self, verified, media_type):
        artifact = EvidenceArtifact(
            verified.content_sha256,
            verified.object_key,
            verified.byte_size,
            media_type,
            NOW,
            NOW,
        )
        self.rows[artifact.content_sha256] = artifact
        return artifact

    def get(self, digest):
        return self.rows[digest]


class ResourceObservations:
    def __init__(self, *, state="present"):
        self.calls = []
        self.archive = None
        self.parent = SimpleNamespace(
            item=SimpleNamespace(
                source_item_id=41,
                source_system="EDINET",
                item_kind="filing",
                source_key="S100TEST",
                issuer_source_identifier="E01772",
            ),
            observation=SimpleNamespace(
                source_observation_id=51,
                observation_sha256="5" * 64,
                state=state,
                canonical_metadata={
                    "index_date": "2026-09-29",
                    "row": _row(),
                },
            ),
        )

    def latest_edinet_filing(self, observation_id):
        assert observation_id == 51
        return self.parent

    def record(self, lease, **values):
        assert lease == LeaseToken(1, "worker", 1)
        self.calls.append(values)
        self.archive = SimpleNamespace(
            item=SimpleNamespace(
                source_item_id=61,
                source_system="EDINET",
                item_kind="resource",
                source_key="S100TEST/xbrl",
                issuer_source_identifier="E01772",
                parent_source_item_id=41,
            ),
            observation=SimpleNamespace(
                source_observation_id=71,
                observation_sha256="7" * 64,
                artifact_sha256=values.get("artifact_sha256"),
                state=values["state"],
                canonical_metadata=values["metadata"],
            ),
        )
        return self.archive

    def by_sha256(self, digest):
        assert digest == "7" * 64
        return self.archive


class ResourceCompanies:
    def __init__(self, current=None):
        self.calls = []
        self.current = current

    def current_by_ticker(self, _ticker):
        return self.current

    def store_candidate(self, lease, **values):
        assert lease == LeaseToken(1, "worker", 1)
        self.calls.append(values)
        return SimpleNamespace(snapshot_id=81, created=len(self.calls) == 1)


def _resource_context(checkpoint=None, **parameter_overrides):
    repository = FakeRepository()
    parameters = {
        "document_id": "S100TEST",
        "edinet_code": "E01772",
        "security_code": "67520",
        "observation_id": 51,
    }
    parameters.update(parameter_overrides)
    job = DurableJobRecord(
        1, 1, "edinet-resource-fetch", parameters,
        "running", 20, NOW, 1, 3, None, None, "worker", None, 1, NOW,
        checkpoint, None, NOW, NOW, None,
    )
    return JobContext(
        job, threading.Event(), repository, LeaseToken(1, "worker", 1),
        lambda: NOW,
    ), repository


def _mapped_facts(record, _archive, *, ticker):
    return {
        "cik": record["edinetCode"],
        "entityName": "Panasonic Holdings Corporation",
        "facts": {"canonical": {}},
        "_adapter": {
            "kind": "edinet_xbrl",
            "statement_basis": "canonical",
            "reporting_currency": "JPY",
            "quote_currency": "JPY",
            "ticker": ticker,
            "security_code": record["secCode"],
            "security_basis": "PRIMARY_ORDINARY_SHARE",
            "reports": [{"document": record["docID"], "published": "2026-09-29"}],
        },
    }


def _resource_handler(monkeypatch, *, observations=None, derive=None, client=None):
    monkeypatch.setattr(
        "screener.edinet_ingestion.build_edinet_companyfacts", _mapped_facts
    )
    observations = observations or ResourceObservations()
    companies = ResourceCompanies()
    objects = ResourceObjects()
    artifacts = ResourceArtifacts()
    client = client or ResourceClient()
    handler = EdinetResourceFetchHandler(
        client=client,
        object_store=objects,
        artifacts=artifacts,
        observations=observations,
        companies=companies,
        listings={"6752": {"name": "Panasonic Holdings Corporation"}},
        derive=derive or (lambda bundle: (
            "ok", {"ticker": bundle.ticker, "cik": bundle.cik}
        )),
        clock=lambda: NOW,
    )
    return handler, client, observations, companies, objects


def test_resource_fetch_retains_archive_and_stages_noncurrent_candidate(monkeypatch):
    handler, client, observations, companies, objects = _resource_handler(monkeypatch)
    context, repository = _resource_context()

    checkpoint = handler(context)

    assert client.calls == ["S100TEST"]
    assert observations.calls[0]["item"].parent_source_item_id == 41
    assert observations.calls[0]["metadata"]["role"] == "raw_filing"
    candidate = companies.calls[0]
    assert candidate["ticker"] == "6752.T"
    assert candidate["issuer_identifier"] == "E01772"
    assert candidate["security_identifier"] == "edinet:E01772:primary-ordinary"
    assert candidate["exchange_code"] == "TSE"
    assert candidate["quote_currency"] == "JPY"
    assert candidate["source_accession"] == "S100TEST"
    assert {item.role for item in candidate["artifacts"]} == {
        "raw_filing", "canonical_edinet_facts"
    }
    assert len(objects.data) == 2
    assert checkpoint["snapshot_id"] == 81
    assert repository.checkpoints[-1] == checkpoint
    assert len(repository.checkpoints) == 2


def test_resource_fetch_rejects_stale_or_changed_parent_before_network(monkeypatch):
    observations = ResourceObservations(state="unavailable")
    handler, client, _observations, companies, _objects = _resource_handler(
        monkeypatch, observations=observations
    )
    context, repository = _resource_context()

    with pytest.raises(ValueError, match="not present"):
        handler(context)

    assert client.calls == []
    assert companies.calls == []
    assert repository.checkpoints == []


def test_resource_fetch_mapping_or_derivation_failure_stages_nothing(monkeypatch):
    handler, _client, observations, companies, objects = _resource_handler(
        monkeypatch, derive=lambda _bundle: ("pending_facts", None)
    )
    context, repository = _resource_context()

    with pytest.raises(ValueError, match="pending_facts"):
        handler(context)

    assert len(observations.calls) == 1
    assert len(objects.data) == 2
    assert companies.calls == []
    assert len(repository.checkpoints) == 1
    assert "snapshot_id" not in repository.checkpoints[0]


def test_resource_fetch_completed_checkpoint_performs_no_io(monkeypatch):
    checkpoint = {
        "document_id": "S100TEST",
        "edinet_code": "E01772",
        "security_code": "67520",
        "filing_observation_id": 51,
        "archive_observation_sha256": "7" * 64,
        "canonical_facts_sha256": "8" * 64,
        "snapshot_id": 81,
        "snapshot_created": True,
    }
    handler, client, observations, companies, objects = _resource_handler(monkeypatch)
    context, repository = _resource_context(checkpoint)

    assert handler(context) == checkpoint
    assert client.calls == []
    assert observations.calls == []
    assert companies.calls == []
    assert objects.data == {}
    assert repository.checkpoints == []


def test_resource_fetch_partial_checkpoint_reads_verified_archive_without_network(
    monkeypatch,
):
    handler, client, observations, companies, objects = _resource_handler(monkeypatch)
    context, _repository = _resource_context()
    first = handler(context)
    partial = {key: value for key, value in first.items() if key not in {
        "canonical_facts_sha256", "snapshot_id", "snapshot_created"
    }}
    client.calls.clear()
    companies.calls.clear()
    context, repository = _resource_context(partial)

    checkpoint = handler(context)

    assert client.calls == []
    assert checkpoint["snapshot_id"] == 81
    assert len(companies.calls) == 1
    assert repository.checkpoints == [checkpoint]
    assert objects.data


def test_resource_fetch_404_is_deferred_without_candidate_or_checkpoint(monkeypatch):
    class MissingClient:
        calls = []

        def fetch_xbrl_archive(self, document_id):
            self.calls.append(document_id)
            raise EdinetHttpError(
                404,
                f"documents/{document_id}",
                f"https://api.edinet-fsa.go.jp/api/v2/documents/{document_id}?type=1",
            )

    client = MissingClient()
    handler, _client, observations, companies, _objects = _resource_handler(
        monkeypatch, client=client
    )
    context, repository = _resource_context()

    with pytest.raises(EdinetResourcePending):
        handler(context)

    assert observations.calls[0]["state"] == "pending"
    assert observations.calls[0]["metadata"]["reason"] == "not_found"
    assert companies.calls == []
    assert repository.checkpoints == []


def test_resource_fetch_rejects_unlisted_security_before_network(monkeypatch):
    handler, client, _observations, companies, _objects = _resource_handler(monkeypatch)
    handler._listings = {}
    context, repository = _resource_context()

    with pytest.raises(ValueError, match="current JPX listing"):
        handler(context)

    assert client.calls == []
    assert companies.calls == []
    assert repository.checkpoints == []


def test_resource_fetch_accepts_an_alphanumeric_jpx_security_code(monkeypatch):
    observations = ResourceObservations()
    observations.parent.observation.canonical_metadata["row"]["secCode"] = "130A0"
    handler, _client, _observations, companies, _objects = _resource_handler(
        monkeypatch, observations=observations
    )
    handler._listings = {"130A": {"name": "Listed Company"}}
    context, _repository = _resource_context(security_code="130A0")

    handler(context)

    assert companies.calls[0]["ticker"] == "130A.T"


def test_corrupt_archive_is_not_checkpointed_and_corrected_retry_refetches(
    monkeypatch,
):
    client = ResourceClient(b"not a zip")
    handler, _client, _observations, companies, _objects = _resource_handler(
        monkeypatch, client=client
    )
    context, repository = _resource_context()

    with pytest.raises(EdinetError, match="not an EDINET XBRL archive"):
        handler(context)
    assert repository.checkpoints == []
    assert companies.calls == []

    client.data = _archive_bytes()
    checkpoint = handler(context)

    assert checkpoint["snapshot_id"] == 81
    assert client.calls == ["S100TEST", "S100TEST"]


def test_resource_fetch_merges_current_canonical_history(monkeypatch):
    old_row = _row(
        "S100OLD1",
        submitDateTime="2025-06-20 10:30",
        periodStart="2024-04-01",
        periodEnd="2025-03-31",
    )
    previous = _mapped_facts(old_row, b"old", ticker="6752.T")
    previous["_adapter"]["reports"][0]["published"] = "2025-06-20"
    objects = ResourceObjects()
    artifacts = ResourceArtifacts()
    encoded = json.dumps(
        previous, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    verified = objects.put_verified(encoded, "application/json")
    artifacts.add_verified(verified, "application/json")
    current = SimpleNamespace(
        issuer_source="EDINET",
        issuer_identifier="E01772",
        security_identifier="edinet:E01772:primary-ordinary",
        artifacts=(
            SnapshotArtifact(verified.content_sha256, "canonical_edinet_facts"),
            SnapshotArtifact("9" * 64, "raw_filing"),
        ),
    )
    companies = ResourceCompanies(current)
    observations = ResourceObservations()
    monkeypatch.setattr(
        "screener.edinet_ingestion.build_edinet_companyfacts", _mapped_facts
    )
    seen = []
    handler = EdinetResourceFetchHandler(
        client=ResourceClient(),
        object_store=objects,
        artifacts=artifacts,
        observations=observations,
        companies=companies,
        listings={"6752": {"name": "Panasonic Holdings Corporation"}},
        derive=lambda bundle: (seen.append(bundle) or ("ok", {"ticker": bundle.ticker})),
        clock=lambda: NOW,
    )
    context, _repository = _resource_context()

    handler(context)

    reports = seen[0].facts["_adapter"]["reports"]
    assert [report["document"] for report in reports] == ["S100OLD1", "S100TEST"]
    assert {artifact.content_sha256 for artifact in companies.calls[0]["artifacts"]
            if artifact.role == "raw_filing"} >= {"9" * 64}


def test_resource_fetch_uses_real_edinet_mapper_before_derivation():
    xbrl = b"""<xbrli:xbrl
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:jpigp="http://disclosure.edinet-fsa.go.jp/taxonomy/jpigp/2025-11-01/jpigp_cor">
      <xbrli:context id="CurrentInstant"><xbrli:entity>
        <xbrli:identifier scheme="edinet">E01772</xbrli:identifier>
      </xbrli:entity><xbrli:period><xbrli:instant>2026-03-31</xbrli:instant>
      </xbrli:period></xbrli:context>
      <xbrli:unit id="JPY"><xbrli:measure>iso4217:JPY</xbrli:measure></xbrli:unit>
      <jpigp:AssetsIFRS contextRef="CurrentInstant" unitRef="JPY">1000</jpigp:AssetsIFRS>
    </xbrli:xbrl>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL/PublicDoc/annual.xbrl", xbrl)
    observations = ResourceObservations()
    companies = ResourceCompanies()
    seen = []
    handler = EdinetResourceFetchHandler(
        client=ResourceClient(buffer.getvalue()),
        object_store=ResourceObjects(),
        artifacts=ResourceArtifacts(),
        observations=observations,
        companies=companies,
        listings={"6752": {"name": "Panasonic Holdings Corporation"}},
        derive=lambda bundle: (
            seen.append(bundle) or ("ok", {"ticker": bundle.ticker})
        ),
        clock=lambda: NOW,
    )
    context, _repository = _resource_context()

    handler(context)

    assets = seen[0].facts["facts"]["canonical"]["Assets"]["units"]["JPY"][0]
    assert assets["val"] == "1000"
    assert assets["accn"] == "S100TEST"
    assert assets["_source_file"] == "XBRL/PublicDoc/annual.xbrl"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("document_id", "bad", "document_id"),
        ("edinet_code", "E1", "edinet_code"),
        ("security_code", "67521", "security_code"),
        ("observation_id", 0, "observation_id"),
    ],
)
def test_resource_fetch_rejects_invalid_job_identity(
    monkeypatch, field, value, message
):
    handler, client, _observations, companies, _objects = _resource_handler(monkeypatch)
    context, repository = _resource_context(**{field: value})

    with pytest.raises(ValueError, match=message):
        handler(context)

    assert client.calls == []
    assert companies.calls == []
    assert repository.checkpoints == []
