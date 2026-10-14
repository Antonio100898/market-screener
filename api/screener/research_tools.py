"""Run-scoped shared company reads; no provider or production handler registration."""
from pydantic import Field, model_validator

from .company_queries import CompanyQuery, CompanyQueryRepository, MAX_CURSOR_LENGTH
from .durable_jobs import LeaseToken
from .research_runs import ResearchRunRepository, RunConflict
from .workspace_config import PresentationModel


SEARCH_PAGE_SIZE = 50


class CompanySearch(PresentationModel):
    q: str = Field(default="", max_length=1000)
    cursor: str | None = Field(default=None, max_length=MAX_CURSOR_LENGTH)


class CompanyDetail(PresentationModel):
    ticker: str | None = Field(default=None, min_length=1, max_length=100)
    security_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def exact_identity(self):
        if (self.ticker is None) == (self.security_id is None):
            raise ValueError("select exactly one company ticker or security identity")
        return self


class ResearchToolStopped(RuntimeError):
    pass


class ResearchCompanyTools:
    def __init__(self, runs: ResearchRunRepository, companies: CompanyQueryRepository):
        self.runs = runs
        self.companies = companies

    def search(self, lease: LeaseToken, run_id: int, request: CompanySearch | dict):
        request = CompanySearch.model_validate(request)
        query = CompanyQuery(q=request.q, row_fields="identity", limit=SEARCH_PAGE_SIZE)
        return self._read(lease, run_id, lambda release: self.companies.list(
            query, release_id=release, cursor=request.cursor))

    def detail(self, lease: LeaseToken, run_id: int, request: CompanyDetail | dict):
        request = CompanyDetail.model_validate(request)
        return self._read(lease, run_id, lambda release: self.companies.detail(
            ticker=request.ticker, security_id=request.security_id,
            release_id=release, assume_absent_zero=False))

    def _access(self, lease, run_id):
        run = self.runs.tool_access(lease, run_id)
        if run is None:
            raise ResearchToolStopped("research run is not active")
        return run

    def _read(self, lease, run_id, operation):
        before = self._access(lease, run_id)
        try:
            result = operation(before["release_id"])
        finally:
            after = self._access(lease, run_id)
            if after["release_id"] != before["release_id"]:
                raise RunConflict("research run release changed during the read")
        if result is not None and result["release"]["release_id"] != before["release_id"]:
            raise RunConflict("company result does not belong to the research release")
        return result
