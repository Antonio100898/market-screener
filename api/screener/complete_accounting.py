"""Declared complete input accounting; no security retirement or financial policy."""
from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .sources.sec_json import decode_object

Identity = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
Sha = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"^[^\x00]+$")]


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RetainedInput(_Record):
    key: Text
    artifact_sha256s: tuple[Sha, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_artifacts(self):
        if not self.key.strip() or len(set(self.artifact_sha256s)) != len(self.artifact_sha256s):
            raise ValueError("input requires a key and distinct retained artifacts")
        return self


class MemberBinding(_Record):
    security_id: Identity
    snapshot_id: Identity
    exact_bundle_id: Identity | None = None

    def identity(self):
        return self.security_id, self.snapshot_id, self.exact_bundle_id


class InputOutcome(_Record):
    input_key: Text
    status: Literal["SELECTED", "UNSUPPORTED", "UNRESOLVED", "DEFERRED"]
    selection: MemberBinding | None = None
    reason: Text | None = None

    @model_validator(mode="after")
    def outcome_shape(self):
        if self.status == "SELECTED":
            if self.selection is None or self.reason is not None:
                raise ValueError("selected input requires only its actual selection")
        elif self.selection is not None or self.reason is None or not self.reason.strip():
            raise ValueError("unselected input requires a reason and no security selection")
        return self


class SelectedMember(MemberBinding):
    disposition: Literal["NEW", "RETAINED", "REPLACED"]
    candidate_id: Identity | None = None

    @model_validator(mode="after")
    def candidate_shape(self):
        if (self.disposition == "RETAINED") != (self.candidate_id is None):
            raise ValueError("new/replaced member requires a candidate; retained member cannot carry one")
        return self


class CompleteAccounting(_Record):
    accounting_revision: Literal[1] = 1
    expected_parent_release_id: Identity | None
    engine_identity_sha256: Sha
    engine_identity_status: Literal["DECLARED_NOT_VERIFIED"] = "DECLARED_NOT_VERIFIED"
    universe: tuple[RetainedInput, ...] = Field(min_length=1)
    outcomes: tuple[InputOutcome, ...] = Field(min_length=1)
    parent_members: tuple[MemberBinding, ...]
    selected_members: tuple[SelectedMember, ...]
    retirements: tuple[Identity, ...] = ()

    @field_validator("accounting_revision", mode="before")
    @classmethod
    def revision_type(cls, value):
        if type(value) is not int:
            raise ValueError("accounting revision must be an integer")
        return value

    @model_validator(mode="after")
    def complete_sets(self):
        if self.retirements:
            raise ValueError("security retirement authority is unsupported")
        keys = [item.key for item in self.universe]
        outcomes = [item.input_key for item in self.outcomes]
        if len(set(keys)) != len(keys) or len(set(outcomes)) != len(outcomes) or set(keys) != set(outcomes):
            raise ValueError("each declared universe input requires exactly one outcome")
        parents = {member.security_id: member for member in self.parent_members}
        selected = {member.security_id: member for member in self.selected_members}
        if len(parents) != len(self.parent_members) or len(selected) != len(self.selected_members):
            raise ValueError("duplicate parent or selected security")
        if self.expected_parent_release_id is None and parents:
            raise ValueError("initial accounting cannot declare parent members")
        if not set(parents).issubset(selected):
            raise ValueError("complete accounting cannot omit a parent security")
        bindings = [outcome.selection.identity() for outcome in self.outcomes if outcome.selection is not None]
        if len(set(bindings)) != len(bindings) or set(bindings) != {member.identity() for member in self.selected_members}:
            raise ValueError("selected outputs require exactly one matching input outcome")
        candidates = [member.candidate_id for member in self.selected_members if member.candidate_id is not None]
        if len(set(candidates)) != len(candidates):
            raise ValueError("duplicate selected candidate")
        for security, member in selected.items():
            prior = parents.get(security)
            if (prior is None) != (member.disposition == "NEW"):
                raise ValueError("selected disposition differs from parent membership")
            if member.disposition == "RETAINED" and member.identity() != prior.identity():
                raise ValueError("retained member must keep its exact parent binding")
        return self

    def validate_publication(self, parent, memberships, candidates):
        if parent != self.expected_parent_release_id:
            raise ValueError("complete accounting expected parent differs")
        actual = {(m["security_id"], m["snapshot_id"], m.get("exact_bundle_id")) for m in memberships}
        if actual != {m.identity() for m in self.parent_members} or len(actual) != len(memberships):
            raise ValueError("complete accounting differs from actual locked parent")
        expected = {m.candidate_id: m.identity() for m in self.selected_members if m.candidate_id is not None}
        actual_candidates = {m["candidate_id"]: (m["security_id"], m["snapshot_id"], m.get("exact_bundle_id")) for m in candidates}
        if actual_candidates != expected or len(actual_candidates) != len(candidates):
            raise ValueError("complete accounting candidates differ from registered selection")

    def validate_display(self, members):
        for member in members:
            MemberBinding.model_validate_json(json.dumps({key: member.get(key) for key in
                ("security_id", "snapshot_id", "exact_bundle_id")}))
        actual = {(m["security_id"], m["snapshot_id"], m.get("exact_bundle_id")) for m in members}
        if not actual or actual != {m.identity() for m in self.selected_members} or len(actual) != len(members):
            raise ValueError("complete display requires exactly the nonempty accounted selection")

    def validate_artifacts(self, members):
        inputs = {item.key: set(item.artifact_sha256s) for item in self.universe}
        selected = {outcome.selection.security_id: inputs[outcome.input_key]
                    for outcome in self.outcomes if outcome.selection is not None}
        for member in members:
            required = {artifact["content_sha256"] for artifact in member["canonical_input_artifacts"]}
            if not required.issubset(selected[member["security_id"]]):
                raise ValueError("selected input omits actual canonical snapshot artifacts")


def load_accounting(raw: bytes) -> CompleteAccounting:
    return CompleteAccounting.model_validate_json(json.dumps(decode_object(raw, "complete accounting")))
