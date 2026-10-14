import pytest
from pydantic import ValidationError

from screener.company_queries import QueryUnavailable, RestartQuery
from screener.durable_jobs import LeaseLost, LeaseToken
from screener.research_runs import RunConflict
from screener.research_tools import ResearchCompanyTools, ResearchToolStopped


LEASE = LeaseToken(7, "worker", 3)


class Runs:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)

    def tool_access(self, lease, run_id):
        assert lease == LEASE and run_id == 9
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return result


class Companies:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def list(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return self._result()

    def detail(self, **kwargs):
        self.calls.append(kwargs)
        return self._result()

    def _result(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_strict_detail_keeps_exact_owning_envelope_and_missing_evidence():
    envelope = {"release": {"release_id": 4}, "security_id": 2,
        "row": {"ticker": "ABT", "cash": None, "debt": 0,
            "criteria": [{"n": 1, "status": "INSUFFICIENT"}], "sources": {"cash": None}},
        "source_validity": {"official_financial_readiness": "NOT_ASSESSED"}}
    companies = Companies(envelope)
    tools = ResearchCompanyTools(Runs([{"release_id": 4}] * 2), companies)
    assert tools.detail(LEASE, 9, {"ticker": "ABT"}) is envelope
    assert companies.calls == [{"ticker": "ABT", "security_id": None,
        "release_id": 4, "assume_absent_zero": False}]
    assert "security_id" not in envelope["row"]


def test_search_is_bounded_identity_query_and_cursor_stays_opaque():
    result = {"release": {"release_id": 4}, "rows": [], "next_cursor": "opaque"}
    companies = Companies(result)
    tools = ResearchCompanyTools(Runs([{"release_id": 4}] * 2), companies)
    assert tools.search(LEASE, 9, {"q": "'; DROP TABLE issuer;--", "cursor": "opaque"}) is result
    query, options = companies.calls[0]
    assert query.q == "'; DROP TABLE issuer;--"
    assert query.limit == 50 and query.row_fields == "identity"
    assert query.tracked_only is False and query.workspace_revision is None
    assert options == {"release_id": 4, "cursor": "opaque"}


@pytest.mark.parametrize("method,inputs", [
    ("search", {"owner_id": 2}), ("search", {"release_id": 5}),
    ("search", {"tracked_only": True}), ("search", {"q": 7}),
    ("search", {"cursor": "x" * 8193}), ("search", {"code": "print(1)"}),
    ("detail", {}), ("detail", {"ticker": "ABT", "security_id": 2}),
    ("detail", {"security_id": True}), ("detail", {"security_id": 0}),
    ("detail", {"ticker": ""}), ("detail", {"url": "https://example.com"}),
])
def test_invalid_or_extra_input_never_reaches_private_or_shared_storage(method, inputs):
    companies = Companies(None)
    tools = ResearchCompanyTools(Runs([]), companies)
    with pytest.raises(ValidationError):
        getattr(tools, method)(LEASE, 9, inputs)
    assert not companies.calls


@pytest.mark.parametrize("failure", [None, LeaseLost("expired")])
def test_pre_read_denial_never_queries_company(failure):
    companies = Companies(None)
    tools = ResearchCompanyTools(Runs([failure]), companies)
    with pytest.raises(LeaseLost if failure else ResearchToolStopped):
        tools.detail(LEASE, 9, {"security_id": 2})
    assert not companies.calls


@pytest.mark.parametrize("failure", [None, LeaseLost("expired")])
def test_post_read_denial_suppresses_successful_result(failure):
    companies = Companies({"release": {"release_id": 4}, "row": {"ticker": "ABT"}})
    tools = ResearchCompanyTools(Runs([{"release_id": 4}, failure]), companies)
    with pytest.raises(LeaseLost if failure else ResearchToolStopped):
        tools.detail(LEASE, 9, {"security_id": 2})


@pytest.mark.parametrize("error", [RestartQuery("restart"), QueryUnavailable("unavailable")])
def test_query_failures_stay_visible_after_final_authorization(error):
    tools = ResearchCompanyTools(Runs([{"release_id": 4}] * 2), Companies(error))
    with pytest.raises(type(error)):
        tools.search(LEASE, 9, {})


def test_unknown_exact_company_stays_missing():
    tools = ResearchCompanyTools(Runs([{"release_id": 4}] * 2), Companies(None))
    assert tools.detail(LEASE, 9, {"ticker": "UNKNOWN"}) is None


@pytest.mark.parametrize("after,result", [
    ({"release_id": 5}, {"release": {"release_id": 4}}),
    ({"release_id": 4}, {"release": {"release_id": 5}}),
])
def test_mismatched_run_or_result_release_fails_closed(after, result):
    tools = ResearchCompanyTools(Runs([{"release_id": 4}, after]), Companies(result))
    with pytest.raises(RunConflict):
        tools.search(LEASE, 9, {})
