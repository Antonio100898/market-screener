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
    SecQuarterlyReconciliationHandler,
    SecRecentDiscoveryHandler,
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
