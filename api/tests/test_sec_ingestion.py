import hashlib
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
import threading

import httpx
import pytest

from screener.artifacts import EvidenceArtifact
from screener.durable_jobs import DurableJobRecord, LeaseLost, LeaseToken
from screener.job_runtime import JobContext
from screener.object_store import VerifiedObject
from screener.sec_ingestion import (
    SecCandidateStageHandler,
    SecDiscoveryStopped,
    SecQuarterlyReconciliationHandler,
    SecRecentDiscoveryHandler,
    SecResourceFetchHandler,
    SecResourcePending,
    parse_form_index,
)
from screener.source_observations import SourceItemIdentityConflict
from screener.sources.edgar import (
    EdgarClient,
    EdgarError,
    EdgarTransportResult,
    NoXbrlDataError,
)


HEADER = (
    "Form Type   Company Name                                                  "
    "CIK         Date Filed  File Name"
)
NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)


def _row(form, company, cik, filed, accession):
    return (
        f"{form:<12}{company:<62}{cik:<12}{filed:<12}"
        f"edgar/data/{int(cik)}/{accession}.txt"
    )


def _index(*rows):
    separator = (
        f"{'-' * 10}  {'-' * 60}  {'-' * 10}  {'-' * 10}  {'-' * 50}"
    )
    return (
        "Description: Daily Index of EDGAR Dissemination Feed\n\n"
        f"{HEADER}\n{separator}\n"
        + "\n".join(rows)
        + "\n"
    ).encode("ascii")


def test_parse_form_index_keeps_domestic_foreign_amendment_and_transition_forms():
    domestic = _row(
        "10-K",
        "DOMESTIC CORP",
        "1234",
        "2026-09-28",
        "0000001234-26-000001",
    )
    foreign = _row(
        "20-F/A",
        "FOREIGN PLC",
        "987654",
        "2026-09-28",
        "0000987654-26-000002",
    )
    transition = _row(
        "10-QT/A",
        "TRANSITION INC",
        "42",
        "2026-09-28",
        "0000000042-26-000003",
    )
    non_financial = _row(
        "8-K",
        "EVENT CO",
        "77",
        "2026-09-28",
        "0000000077-26-000004",
    )

    filings = parse_form_index(
        _index(domestic, foreign, transition, non_financial)
    )

    assert [(row.form, row.cik, row.accession) for row in filings] == [
        ("10-K", "0000001234", "0000001234-26-000001"),
        ("20-F/A", "0000987654", "0000987654-26-000002"),
        ("10-QT/A", "0000000042", "0000000042-26-000003"),
    ]
    assert filings[1].company_name == "FOREIGN PLC"
    assert filings[1].filing_date == date(2026, 9, 28)
    assert filings[1].source_row == foreign


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"Form|Company|CIK|Date|File\n", "header shape changed"),
        (
            f"{HEADER}\nnot-a-separator\n".encode(),
            "separator shape changed",
        ),
        (
            _index("10-K       too short"),
            "short data row",
        ),
        (
            _index(
                _row(
                    "8-K",
                    "BAD ARCHIVE",
                    "1234",
                    "2026-09-28",
                    "0000009999-26-000001",
                ).replace("edgar/data/1234/", "edgar/data/9999/")
            ),
            "invalid archive filename",
        ),
    ],
)
def test_parse_form_index_rejects_changed_or_malformed_shape(payload, message):
    with pytest.raises(ValueError, match=message):
        parse_form_index(payload)


def test_edgar_fetch_returns_exact_transport_result_through_existing_request_path(
    tmp_path,
):
    requested = "https://www.sec.gov/original"
    final = "https://www.sec.gov/final"
    response = httpx.Response(
        200,
        content=b"\x00exact\xff",
        headers={
            "Content-Type": "text/plain; charset=iso-8859-1",
            "ETag": '"index-v1"',
            "Last-Modified": "Mon, 28 Sep 2026 20:00:00 GMT",
        },
        request=httpx.Request("GET", final),
    )

    class FakeHttp:
        def get(self, url):
            assert url == requested
            return response

    client = EdgarClient(cache_dir=tmp_path)
    client._http = FakeHttp()

    result = client.fetch(requested)

    assert result.data == b"\x00exact\xff"
    assert result.url == final
    assert result.media_type == "text/plain; charset=iso-8859-1"
    assert result.etag == '"index-v1"'
    assert result.last_modified == "Mon, 28 Sep 2026 20:00:00 GMT"


class FakeEdgar:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


class FakeObjectStore:
    def put_verified(self, data, media_type):
        assert media_type == "text/plain"
        return VerifiedObject("a" * 64, f"raw/sha256/{'a' * 64}", len(data), NOW)


class FakeArtifacts:
    def add_verified(self, verified, media_type):
        return EvidenceArtifact(
            verified.content_sha256,
            verified.object_key,
            verified.byte_size,
            media_type,
            NOW,
            NOW,
        )


class FakeObservations:
    def __init__(self, events, latest=()):
        self.events = events
        self.calls = []
        self.identities = {}
        self.latest = tuple(latest)
        self.latest_quarters = []
        self.removals = []

    def record(self, lease, **values):
        assert lease == LeaseToken(1, "worker", 1)
        self.events.append(("observation", values["item_outcome_key"]))
        self.calls.append(values)
        identity = (
            values["item"].source_system,
            values["item"].item_kind,
            values["item"].source_key,
            values["state"],
            repr(sorted(values["metadata"].items())),
        )
        if identity not in self.identities:
            self.identities[identity] = (
                len(self.identities) + 1,
                chr(97 + len(self.identities)) * 64,
            )
        observation_id, digest = self.identities[identity]
        return SimpleNamespace(
            item=SimpleNamespace(source_item_id=observation_id),
            observation=SimpleNamespace(
                source_observation_id=observation_id,
                observation_sha256=digest,
                artifact_sha256=values.get("artifact_sha256"),
                detecting_job_id=1,
            )
        )

    def latest_sec_financial_filings(self, quarter):
        self.latest_quarters.append(quarter)
        return self.latest

    def record_witnessed_sec_removal(self, lease, **values):
        assert lease == LeaseToken(1, "worker", 1)
        self.events.append(("removal", values["item_outcome_key"]))
        self.removals.append(values)
        return object()


class FakeJobs:
    def __init__(self, events, fail_once=False):
        self.events = events
        self.children = []
        self.fail_once = fail_once

    def enqueue_child(self, lease, **values):
        assert lease == LeaseToken(1, "worker", 1)
        self.events.append(("child", values["child_key"]))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("interrupted")
        self.children.append(values)
        return object()


class FakeContextRepository:
    def __init__(self, events, stop_after_checkpoint=None):
        self.events = events
        self.checkpoints = []
        self.outcomes = []
        self.stop_after_checkpoint = stop_after_checkpoint

    def save_checkpoint(self, lease, checkpoint, *, now):
        assert lease == LeaseToken(1, "worker", 1)
        self.events.append(("checkpoint", dict(checkpoint)))
        self.checkpoints.append(dict(checkpoint))
        if self.stop_after_checkpoint is not None:
            self.stop_after_checkpoint.set()
        return object()

    def record_item_outcome(self, lease, **values):
        assert lease == LeaseToken(1, "worker", 1)
        self.events.append(("outcome", values["item_key"]))
        self.outcomes.append(values)
        return object()


def _job(parameters, *, checkpoint=None, kind="sec-recent-discovery"):
    return DurableJobRecord(
        job_id=1,
        occurrence_id=1,
        kind=kind,
        parameters=parameters,
        status="running",
        priority=30,
        due_at=NOW,
        attempts=1,
        max_attempts=3,
        next_retry_at=None,
        deadline_at=None,
        owner="worker",
        lease_expires_at=None,
        ownership_generation=1,
        heartbeat_at=NOW,
        checkpoint=checkpoint,
        error_summary=None,
        created_at=NOW,
        updated_at=NOW,
        finished_at=None,
    )


def _context(
    parameters,
    events,
    *,
    checkpoint=None,
    stopping=None,
    stop_after_checkpoint=False,
    kind="sec-recent-discovery",
):
    stopping = stopping or threading.Event()
    repository = FakeContextRepository(
        events,
        stopping if stop_after_checkpoint else None,
    )
    return (
        JobContext(
            job=_job(parameters, checkpoint=checkpoint, kind=kind),
            stopping=stopping,
            _repository=repository,
            _lease=LeaseToken(1, "worker", 1),
            _clock=lambda: NOW,
        ),
        repository,
    )


def _handler(edgar, events, *, observations=None, jobs=None):
    observations = observations or FakeObservations(events)
    jobs = jobs or FakeJobs(events)
    return (
        SecRecentDiscoveryHandler(
            edgar=edgar,
            object_store=FakeObjectStore(),
            artifacts=FakeArtifacts(),
            observations=observations,
            jobs=jobs,
            clock=lambda: NOW,
        ),
        observations,
        jobs,
    )


def _url(day):
    return (
        "https://www.sec.gov/Archives/edgar/daily-index/"
        f"{day.year}/QTR{(day.month - 1) // 3 + 1}/form.{day:%Y%m%d}.idx"
    )


def test_discovery_records_index_filings_stable_children_then_checkpoint():
    day = date(2026, 9, 28)
    payload = _index(
        _row("10-K", "DOMESTIC CORP", "1234", day.isoformat(), "0000001234-26-000001"),
        _row("20-F/A", "FOREIGN PLC", "987654", day.isoformat(), "0000987654-26-000002"),
    )
    result = EdgarTransportResult(
        payload,
        _url(day),
        "text/plain",
        '"v1"',
        "Mon, 28 Sep 2026 20:00:00 GMT",
    )
    events = []
    context, context_repository = _context(
        {"start_date": day.isoformat(), "end_date": day.isoformat()}, events
    )
    handler, observations, jobs = _handler(
        FakeEdgar({_url(day): result}), events
    )

    checkpoint = handler(context)

    assert checkpoint["through_date"] == day.isoformat()
    assert [call["item"].item_kind for call in observations.calls] == [
        "inventory",
        "filing",
        "filing",
    ]
    assert observations.calls[1]["item"].parent_source_item_id is None
    assert observations.calls[2]["metadata"]["source_row"].startswith("20-F/A")
    assert observations.calls[2]["metadata"]["quarter"] == "2026Q3"
    assert len(jobs.children) == 2
    assert jobs.children[0]["parameters"] == {
        "accession": "0000001234-26-000001",
        "cik": "0000001234",
        "form": "10-K",
        "filename": "edgar/data/1234/0000001234-26-000001.txt",
        "observation_id": 2,
    }
    assert jobs.children[0]["priority"] == 30
    assert jobs.children[0]["child_key"].endswith("b" * 64)
    assert events[-1] == (
        "checkpoint",
        {
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
            "through_date": day.isoformat(),
        },
    )
    assert len(context_repository.checkpoints) == 1


def test_discovery_404_is_durable_unavailable_but_transient_error_retries():
    day = date(2026, 9, 27)
    parameters = {"start_date": day.isoformat(), "end_date": day.isoformat()}
    events = []
    context, repository = _context(parameters, events)
    handler, observations, jobs = _handler(
        FakeEdgar({_url(day): NoXbrlDataError("not found")}), events
    )

    assert handler(context)["through_date"] == day.isoformat()
    assert observations.calls[0]["state"] == "unavailable"
    assert observations.calls[0]["metadata"]["reason"] == "not_found"
    assert jobs.children == []
    assert len(repository.checkpoints) == 1

    transient_events = []
    transient_context, transient_repository = _context(parameters, transient_events)
    transient, transient_observations, _ = _handler(
        FakeEdgar({_url(day): EdgarError("timeout")}), transient_events
    )
    with pytest.raises(EdgarError, match="timeout"):
        transient(transient_context)
    assert transient_observations.calls == []
    assert transient_repository.checkpoints == []


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({}, "requires only"),
        (
            {"start_date": "2026-09-29", "end_date": "2026-09-28"},
            "must not be before",
        ),
        (
            {"start_date": "2026-09-01", "end_date": "2026-10-02"},
            "at most 31",
        ),
        (
            {"start_date": "09/29/2026", "end_date": "2026-09-29"},
            "ISO date",
        ),
    ],
)
def test_discovery_validates_explicit_bounded_range(parameters, message):
    events = []
    context, repository = _context(parameters, events)
    handler, observations, jobs = _handler(FakeEdgar({}), events)

    with pytest.raises(ValueError, match=message):
        handler(context)

    assert observations.calls == []
    assert jobs.children == []
    assert repository.checkpoints == []


def test_interruption_after_observation_recovers_with_same_child_identity():
    day = date(2026, 9, 28)
    payload = _index(
        _row("40-F", "CANADIAN CO", "1234", day.isoformat(), "0000001234-26-000001")
    )
    result = EdgarTransportResult(payload, _url(day), "text/plain", None, None)
    events = []
    observations = FakeObservations(events)
    jobs = FakeJobs(events, fail_once=True)
    handler, _, _ = _handler(
        FakeEdgar({_url(day): result}),
        events,
        observations=observations,
        jobs=jobs,
    )
    parameters = {"start_date": day.isoformat(), "end_date": day.isoformat()}
    first, first_repository = _context(parameters, events)

    with pytest.raises(RuntimeError, match="interrupted"):
        handler(first)
    assert first_repository.checkpoints == []

    second, second_repository = _context(parameters, events)
    assert handler(second)["through_date"] == day.isoformat()
    assert len(jobs.children) == 1
    assert jobs.children[0]["child_key"].endswith("b" * 64)
    assert len(second_repository.checkpoints) == 1


def test_stop_after_completed_day_retries_without_fetching_next_day():
    first = date(2026, 9, 28)
    second = date(2026, 9, 29)
    responses = {
        _url(first): EdgarTransportResult(
            _index(), _url(first), "text/plain", None, None
        ),
        _url(second): EdgarTransportResult(
            _index(), _url(second), "text/plain", None, None
        ),
    }
    edgar = FakeEdgar(responses)
    events = []
    context, repository = _context(
        {"start_date": first.isoformat(), "end_date": second.isoformat()},
        events,
        stop_after_checkpoint=True,
    )
    handler, _observations, _jobs = _handler(edgar, events)

    with pytest.raises(SecDiscoveryStopped, match="before the next date"):
        handler(context)

    assert edgar.urls == [_url(first)]
    assert repository.checkpoints == [
        {
            "start_date": first.isoformat(),
            "end_date": second.isoformat(),
            "through_date": first.isoformat(),
        }
    ]


def _quarterly_url(year=2026, quarter=3):
    return (
        "https://www.sec.gov/Archives/edgar/full-index/"
        f"{year}/QTR{quarter}/form.idx"
    )


def _prior(
    accession,
    *,
    company="OLD COMPANY",
    state="present",
    quarter="2026Q3",
    observation_id=100,
    detecting_job_id=99,
):
    cik = accession[:10]
    return SimpleNamespace(
        source_observation_id=observation_id,
        source_item_id=observation_id,
        observation_sha256=f"{observation_id % 10}" * 64,
        state=state,
        canonical_metadata={
            "form": "10-K",
            "company_name": company,
            "cik": cik,
            "filing_date": "2026-09-28",
            "archive_filename": f"edgar/data/{int(cik)}/{accession}.txt",
            "accession": accession,
            "quarter": quarter,
        },
        artifact_sha256="f" * 64 if state == "present" else None,
        detecting_job_id=detecting_job_id,
    )


def _quarterly_handler(edgar, events, *, observations=None, jobs=None):
    observations = observations or FakeObservations(events)
    jobs = jobs or FakeJobs(events)
    return (
        SecQuarterlyReconciliationHandler(
            edgar=edgar,
            object_store=FakeObjectStore(),
            artifacts=FakeArtifacts(),
            observations=observations,
            jobs=jobs,
            clock=lambda: NOW,
        ),
        observations,
        jobs,
    )


def test_quarterly_reconciliation_handles_every_latest_state_then_checkpoints():
    unchanged = "0000000001-26-000001"
    changed = "0000000002-26-000002"
    removed = "0000000003-26-000003"
    already_removed = "0000000004-26-000004"
    reappeared = "0000000005-26-000005"
    new = "0000000006-26-000006"
    payload = _index(
        _row("10-K", "OLD COMPANY", "1", "2026-09-28", unchanged),
        _row("10-K", "CHANGED COMPANY", "2", "2026-09-28", changed),
        _row("10-K", "RETURNED COMPANY", "5", "2026-09-28", reappeared),
        _row("10-K", "NEW COMPANY", "6", "2026-09-28", new),
    )
    result = EdgarTransportResult(
        payload, _quarterly_url(), "text/plain", '"quarter"', None
    )
    events = []
    latest = (
        _prior(unchanged, observation_id=101),
        _prior(changed, observation_id=102),
        _prior(removed, observation_id=103),
        _prior(already_removed, state="removed", observation_id=104),
        _prior(reappeared, state="removed", observation_id=105),
    )
    observations = FakeObservations(events, latest)
    handler, _, jobs = _quarterly_handler(
        FakeEdgar({_quarterly_url(): result}),
        events,
        observations=observations,
    )
    context, repository = _context(
        {"year": 2026, "quarter": 3}, events, kind="sec-index-reconciliation"
    )

    checkpoint = handler(context)

    assert observations.latest_quarters == ["2026Q3"]
    filing_calls = [
        call for call in observations.calls if call["item"].item_kind == "filing"
    ]
    assert [call["item"].source_key for call in filing_calls] == [
        changed,
        reappeared,
        new,
    ]
    assert [call["item"].issuer_source_identifier for call in filing_calls] == [
        "0000000002",
        "0000000005",
        "0000000006",
    ]
    assert [child["parameters"]["accession"] for child in jobs.children] == [
        changed,
        reappeared,
        new,
    ]
    assert [call["prior_observation_id"] for call in observations.removals] == [103]
    assert {item["outcome"]["action"] for item in repository.outcomes} == {
        "unchanged",
        "already_removed",
    }
    assert checkpoint == {
        "year": 2026,
        "quarter": 3,
        "inventory_observation_id": 1,
        "inventory_observation_sha256": "a" * 64,
    }
    assert repository.checkpoints == [checkpoint]


def test_quarterly_changed_cik_uses_rebuilt_identity_and_fails_closed():
    accession = "0000000001-26-000001"
    payload = _index(
        _row("10-K", "MOVED COMPANY", "2", "2026-09-28", accession)
    )
    response = EdgarTransportResult(
        payload, _quarterly_url(), "text/plain", None, None
    )
    events = []

    class IdentityGuard(FakeObservations):
        def record(self, lease, **values):
            if values["item"].item_kind == "filing":
                assert values["item"].issuer_source_identifier == "0000000002"
                raise SourceItemIdentityConflict("different immutable content")
            return super().record(lease, **values)

    observations = IdentityGuard(
        events, (_prior(accession, observation_id=101),)
    )
    handler, _, jobs = _quarterly_handler(
        FakeEdgar({_quarterly_url(): response}),
        events,
        observations=observations,
    )
    context, repository = _context(
        {"year": 2026, "quarter": 3}, events, kind="sec-index-reconciliation"
    )

    with pytest.raises(SourceItemIdentityConflict, match="immutable"):
        handler(context)

    assert jobs.children == []
    assert repository.checkpoints == []


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({}, "requires only"),
        ({"year": "2026", "quarter": 3}, "four-digit integer"),
        ({"year": 2026, "quarter": True}, "integer from 1 through 4"),
        ({"year": 2026, "quarter": 5}, "integer from 1 through 4"),
    ],
)
def test_quarterly_reconciliation_validates_strict_parameters(parameters, message):
    events = []
    context, repository = _context(
        parameters, events, kind="sec-index-reconciliation"
    )
    handler, observations, jobs = _quarterly_handler(FakeEdgar({}), events)

    with pytest.raises(ValueError, match=message):
        handler(context)

    assert observations.calls == []
    assert jobs.children == []
    assert repository.checkpoints == []


def test_quarterly_404_is_recorded_but_raised_without_checkpoint_or_removal():
    events = []
    url = _quarterly_url()
    context, repository = _context(
        {"year": 2026, "quarter": 3}, events, kind="sec-index-reconciliation"
    )
    handler, observations, jobs = _quarterly_handler(
        FakeEdgar({url: NoXbrlDataError("not found")}), events
    )

    with pytest.raises(NoXbrlDataError, match="not found"):
        handler(context)

    assert observations.calls[0]["state"] == "unavailable"
    assert observations.calls[0]["metadata"] == {
        "quarter": "2026Q3",
        "reason": "not_found",
    }
    assert observations.removals == []
    assert jobs.children == []
    assert repository.checkpoints == []


@pytest.mark.parametrize(
    "response",
    [
        EdgarError("timeout"),
        EdgarTransportResult(b"partial", _quarterly_url(), "text/plain", None, None),
    ],
)
def test_quarterly_transport_or_parse_failure_cannot_remove_or_checkpoint(response):
    events = []
    context, repository = _context(
        {"year": 2026, "quarter": 3}, events, kind="sec-index-reconciliation"
    )
    observations = FakeObservations(
        events, (_prior("0000000003-26-000003", observation_id=103),)
    )
    handler, _, jobs = _quarterly_handler(
        FakeEdgar({_quarterly_url(): response}),
        events,
        observations=observations,
    )

    with pytest.raises((EdgarError, ValueError)):
        handler(context)

    assert observations.calls == []
    assert observations.removals == []
    assert observations.latest_quarters == []
    assert jobs.children == []
    assert repository.checkpoints == []


def test_quarterly_stop_and_completed_checkpoint_do_no_new_work():
    parameters = {"year": 2026, "quarter": 3}
    events = []
    stopping = threading.Event()
    stopping.set()
    context, repository = _context(
        parameters,
        events,
        stopping=stopping,
        kind="sec-index-reconciliation",
    )
    edgar = FakeEdgar({})
    handler, observations, jobs = _quarterly_handler(edgar, events)

    with pytest.raises(SecDiscoveryStopped, match="before new work"):
        handler(context)
    assert edgar.urls == []
    assert observations.calls == []
    assert jobs.children == []
    assert repository.checkpoints == []

    checkpoint = {
        "year": 2026,
        "quarter": 3,
        "inventory_observation_id": 9,
        "inventory_observation_sha256": "9" * 64,
    }
    resumed, resumed_repository = _context(
        parameters,
        events,
        checkpoint=checkpoint,
        kind="sec-index-reconciliation",
    )
    assert handler(resumed) == checkpoint
    assert resumed_repository.checkpoints == []
    assert edgar.urls == []


def test_quarterly_retry_keeps_child_identity_and_waits_to_checkpoint():
    accession = "0000000006-26-000006"
    payload = _index(
        _row("10-K", "NEW COMPANY", "6", "2026-09-28", accession)
    )
    response = EdgarTransportResult(
        payload, _quarterly_url(), "text/plain", None, None
    )
    events = []
    observations = FakeObservations(events)
    jobs = FakeJobs(events, fail_once=True)
    handler, _, _ = _quarterly_handler(
        FakeEdgar({_quarterly_url(): response}),
        events,
        observations=observations,
        jobs=jobs,
    )
    parameters = {"year": 2026, "quarter": 3}
    first, first_repository = _context(
        parameters, events, kind="sec-index-reconciliation"
    )

    with pytest.raises(RuntimeError, match="interrupted"):
        handler(first)
    assert first_repository.checkpoints == []
    failed_child_key = next(value for kind, value in events if kind == "child")

    second, second_repository = _context(
        parameters, events, kind="sec-index-reconciliation"
    )
    handler(second)
    assert jobs.children[0]["child_key"] == failed_child_key
    assert len(second_repository.checkpoints) == 1


ROOT_ACCESSION = "0000001234-26-000001"
ROOT_CIK = "0000001234"
ROOT_FORM = "10-K"
ROOT_FILENAME = "edgar/data/1234/0000001234-26-000001.txt"
ROOT_PARAMETERS = {
    "accession": ROOT_ACCESSION,
    "cik": ROOT_CIK,
    "form": ROOT_FORM,
    "filename": ROOT_FILENAME,
    "observation_id": 42,
}


def _root_parent(**overrides):
    metadata = {
        "accession": ROOT_ACCESSION,
        "cik": ROOT_CIK,
        "form": ROOT_FORM,
        "archive_filename": ROOT_FILENAME,
        "filing_date": "2026-09-28",
    }
    metadata.update(overrides.pop("metadata", {}))
    item_values = {
        "source_item_id": 41,
        "source_system": "SEC",
        "item_kind": "filing",
        "source_key": ROOT_ACCESSION,
        "issuer_source_identifier": ROOT_CIK,
        "parent_source_item_id": None,
    }
    item_values.update(overrides.pop("item", {}))
    observation_values = {
        "source_observation_id": 42,
        "observation_sha256": "f" * 64,
        "state": "present",
        "canonical_metadata": metadata,
    }
    observation_values.update(overrides.pop("observation", {}))
    assert not overrides
    return SimpleNamespace(
        item=SimpleNamespace(**item_values),
        observation=SimpleNamespace(**observation_values),
    )


def _root_urls():
    return {
        "complete_submission": (
            "https://www.sec.gov/Archives/"
            "edgar/data/1234/0000001234-26-000001.txt"
        ),
        "accession_inventory": (
            "https://www.sec.gov/Archives/edgar/data/1234/"
            "000000123426000001/index.json"
        ),
        "submissions": (
            "https://data.sec.gov/submissions/CIK0000001234.json"
        ),
        "company_facts": (
            "https://data.sec.gov/api/xbrl/companyfacts/"
            "CIK0000001234.json"
        ),
    }


def _root_payloads(*, fact_value=1):
    return {
        "complete_submission": b"<SEC-DOCUMENT>exact filing bytes</SEC-DOCUMENT>",
        "accession_inventory": json.dumps(
            {
                "directory": {
                    "name": (
                        "/Archives/edgar/data/1234/000000123426000001"
                    ),
                    "item": [{"name": "annual.htm"}],
                }
            }
        ).encode(),
        "submissions": json.dumps(
            {
                "cik": "1234",
                "filings": {
                    "recent": {
                        "accessionNumber": [ROOT_ACCESSION],
                        "form": [ROOT_FORM],
                    }
                },
            }
        ).encode(),
        "company_facts": json.dumps(
            {
                "cik": 1234,
                "facts": {
                    "us-gaap": {
                        "Assets": {
                            "units": {
                                "USD": [
                                    {
                                        "accn": ROOT_ACCESSION,
                                        "form": ROOT_FORM,
                                        "val": fact_value,
                                    }
                                ]
                            }
                        }
                    }
                },
            }
        ).encode(),
    }


def _root_responses(*, fact_value=1):
    payloads = _root_payloads(fact_value=fact_value)
    media_types = {
        "complete_submission": "text/plain",
        "accession_inventory": "application/json",
        "submissions": "application/json; charset=utf-8",
        "company_facts": "application/json",
    }
    return {
        url: EdgarTransportResult(
            payloads[role],
            url,
            media_types[role],
            f'"{role}-{fact_value if role == "company_facts" else 1}"',
            "Tue, 29 Sep 2026 12:00:00 GMT",
        )
        for role, url in _root_urls().items()
    }


class RootObjectStore:
    def __init__(self):
        self.puts = []

    def put_verified(self, data, media_type):
        digest = hashlib.sha256(data).hexdigest()
        self.puts.append((bytes(data), media_type, digest))
        return VerifiedObject(
            digest,
            f"raw/sha256/{digest}",
            len(data),
            NOW,
        )


class RootArtifacts:
    def __init__(self):
        self.calls = []

    def add_verified(self, verified, media_type):
        self.calls.append((verified, media_type))
        return EvidenceArtifact(
            verified.content_sha256,
            verified.object_key,
            verified.byte_size,
            media_type,
            NOW,
            NOW,
        )


class RootObservations:
    def __init__(self, parent=None, parent_error=None):
        self.parent = parent or _root_parent()
        self.parent_error = parent_error
        self.parent_ids = []
        self.calls = []
        self.item_ids = {}
        self.observation_ids = {}

    def latest_sec_filing(self, observation_id):
        self.parent_ids.append(observation_id)
        if self.parent_error is not None:
            raise self.parent_error
        return self.parent

    def record(self, lease, **values):
        assert lease == LeaseToken(1, "worker", 1)
        self.calls.append(values)
        item = values["item"]
        item_key = (
            item.source_system,
            item.item_kind,
            item.source_key,
            item.issuer_source_identifier,
            item.parent_source_item_id,
        )
        item_id = self.item_ids.setdefault(item_key, 100 + len(self.item_ids))
        observation_key = (
            item_key,
            values["state"],
            values.get("artifact_sha256"),
            values["source_url"],
            values.get("etag"),
            values.get("last_modified"),
            json.dumps(values["metadata"], sort_keys=True),
        )
        if observation_key not in self.observation_ids:
            digest = hashlib.sha256(repr(observation_key).encode()).hexdigest()
            self.observation_ids[observation_key] = (
                200 + len(self.observation_ids),
                digest,
            )
        observation_id, digest = self.observation_ids[observation_key]
        return SimpleNamespace(
            item=SimpleNamespace(source_item_id=item_id),
            observation=SimpleNamespace(
                source_observation_id=observation_id,
                observation_sha256=digest,
            ),
        )


def _root_handler(
    edgar, *, observations=None, object_store=None, artifacts=None, jobs=None
):
    observations = observations or RootObservations()
    object_store = object_store or RootObjectStore()
    artifacts = artifacts or RootArtifacts()
    jobs = jobs or FakeJobs([])
    return (
        SecResourceFetchHandler(
            edgar=edgar,
            object_store=object_store,
            artifacts=artifacts,
            observations=observations,
            jobs=jobs,
            clock=lambda: NOW,
        ),
        observations,
        object_store,
        artifacts,
    )


def _root_context(*, parameters=None, stopping=None, checkpoint=None):
    events = []
    context, repository = _context(
        ROOT_PARAMETERS if parameters is None else parameters,
        events,
        stopping=stopping,
        checkpoint=checkpoint,
        kind="sec-resource-fetch",
    )
    return context, repository, events


def test_resource_fetch_uses_exact_urls_roles_parentage_and_transport_metadata():
    urls = _root_urls()
    responses = _root_responses()
    submission = responses[urls["complete_submission"]]
    final_submission_url = "https://www.sec.gov/Archives/final-submission.txt"
    responses[urls["complete_submission"]] = EdgarTransportResult(
        submission.data,
        final_submission_url,
        submission.media_type,
        submission.etag,
        submission.last_modified,
    )
    edgar = FakeEdgar(responses)
    jobs = FakeJobs([])
    handler, observations, object_store, artifacts = _root_handler(
        edgar, jobs=jobs
    )
    context, repository, events = _root_context()

    checkpoint = handler(context)

    assert edgar.urls == list(urls.values())
    assert [call["metadata"]["role"] for call in observations.calls] == list(urls)
    assert [call["item"].item_kind for call in observations.calls] == [
        "resource",
        "resource",
        "aggregate",
        "aggregate",
    ]
    assert [call["item"].source_key for call in observations.calls] == [
        f"{ROOT_ACCESSION}/complete-submission",
        f"{ROOT_ACCESSION}/index",
        f"submissions/{ROOT_CIK}",
        f"companyfacts/{ROOT_CIK}",
    ]
    assert [call["item"].parent_source_item_id for call in observations.calls] == [
        41,
        41,
        None,
        None,
    ]
    assert all(call["item"].issuer_source_identifier == ROOT_CIK for call in observations.calls)
    assert all(
        call["metadata"]["parent_source_item_id"] == 41
        for call in observations.calls[:2]
    )
    assert all(
        call["metadata"]["parent_observation_id"] == 42
        for call in observations.calls[:2]
    )
    assert [call["metadata"] for call in observations.calls[2:]] == [
        {"role": "submissions", "cik": ROOT_CIK},
        {"role": "company_facts", "cik": ROOT_CIK},
    ]
    assert [call["source_url"] for call in observations.calls] == [
        final_submission_url,
        *list(urls.values())[1:],
    ]
    assert all(call["etag"] and call["last_modified"] for call in observations.calls)
    assert [media_type for _verified, media_type in artifacts.calls] == [
        "text/plain",
        "application/json",
        "application/json; charset=utf-8",
        "application/json",
    ]
    assert [data for data, _media_type, _digest in object_store.puts] == list(
        _root_payloads().values()
    )
    assert set(checkpoint["roots"]) == set(urls)
    assert jobs.children[0]["kind"] == "sec-candidate-stage"
    assert jobs.children[0]["parameters"]["roots"] == checkpoint["roots"]
    assert repository.checkpoints == [checkpoint]
    assert events[-1] == ("checkpoint", checkpoint)


def test_issuer_aggregate_revision_is_reused_across_filing_jobs():
    second_accession = "0000001234-26-000002"
    second_filename = "edgar/data/1234/0000001234-26-000002.txt"
    aggregate_payloads = {
        "submissions": json.dumps(
            {
                "cik": "1234",
                "filings": {
                    "recent": {
                        "accessionNumber": [ROOT_ACCESSION, second_accession],
                        "form": [ROOT_FORM, ROOT_FORM],
                    }
                },
            }
        ).encode(),
        "company_facts": json.dumps(
            {
                "cik": 1234,
                "facts": {
                    "us-gaap": {
                        "Assets": {
                            "units": {
                                "USD": [
                                    {"accn": ROOT_ACCESSION, "form": ROOT_FORM},
                                    {"accn": second_accession, "form": ROOT_FORM},
                                ]
                            }
                        }
                    }
                },
            }
        ).encode(),
    }

    def responses(accession, filename):
        urls = {
            "complete_submission": f"https://www.sec.gov/Archives/{filename}",
            "accession_inventory": (
                "https://www.sec.gov/Archives/edgar/data/1234/"
                f"{accession.replace('-', '')}/index.json"
            ),
            **{
                role: _root_urls()[role]
                for role in ("submissions", "company_facts")
            },
        }
        payloads = {
            "complete_submission": f"filing:{accession}".encode(),
            "accession_inventory": json.dumps(
                {
                    "directory": {
                        "name": (
                            "/Archives/edgar/data/1234/"
                            f"{accession.replace('-', '')}"
                        ),
                        "item": [{"name": "annual.htm"}],
                    }
                }
            ).encode(),
            **aggregate_payloads,
        }
        return {
            url: EdgarTransportResult(
                payloads[role],
                url,
                "application/json",
                f'"{role}"',
                None,
            )
            for role, url in urls.items()
        }

    observations = RootObservations()
    first_handler, _, _, _ = _root_handler(
        FakeEdgar(responses(ROOT_ACCESSION, ROOT_FILENAME)),
        observations=observations,
    )
    first_context, _repository, _events = _root_context()
    first_handler(first_context)

    observations.parent = _root_parent(
        item={"source_item_id": 51, "source_key": second_accession},
        observation={
            "source_observation_id": 52,
            "observation_sha256": "e" * 64,
        },
        metadata={
            "accession": second_accession,
            "archive_filename": second_filename,
        },
    )
    second_handler, _, _, _ = _root_handler(
        FakeEdgar(responses(second_accession, second_filename)),
        observations=observations,
    )
    second_context, _repository, _events = _root_context(
        parameters={
            **ROOT_PARAMETERS,
            "accession": second_accession,
            "filename": second_filename,
            "observation_id": 52,
        }
    )
    second_handler(second_context)

    assert len(observations.item_ids) == 6
    assert len(observations.observation_ids) == 6
    aggregate_calls = [
        call for call in observations.calls if call["item"].item_kind == "aggregate"
    ]
    assert [call["metadata"] for call in aggregate_calls] == [
        {"role": "submissions", "cik": ROOT_CIK},
        {"role": "company_facts", "cik": ROOT_CIK},
    ] * 2


def test_historical_submissions_shard_can_cover_accession_absent_from_recent():
    urls = _root_urls()
    responses = _root_responses()
    historical = json.dumps(
        {
            "cik": "1234",
            "filings": {
                "recent": {
                    "accessionNumber": ["0000001234-26-999999"],
                    "form": [ROOT_FORM],
                },
                "files": [
                    {
                        "name": "CIK0000001234-submissions-001.json",
                        "filingCount": 1000,
                        "filingFrom": "2020-01-01",
                        "filingTo": "2026-09-28",
                    }
                ],
            },
        }
    ).encode()
    current = responses[urls["submissions"]]
    responses[urls["submissions"]] = EdgarTransportResult(
        historical,
        current.url,
        current.media_type,
        current.etag,
        current.last_modified,
    )
    handler, _observations, _objects, _artifacts = _root_handler(
        FakeEdgar(responses)
    )
    context, repository, _events = _root_context()

    checkpoint = handler(context)

    assert "submissions" in checkpoint["roots"]
    assert repository.checkpoints == [checkpoint]


def test_historical_submissions_rejects_two_covering_shards():
    urls = _root_urls()
    responses = _root_responses()
    ambiguous = json.dumps(
        {
            "cik": "1234",
            "filings": {
                "recent": {"accessionNumber": [], "form": []},
                "files": [
                    {
                        "name": f"CIK{ROOT_CIK}-submissions-001.json",
                        "filingCount": 1000,
                        "filingFrom": "2020-01-01",
                        "filingTo": "2026-09-28",
                    },
                    {
                        "name": f"CIK{ROOT_CIK}-submissions-002.json",
                        "filingCount": 1000,
                        "filingFrom": "2026-09-28",
                        "filingTo": "2026-09-29",
                    },
                ],
            },
        }
    ).encode()
    current = responses[urls["submissions"]]
    responses[urls["submissions"]] = EdgarTransportResult(
        ambiguous,
        current.url,
        current.media_type,
        current.etag,
        current.last_modified,
    )
    handler, observations, _objects, _artifacts = _root_handler(
        FakeEdgar(responses)
    )
    context, repository, _events = _root_context()

    with pytest.raises(ValueError, match="historical ranges are ambiguous"):
        handler(context)

    submissions = [
        call
        for call in observations.calls
        if call["metadata"]["role"] == "submissions"
    ]
    assert len(submissions) == 1
    assert submissions[0]["state"] == "present"
    assert repository.checkpoints == []


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({}, "requires only"),
        ({**ROOT_PARAMETERS, "extra": True}, "requires only"),
        ({**ROOT_PARAMETERS, "accession": "bad"}, "accession format"),
        ({**ROOT_PARAMETERS, "cik": "1234"}, "zero-padded"),
        ({**ROOT_PARAMETERS, "cik": "0000000000"}, "positive"),
        ({**ROOT_PARAMETERS, "form": "8-K"}, "supported"),
        ({**ROOT_PARAMETERS, "form": "10-K-WRONG"}, "supported"),
        ({**ROOT_PARAMETERS, "filename": "edgar/data/1234/wrong.txt"}, "match"),
        ({**ROOT_PARAMETERS, "observation_id": True}, "positive integer"),
        ({**ROOT_PARAMETERS, "observation_id": 0}, "positive integer"),
    ],
)
def test_resource_fetch_validates_all_parameters_before_parent_or_io(parameters, message):
    observations = RootObservations()
    object_store = RootObjectStore()
    edgar = FakeEdgar({})
    handler, _, _, _ = _root_handler(
        edgar,
        observations=observations,
        object_store=object_store,
    )
    context, repository, _events = _root_context(parameters=parameters)

    with pytest.raises(ValueError, match=message):
        handler(context)

    assert observations.parent_ids == []
    assert observations.calls == []
    assert edgar.urls == []
    assert object_store.puts == []
    assert repository.checkpoints == []


@pytest.mark.parametrize(
    ("observations", "message"),
    [
        (RootObservations(parent_error=ValueError("does not exist")), "does not exist"),
        (RootObservations(parent_error=ValueError("not latest")), "not latest"),
        (
            RootObservations(
                parent=_root_parent(observation={"state": "removed"})
            ),
            "not present",
        ),
        (
            RootObservations(
                parent=_root_parent(observation={"state": "unavailable"})
            ),
            "not present",
        ),
        (
            RootObservations(
                parent=_root_parent(item={"issuer_source_identifier": "0000009999"})
            ),
            "does not match",
        ),
        (
            RootObservations(
                parent=_root_parent(metadata={"form": "10-Q"})
            ),
            "does not match",
        ),
    ],
)
def test_resource_fetch_rejects_missing_stale_changed_or_nonpresent_parent_before_io(
    observations, message
):
    edgar = FakeEdgar({})
    object_store = RootObjectStore()
    handler, _, _, _ = _root_handler(
        edgar,
        observations=observations,
        object_store=object_store,
    )
    context, repository, _events = _root_context()

    with pytest.raises(ValueError, match=message):
        handler(context)

    assert edgar.urls == []
    assert object_store.puts == []
    assert observations.calls == []
    assert repository.checkpoints == []


@pytest.mark.parametrize("missing_role", list(_root_urls()))
def test_resource_fetch_404_records_pending_and_remains_retryable(missing_role):
    urls = _root_urls()
    responses = _root_responses()
    responses[urls[missing_role]] = NoXbrlDataError("not found")
    edgar = FakeEdgar(responses)
    handler, observations, object_store, _artifacts = _root_handler(edgar)
    context, repository, _events = _root_context()

    with pytest.raises(SecResourcePending, match=missing_role):
        handler(context)

    pending = [call for call in observations.calls if call["state"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["metadata"]["role"] == missing_role
    assert pending[0]["metadata"]["reason"] == "not_found"
    assert len(object_store.puts) == list(urls).index(missing_role)
    assert repository.checkpoints == []


def test_resource_fetch_transient_failure_records_no_invented_state():
    urls = _root_urls()
    responses = _root_responses()
    responses[urls["submissions"]] = EdgarError("timeout")
    edgar = FakeEdgar(responses)
    handler, observations, _object_store, _artifacts = _root_handler(edgar)
    context, repository, _events = _root_context()

    with pytest.raises(EdgarError, match="timeout"):
        handler(context)

    assert [call["metadata"]["role"] for call in observations.calls] == [
        "complete_submission",
        "accession_inventory",
    ]
    assert all(call["state"] == "present" for call in observations.calls)
    assert repository.checkpoints == []


@pytest.mark.parametrize(
    ("role", "payload", "error", "pending"),
    [
        ("accession_inventory", b"{", "valid UTF-8 JSON", False),
        (
            "accession_inventory",
            json.dumps({"directory": {"name": "/wrong", "item": [{"name": "x"}]}}).encode(),
            "another accession",
            False,
        ),
        (
            "submissions",
            json.dumps(
                {
                    "cik": "9999",
                    "filings": {
                        "recent": {"accessionNumber": [], "form": []},
                        "files": [],
                    },
                }
            ).encode(),
            "another issuer",
            False,
        ),
        (
            "submissions",
            json.dumps(
                {
                    "cik": "1234",
                    "filings": {
                        "recent": {"accessionNumber": [], "form": []},
                        "files": [],
                    },
                }
            ).encode(),
            "does not yet identify",
            True,
        ),
        (
            "company_facts",
            json.dumps({"cik": 1234, "facts": {"us-gaap": []}}).encode(),
            "malformed taxonomy",
            False,
        ),
        (
            "company_facts",
            json.dumps({"cik": 1234, "facts": {}}).encode(),
            "does not yet identify",
            True,
        ),
    ],
)
def test_resource_fetch_retains_json_revision_but_never_checkpoints_invalid_graph(
    role, payload, error, pending
):
    urls = _root_urls()
    responses = _root_responses()
    prior = responses[urls[role]]
    responses[urls[role]] = EdgarTransportResult(
        payload,
        prior.url,
        prior.media_type,
        prior.etag,
        prior.last_modified,
    )
    handler, observations, object_store, _artifacts = _root_handler(
        FakeEdgar(responses)
    )
    context, repository, _events = _root_context()

    expected_error = SecResourcePending if pending else ValueError
    with pytest.raises(expected_error, match=error):
        handler(context)

    calls = [call for call in observations.calls if call["metadata"]["role"] == role]
    assert calls[0]["state"] == "present"
    assert calls[0]["artifact_sha256"] == hashlib.sha256(payload).hexdigest()
    assert all(call["state"] == "present" for call in calls)
    if pending:
        assert repository.outcomes[-1]["status"] == "failed"
        assert repository.outcomes[-1]["outcome"]["state"] == "pending"
    assert any(data == payload for data, _media, _digest in object_store.puts)
    assert repository.checkpoints == []


def test_resource_fetch_duplicate_replay_and_aggregate_revision_are_immutable():
    observations = RootObservations()
    object_store = RootObjectStore()
    artifacts = RootArtifacts()
    first_handler, _, _, _ = _root_handler(
        FakeEdgar(_root_responses()),
        observations=observations,
        object_store=object_store,
        artifacts=artifacts,
    )
    first_context, _first_repository, _events = _root_context()
    first = first_handler(first_context)

    replay_context, _replay_repository, _events = _root_context()
    replay = first_handler(replay_context)
    assert replay == first
    assert len(observations.item_ids) == 4
    assert len(observations.observation_ids) == 4

    changed_handler, _, _, _ = _root_handler(
        FakeEdgar(_root_responses(fact_value=2)),
        observations=observations,
        object_store=object_store,
        artifacts=artifacts,
    )
    changed_context, _changed_repository, _events = _root_context()
    changed = changed_handler(changed_context)

    assert changed["roots"]["company_facts"] != first["roots"]["company_facts"]
    assert {
        role: digest
        for role, digest in changed["roots"].items()
        if role != "company_facts"
    } == {
        role: digest
        for role, digest in first["roots"].items()
        if role != "company_facts"
    }
    assert len(observations.item_ids) == 4
    assert len(observations.observation_ids) == 5


def test_resource_fetch_checks_stop_before_each_new_fetch():
    stopping = threading.Event()

    class StopAfterFirst(FakeEdgar):
        def fetch(self, url):
            result = super().fetch(url)
            stopping.set()
            return result

    edgar = StopAfterFirst(_root_responses())
    handler, observations, _object_store, _artifacts = _root_handler(edgar)
    context, repository, _events = _root_context(stopping=stopping)

    with pytest.raises(SecDiscoveryStopped, match="next resource"):
        handler(context)

    assert edgar.urls == [_root_urls()["complete_submission"]]
    assert len(observations.calls) == 1
    assert repository.checkpoints == []


def test_resource_fetch_expired_lease_leaves_verified_orphan_only():
    class ExpiredObservations(RootObservations):
        def record(self, lease, **values):
            raise LeaseLost("expired")

    observations = ExpiredObservations()
    object_store = RootObjectStore()
    artifacts = RootArtifacts()
    handler, _, _, _ = _root_handler(
        FakeEdgar(_root_responses()),
        observations=observations,
        object_store=object_store,
        artifacts=artifacts,
    )
    context, repository, _events = _root_context()

    with pytest.raises(LeaseLost, match="expired"):
        handler(context)

    assert len(object_store.puts) == 1
    assert len(artifacts.calls) == 1
    assert observations.calls == []
    assert repository.checkpoints == []


def test_resource_fetch_completed_checkpoint_only_reasserts_stable_candidate_child():
    jobs = FakeJobs([])
    handler, observations, object_store, _artifacts = _root_handler(
        FakeEdgar({}), jobs=jobs
    )
    roots = {
        role: f"{index}" * 64
        for index, role in enumerate(_root_urls(), start=1)
    }
    checkpoint = {
        "accession": ROOT_ACCESSION,
        "cik": ROOT_CIK,
        "filing_observation_id": 42,
        "roots": roots,
    }
    context, repository, _events = _root_context(checkpoint=checkpoint)

    assert handler(context) == checkpoint
    assert observations.parent_ids == []
    assert object_store.puts == []
    assert repository.checkpoints == []
    assert jobs.children[0]["parameters"]["roots"] == roots


class CandidateObjectStore:
    def __init__(self, payloads):
        self.payloads = {
            hashlib.sha256(payload).hexdigest(): payload
            for payload in payloads
        }

    def put_verified(self, data, _media_type):
        digest = hashlib.sha256(data).hexdigest()
        self.payloads[digest] = bytes(data)
        return VerifiedObject(digest, f"raw/sha256/{digest}", len(data), NOW)

    def read_verified(self, _key, digest, size):
        payload = self.payloads[digest]
        assert len(payload) == size
        return payload


class CandidateArtifacts(RootArtifacts):
    def __init__(self, payloads):
        super().__init__()
        self.rows = {}
        for payload in payloads:
            digest = hashlib.sha256(payload).hexdigest()
            self.rows[digest] = EvidenceArtifact(
                digest, f"raw/sha256/{digest}", len(payload),
                "application/octet-stream", NOW, NOW,
            )

    def add_verified(self, verified, media_type):
        row = super().add_verified(verified, media_type)
        self.rows[row.content_sha256] = row
        return row

    def get(self, digest):
        return self.rows[digest]


class CandidateObservations(RootObservations):
    def __init__(self, roots, parent=None):
        super().__init__(parent=parent)
        self.roots = roots

    def by_sha256(self, digest):
        return self.roots[digest]


class CandidateCompanies:
    def __init__(self):
        self.calls = []

    def store_candidate(self, lease, **values):
        assert lease == LeaseToken(1, "worker", 1)
        self.calls.append(values)
        return SimpleNamespace(snapshot_id=701, created=len(self.calls) == 1)


def _candidate_roots(
    *,
    form="10-K",
    inventory_names=("annual.htm", "R1.htm"),
    primary_document="annual.htm",
    submissions_value=None,
):
    inventory = json.dumps({
        "directory": {
            "name": "/Archives/edgar/data/1234/000000123426000001",
            "item": [{"name": name} for name in inventory_names],
        }
    }).encode()
    submissions = json.dumps(submissions_value or {
        "cik": "1234",
        "name": "TEST COMPANY",
        "tickers": ["TEST"],
        "exchanges": ["Nasdaq"],
        "filings": {"recent": {
            "accessionNumber": [ROOT_ACCESSION],
            "form": [form],
            "filingDate": ["2026-09-28"],
            "reportDate": ["2025-12-31"],
            "primaryDocument": [primary_document],
        }},
    }).encode()
    payloads = {
        "complete_submission": b"complete",
        "accession_inventory": inventory,
        "submissions": submissions,
        "company_facts": json.dumps({"cik": 1234, "facts": {}}).encode(),
    }
    parent = _root_parent(metadata={"form": form})
    roots = {}
    stored = {}
    for index, (role, payload) in enumerate(payloads.items(), start=1):
        observation_digest = f"{index}" * 64
        artifact_digest = hashlib.sha256(payload).hexdigest()
        roots[role] = observation_digest
        parent_id = 41 if role in {"complete_submission", "accession_inventory"} else None
        stored[observation_digest] = SimpleNamespace(
            item=SimpleNamespace(
                source_item_id=50 + index,
                source_system="SEC",
                item_kind="resource" if parent_id else "aggregate",
                source_key=role,
                issuer_source_identifier=ROOT_CIK,
                parent_source_item_id=parent_id,
            ),
            observation=SimpleNamespace(
                state="present",
                artifact_sha256=artifact_digest,
                canonical_metadata={
                    "role": role,
                    "cik": ROOT_CIK,
                    **({"accession": ROOT_ACCESSION} if parent_id else {}),
                },
            ),
        )
    return parent, payloads, roots, stored


def _candidate_handler(
    responses,
    *,
    form="10-K",
    inventory_names=("annual.htm", "R1.htm"),
    primary_document="annual.htm",
    submissions_value=None,
    derive=None,
):
    parent, payloads, roots, stored = _candidate_roots(
        form=form,
        inventory_names=inventory_names,
        primary_document=primary_document,
        submissions_value=submissions_value,
    )
    objects = CandidateObjectStore(payloads.values())
    artifacts = CandidateArtifacts(payloads.values())
    observations = CandidateObservations(stored, parent=parent)
    companies = CandidateCompanies()
    handler = SecCandidateStageHandler(
        edgar=FakeEdgar(responses),
        object_store=objects,
        artifacts=artifacts,
        observations=observations,
        companies=companies,
        derive=derive or (lambda bundle: ("ok", {"ticker": bundle.ticker})),
        clock=lambda: NOW,
    )
    parameters = {
        **ROOT_PARAMETERS,
        "form": form,
        "roots": roots,
    }
    context, repository = _context(
        parameters, [], kind="sec-candidate-stage"
    )[:2]
    return handler, context, repository, observations, companies


def test_candidate_stage_fetches_bounded_10k_leaves_and_stores_noncurrent_candidate():
    base = "https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/"
    cover_html = (
        b"<html>Title of each class Common Stock Trading Symbol TEST "
        b"Name of each exchange Nasdaq</html>"
    )
    responses = {
        base + "annual.htm": EdgarTransportResult(
            b"<html>annual</html>", base + "annual.htm", "text/html", None, None
        ),
        base + "R1.htm": EdgarTransportResult(
            cover_html, base + "R1.htm", "text/html", None, None
        ),
    }
    seen = []

    def derive(bundle):
        seen.append(bundle)
        return "ok", {"ticker": bundle.ticker, "source": "fixture"}

    handler, context, repository, observations, companies = _candidate_handler(
        responses, derive=derive
    )

    checkpoint = handler(context)

    assert [call["metadata"]["role"] for call in observations.calls] == [
        "primary_document",
        "cover_r1",
    ]
    assert seen[0].ticker == "TEST"
    assert seen[0].receipt["title"] == "Common Stock"
    assert companies.calls[0]["security_basis"] == "PRIMARY_COMMON_SHARE"
    assert companies.calls[0]["canonical_payload"]["source"] == "fixture"
    assert {artifact.role for artifact in companies.calls[0]["artifacts"]} == {
        "sec_root_complete_submission",
        "sec_root_accession_inventory",
        "sec_root_submissions",
        "sec_root_company_facts",
        "sec_primary_document",
        "sec_cover_r1",
    }
    assert checkpoint["snapshot_id"] == 701
    assert repository.checkpoints == [checkpoint]


def test_candidate_stage_pending_leaf_never_stores_candidate_or_checkpoint():
    base = "https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/"
    handler, context, repository, observations, companies = _candidate_handler({
        base + "annual.htm": NoXbrlDataError("pending"),
    })

    with pytest.raises(SecResourcePending, match="annual.htm"):
        handler(context)

    assert observations.calls[-1]["state"] == "pending"
    assert companies.calls == []
    assert repository.checkpoints == []


def test_candidate_stage_partial_inline_inventory_stays_pending():
    names = ("annual.htm", "R1.htm", "issuer_htm.xml", "issuer.xsd")
    base = "https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/"
    responses = {
        base + "annual.htm": EdgarTransportResult(
            b"<html>annual</html>", base + "annual.htm", "text/html", None, None
        ),
        base + "R1.htm": EdgarTransportResult(
            (
                b"<html>Title of each class Common Stock Trading Symbol TEST "
                b"Name of each exchange Nasdaq</html>"
            ),
            base + "R1.htm",
            "text/html",
            None,
            None,
        ),
    }
    handler, context, repository, _observations, companies = _candidate_handler(
        responses, inventory_names=names
    )

    with pytest.raises(SecResourcePending, match="Inline-XBRL graph"):
        handler(context)

    assert companies.calls == []
    assert repository.checkpoints == []


def test_candidate_stage_missing_instance_with_companions_stays_pending():
    names = ("annual.htm", "R1.htm", "FilingSummary.xml", "issuer.xsd")
    base = "https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/"
    responses = {
        base + "annual.htm": EdgarTransportResult(
            b"<html>annual</html>", base + "annual.htm", "text/html", None, None
        ),
        base + "R1.htm": EdgarTransportResult(
            (
                b"<html>Title of each class Common Stock Trading Symbol TEST "
                b"Name of each exchange Nasdaq</html>"
            ),
            base + "R1.htm",
            "text/html",
            None,
            None,
        ),
    }
    handler, context, repository, _observations, companies = _candidate_handler(
        responses, inventory_names=names
    )

    with pytest.raises(SecResourcePending, match="Inline-XBRL graph"):
        handler(context)

    assert companies.calls == []
    assert repository.checkpoints == []


def test_candidate_stage_routes_direct_20f_through_inline_bytes(monkeypatch):
    names = (
        "annual.htm",
        "R1.htm",
        "R2.htm",
        "issuer_htm.xml",
        "FilingSummary.xml",
        "issuer.xsd",
        "issuer_pre.xml",
    )
    base = "https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/"
    cover_html = (
        b"<html>Title of each class American Depositary Shares, each representing "
        b"two Ordinary Shares Trading Symbol TEST Name of each exchange Nasdaq</html>"
    )
    responses = {
        base + name: EdgarTransportResult(
            (
                cover_html
                if name == "R1.htm"
                else b"<html>American Depositary Shares each representing three Ordinary Shares</html>"
                if name == "R2.htm"
                else f"<{name}/>".encode()
            ),
            base + name,
            "text/html" if name.endswith(".htm") else "application/xml",
            None,
            None,
        )
        for name in names
    }
    captured = []

    def parse(manifest, payloads):
        captured.append((manifest, payloads))
        return {"cik": ROOT_CIK, "facts": {}, "_inline_xbrl": {}}

    monkeypatch.setattr("screener.sec_ingestion._parse_inline_manifest", parse)
    monkeypatch.setattr(
        "screener.sec_ingestion.normalize._current_supported_foreign_annual",
        lambda _facts: (("2026-09-28", ROOT_ACCESSION), "ifrs-full"),
    )
    handler, context, _repository, _observations, companies = _candidate_handler(
        responses,
        form="20-F",
        inventory_names=names,
    )

    handler(context)

    assert captured[0][0]["relationship"] == "direct_annual"
    assert set(captured[0][1]) == {
        "instance", "filing_summary", "presentation", "schema", "primary_document"
    }
    assert companies.calls[0]["receipt_ratio"] == 2
    roles = {
        artifact.role for artifact in companies.calls[0]["artifacts"]
    }
    assert {
        "sec_inline_direct_annual_instance",
        "sec_inline_direct_annual_filing_summary",
        "sec_inline_direct_annual_presentation",
        "sec_inline_direct_annual_schema",
        "sec_inline_direct_annual_primary_document",
        "sec_inline_direct_annual_index",
        "sec_inline_current_manifest",
    } <= roles


def test_candidate_stage_routes_40f_to_incorporated_6k(monkeypatch):
    source_accession = "0000001234-26-000002"
    wrapper = "wrapper.htm"
    annual_names = (wrapper, "R1.htm")
    source_names = (
        "cover6k.htm",
        "statements.htm",
        "source_htm.xml",
        "FilingSummary.xml",
        "source.xsd",
        "source_pre.xml",
    )
    annual_base = "https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/"
    source_base = "https://www.sec.gov/Archives/edgar/data/1234/000000123426000002/"
    wrapper_html = f"""<html>
      Title of each class Common Shares Trading Symbol TEST Name of each exchange NYSE
      incorporated by reference into this Form 40-F from Form 6-K
      <a href="https://www.sec.gov/Archives/edgar/data/1234/000000123426000002/statements.htm">
      Audited consolidated financial statements</a></html>""".encode()
    historical_name = f"CIK{ROOT_CIK}-submissions-001.json"
    submissions = {
        "cik": "1234",
        "name": "TEST COMPANY",
        "tickers": ["TEST"],
        "exchanges": ["NYSE"],
        "filings": {"recent": {
            "accessionNumber": [],
            "form": [],
            "filingDate": [],
            "reportDate": [],
            "primaryDocument": [],
        }, "files": [{
            "name": historical_name,
            "filingCount": 2,
            "filingFrom": "2026-09-01",
            "filingTo": "2026-09-29",
        }]},
    }
    historical = json.dumps({
            "accessionNumber": [ROOT_ACCESSION, source_accession],
            "form": ["40-F", "6-K"],
            "filingDate": ["2026-09-28", "2026-09-20"],
            "reportDate": ["2025-12-31", "2025-12-31"],
            "primaryDocument": [wrapper, "cover6k.htm"],
    }).encode()
    source_index = json.dumps({
        "directory": {
            "name": "/Archives/edgar/data/1234/000000123426000002",
            "item": [{"name": name} for name in source_names],
        }
    }).encode()
    responses = {
        "https://data.sec.gov/submissions/" + historical_name: EdgarTransportResult(
            historical,
            "https://data.sec.gov/submissions/" + historical_name,
            "application/json",
            None,
            None,
        ),
        annual_base + wrapper: EdgarTransportResult(
            wrapper_html, annual_base + wrapper, "text/html", None, None
        ),
        annual_base + "R1.htm": EdgarTransportResult(
            wrapper_html, annual_base + "R1.htm", "text/html", None, None
        ),
        source_base + "index.json": EdgarTransportResult(
            source_index, source_base + "index.json", "application/json", None, None
        ),
        **{
            source_base + name: EdgarTransportResult(
                f"<{name}/>".encode(), source_base + name,
                "text/html" if name.endswith(".htm") else "application/xml",
                None, None,
            )
            for name in source_names
        },
    }
    captured = []

    def parse(manifest, payloads):
        captured.append((manifest, payloads))
        return {"cik": ROOT_CIK, "facts": {}, "_inline_xbrl": {}}

    monkeypatch.setattr("screener.sec_ingestion._parse_inline_manifest", parse)
    monkeypatch.setattr(
        "screener.sec_ingestion.normalize._current_supported_foreign_annual",
        lambda _facts: (("2026-09-28", ROOT_ACCESSION), "us-gaap"),
    )
    handler, context, _repository, observations, companies = _candidate_handler(
        responses,
        form="40-F",
        inventory_names=annual_names,
        primary_document=wrapper,
        submissions_value=submissions,
    )

    handler(context)

    assert captured[0][0]["relationship"] == "incorporated_annual_exhibit"
    assert captured[0][0]["source"]["accession"] == source_accession
    assert captured[0][0]["source"]["document"] == "statements.htm"
    assert companies.calls[0]["source_accession"] == ROOT_ACCESSION
    source_items = [
        call["item"]
        for call in observations.calls
        if call["metadata"].get("accession") == source_accession
    ]
    assert source_items
    assert all(
        item.source_key.startswith(f"{source_accession}/")
        and item.parent_source_item_id is None
        for item in source_items
    )
    roles = {artifact.role for artifact in companies.calls[0]["artifacts"]}
    assert {
        "sec_inline_annual_wrapper_index",
        "sec_inline_annual_wrapper_primary_document",
        "sec_inline_incorporated_source_index",
        "sec_inline_incorporated_source_instance",
        "sec_inline_incorporated_source_filing_summary",
        "sec_inline_incorporated_source_presentation",
        "sec_inline_incorporated_source_schema",
        "sec_inline_incorporated_source_primary_document",
        "sec_inline_current_manifest",
    } <= roles
