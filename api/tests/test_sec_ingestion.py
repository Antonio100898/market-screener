from datetime import date, datetime, timezone
from types import SimpleNamespace
import threading

import httpx
import pytest

from screener.artifacts import EvidenceArtifact
from screener.durable_jobs import DurableJobRecord, LeaseToken
from screener.job_runtime import JobContext
from screener.object_store import VerifiedObject
from screener.sec_ingestion import (
    SecDiscoveryStopped,
    SecRecentDiscoveryHandler,
    parse_form_index,
)
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
    def __init__(self, events):
        self.events = events
        self.calls = []
        self.identities = {}

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
            observation=SimpleNamespace(
                source_observation_id=observation_id,
                observation_sha256=digest,
            )
        )


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
        self.stop_after_checkpoint = stop_after_checkpoint

    def save_checkpoint(self, lease, checkpoint, *, now):
        assert lease == LeaseToken(1, "worker", 1)
        self.events.append(("checkpoint", checkpoint["through_date"]))
        self.checkpoints.append(dict(checkpoint))
        if self.stop_after_checkpoint is not None:
            self.stop_after_checkpoint.set()
        return object()


def _job(parameters, *, checkpoint=None):
    return DurableJobRecord(
        job_id=1,
        occurrence_id=1,
        kind="sec-recent-discovery",
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
):
    stopping = stopping or threading.Event()
    repository = FakeContextRepository(
        events,
        stopping if stop_after_checkpoint else None,
    )
    return (
        JobContext(
            job=_job(parameters, checkpoint=checkpoint),
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
    assert events[-1] == ("checkpoint", day.isoformat())
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
