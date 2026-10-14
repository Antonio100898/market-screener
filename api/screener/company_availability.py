"""Retained input diagnoses, distinct from reported financial observations."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal, Mapping
from urllib.parse import quote

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


ARTIFACT_ROLE = "company_unavailability_assessment"
LOCAL_KIND = "LOCAL_RETAINED_INPUT_ASSESSMENT"
REMOTE_KIND = "CAPTURED_REMOTE_UNAVAILABLE_ASSESSMENT"
MAX_ASSESSMENT_BYTES = 1024 * 1024


class UnavailabilityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["LOCAL_RETAINED_INPUT_ASSESSMENT", "CAPTURED_REMOTE_UNAVAILABLE_ASSESSMENT"]
    issuer_source: Literal["SEC", "EDINET", "COMPANY_ADAPTER"]
    issuer_identifier: str = Field(min_length=1, max_length=100)
    ticker: str = Field(min_length=1, max_length=100)
    reason_code: Literal["LOCAL_INPUT_MISSING", "NORMALIZATION_UNSUPPORTED", "FILING_FACTS_PENDING", "REMOTE_SOURCE_UNAVAILABLE"]
    reason: str = Field(min_length=1, max_length=4096)
    assessed_at: AwareDatetime
    input_artifact_sha256s: tuple[str, ...] = ()
    remote_observation_ids: tuple[int, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def bounded_input(cls, value):
        if not isinstance(value, dict):
            raise ValueError("assessment must be an object")
        for key in ("input_artifact_sha256s", "remote_observation_ids"):
            items = value.get(key, ())
            if type(items) not in (list, tuple) or len(items) > 8192:
                raise ValueError("assessment exceeds the existing reference count frontier")
        for key, limit in (("issuer_identifier", 100), ("ticker", 100), ("reason", 4096)):
            text = value.get(key)
            if type(text) is not str or len(text) > limit or len(text.encode()) > 4096:
                raise ValueError("assessment text exceeds reference capacity")
        if any(type(item) is not str or len(item) != 64 for item in value.get("input_artifact_sha256s", ())):
            raise ValueError("assessment requires bounded artifact hashes")
        if any(type(item) is not int or not 0 < item < 2**63 for item in value.get("remote_observation_ids", ())):
            raise ValueError("assessment requires bounded exact observation IDs")
        return {**value, "input_artifact_sha256s": tuple(value.get("input_artifact_sha256s", ())),
                "remote_observation_ids": tuple(value.get("remote_observation_ids", ()))}

    @field_validator("assessed_at", mode="before")
    @classmethod
    def observed_time(cls, value):
        if type(value) is str and len(value) <= 64:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime):
            return value
        raise ValueError("assessment requires a bounded observed time")

    @field_validator("ticker", "reason", "issuer_identifier")
    @classmethod
    def text(cls, value):
        if not value.strip() or "\0" in value:
            raise ValueError("assessment requires nonempty bounded text")
        return value

    @model_validator(mode="after")
    def identity_and_basis(self):
        import re
        pattern = {"SEC": r"[0-9]{10}", "EDINET": r"E[0-9]{5}",
                   "COMPANY_ADAPTER": r"IFRS-ADIDAS-AG"}[self.issuer_source]
        if not re.fullmatch(pattern, self.issuer_identifier) or self.ticker != self.ticker.upper():
            raise ValueError("assessment issuer/ticker identity is invalid")
        if self.issuer_source == "COMPANY_ADAPTER" and self.ticker != "ADS.DE":
            raise ValueError("company adapter assessment must identify its configured ticker")
        if len(set(self.input_artifact_sha256s)) != len(self.input_artifact_sha256s):
            raise ValueError("duplicate assessment input artifact")
        if any(not re.fullmatch(r"[a-f0-9]{64}", digest) for digest in self.input_artifact_sha256s):
            raise ValueError("assessment requires exact artifact hashes")
        ids = self.remote_observation_ids
        if any(type(value) is not int or not 0 < value < 2**63 for value in ids) or len(set(ids)) != len(ids):
            raise ValueError("assessment requires distinct exact observation IDs")
        remote = self.kind == REMOTE_KIND
        if remote != (self.reason_code == "REMOTE_SOURCE_UNAVAILABLE") or remote != bool(ids):
            raise ValueError("local missing inputs are not remote unavailable observations")
        return self

    def json_record(self):
        return self.model_dump(mode="json")


def load_assessment(value) -> UnavailabilityAssessment:
    model = UnavailabilityAssessment.model_validate(value)
    raw = model.model_dump_json().encode()
    if len(raw) > MAX_ASSESSMENT_BYTES:
        raise ValueError("assessment exceeds the existing one-MiB reference frontier")
    return model


def unavailable_row(assessment, evidence_sha256, *, name=None, exchange=None):
    assessment = load_assessment(assessment.json_record() if isinstance(assessment, UnavailabilityAssessment) else assessment)
    import re
    if not isinstance(evidence_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", evidence_sha256):
        raise ValueError("unavailable record requires its diagnostic artifact")
    for value in (name, exchange):
        if value is not None and (type(value) is not str or len(value.encode()) > 4096 or "\0" in value):
            raise ValueError("invalid retained identity metadata")
    return dict(ticker=assessment.ticker, cik=assessment.issuer_identifier, name=name, exchange=exchange,
        data_availability=dict(status="UNAVAILABLE", assessment=assessment.json_record(), evidence_sha256=evidence_sha256),
        verdict="INDETERMINATE", n_pass=0,
        criteria=[dict(n=n, status="INSUFFICIENT_DATA", value=None, note=assessment.reason) for n in (1, 2, 3, 4, 5, 7)],
        context_notes=[assessment.reason],
        annual_eps={}, annual_net_income={}, price=None, shares=None, owner_earnings=None,
        reporting_currency=None, quote_currency=None, balance_sheet_date=None)


def unavailable_info(row):
    if "data_availability" not in row:
        return None
    info = row["data_availability"]
    if not isinstance(info, Mapping) or set(info) != {"status", "assessment", "evidence_sha256"} or info["status"] != "UNAVAILABLE":
        raise ValueError("invalid unavailable record")
    assessment = load_assessment(info["assessment"])
    unavailable_row(assessment, info["evidence_sha256"])
    return assessment, info["evidence_sha256"]


def validate_unavailable_row(row):
    info = unavailable_info(row)
    if info is None:
        return None
    assessment, digest = info
    expected = unavailable_row(assessment, digest, name=row.get("name"), exchange=row.get("exchange"))
    if row != expected or type(row.get("n_pass")) is not int:
        raise ValueError("unavailable data cannot contain financial figures or price ratios")
    if any(type(item.get("n")) is not int for item in row["criteria"]):
        raise ValueError("unavailable criterion identities must be exact integers")
    return info


def local_source_url(assessment):
    return (f"urn:market-screener:retained-input-assessment:{assessment.issuer_source}:"
            f"{assessment.issuer_identifier}:{quote(assessment.ticker, safe='')}")


def local_observation_metadata(assessment, digest):
    if assessment.kind != LOCAL_KIND:
        raise ValueError("remote observations cannot become local inventory evidence")
    return dict(kind=LOCAL_KIND, assessment=assessment.json_record(), diagnostic_artifact_sha256=digest)


def local_source_item(assessment):
    from .source_observations import SourceItemIdentity
    return SourceItemIdentity(assessment.issuer_source, "inventory",
        f"retained-input-assessment/{assessment.issuer_identifier}/{assessment.ticker}", assessment.issuer_identifier)


def admit_local_source_url(value, *, item, state, metadata, artifact, etag, last_modified, published_at):
    """Only an internal inventory assessment may use a non-fetchable URN."""
    if not isinstance(metadata, Mapping) or metadata.get("kind") != LOCAL_KIND:
        return None
    if set(metadata) != {"kind", "assessment", "diagnostic_artifact_sha256"}:
        raise ValueError("invalid local assessment metadata")
    assessment = load_assessment(metadata["assessment"])
    unavailable_row(assessment, metadata["diagnostic_artifact_sha256"])
    if (assessment.kind != LOCAL_KIND or item.item_kind != "inventory" or state != "present"
            or item.source_system != assessment.issuer_source
            or item.issuer_source_identifier != assessment.issuer_identifier
            or item.source_key != local_source_item(assessment).source_key
            or item.parent_source_item_id is not None
            or artifact != metadata["diagnostic_artifact_sha256"] or artifact is None
            or etag is not None or last_modified is not None or published_at is not None
            or value != local_source_url(assessment)):
        raise ValueError("local assessment is not a remote source or publication observation")
    return value


def validate_source_rows(assessment, digest, rows):
    """Publication supplies actual locked/current rows; no observation is invented."""
    if not rows or any(row["source_system"] != assessment.issuer_source
                       or row["issuer_source_identifier"] != assessment.issuer_identifier for row in rows):
        raise ValueError("unavailable evidence must identify its actual issuer")
    remote = set(assessment.remote_observation_ids)
    if remote:
        actual = {row["source_observation_id"] for row in rows if row["state"] == "unavailable"}
        if actual != remote or any(row["state"] not in {"present", "unavailable"} for row in rows):
            raise ValueError("remote unavailable diagnosis requires the actual unavailable observations")
    else:
        if any(row["state"] != "present" for row in rows):
            raise ValueError("local diagnosis cannot claim remote unavailability or withdrawal")
        matching = [row for row in rows if row["artifact_sha256"] == digest]
        if len(matching) != 1:
            raise ValueError("local diagnosis requires its recorded inventory artifact")
        row = matching[0]
        from .source_observations import SourceItemIdentity
        admitted = admit_local_source_url(row["source_url"],
            item=SourceItemIdentity(row["source_system"], row["item_kind"], row["source_key"], row["issuer_source_identifier"], row.get("parent_source_item_id")),
            state=row["state"], metadata=row["canonical_metadata"], artifact=row["artifact_sha256"],
            etag=row["etag"], last_modified=row["last_modified"], published_at=row["source_published_at"])
        if admitted is None or row["canonical_metadata"]["assessment"] != assessment.json_record():
            raise ValueError("recorded inventory does not match the unavailable diagnosis")


def validate_snapshot(row, artifacts, *, issuer_source, issuer_identifier, ticker):
    info = validate_unavailable_row(row)
    diagnostics = {item["content_sha256"] for item in artifacts if item["role"] == ARTIFACT_ROLE}
    if info is None:
        if diagnostics:
            raise ValueError("diagnostic artifacts require an unavailable record")
        return None
    assessment, digest = info
    if (assessment.issuer_source != issuer_source or assessment.issuer_identifier != issuer_identifier
            or assessment.ticker != ticker or diagnostics != {digest}):
        raise ValueError("unavailable snapshot identity/artifact binding differs")
    hashes = {item["content_sha256"] for item in artifacts}
    if hashes != {digest, *assessment.input_artifact_sha256s}:
        raise ValueError("unavailable snapshot must retain all actual assessment inputs")
    return info


def read_snapshot_info(connection, snapshot_id):
    """Read only the bounded diagnosis, never an available financial payload."""
    from sqlalchemy import Text, case, cast, func, select
    from .postgres import company_snapshot
    value = company_snapshot.c.canonical_payload["data_availability"]
    size = func.octet_length(cast(value, Text))
    length, info = connection.execute(select(size, case((size <= MAX_ASSESSMENT_BYTES, value), else_=None))
        .where(company_snapshot.c.snapshot_id == snapshot_id)).one()
    if length is not None and length > MAX_ASSESSMENT_BYTES:
        raise ValueError("unavailable diagnosis exceeds reference capacity")
    return unavailable_info({"data_availability": info}) if info is not None else None
