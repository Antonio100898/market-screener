"""Private workspace persistence; shared financial records are read-only to callers."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import insert, select, text, update
from sqlalchemy.engine import Engine

from .postgres import research_identity, research_user, research_workspace, research_workspace_revision
from .workspace_config import validate_config


class WorkspaceNotFound(LookupError):
    pass


class DisabledResearchUser(PermissionError):
    pass


class WorkspaceConflict(ValueError):
    def __init__(self, message: str, current_revision: int):
        super().__init__(message)
        self.current_revision = current_revision


@dataclass(frozen=True)
class VerifiedPrincipal:
    issuer: str
    subject: str

    def __post_init__(self):
        for value in (self.issuer, self.subject):
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError("issuer and subject must be nonempty bounded strings")


class WorkspaceRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def resolve_identity(self, principal: VerifiedPrincipal) -> int:
        identity = {"issuer": principal.issuer, "subject": principal.subject}
        digest = hashlib.sha256(_json(identity).encode()).digest()
        lock_key = int.from_bytes(digest[:8], "big", signed=True)
        with self.engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
            user_id = connection.scalar(select(research_identity.c.user_id).where(
                research_identity.c.issuer == principal.issuer,
                research_identity.c.subject == principal.subject,
            ))
            if user_id is None:
                user_id = connection.scalar(insert(research_user).returning(research_user.c.user_id))
                connection.execute(insert(research_identity).values(**identity, user_id=user_id))
            _active_user(connection, user_id)
            return user_id

    def default(self, owner_id: int) -> dict:
        configuration = validate_config({})
        with self.engine.begin() as connection:
            _active_user(connection, owner_id)
            workspace = connection.execute(select(research_workspace).where(
                research_workspace.c.owner_id == owner_id
            )).mappings().first()
            if workspace is None:
                workspace = connection.execute(insert(research_workspace).values(
                    owner_id=owner_id, schema_revision=1, edit_revision=1,
                ).returning(research_workspace)).mappings().one()
                connection.execute(insert(research_workspace_revision).values(
                    workspace_id=workspace["workspace_id"], owner_id=owner_id,
                    edit_revision=1, schema_revision=1, configuration=configuration,
                    request_key="initial", request_sha256=_digest({"initial": configuration}), actor="SYSTEM",
                ))
            return _view(connection, workspace, owner_id)

    def get(self, owner_id: int, workspace_id: int) -> dict:
        with self.engine.begin() as connection:
            _active_user(connection, owner_id)
            workspace = _owned_workspace(connection, owner_id, workspace_id)
            return _view(connection, workspace, owner_id)

    def replace(self, owner_id: int, workspace_id: int, *, expected_revision: int,
                configuration: dict, request_key: str, actor: str = "USER") -> dict:
        configuration = validate_config(configuration)
        _validate_edit_request(expected_revision, request_key)
        if actor not in ("USER", "AGENT"):
            raise ValueError("unsupported edit actor")
        request_digest = _digest({"expected_revision": expected_revision,
                                  "configuration": configuration, "actor": actor})
        with self.engine.begin() as connection:
            _active_user(connection, owner_id)
            workspace = _owned_workspace(connection, owner_id, workspace_id, lock=True)
            return self._append_on_connection(connection, workspace, owner_id,
                expected_revision=expected_revision, configuration=configuration,
                request_key=request_key, request_digest=request_digest, actor=actor)

    def restore(self, owner_id: int, workspace_id: int, *, expected_revision: int,
                source_revision: int, request_key: str) -> dict:
        _validate_edit_request(expected_revision, request_key)
        if type(source_revision) is not int or source_revision < 1:
            raise ValueError("source_revision must be a positive integer")
        with self.engine.begin() as connection:
            _active_user(connection, owner_id)
            workspace = _owned_workspace(connection, owner_id, workspace_id, lock=True)
            source = _revision(connection, workspace_id, owner_id, source_revision)
            configuration = validate_config(source["configuration"])
            request_digest = _digest({"operation": "RESTORE", "expected_revision": expected_revision,
                                      "source_revision": source_revision,
                                      "configuration": configuration, "actor": "USER"})
            return self._append_on_connection(connection, workspace, owner_id,
                expected_revision=expected_revision, configuration=configuration,
                request_key=request_key, request_digest=request_digest, actor="USER",
                restored_from_revision=source_revision)

    def _append_on_connection(self, connection, workspace, owner_id, *, expected_revision,
                              configuration, request_key, request_digest, actor,
                              restored_from_revision=None):
        workspace_id = workspace["workspace_id"]
        previous = connection.execute(select(research_workspace_revision).where(
            research_workspace_revision.c.owner_id == owner_id,
            research_workspace_revision.c.workspace_id == workspace_id,
            research_workspace_revision.c.request_key == request_key,
        )).mappings().first()
        if previous:
            if previous["request_sha256"] != request_digest:
                raise WorkspaceConflict("request key was used for a different edit", workspace["edit_revision"])
            return _view(connection, workspace, owner_id, previous["edit_revision"])
        if workspace["edit_revision"] != expected_revision:
            raise WorkspaceConflict("workspace changed; reload before editing", workspace["edit_revision"])
        revision = expected_revision + 1
        connection.execute(insert(research_workspace_revision).values(
            workspace_id=workspace_id, owner_id=owner_id, edit_revision=revision,
            schema_revision=1, configuration=configuration, request_key=request_key,
            request_sha256=request_digest, actor=actor,
            restored_from_revision=restored_from_revision,
        ))
        workspace = connection.execute(update(research_workspace).where(
            research_workspace.c.workspace_id == workspace_id,
            research_workspace.c.owner_id == owner_id,
        ).values(edit_revision=revision, updated_at=text("CURRENT_TIMESTAMP"))
            .returning(research_workspace)).mappings().one()
        return _view(connection, workspace, owner_id)

    def history(self, owner_id: int, workspace_id: int, *, before: int | None = None, limit: int = 50) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("history limit must be between 1 and 100")
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError("history cursor must be a positive revision")
        with self.engine.begin() as connection:
            _active_user(connection, owner_id)
            _owned_workspace(connection, owner_id, workspace_id)
            statement = select(research_workspace_revision).where(
                research_workspace_revision.c.owner_id == owner_id,
                research_workspace_revision.c.workspace_id == workspace_id,
            )
            if before is not None:
                statement = statement.where(research_workspace_revision.c.edit_revision < before)
            return [dict(row) for row in connection.execute(statement.order_by(
                research_workspace_revision.c.edit_revision.desc()
            ).limit(limit)).mappings()]


def _active_user(connection, owner_id):
    status = connection.scalar(select(research_user.c.status).where(
        research_user.c.user_id == owner_id
    ).with_for_update())
    if status != "ACTIVE":
        raise DisabledResearchUser("research account is unavailable")


def _owned_workspace(connection, owner_id, workspace_id, *, lock=False):
    statement = select(research_workspace).where(
        research_workspace.c.owner_id == owner_id,
        research_workspace.c.workspace_id == workspace_id,
    )
    if lock:
        statement = statement.with_for_update()
    result = connection.execute(statement).mappings().first()
    if result is None:
        raise WorkspaceNotFound("workspace not found")
    return result


def _validate_edit_request(expected_revision, request_key):
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("expected_revision must be a positive integer")
    if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 200:
        raise ValueError("request_key must be a nonempty string of at most 200 characters")


def _revision(connection, workspace_id, owner_id, selected_revision):
    stored = connection.execute(select(research_workspace_revision).where(
        research_workspace_revision.c.workspace_id == workspace_id,
        research_workspace_revision.c.owner_id == owner_id,
        research_workspace_revision.c.edit_revision == selected_revision,
    )).mappings().first()
    if stored is None:
        raise WorkspaceNotFound("workspace revision not found")
    return stored


def _view(connection, workspace, owner_id, revision=None):
    selected_revision = workspace["edit_revision"] if revision is None else revision
    stored = _revision(connection, workspace["workspace_id"], owner_id, selected_revision)
    return {"workspace_id": workspace["workspace_id"], "name": workspace["name"],
            "schema_revision": stored["schema_revision"], "edit_revision": selected_revision,
            "configuration": stored["configuration"], "created_at": workspace["created_at"],
            "updated_at": stored["created_at"], "restored_from_revision": stored["restored_from_revision"]}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()
