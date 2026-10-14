"""Occurrence-bound routing over existing self-contained producer fixtures."""
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from screener.sec_ingestion import SecRecentDiscoveryHandler, SecQuarterlyReconciliationHandler
from screener.sources.edgar import EdgarTransportResult
from test_sec_ingestion import (
    FakeArtifacts, FakeEdgar, FakeJobs, FakeObjectStore, FakeObservations,
    NOW, _context, _index, _prior, _quarterly_url, _row, _url,
)
from test_automatic_ingestion import assembled, registry, request, write_plan
from screener import automatic_ingestion as automatic


ACCESSION = "0000001234-26-000001"
DAY = date(2026, 9, 28)


def run_producer(route, *, resource_kind="sec-resource-fetch", bound=None,
                 occurrence=41, generation=1, prior=None, jobs=None):
    events = []
    quarterly = route == "quarterly"
    url = _quarterly_url() if quarterly else _url(DAY)
    result = EdgarTransportResult(_index(_row("10-K", "FIXTURE", "1234", DAY.isoformat(), ACCESSION)),
                                  url, "text/plain", None, None)
    observations = FakeObservations(events, latest=() if prior is None else (prior,))
    jobs = jobs or FakeJobs(events)
    handler_type = SecQuarterlyReconciliationHandler if quarterly else SecRecentDiscoveryHandler
    options = {} if bound is None else {"occurrence_bound": bound}
    handler = handler_type(edgar=FakeEdgar({url: result}), object_store=FakeObjectStore(),
                           artifacts=FakeArtifacts(), observations=observations, jobs=jobs,
                           clock=lambda: NOW, resource_kind=resource_kind, **options)
    parameters = {"year": 2026, "quarter": 3} if quarterly else {
        "start_date": DAY.isoformat(), "end_date": DAY.isoformat()}
    context, repository = _context(parameters, events)
    context = replace(context, job=replace(context.job, occurrence_id=occurrence,
                                          ownership_generation=generation))
    result = handler(context)
    return jobs.children, result, repository


@pytest.mark.parametrize("route", ["daily", "quarterly"])
@pytest.mark.parametrize("resource_kind", ["sec-resource-fetch", "sec-resource-fetch-v2"])
def test_old_request_bytes_and_filing_date_remain_exact(route, resource_kind):
    children, _, _ = run_producer(route, resource_kind=resource_kind)
    assert len(children) == 1
    child = children[0]
    assert child["child_key"] == f"{resource_kind}:0000001234:{ACCESSION}:" + "b" * 64
    assert child["parameters"] == dict(accession=ACCESSION, cik="0000001234", form="10-K",
                                       filename=f"edgar/data/1234/{ACCESSION}.txt", observation_id=2)
    assert child["kind"] == resource_kind and child["priority"] == 30
    assert child["scheduled_for"] == child["due_at"] == datetime(2026, 9, 28, tzinfo=timezone.utc)


def test_daily_and_quarterly_share_occurrence_identity_and_preserve_parameters():
    daily, _, _ = run_producer("daily", resource_kind="sec-resource-fetch-v2", bound=True)
    quarter, _, _ = run_producer("quarterly", resource_kind="sec-resource-fetch-v2", bound=True)
    assert daily[0]["child_key"] == quarter[0]["child_key"] == (
        f"sec-resource-fetch-v2:occurrence:41:0000001234:{ACCESSION}:" + "b" * 64)
    assert daily[0]["parameters"] == quarter[0]["parameters"]


@pytest.mark.parametrize("route", ["daily", "quarterly"])
def test_distinct_occurrences_check_but_reclaimed_generation_keeps_one_request(route):
    first, _, _ = run_producer(route, resource_kind="sec-resource-fetch-v2", bound=True)
    reclaimed, _, _ = run_producer(route, resource_kind="sec-resource-fetch-v2", bound=True, generation=8)
    later, _, _ = run_producer(route, resource_kind="sec-resource-fetch-v2", bound=True, occurrence=42)
    assert first[0]["child_key"] == reclaimed[0]["child_key"] != later[0]["child_key"]
    assert first[0]["parameters"] == reclaimed[0]["parameters"] == later[0]["parameters"]


@pytest.mark.parametrize("route", ["daily", "quarterly"])
@pytest.mark.parametrize("occurrence", [None, True, False, 0, -1, 41.0, "41"])
def test_invalid_occurrence_rejects_before_child_enqueue(route, occurrence):
    jobs = FakeJobs([])
    with pytest.raises(ValueError, match="occurrence"):
        run_producer(route, resource_kind="sec-resource-fetch-v2", bound=True,
                     occurrence=occurrence, jobs=jobs)
    assert jobs.children == []


@pytest.mark.parametrize("resource_kind", ["sec-resource-fetch", "sec-resource-fetch-v2"])
def test_old_unchanged_quarterly_filings_from_earlier_job_stay_skipped(resource_kind):
    prior = _prior(ACCESSION, company="FIXTURE", detecting_job_id=99)
    children, result, repository = run_producer("quarterly", resource_kind=resource_kind, prior=prior)
    assert children == [] and result == repository.checkpoints[0]
    assert repository.outcomes[0]["outcome"]["action"] == "unchanged"


def test_occurrence_bound_quarterly_checks_unchanged_earlier_detected_filing():
    prior = _prior(ACCESSION, company="FIXTURE", detecting_job_id=99)
    children, result, repository = run_producer("quarterly", resource_kind="sec-resource-fetch-v2",
                                              bound=True, prior=prior)
    assert len(children) == 1 and children[0]["parameters"]["observation_id"] == prior.source_observation_id
    assert children[0]["child_key"].endswith(prior.observation_sha256)
    assert result == repository.checkpoints[0]
    assert repository.outcomes[0]["outcome"]["action"] == "unchanged"


@pytest.mark.parametrize("handler", [SecRecentDiscoveryHandler, SecQuarterlyReconciliationHandler])
@pytest.mark.parametrize("bound,kind", [(1,"sec-resource-fetch-v2"), (None,"sec-resource-fetch-v2"),
                                      (True,"sec-resource-fetch")])
def test_new_mode_constructor_rejects_ambiguous_or_legacy_resource_contract(handler, bound, kind):
    with pytest.raises(ValueError, match="occurrence"):
        handler(edgar=None, object_store=None, artifacts=None, observations=None, jobs=None,
                resource_kind=kind, occurrence_bound=bound)


@pytest.mark.parametrize("route", ["daily", "quarterly"])
def test_child_enqueue_interruption_retries_exact_occurrence_request(route):
    jobs = FakeJobs([], fail_once=True)
    with pytest.raises(RuntimeError, match="interrupted"):
        run_producer(route, resource_kind="sec-resource-fetch-v2", bound=True, jobs=jobs)
    children, _, _ = run_producer(route, resource_kind="sec-resource-fetch-v2", bound=True,
                                  generation=2, jobs=jobs)
    assert len(children) == 1
    assert children[0]["child_key"].startswith("sec-resource-fetch-v2:occurrence:41:")


def test_v3_plan_registry_reuses_v2_resource_and_old_producer_modes(monkeypatch, tmp_path, registry):
    current, _, _ = assembled(monkeypatch, tmp_path)
    recent = request("sec-recent-discovery-v3")
    quarter = request("sec-index-reconciliation-v3")
    quarter["parameters"] = {"year": 2026, "quarter": 3}
    current.plan_path.write_bytes(write_plan(tmp_path, [recent, quarter]).read_bytes())
    assembly = automatic.start_automatic_ingestion(current)
    try:
        runtime = assembly.runtime
        assert [x.kind for x in runtime.source(NOW)] == [recent["kind"], quarter["kind"]]
        for kind in ("sec-recent-discovery", "sec-index-reconciliation"):
            assert not runtime.handlers[kind]._occurrence_bound
            assert not runtime.handlers[kind + "-v2"]._occurrence_bound
            assert runtime.handlers[kind + "-v3"]._occurrence_bound
            assert runtime.handlers[kind + "-v3"]._resource_kind == "sec-resource-fetch-v2"
        assert runtime.handlers["sec-resource-fetch-v2"]._annotate_annual
        wrong = {**recent, "kind": "sec-recent-discovery-v2"}
        with pytest.raises(ValueError, match="namespace"):
            automatic.read_plan(write_plan(tmp_path, [wrong]))
    finally:
        automatic.stop_automatic_ingestion(assembly)
