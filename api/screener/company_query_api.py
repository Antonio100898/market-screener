"""Versioned public query router; application wiring is a separate migration step."""
from functools import lru_cache
from typing import Annotated
from datetime import date

from fastapi import APIRouter, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as RoutingHTTPException
from starlette.routing import Match
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from .company_projection import PROJECTION_REVISION
from .company_queries import (CompanyQuery, CompanyQueryRepository, InvalidQuery, QueryUnavailable,
    RestartQuery, SORT_COLUMNS, HistoryQuery, HistoryNotFound)
from .figure_history import FigureHistoryConflict
from .postgres import create_postgres_engine


router = APIRouter(prefix="/api/v1")


class CompanyQueryParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str = Field(default="", max_length=1000)
    lens: str = "BOTH"
    fit: str = "ALL"
    gaps: str = "ALL"
    profiles: list[str] = Field(default_factory=list, alias="profile", max_length=100)
    sectors: list[str] = Field(default_factory=list, alias="sector", max_length=100)
    venues: list[str] = Field(default_factory=list, alias="venue", max_length=100)
    indexes: list[str] = Field(default_factory=list, alias="index", max_length=100)
    min_cap: float = Field(default=0, ge=0, allow_inf_nan=False)
    min_met: int = Field(default=0, ge=0, le=6)
    min_positive_eps: int = Field(default=0, ge=0, le=10)
    max_pe: float = Field(default=0, ge=0, allow_inf_nan=False)
    max_pe3: float = Field(default=0, ge=0, allow_inf_nan=False)
    hide_na: bool = False
    hide_no_apply: bool = False
    below_ncav: bool = False
    tracked_only: bool = False
    workspace_revision: int | None = None
    sort: list[str] = Field(default_factory=lambda: ["ticker:1"], max_length=13)
    limit: int = Field(default=300, ge=1, le=300)
    row_fields: str = "full"
    release: int | None = Field(default=None, gt=0)
    cursor: str | None = Field(default=None, max_length=8192)


@lru_cache(maxsize=1)
def query_repository():
    return CompanyQueryRepository(create_postgres_engine())


def _call(operation, **kwargs):
    try:
        return operation(**kwargs)
    except InvalidQuery as exc:
        raise HTTPException(400, str(exc)) from exc
    except RestartQuery as exc:
        raise HTTPException(409, {"code": "RESTART_QUERY", "message": str(exc)}) from exc
    except QueryUnavailable as exc:
        raise HTTPException(503, {"code": "QUERY_NOT_READY", "message": str(exc)}) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, {"code": "QUERY_STORE_UNAVAILABLE", "message": "PostgreSQL display queries unavailable"}) from exc


@router.get("/companies")
def companies(params: Annotated[CompanyQueryParameters, Query()]):
    values = params.model_dump(exclude={"sort", "release", "cursor"})
    for key in ("profiles", "sectors", "venues", "indexes"):
        values[key] = tuple(values[key])
    try:
        sorts = []
        for specification in params.sort:
            key, direction = specification.split(":")
            if direction not in {"1", "-1"}:
                raise ValueError("direction")
            sorts.append((key, int(direction)))
        query = CompanyQuery(**values, sorts=tuple(sorts))
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc) if isinstance(exc, InvalidQuery) else "sort must use key:1 or key:-1") from exc
    return _call(query_repository().list, query=query, release_id=params.release, cursor=params.cursor)


@router.get("/companies/by-ticker/{ticker}")
def company_by_ticker(ticker: str, release: Annotated[int | None, Query(gt=0)] = None,
                      assume_absent_zero: bool = True):
    result = _call(query_repository().detail, ticker=ticker, release_id=release, assume_absent_zero=assume_absent_zero)
    if result is None:
        raise HTTPException(404, "ticker is not in this release")
    return result


@router.get("/companies/{security_id}")
def company(security_id: int, release: Annotated[int | None, Query(gt=0)] = None,
            assume_absent_zero: bool = True):
    result = _call(query_repository().detail, security_id=security_id, release_id=release, assume_absent_zero=assume_absent_zero)
    if result is None:
        raise HTTPException(404, "security is not in this release")
    return result


@router.get("/facets")
def facets(release: Annotated[int | None, Query(gt=0)] = None):
    return _call(query_repository().facets, release_id=release)


@router.get("/overview")
def overview(release: Annotated[int | None, Query(gt=0)] = None):
    return _call(query_repository().overview, release_id=release)


@router.get("/data-status")
def data_status():
    return _call(query_repository().status)


@router.get("/capabilities")
def capabilities():
    return {"api_contract_major": 1, "projection_revision": PROJECTION_REVISION,
            "sorts": list(SORT_COLUMNS), "default_sort": "ticker:1", "max_page_size": 300,
            "private_queries_supported": False, "text_order": "en-US ICU nondeterministic",
            "row_fields": ["full", "identity"],
            "source_validity_supported": True, "source_validity_scope": "CAPTURED_DEPENDENCIES_ONLY",
            "newest_filings_assessed": False, "numeric_invalidation_assessed": False,
            "official_financial_readiness": "NOT_ASSESSED", "freshness_target": None,
            "cursor_restart_scope": "GLOBAL_SEMANTIC_SOURCE_GENERATION",
            "cursor_configuration_required": "SCREENER_CURSOR_SECRET",
            "figure_history_supported": True, "figure_history_contract_revision": 1,
            "figure_history_scope": "CAPTURED_RELEASE_SELECTIONS_AND_CHECKS",
            "figure_history_operations": ["catalog", "selected", "timeline", "activations", "dependencies", "source-events"],
            "figure_history_financial_delta_supported": False, "figure_history_cause_assessed": False,
            "figure_history_complete": False}


class HistoryRoute(APIRoute):
    def matches(self, scope):
        match, child = super().matches(scope)
        # Keep method rejection here instead of the legacy catch-all UI mount.
        return (Match.FULL if match == Match.PARTIAL else match), child

    async def handle(self, scope, receive, send):
        # Method rejection precedes the normal request handler.
        async def uncached_send(message):
            if message["type"] == "http.response.start":
                headers = [(key, value) for key, value in message.get("headers", []) if key.lower() != b"cache-control"]
                message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
            await send(message)
        try:
            await super().handle(scope, receive, uncached_send)
        except RoutingHTTPException as error:
            if error.status_code != 405:
                raise
            response = JSONResponse({"detail": error.detail}, status_code=405, headers=error.headers)
            await response(scope, receive, uncached_send)

    def get_route_handler(self):
        handler = super().get_route_handler()
        async def no_store(request):
            try:
                response = await handler(request)
            except RequestValidationError as exc:
                response = await request_validation_exception_handler(request, exc)
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
                raise
            response.headers["Cache-Control"] = "no-store"
            return response
        return no_store


history_router = APIRouter(route_class=HistoryRoute)


class HistoryParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_path: str = Field(min_length=1)
    view: str = "STRICT"
    period_kind: str
    period_start: date | None = None
    period_end: date | None = None
    limit: int = Field(default=300, ge=1, le=300)
    release: int | None = Field(default=None, gt=0)
    cursor: str | None = Field(default=None, max_length=8192)


class CatalogParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view: str = "STRICT"
    limit: int = Field(default=300, ge=1, le=300)
    release: int | None = Field(default=None, gt=0)
    cursor: str | None = Field(default=None, max_length=8192)


class SelectedHistoryParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: str = Field(min_length=1, max_length=8192)
    operation: str = "timeline"
    limit: int = Field(default=300, ge=1, le=300)
    cursor: str | None = Field(default=None, max_length=8192)


class SupportingParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: str = Field(min_length=1, max_length=8192)
    limit: int = Field(default=300, ge=1, le=300)
    cursor: str | None = Field(default=None, max_length=8192)


class SourceHistoryParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_item_id: int = Field(gt=0)
    limit: int = Field(default=300, ge=1, le=300)
    release: int | None = Field(default=None, gt=0)
    cursor: str | None = Field(default=None, max_length=8192)


def _history_call(method, **kwargs):
    try:
        return _call(lambda: getattr(query_repository(), method)(**kwargs))
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("code") == "QUERY_STORE_UNAVAILABLE":
            raise HTTPException(503, {"code": "QUERY_STORE_UNAVAILABLE", "message": "PostgreSQL history queries unavailable"}) from exc
        raise
    except HistoryNotFound as exc:
        raise HTTPException(404, "published history binding unavailable") from exc
    except (FigureHistoryConflict, ValueError, SQLAlchemyError) as exc:
        raise HTTPException(503, {"code": "HISTORY_NOT_READY", "message": "Exact history contract unavailable"}) from exc


def _history_query(params):
    try:
        return HistoryQuery(**params.model_dump(exclude={"release", "cursor"}))
    except InvalidQuery as exc:
        raise HTTPException(400, str(exc)) from exc


@history_router.get("/companies/{security_id}/figure-history")
def figure_history(security_id: int, params: Annotated[HistoryParameters, Query()]):
    return _history_call("figure_history", query=_history_query(params), security_id=security_id,
        release_id=params.release, cursor=params.cursor)


@history_router.get("/companies/{security_id}/figure-history/activations")
def figure_activations(security_id: int, params: Annotated[HistoryParameters, Query()]):
    return _history_call("figure_history", query=_history_query(params), security_id=security_id,
        release_id=params.release, cursor=params.cursor, activations=True)


@history_router.get("/companies/{security_id}/figure-history/figures")
def figure_catalog(security_id: int, params: Annotated[CatalogParameters, Query()]):
    return _history_call("figure_catalog", security_id=security_id, release_id=params.release,
        **params.model_dump(exclude={"release"}))


@history_router.get("/companies/{security_id}/figure-history/selected")
def figure_selected(security_id: int, params: Annotated[SelectedHistoryParameters, Query()]):
    return _history_call("figure_selected", security_id=security_id, **params.model_dump())


@history_router.get("/companies/{security_id}/figure-history/dependencies")
def figure_dependencies(security_id: int, params: Annotated[SupportingParameters, Query()]):
    return _history_call("figure_support", security_id=security_id, **params.model_dump())


@history_router.get("/companies/{security_id}/figure-history/source-events")
def figure_source_events(security_id: int, params: Annotated[SourceHistoryParameters, Query()]):
    return _history_call("figure_source_events", security_id=security_id,
        source_item_id=params.source_item_id, release_id=params.release, limit=params.limit, cursor=params.cursor)


router.include_router(history_router)
