"""Captured SEC metadata; no acquisition or financial selection."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field, StrictInt, model_validator, field_validator

from .models import CapturedSource
from .complete_accounting import Identity, Sha, Text, _Record
from .artifacts import read_registered_artifact
from .sources.sec_json import decode_object
from .sec_ingestion import _validate_submissions, _recent_filing_metadata
from .primary_input_plan import _captured
from .sync import _submissions_identity, material_events
from .sectors import sector_for

MAX_BODY_BYTES = 8 * 1024 * 1024
QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A", "6-K", "6-K/A"})


class Delivery(_Record):
    occurrence_id: Identity
    source_item_id: Identity
    source_observation_id: Identity
    job_id: Identity
    ownership_generation: Identity
    item_key: Text
    checked_at: AwareDatetime


class SourceRef(_Record):
    observation_sha256: Sha
    item_kind: Literal["inventory", "aggregate", "filing", "resource"]
    source_key: Text
    parent_source_item_id: Identity | None
    role: Text | None
    artifact_sha256: Sha | None
    artifact_byte_size: StrictInt | None = Field(ge=0)
    captured_source: CapturedSource | None
    source_url: Text
    state: Literal["present", "pending", "unavailable", "removed"]
    delivery: Delivery

    @field_validator("captured_source", mode="before")
    @classmethod
    def exact_captured_fields(cls, value):
        return _captured(value) if value is not None else None

    @model_validator(mode="after")
    def captured_identity(self):
        if (self.artifact_sha256 is None) != (self.artifact_byte_size is None):
            raise ValueError("source artifact identity requires its retained byte size")
        source = self.captured_source
        if source is not None:
            if (source.role != self.role or source.artifact_sha256 != self.artifact_sha256
                    or source.source_document != self.source_url
                    or any(type(value) is not int for value in (source.source_item_id, source.source_observation_id, source.occurrence_id))
                    or (source.source_item_id, source.source_observation_id, source.occurrence_id) !=
                       (self.delivery.source_item_id, self.delivery.source_observation_id, self.delivery.occurrence_id)
                    or source.fidelity not in {"EXACT_LEXICAL", "DECODED_DECIMAL", "UNKNOWN"}):
                raise ValueError("original CapturedSource differs from pinned delivery/source")
        return self


class SubmissionsRef(_Record):
    source: SourceRef
    accession: Text
    form: Text
    filing_date: Text


class MetadataHeader(_Record):
    name: str | None
    sic: StrictInt | str | None
    sicDescription: str | None
    category: str | None


class MaterialEvent(_Record):
    filed: Text
    item: Text
    accn: Text


class Quarterly(_Record):
    form: Literal["10-Q", "10-Q/A", "6-K", "6-K/A"]
    filed: Text
    period: Text | None
    accession: Text
    document: Text | None
    url: Text


class MetadataResult(_Record):
    source: SubmissionsRef
    header: MetadataHeader
    incorporation: str | None
    incorporation_description: str | None
    first_visible_filing: Text | None
    last_visible_filing: Text | None
    sector: str | None
    material_events: tuple[MaterialEvent, ...]
    material_events_scanned_since: Text | None
    quarterly_filing: Quarterly | None
    coverage: Literal["CAPTURED_RECENT_INDEX"]
    listing_status: Literal["NOT_ASSESSED"]


class MetadataArtifact(_Record):
    contract: Literal["CAPTURED_SEC_METADATA1"]
    resolver_build_sha256: Sha
    semantic_verification: Literal["MINTED_CHECKPOINT_TRUST_ONLY"]
    result: MetadataResult


def verify_source(ref, issuer_source, issuer_identifier, observations):
    stored = observations.by_sha256(ref.observation_sha256)
    item, obs = stored.item, stored.observation
    if (item.source_system != issuer_source or item.issuer_source_identifier != issuer_identifier
            or item.item_kind != ref.item_kind or item.source_key != ref.source_key
            or item.parent_source_item_id != ref.parent_source_item_id
            or (obs.canonical_metadata.get("role") != ref.role
                and not (item.item_kind == "filing" and ref.role == "filing"))
            or item.source_item_id != ref.delivery.source_item_id
            or obs.source_observation_id != ref.delivery.source_observation_id
            or obs.source_item_id != ref.delivery.source_item_id
            or obs.observation_sha256 != ref.observation_sha256
            or obs.artifact_sha256 != ref.artifact_sha256
            or obs.state != ref.state or obs.source_url != ref.source_url):
        raise ValueError("source identity differs from pinned issuer/observation")
    delivery = observations.delivery(ref.delivery.occurrence_id,
        source_item_id=ref.delivery.source_item_id, source_observation_id=ref.delivery.source_observation_id)
    if Delivery.model_validate_json(json.dumps(asdict(delivery), default=str)) != ref.delivery:
        raise ValueError("source delivery differs from pinned seven-field occurrence")
    if ref.role == "company_facts" and ref.captured_source is not None and ref.captured_source.fidelity == "EXACT_LEXICAL":
        if type(obs.canonical_metadata.get("capture_revision")) is not int or obs.canonical_metadata.get("capture_revision") != 2 or obs.canonical_metadata.get("capture_fidelity") != "RAW_HTTP_BYTES":
            raise ValueError("exact lexical captured source lacks raw HTTP admission")
    return stored


def latest_quarterly_from_submissions(cik: str, submissions: dict) -> dict | None:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    fields = (recent.get("form") or [], recent.get("filingDate") or [],
              recent.get("reportDate") or [], recent.get("accessionNumber") or [],
              recent.get("primaryDocument") or [])
    candidates = []
    for form, filed, period, accession, document in zip(*fields):
        if form not in QUARTERLY_FORMS or not accession or not filed:
            continue
        candidates.append({
            "form": form, "filed": filed, "period": period or None,
            "accession": accession, "document": document or None,
        })
    if not candidates:
        return None
    latest = max(candidates, key=lambda item: (item["filed"], item["accession"]))
    if str(cik).isdigit():
        latest["url"] = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{latest['accession'].replace('-', '')}/{latest['accession']}-index.htm"
        )
    return latest



def resolve_submissions(ref, issuer_identifier, *, artifacts, objects, observations, payloads):
    stored = verify_source(ref.source, "SEC", issuer_identifier, observations)
    if ref.source.state != "present" or ref.source.artifact_sha256 is None:
        raise ValueError("submissions metadata requires a present retained response")
    artifact, raw = read_registered_artifact(ref.source.artifact_sha256, objects, artifacts,
        maximum=MAX_BODY_BYTES, label="captured submissions", verified_payloads=payloads)
    if artifact.byte_size != ref.source.artifact_byte_size:
        raise ValueError("submissions byte size differs from pinned source")
    if stored.observation.canonical_metadata.get("role") != "submissions":
        raise ValueError("metadata source is not a captured submissions root")
    _validate_submissions(raw, ref.accession, issuer_identifier, ref.form, ref.filing_date)
    data = decode_object(raw, "captured submissions")
    selected_filing = _recent_filing_metadata(data, ref.accession)
    if selected_filing is None or selected_filing["form"] != ref.form or selected_filing["filed"] != ref.filing_date:
        raise ValueError("captured metadata reference lacks its exact filing/date association")
    recent = data["filings"]["recent"]
    fields = ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument", "items")
    arrays = [recent.get(key) for key in fields]
    if any(not isinstance(a, list) or any(type(v) is not str for v in a) for a in arrays) or len({len(a) for a in arrays}) != 1:
        raise ValueError("captured submissions metadata arrays are incomplete or misaligned")
    for key in ("filingDate", "reportDate"):
        for value in recent[key]:
            if value:
                if len(value) != 10 or date.fromisoformat(value).isoformat() != value:
                    raise ValueError("captured metadata dates require calendar dates")
    files = data["filings"].get("files", [])
    if not isinstance(files, list) or any(not isinstance(f, dict) for f in files):
        raise ValueError("captured submissions history files are malformed")
    for file in files:
        value = file.get("filingFrom")
        if value is not None and (type(value) is not str or date.fromisoformat(value).isoformat() != value):
            raise ValueError("captured submissions history date is malformed")
    for key in ("stateOfIncorporation", "stateOfIncorporationDescription"):
        if data.get(key) is not None and type(data[key]) is not str:
            raise ValueError("captured metadata identity is malformed")
    sic = data.get("sic")
    if sic is not None and (type(sic) not in (str, int) or isinstance(sic, bool)):
        raise ValueError("captured metadata SIC is malformed")
    header = {}
    for name in ("name", "sicDescription", "category"):
        value = data.get(name)
        if value is not None and type(value) is not str:
            raise ValueError("captured metadata header is malformed")
        header[name] = value
    header["sic"] = sic
    events, scanned_since = material_events(data)
    identity = _submissions_identity(data)
    result = dict(source=ref.model_dump(mode="json"), header=header, incorporation=identity[0],
        incorporation_description=identity[1], first_visible_filing=identity[2], last_visible_filing=identity[3],
        sector=sector_for(sic), material_events=events, material_events_scanned_since=scanned_since,
        quarterly_filing=latest_quarterly_from_submissions(issuer_identifier, data),
        coverage="CAPTURED_RECENT_INDEX", listing_status="NOT_ASSESSED")
    return MetadataResult.model_validate_json(json.dumps(result)).model_dump(mode="json")
