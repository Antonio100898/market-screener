"""Owner-only run lifecycle and persisted SSE replay; production dispatch stays closed."""
import asyncio
import json
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from .postgres import create_postgres_engine
from .research_api import PrivateResearchRoute, _call as workspace_call, current_owner
from .research_runs import ResearchRunRepository, RunConflict, TERMINAL
from .workspace_config import PresentationModel
from .workspaces import DisabledResearchUser


router = APIRouter(prefix="/research/workspaces/{workspace_id}/runs", route_class=PrivateResearchRoute)
STREAM_POLL_SECONDS = 0.5


@lru_cache(maxsize=1)
def run_repository():
    return ResearchRunRepository(create_postgres_engine())


def require_research_dispatch():
    raise HTTPException(503, "research execution is not configured")


class CreateRun(PresentationModel):
    expected_revision: int = Field(ge=1)
    release_id: int = Field(ge=1)
    request_key: str = Field(min_length=1, max_length=200, pattern=r"\S")
    request_text: str = Field(min_length=1, max_length=8000, pattern=r"\S")


def _call(operation, *args, **kwargs):
    try:
        return workspace_call(operation, *args, **kwargs)
    except RunConflict as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("")
def create_run(workspace_id: int, body: CreateRun, owner: int = Depends(current_owner),
               dispatch=Depends(require_research_dispatch), repository=Depends(run_repository)):
    return _call(repository.create, owner, workspace_id, **body.model_dump())


@router.get("/{run_id}")
def get_run(workspace_id: int, run_id: int, owner: int = Depends(current_owner), repository=Depends(run_repository)):
    return _call(repository.get, owner, workspace_id, run_id)


@router.post("/{run_id}/cancel")
def cancel_run(workspace_id: int, run_id: int, owner: int = Depends(current_owner), repository=Depends(run_repository)):
    return _call(repository.cancel, owner, workspace_id, run_id)


@router.get("/{run_id}/events")
def get_events(workspace_id: int, run_id: int, after: Annotated[int, Query(ge=0)] = 0,
               limit: Annotated[int, Query(ge=1, le=100)] = 50,
               owner: int = Depends(current_owner), repository=Depends(run_repository)):
    return _call(repository.events, owner, workspace_id, run_id, after=after, limit=limit)


def _resume_sequence(run_id, last_event_id):
    if last_event_id is None: return 0
    try:
        prior_run, sequence = last_event_id.split(":")
        if int(prior_run) != run_id or not sequence.isdecimal() or int(sequence) < 1:
            raise ValueError("mismatched cursor")
        return int(sequence)
    except (ValueError, AttributeError) as error:
        raise HTTPException(400, "Last-Event-ID must identify an event from this run") from error


@router.get("/{run_id}/stream")
async def stream_run(request: Request, workspace_id: int, run_id: int,
                     last_event_id: Annotated[str | None, Header(max_length=100)] = None,
                     owner: int = Depends(current_owner), repository=Depends(run_repository)):
    after = _resume_sequence(run_id, last_event_id)
    # Validate owner/cursor before starting HTTP200; each later page rechecks revocation.
    first = await run_in_threadpool(_call, repository.events, owner, workspace_id, run_id, after=after, limit=50)

    async def frames():
        nonlocal after
        batch = first
        idle = 0
        while not await request.is_disconnected():
            if batch is None:
                try:
                    batch = await run_in_threadpool(repository.events, owner, workspace_id, run_id, after=after, limit=50)
                except DisabledResearchUser:
                    yield 'event: denied\ndata: {"state":"DENIED"}\n\n'
                    return
                except Exception:
                    yield 'event: unavailable\ndata: {"message":"Research stream unavailable. Reconnect to resume."}\n\n'
                    return
            for event in batch["events"]:
                if await request.is_disconnected(): return
                yield f'id: {run_id}:{event["sequence"]}\nevent: lifecycle\ndata: {json.dumps(jsonable_encoder(event), separators=(",", ":"))}\n\n'
                after = event["sequence"]
                idle = 0
            if batch["run"]["state"] in TERMINAL and not batch["has_more"]: return
            more = batch["has_more"]
            batch = None
            if more: continue
            idle += 1
            if idle >= 20:
                yield ": heartbeat\n\n"
                idle = 0
            await asyncio.sleep(STREAM_POLL_SECONDS)

    return StreamingResponse(frames(), media_type="text/event-stream",
        headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no", "X-Robots-Tag": "noindex"})
