"""Bounded storage contract for the dashboard's existing presentation choices."""
from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


COLUMN_IDS = ("ticker", "name", "sector", "eps10", "returns", "mcap", "price",
              "offhigh", "pe", "pe3", "dividend")
ColumnId = Literal["ticker", "name", "sector", "eps10", "returns", "mcap", "price",
                   "offhigh", "pe", "pe3", "dividend"]
Label = Annotated[str, Field(min_length=1, max_length=120)]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class PresentationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WorkspaceSort(PresentationModel):
    key: ColumnId | Literal["fit", "n_pass"]
    dir: Literal[-1, 1] = 1

    @field_validator("dir", mode="before")
    @classmethod
    def integer_direction(cls, value):
        if type(value) is not int:
            raise ValueError("sort direction must be 1 or -1")
        return value


class WorkspaceView(PresentationModel):
    q: str = Field(default="", max_length=1000)
    gaps: Literal["ALL", "ALL_PASS", "ONE_GAP", "TWO_GAPS", "INCOMPLETE"] = "ALL"
    lens: Literal["BOTH", "ENTERPRISING", "DEFENSIVE"] = "BOTH"
    fit: Literal["ALL", "ALIGNED", "EVIDENCE_INCOMPLETE", "BLOCKED"] = "ALL"
    profiles: list[Label] = Field(default_factory=list, max_length=100)
    sectors: list[Label] = Field(default_factory=list, max_length=100)
    venues: list[Label] = Field(default_factory=list, max_length=100)
    indexes: list[Label] = Field(default_factory=list, max_length=100)
    minCap: Nonnegative = 0
    minMet: int = Field(default=0, ge=0, le=6)
    minPositiveEps: int = Field(default=0, ge=0, le=10)
    maxPe: Nonnegative = 0
    maxPe3: Nonnegative = 0
    trackedOnly: bool = False
    hideNA: bool = False
    hideNoApply: bool = False
    belowNcav: bool = False
    sort: list[WorkspaceSort] = Field(
        default_factory=lambda: [WorkspaceSort(key="fit", dir=1)], min_length=1, max_length=13
    )

    @model_validator(mode="after")
    def unique_selections(self):
        for name in ("profiles", "sectors", "venues", "indexes"):
            labels = getattr(self, name)
            if len(labels) != len(set(labels)):
                raise ValueError(f"duplicate {name}")
        keys = [item.key for item in self.sort]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate sort keys")
        return self


class WorkspaceConfig(PresentationModel):
    schema_revision: Literal[1] = 1
    view: WorkspaceView = Field(default_factory=WorkspaceView)
    columns: list[ColumnId] = Field(default_factory=lambda: list(COLUMN_IDS), max_length=11)

    @field_validator("schema_revision", mode="before")
    @classmethod
    def integer_schema(cls, value):
        if type(value) is not int:
            raise ValueError("schema_revision must be 1")
        return value

    @model_validator(mode="after")
    def bounded_configuration(self):
        if len(self.columns) != len(set(self.columns)):
            raise ValueError("duplicate columns")
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode()) > 65536:
            raise ValueError("workspace configuration exceeds 64 KiB")
        return self


def validate_config(value: dict) -> dict:
    return WorkspaceConfig.model_validate(value).model_dump(mode="json")
