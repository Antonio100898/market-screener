"""Private durable lifecycle; provider dispatch and research tools are not registered."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import insert, select, text, update

from .durable_jobs import DurableJobRepository, LeaseLost, LeaseToken
from .postgres import data_release, durable_job, research_run, research_run_event
from .workspaces import (_active_user, _owned_workspace, _view, _digest, DisabledResearchUser,
                         WorkspaceConflict, WorkspaceNotFound)


RESEARCH_JOB_KIND = "private_research_run"
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "DENIED"}
_META = [research_run.c[key] for key in ("run_id", "workspace_id", "owner_id", "job_id", "state",
         "last_event_seq", "lease_generation", "initial_workspace_revision", "release_id", "created_at",
         "updated_at", "finished_at", "cancel_requested_at")]


class RunConflict(ValueError):
    pass


def _bounded_text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"text must be nonempty and at most {maximum} UTF-8 bytes")
    return value


def _positive(value):
    if type(value) is not int or value < 1:
        raise ValueError("identity/revision must be a positive integer")
    return value


class ResearchRunRepository:
    def __init__(self, engine):
        self.engine = engine
        self.jobs = DurableJobRepository(engine)

    def create(self, owner, workspace_id, *, expected_revision, release_id, request_key, request_text):
        _positive(expected_revision); _positive(release_id)
        _bounded_text(request_key, 200); _bounded_text(request_text, 8000)
        digest = _digest({"revision": expected_revision, "release": release_id, "text": request_text})
        with self.engine.begin() as connection:
            _active_user(connection, owner)
            workspace = _owned_workspace(connection, owner, workspace_id, lock=True)
            previous = connection.scalar(select(research_run.c.run_id).where(
                research_run.c.owner_id == owner, research_run.c.workspace_id == workspace_id,
                research_run.c.request_key == request_key))
            if previous is not None:
                run = self._lock(connection, owner, workspace_id, previous, account_locked=True)[0]
                stored = connection.scalar(select(research_run.c.request_sha256).where(research_run.c.run_id == previous))
                if stored != digest:
                    raise RunConflict("request key identifies a different research request")
                return dict(run)
            if workspace["edit_revision"] != expected_revision:
                raise WorkspaceConflict("workspace changed; reload before starting research", workspace["edit_revision"])
            if connection.scalar(select(data_release.c.release_id).where(data_release.c.release_id == release_id,
                    data_release.c.published_at.is_not(None))) is None:
                raise RunConflict("shared release is not published")
            now = datetime.now(timezone.utc)
            run_id = connection.scalar(text("SELECT nextval(pg_get_serial_sequence('research_run','run_id'))"))
            job = self.jobs._enqueue_on_connection(connection, schedule_key=f"research-run:{run_id}", scheduled_for=now,
                immutable={"kind": RESEARCH_JOB_KIND, "parameters": {"run_id": run_id}, "priority": 0,
                    "due_at": now, "max_attempts": 3, "deadline_at": None})
            run = connection.execute(insert(research_run).values(run_id=run_id, owner_id=owner, workspace_id=workspace_id,
                initial_workspace_revision=expected_revision, release_id=release_id, job_id=job["job_id"],
                request_key=request_key, request_sha256=digest, request_text=request_text, state="QUEUED")
                .returning(*_META)).mappings().one()
            return dict(self._append(connection, run, "created", "CREATED", {"state": "QUEUED"}))

    def get(self, owner, workspace_id, run_id):
        with self.engine.begin() as connection:
            run, job, _ = self._lock(connection, owner, workspace_id, run_id)
            run = self._reconcile(connection, run, job, revoked=False)
            return dict(run)

    def events(self, owner, workspace_id, run_id, *, after=0, limit=50):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("event cursor/limit is invalid")
        with self.engine.begin() as connection:
            run, job, _ = self._lock(connection, owner, workspace_id, run_id)
            run = self._reconcile(connection, run, job, revoked=False)
            if after > run["last_event_seq"]:
                raise RunConflict("event cursor is beyond this run")
            events = [dict(event) for event in connection.execute(select(research_run_event).where(
                research_run_event.c.run_id == run_id, research_run_event.c.sequence > after)
                .order_by(research_run_event.c.sequence).limit(limit)).mappings()]
            return {"run": dict(run), "events": events,
                "next_after": events[-1]["sequence"] if events else after,
                "has_more": bool(events and events[-1]["sequence"] < run["last_event_seq"])}

    def cancel(self, owner, workspace_id, run_id):
        with self.engine.begin() as connection:
            run, job, _ = self._lock(connection, owner, workspace_id, run_id)
            run = self._reconcile(connection, run, job, revoked=False)
            if run["state"] in TERMINAL or run["state"] == "CANCEL_REQUESTED":
                return dict(run)
            return dict(self._append(connection, run, "cancel", "CANCEL_REQUESTED", {"state": "CANCEL_REQUESTED"},
                state="CANCEL_REQUESTED", cancel_requested_at=datetime.now(timezone.utc)))

    def begin(self, lease: LeaseToken, run_id):
        with self.engine.begin() as connection:
            run = self._worker_lock(connection, lease, run_id)
            if run["state"] in TERMINAL or run["state"] == "CANCEL_REQUESTED":
                return dict(run)
            return dict(self._append(connection, run, f"started:{lease.generation}", "STARTED",
                {"state": "RUNNING"}, state="RUNNING", lease_generation=lease.generation))

    def context(self, lease: LeaseToken, run_id):
        with self.engine.begin() as connection:
            run = self._worker_lock(connection, lease, run_id)
            if run["state"] in TERMINAL or run["state"] == "CANCEL_REQUESTED":
                return {"run": dict(run), "context": None}
            workspace = _owned_workspace(connection, run["owner_id"], run["workspace_id"])
            configuration = _view(connection, workspace, run["owner_id"], run["initial_workspace_revision"])
            request_text = connection.scalar(select(research_run.c.request_text).where(research_run.c.run_id == run_id))
            self.jobs._lock_live_lease(connection, lease)
            return {"run": dict(run), "context": {"request_text": request_text, "workspace": configuration}}

    def tool_access(self, lease: LeaseToken, run_id):
        """Authorize a shared-data read without loading private request/configuration."""
        with self.engine.begin() as connection:
            run = self._worker_lock(connection, lease, run_id)
            if run["state"] != "RUNNING":
                return None
            if run["lease_generation"] != lease.generation:
                raise RunConflict("research run has not started under this worker generation")
            return dict(run)

    def progress(self, lease: LeaseToken, run_id, *, event_key, stage):
        _bounded_text(event_key, 200); _bounded_text(stage, 100)
        with self.engine.begin() as connection:
            run = self._worker_lock(connection, lease, run_id)
            if run["state"] in TERMINAL or run["state"] == "CANCEL_REQUESTED":
                return dict(run)
            if run["state"] != "RUNNING":
                raise RunConflict("research run has not started")
            return dict(self._append(connection, run, f"progress:{event_key}", "PROGRESS", {"stage": stage},
                lease_generation=lease.generation))

    def finish(self, lease: LeaseToken, run_id, *, failed=False):
        if type(failed) is not bool:
            raise ValueError("failed must be boolean")
        with self.engine.begin() as connection:
            run = self._worker_lock(connection, lease, run_id)
            if run["state"] in TERMINAL:
                return dict(run)
            if run["state"] not in {"RUNNING", "CANCEL_REQUESTED"}:
                raise RunConflict("research run has not started")
            state = "CANCELLED" if run["state"] == "CANCEL_REQUESTED" else "FAILED" if failed else "SUCCEEDED"
            return dict(self._terminal(connection, run, state, lease.generation))

    def reconcile(self, *, after=0, limit=50):
        """Called by service recovery; no private payload or provider dispatch is read."""
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("reconciliation limit must be between 1 and 100")
        with self.engine.connect() as connection:
            candidates = list(connection.execute(select(research_run.c.run_id, research_run.c.owner_id,
                research_run.c.workspace_id).where(research_run.c.state.not_in(TERMINAL), research_run.c.run_id > after)
                .order_by(research_run.c.run_id).limit(limit)).mappings())
        result = []
        for candidate in candidates:
            with self.engine.begin() as connection:
                run, job, revoked = self._lock(connection, candidate["owner_id"], candidate["workspace_id"],
                    candidate["run_id"], service=True)
                result.append(dict(self._reconcile(connection, run, job, revoked=revoked)))
        return {"runs": result, "next_after": candidates[-1]["run_id"] if len(candidates) == limit else 0}

    def _lock(self, connection, owner, workspace_id, run_id, *, account_locked=False, service=False, lease=None):
        revoked = False
        if not account_locked:
            try: _active_user(connection, owner)
            except DisabledResearchUser:
                if not service: raise
                revoked = True
            _owned_workspace(connection, owner, workspace_id, lock=True)
        locator = connection.execute(select(research_run.c.job_id).where(research_run.c.run_id == run_id,
            research_run.c.workspace_id == workspace_id, research_run.c.owner_id == owner)).first()
        if locator is None: raise WorkspaceNotFound("research run not found")
        if lease is not None:
            if lease.job_id != locator.job_id: raise LeaseLost("lease belongs to another research job")
            job = self.jobs._lock_live_lease(connection, lease)
        else:
            job = connection.execute(select(durable_job).where(durable_job.c.job_id == locator.job_id)
                .with_for_update()).mappings().one()
        if job["kind"] != RESEARCH_JOB_KIND or job["parameters"] != {"run_id": run_id}:
            raise RunConflict("research job identity is inconsistent")
        run = connection.execute(select(*_META).where(research_run.c.run_id == run_id,
            research_run.c.owner_id == owner, research_run.c.workspace_id == workspace_id).with_for_update()).mappings().one()
        if lease is not None:
            self.jobs._lock_live_lease(connection, lease)
        return run, job, revoked

    def _worker_lock(self, connection, lease, run_id):
        locator = connection.execute(select(research_run.c.owner_id, research_run.c.workspace_id)
            .where(research_run.c.run_id == run_id)).first()
        if locator is None: raise WorkspaceNotFound("research run not found")
        run, job, revoked = self._lock(connection, locator.owner_id, locator.workspace_id, run_id,
            service=True, lease=lease)
        if revoked and run["state"] not in TERMINAL:
            return self._terminal(connection, run, "DENIED", job["ownership_generation"])
        return run

    def _reconcile(self, connection, run, job, *, revoked):
        if run["state"] in TERMINAL: return run
        if revoked: return self._terminal(connection, run, "DENIED", job["ownership_generation"])
        if job["status"] in {"failed", "succeeded"}:
            return self._terminal(connection, run, "CANCELLED" if run["state"] == "CANCEL_REQUESTED" else "FAILED",
                job["ownership_generation"])
        return run

    def _terminal(self, connection, run, state, generation):
        return self._append(connection, run, "terminal", state, {"state": state}, state=state,
            lease_generation=generation, finished_at=datetime.now(timezone.utc))

    def _append(self, connection, run, key, kind, payload, **changes):
        previous = connection.execute(select(research_run_event).where(research_run_event.c.run_id == run["run_id"],
            research_run_event.c.event_key == key)).mappings().first()
        if previous is not None:
            if previous["kind"] != kind or previous["payload"] != payload:
                raise RunConflict("event key identifies different lifecycle content")
            return run
        sequence = run["last_event_seq"] + 1
        connection.execute(insert(research_run_event).values(run_id=run["run_id"], sequence=sequence,
            event_key=key, kind=kind, payload=payload, lease_generation=changes.get("lease_generation", run["lease_generation"])))
        return connection.execute(update(research_run).where(research_run.c.run_id == run["run_id"])
            .values(last_event_seq=sequence, updated_at=datetime.now(timezone.utc), **changes)
            .returning(*_META)).mappings().one()
