"""Private research routes. A verified identity adapter must be supplied by the server."""
from functools import lru_cache
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as RoutingHTTPException
from starlette.routing import Match, get_route_path

from .postgres import create_postgres_engine
from .workspace_config import PresentationModel, WorkspaceConfig
from .workspaces import (DisabledResearchUser, VerifiedPrincipal, WorkspaceConflict,
                         WorkspaceNotFound, WorkspaceRepository)

logger = logging.getLogger(__name__)


class PrivateResearchRoute(APIRoute):
    def matches(self, scope):
        match, child_scope = super().matches(scope)
        if match == Match.PARTIAL:
            # A separate GET/PUT route may still accept this named path.
            for route in iter_route_contexts(scope["router"].routes):
                if (isinstance(route.original_route, PrivateResearchRoute)
                        and scope["method"] in route.methods
                        and route.path_regex.match(get_route_path(scope))):
                    return match, child_scope
            return Match.FULL, child_scope
        return match, child_scope

    async def handle(self, scope, receive, send):
        # Method rejection happens before the normal private request handler.
        async def private_send(message):
            if message["type"] == "http.response.start":
                headers = [(key, value) for key, value in message.get("headers", []) if key.lower() != b"cache-control"]
                message = {**message, "headers": [*headers, (b"cache-control", b"private, no-store")]}
            await send(message)
        try:
            await super().handle(scope, receive, private_send)
        except RoutingHTTPException as error:
            if error.status_code != 405:
                raise
            response = JSONResponse({"detail": error.detail}, status_code=405, headers=error.headers)
            await response(scope, receive, private_send)

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_response(request):
            try:
                response = await handler(request)
            except HTTPException as error:
                response = JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=error.headers)
            except RequestValidationError as error:
                response = JSONResponse({"detail": jsonable_encoder(error.errors())}, status_code=422)
            except SQLAlchemyError:
                response = JSONResponse({"detail": "private workspace storage is unavailable"}, status_code=503)
            except Exception as error:
                logger.error("Private research request failed (%s)", type(error).__name__)
                response = JSONResponse({"detail": "private workspace request failed"}, status_code=500)
            response.headers["Cache-Control"] = "private, no-store"
            return response

        return private_response


router = APIRouter(prefix="/research/workspaces", route_class=PrivateResearchRoute)


def require_principal() -> VerifiedPrincipal:
    raise HTTPException(401, "verified sign-in is required")


@lru_cache(maxsize=1)
def workspace_repository() -> WorkspaceRepository:
    return WorkspaceRepository(create_postgres_engine())


def current_owner(principal: VerifiedPrincipal = Depends(require_principal),
                  repository: WorkspaceRepository = Depends(workspace_repository)) -> int:
    try:
        return repository.resolve_identity(principal)
    except DisabledResearchUser as error:
        raise HTTPException(403, str(error)) from error


class WorkspaceEdit(PresentationModel):
    expected_revision: int = Field(ge=1)
    request_key: str = Field(min_length=1, max_length=200, pattern=r"\S")
    configuration: WorkspaceConfig


class WorkspaceRestore(PresentationModel):
    expected_revision: int = Field(ge=1)
    source_revision: int = Field(ge=1)
    request_key: str = Field(min_length=1, max_length=200, pattern=r"\S")


def _call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except WorkspaceNotFound as error:
        raise HTTPException(404, str(error)) from error
    except DisabledResearchUser as error:
        raise HTTPException(403, str(error)) from error
    except WorkspaceConflict as error:
        raise HTTPException(409, {"message": str(error), "current_revision": error.current_revision}) from error


@router.get("/default")
def default_workspace(owner: int = Depends(current_owner), repository: WorkspaceRepository = Depends(workspace_repository)):
    return _call(repository.default, owner)


@router.get("/{workspace_id}")
def get_workspace(workspace_id: int, owner: int = Depends(current_owner), repository: WorkspaceRepository = Depends(workspace_repository)):
    return _call(repository.get, owner, workspace_id)


@router.put("/{workspace_id}")
def edit_workspace(workspace_id: int, edit: WorkspaceEdit, owner: int = Depends(current_owner), repository: WorkspaceRepository = Depends(workspace_repository)):
    return _call(repository.replace, owner, workspace_id, expected_revision=edit.expected_revision,
                 request_key=edit.request_key, configuration=edit.configuration.model_dump(mode="json"))


@router.post("/{workspace_id}/restore")
def restore_workspace(workspace_id: int, edit: WorkspaceRestore,
                      owner: int = Depends(current_owner), repository: WorkspaceRepository = Depends(workspace_repository)):
    return _call(repository.restore, owner, workspace_id, expected_revision=edit.expected_revision,
                 source_revision=edit.source_revision, request_key=edit.request_key)


@router.get("/{workspace_id}/revisions")
def workspace_history(workspace_id: int, before: Annotated[int | None, Query(ge=1)] = None,
                      limit: Annotated[int, Query(ge=1, le=100)] = 50,
                      owner: int = Depends(current_owner), repository: WorkspaceRepository = Depends(workspace_repository)):
    rows = _call(repository.history, owner, workspace_id, before=before, limit=limit)
    return {"revisions": rows, "next_before": rows[-1]["edit_revision"] if len(rows) == limit else None}
