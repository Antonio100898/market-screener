"""Finite retained SEC inventory and exact requests; no financial selection."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue, StrictInt, model_validator

from .artifacts import read_registered_artifact, store_evidence
from .captured_metadata import SourceRef
from .complete_accounting import Identity, Sha, Text, _Record
from .durable_jobs import _utc_datetime
from .durable_jobs import _bounded_schedule_parameters, _bounded_schedule_text, _schedule_datetime
from .sec_ingestion import (
    SecDiscoveryStopped, SecRecentDiscoveryHandler, SecResourceFetchHandler, SecCandidateStageHandler, SecIndexFiling,
    _capture_job_stamp, _capture_sha,
    _SUBMISSION_PART_FINGERPRINT_BUDGET, _submission_part_source_hashes, _submission_part_producer_encoding,
    _capture_result_json, _capture_text, _date_range, _finite_transport_texts,
    _resource_parameters, parse_all_form_index, resource_child_key_for_occurrence, resource_route, validate_finite_daily_source,
)
from .source_observations import SourceItemIdentity
from .sources.edgar import NoXbrlDataError, TICKER_MAP_URL
from .sources.sec_json import decode_object, _escape

KIND = "sec-finite-inventory-v1"
PARTIAL_KIND = "sec-finite-inventory-v2"
NAMESPACE = f"automatic-ingestion:v1:{KIND}:"
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_ENVELOPE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 100000
MAX_CHUNKS = 1024
MAX_ENTRY_BYTES = 128 * 1024
MAX_SOURCE_TEXT_BYTES = 2048
# Scalar names, hashes, bigint widths, timestamps and exact child-key grammar.
REQUEST_FIXED_BYTES = 2304
MEDIA_TYPE = "application/vnd.market-screener.sec-inventory+json"


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def load(model, raw, *, record_count=None, index_start=None):
    if type(raw) is not bytes or len(raw) > MAX_ENVELOPE_BYTES:
        raise ValueError("finite inventory envelope exceeds1MiB")
    value = decode_object(raw, "finite SEC inventory")
    if record_count is not None:
        if (type(record_count) is not int or not 0 < record_count <= MAX_RECORDS
                or type(value.get("records")) is not list or len(value["records"]) != record_count):
            raise ValueError("finite selected chunk count differs before entry factories")
    if index_start is not None:
        if record_count is None or type(index_start) is not int or not 0 <= index_start < MAX_RECORDS:
            raise ValueError("selected day chunk requires a bounded original start/count")
        for offset, record in enumerate(value["records"]):
            if (type(record) is not dict or record.get("type") != "INDEX"
                    or type(record.get("ordinal")) is not int or record["ordinal"] != index_start+offset
                    or record["ordinal"] >= MAX_RECORDS):
                raise ValueError("selected day chunk INDEX/ordinal differs before entry factories")
    return model.model_validate_json(encode(value))


_BUILD_FILES = ("sec_inventory_plan.py", "sec_ingestion.py", "durable_jobs.py", "automatic_ingestion.py",
    "source_observations.py", "source_validity.py", "artifacts.py", "object_store.py", "job_runtime.py",
    "captured_metadata.py", "complete_accounting.py", "sources/edgar.py", "sources/sec_json.py", "sources/sec_submission.py")


def _source_hashes():
    root = Path(__file__).parent
    budget = _SUBMISSION_PART_FINGERPRINT_BUDGET.get()
    if budget is not None:
        entries = tuple((name, root / name) for name in _BUILD_FILES)
        return _submission_part_source_hashes(entries, budget, owner_kind='producer', encode_result=False)
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in _BUILD_FILES}


_IMPORTED_SOURCES = _source_hashes()


def producer_build_sha256():
    current = _source_hashes()
    budget = _SUBMISSION_PART_FINGERPRINT_BUDGET.get()
    if budget is not None:
        _submission_part_producer_encoding(current, _IMPORTED_SOURCES, budget)
    if current != _IMPORTED_SOURCES:
        raise ValueError("finite producer sources changed since import; restart with the executing build")
    return hashlib.sha256(encode(current)).hexdigest()


class InventoryParameters(_Record):
    request_revision: StrictInt = Field(ge=1, le=1)
    operation_key: Text
    operation_at: AwareDatetime
    start_date: str
    end_date: str
    expected_parent_release_id: Identity | None
    producer_build_sha256: Sha

    @model_validator(mode="after")
    def identities(self):
        _date_range(dict(start_date=self.start_date, end_date=self.end_date))
        if self.operation_key != self.operation_key.strip() or len(self.operation_key.encode("utf-8")) > 512:
            raise ValueError("finite operation key must be canonical text within512 bytes")
        _utc_datetime("finite operation_at", self.operation_at)
        return self


def validate_parameters(value):
    return load(InventoryParameters, encode(value))


def validate_partial_parameters(value):
    detached, canonical = _bounded_schedule_parameters(value)
    if type(detached.get("request_revision")) is not int or detached["request_revision"] != 2:
        raise ValueError("finite-v2 requires exact request_revision2")
    return load(InventoryParameters2, canonical.encode("utf-8"))


class ProducerRequest(_Record):
    contract: Literal["SEC_INVENTORY_REQUEST1"]
    producer_job_id: Identity
    producer_occurrence_id: Identity
    priority: StrictInt = Field(ge=-2147483648, le=2147483647)
    parameters: InventoryParameters


class AcquisitionRef(_Record):
    receipt_key: Text
    source_system: Literal["SEC"]
    issuer_source_identifier: Text | None
    source: SourceRef
    etag: str | None
    last_modified: str | None
    media_type: str | None
    headers_captured: bool

    @model_validator(mode="after")
    def bounds(self):
        if len(self.receipt_key.encode("utf-8")) > 512:
            raise ValueError("finite receipt key exceeds512 bytes")
        for value in (self.source.source_url, self.etag, self.last_modified, self.media_type):
            if value is not None and ("\0" in value or len(value.encode("utf-8")) > MAX_SOURCE_TEXT_BYTES):
                raise ValueError("finite captured source text exceeds2048 bytes")
        if self.source.captured_source is not None or self.source.role is not None:
            raise ValueError("finite inventory cannot invent issuer capture or source role")
        if self.headers_captured != (self.source.state == "present"):
            raise ValueError("finite source headers must disclose present versus unavailable acquisition")
        if not self.headers_captured and any(value is not None for value in (self.etag, self.last_modified, self.media_type)):
            raise ValueError("unavailable acquisition cannot invent captured HTTP headers")
        if self.source.artifact_byte_size is not None and self.source.artifact_byte_size > MAX_BODY_BYTES:
            raise ValueError("finite captured body exceeds8MiB")
        return self


class TickerEntry(_Record):
    type: Literal["TICKER"]
    ordinal: StrictInt = Field(ge=0, lt=MAX_RECORDS)
    original_key: str
    pointer: str
    raw_value: JsonValue
    normalized_cik: str | None
    interpreted_ticker: str | None
    interpretation: Literal["USABLE_ISSUER_ASSOCIATION", "MALFORMED", "AMBIGUOUS"]
    reason: Text | None


class IssuerGroup(_Record):
    type: Literal["ISSUER_GROUP"]
    ordinal: StrictInt = Field(ge=0, lt=MAX_RECORDS)
    normalized_cik: Text
    ticker_entry_ordinals: tuple[StrictInt, ...] = Field(min_length=1, max_length=MAX_RECORDS)
    assessment: Literal["CIK_ASSOCIATION_ONLY"]


class ResourceParameters(_Record):
    accession: Text
    cik: Text
    form: Text
    filename: Text
    observation_id: Identity

    @model_validator(mode="after")
    def resource_contract(self):
        _resource_parameters(self.model_dump())
        return self


class ChildRef(_Record):
    job_id: Identity
    occurrence_id: Identity
    schedule_key: Text
    scheduled_for: AwareDatetime
    kind: Literal["sec-resource-fetch-v2"]
    parameters: ResourceParameters
    due_at: AwareDatetime
    priority: StrictInt = Field(ge=-2147483648, le=2147483647)
    max_attempts: StrictInt = Field(ge=3, le=3)
    deadline_at: None


class AdmissionRequest(_Record):
    capture_revision: StrictInt = Field(ge=2, le=2)
    filing: AcquisitionRef
    child: ChildRef


class IndexEntry(_Record):
    type: Literal["INDEX"]
    ordinal: StrictInt = Field(ge=0, lt=MAX_RECORDS)
    physical_line: StrictInt = Field(ge=0)
    form: Text
    company_name: Text
    cik: Text
    filing_date: date
    archive_filename: Text
    accession: Text
    source_row: str
    route: Literal["RESOURCE_V2", "NONFINANCIAL_PREFIX", "UNSUPPORTED_RESOURCE_ROUTE"]
    route_reason: Text | None
    issuer_group_ordinal: StrictInt | None = Field(ge=0, lt=MAX_RECORDS)
    request: AdmissionRequest | None

    @model_validator(mode="after")
    def request_route(self):
        if self.request is not None and self.route != "RESOURCE_V2":
            raise ValueError("nonroute index row cannot claim a resource request")
        return self


Record = Annotated[TickerEntry | IssuerGroup | IndexEntry, Field(discriminator="type")]


class Chunk(_Record):
    contract: Literal["SEC_INVENTORY_CHUNK1"]
    records: tuple[Record, ...] = Field(min_length=1, max_length=MAX_RECORDS)


class ArtifactRef(_Record):
    artifact_sha256: Sha
    byte_size: StrictInt = Field(ge=1, le=MAX_ENVELOPE_BYTES)


class ChunkRef(ArtifactRef):
    ordinal: StrictInt = Field(ge=0, lt=MAX_CHUNKS)
    start: StrictInt = Field(ge=0, le=MAX_RECORDS)
    count: StrictInt = Field(ge=1, le=MAX_RECORDS)


class DayResult(_Record):
    day: date
    acquisition: AcquisitionRef
    chunks: tuple[ChunkRef, ...] = Field(max_length=MAX_CHUNKS)
    row_count: StrictInt = Field(ge=0, le=MAX_RECORDS)
    request_count: StrictInt = Field(ge=0, le=MAX_RECORDS)
    orphan_request_count: StrictInt = Field(ge=0, le=MAX_RECORDS)
    unsupported_route_count: StrictInt = Field(ge=0, le=MAX_RECORDS)
    nonfinancial_count: StrictInt = Field(ge=0, le=MAX_RECORDS)


class InventoryRoot(_Record):
    contract: Literal["SEC_INVENTORY_ENUMERATION1"]
    request: ProducerRequest
    listing: AcquisitionRef
    ticker_chunks: tuple[ChunkRef, ...] = Field(max_length=MAX_CHUNKS)
    issuer_group_chunks: tuple[ChunkRef, ...] = Field(max_length=MAX_CHUNKS)
    days: tuple[DayResult, ...] = Field(max_length=31)
    ticker_entry_count: StrictInt = Field(ge=0, le=MAX_RECORDS)
    issuer_group_count: StrictInt = Field(ge=0, le=MAX_RECORDS)
    index_row_count: StrictInt = Field(ge=0, le=MAX_RECORDS)
    resource_request_count: StrictInt = Field(ge=0, le=MAX_RECORDS)
    source_authority_status: Literal["NOT_ASSESSED"]
    financial_status: Literal["NOT_ASSESSED"]
    listing_security_status: Literal["NOT_ASSESSED"]
    universe_scope: Literal["DECLARED_SEC_DATE_WINDOW"]


class InventoryCheckpoint(_Record):
    checkpoint_revision: StrictInt = Field(ge=1, le=1)
    request_identity_sha256: Sha
    listing: AcquisitionRef
    ticker_chunks: tuple[ChunkRef, ...] = Field(max_length=MAX_CHUNKS)
    issuer_group_chunks: tuple[ChunkRef, ...] = Field(max_length=MAX_CHUNKS)
    ticker_entry_count: StrictInt = Field(ge=0, le=MAX_RECORDS)
    issuer_group_count: StrictInt = Field(ge=0, le=MAX_RECORDS)
    days: tuple[DayResult, ...] = Field(max_length=31)
    root: ArtifactRef | None


class InventoryParameters2(InventoryParameters):
    request_revision: StrictInt = Field(ge=2, le=2)


class ProducerRequest2(ProducerRequest):
    contract: Literal["SEC_INVENTORY_REQUEST2"]
    producer_kind: Literal["sec-finite-inventory-v2"]
    parameters: InventoryParameters2


class ChildRef3(ChildRef):
    kind: Literal["sec-resource-fetch-v3"]


class AdmissionRequest3(AdmissionRequest):
    child: ChildRef3


class IndexEntry2(IndexEntry):
    request: AdmissionRequest3 | None


Record2 = Annotated[TickerEntry | IssuerGroup | IndexEntry2, Field(discriminator="type")]


class Chunk2(Chunk):
    contract: Literal["SEC_INVENTORY_CHUNK2"]
    records: tuple[Record2, ...] = Field(min_length=1, max_length=MAX_RECORDS)


class InventoryRoot2(InventoryRoot):
    contract: Literal["SEC_INVENTORY_ENUMERATION2"]
    request: ProducerRequest2


class InventoryCheckpoint2(InventoryCheckpoint):
    checkpoint_revision: StrictInt = Field(ge=2, le=2)


def ticker_records(raw):
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("ticker body exceeds8MiB")
    entries = decode_object(raw, "SEC ticker map")
    if len(entries) > MAX_RECORDS:
        raise ValueError("ticker map exceeds100000 entries before factories")
    interpreted, ticker_counts = [], Counter()
    for key, value in entries.items():
        if len(encode(dict(original_key=key, raw_value=value))) > MAX_ENTRY_BYTES:
            raise ValueError("ticker entry exceeds128KiB before factories")
        cik, ticker, reason = None, None, None
        if isinstance(value, dict):
            number = value.get("cik_str")
            if type(number) is int and 0 < number < 10**10:
                cik = f"{number:010d}"
            elif type(number) is str and number.isascii() and number.isdigit() and 0 < len(number) <= 10 and int(number) > 0:
                cik = f"{int(number):010d}"
            token = value.get("ticker")
            if (type(token) is str and token and token == token.strip() and "\0" not in token
                    and len(token.upper().encode("utf-8")) <= 128):
                ticker = token.upper()
            title = value.get("title")
            if cik is None or ticker is None or type(title) is not str or not title.strip():
                reason = "Malformed CIK, ticker or company title; raw entry retained"
        else:
            reason = "Mapping entry is not an object; raw entry retained"
        if ticker is not None:
            ticker_counts[ticker] += 1
        interpreted.append((key, value, cik, ticker, reason))
    records, group_ordinals = [], defaultdict(list)
    for ordinal, (key, value, cik, ticker, reason) in enumerate(interpreted):
        status = "MALFORMED" if reason else "USABLE_ISSUER_ASSOCIATION"
        if reason is None and ticker_counts[ticker] > 1:
            status, reason = "AMBIGUOUS", "Ticker occurs in several retained entries; no security selected"
        record = TickerEntry(type="TICKER", ordinal=ordinal, original_key=key, pointer="/" + _escape(key),
            raw_value=value, normalized_cik=cik, interpreted_ticker=ticker, interpretation=status, reason=reason)
        if len(encode(record.model_dump(mode="json"))) > MAX_ENTRY_BYTES:
            raise ValueError("interpreted ticker entry exceeds128KiB")
        records.append(record)
        if cik is not None:
            group_ordinals[cik].append(ordinal)
    groups = tuple(IssuerGroup(type="ISSUER_GROUP", ordinal=i, normalized_cik=cik,
        ticker_entry_ordinals=tuple(ordinals), assessment="CIK_ASSOCIATION_ONLY")
        for i, (cik, ordinals) in enumerate(group_ordinals.items()))
    return tuple(records), groups


def index_record(row, groups, *, entry_type=IndexEntry):
    route, reason = resource_route(row.filing)
    return entry_type(type="INDEX", ordinal=row.entry_ordinal, physical_line=row.physical_line,
        **asdict(row.filing), route=route, route_reason=reason,
        issuer_group_ordinal=groups.get(row.filing.cik), request=None)


_CHUNK_PREFIX = b'{"contract":"SEC_INVENTORY_CHUNK1","records":['
_CHUNK_SUFFIX = b']}'
_CHUNK_OVERHEAD = len(_CHUNK_PREFIX) + len(_CHUNK_SUFFIX) + 1


def pack_chunks(records, *, contract="SEC_INVENTORY_CHUNK1"):
    prefix = _chunk_prefix(contract)
    pending, size = [], len(prefix) + len(_CHUNK_SUFFIX)
    for record in records:
        raw = encode(record.model_dump(mode="json"))
        if len(raw) + len(prefix) + len(_CHUNK_SUFFIX) > MAX_ENVELOPE_BYTES:
            raise ValueError("finite record exceeds1MiB chunk")
        needed = len(raw) + bool(pending)
        if size + needed > MAX_ENVELOPE_BYTES:
            yield prefix + b','.join(pending) + _CHUNK_SUFFIX
            pending, size, needed = [], len(prefix) + len(_CHUNK_SUFFIX), len(raw)
        pending.append(raw)
        size += needed
    if pending:
        yield prefix + b','.join(pending) + _CHUNK_SUFFIX


def _chunk_prefix(contract):
    if type(contract) is not str or contract not in {"SEC_INVENTORY_CHUNK1", "SEC_INVENTORY_CHUNK2"}:
        raise ValueError("unsupported finite chunk contract")
    return b'{"contract":' + encode(contract) + b',"records":['


def day_frontier(acquired, records, *, used_bytes, remaining_requests, remaining_chunks=MAX_CHUNKS,
                 resource_kind="sec-resource-fetch-v2", chunk_contract="SEC_INVENTORY_CHUNK1"):
    if type(resource_kind) is not str or resource_kind not in {"sec-resource-fetch-v2", "sec-resource-fetch-v3"}:
        raise ValueError("unsupported finite resource profile")
    overhead = len(_chunk_prefix(chunk_contract)) + len(_CHUNK_SUFFIX) + 1
    requests = sum(record.route == "RESOURCE_V2" for record in records)
    if requests > remaining_requests:
        raise ValueError("finite requests exceed100000 before filing/child fanout")
    total = used_bytes + MAX_ENVELOPE_BYTES
    bounds = []
    for record in records:
        bound = len(encode(record.model_dump(mode="json")))
        if record.route == "RESOURCE_V2":
            # Measured text plus scalar-width reservation; these are bytes, never minted IDs.
            bound += len(encode(dict(source_url=acquired.source_url, etag=acquired.etag,
                last_modified=acquired.last_modified, media_type=acquired.media_type,
                filename=record.archive_filename))) + REQUEST_FIXED_BYTES + len(resource_kind.encode()) - len(b"sec-resource-fetch-v2")
        if bound + overhead > MAX_ENVELOPE_BYTES:
            raise ValueError("finite row/request frontier exceeds1MiB before fanout")
        total += bound + overhead
        bounds.append(bound)
    if total > MAX_TOTAL_BYTES:
        raise ValueError("finite encoded frontier exceeds64MiB before fanout")
    # In next-fit packing, each adjacent pair of sealed chunks exceeds one payload.
    payload = MAX_ENVELOPE_BYTES - overhead
    chunk_bound = (2 * sum(bound + 1 for bound in bounds) + payload - 1) // payload + 1 if bounds else 0
    if chunk_bound > remaining_chunks:
        raise ValueError("finite chunk frontier exceeds remaining references before fanout")
    return tuple(bounds)



class SecFiniteInventoryHandler:
    def __init__(self, *, edgar, object_store, artifacts, observations, jobs,
                 clock=lambda: datetime.now(timezone.utc), kind=KIND, stage_references=None):
        if type(kind) is not str or kind not in {KIND, PARTIAL_KIND}:
            raise ValueError("unsupported finite producer profile")
        self.kind, self.namespace = kind, f"automatic-ingestion:v1:{kind}:"
        if kind == PARTIAL_KIND:
            self.request_type, self.child_type, self.admission_type = ProducerRequest2, ChildRef3, AdmissionRequest3
            self.entry_type, self.chunk_type, self.root_type, self.checkpoint_type = IndexEntry2, Chunk2, InventoryRoot2, InventoryCheckpoint2
            self.request_contract, self.chunk_contract, self.root_contract = "SEC_INVENTORY_REQUEST2", "SEC_INVENTORY_CHUNK2", "SEC_INVENTORY_ENUMERATION2"
            self.checkpoint_revision, self.resource_kind = 2, "sec-resource-fetch-v3"
        else:
            self.request_type, self.child_type, self.admission_type = ProducerRequest, ChildRef, AdmissionRequest
            self.entry_type, self.chunk_type, self.root_type, self.checkpoint_type = IndexEntry, Chunk, InventoryRoot, InventoryCheckpoint
            self.request_contract, self.chunk_contract, self.root_contract = "SEC_INVENTORY_REQUEST1", "SEC_INVENTORY_CHUNK1", "SEC_INVENTORY_ENUMERATION1"
            self.checkpoint_revision, self.resource_kind = 1, "sec-resource-fetch-v2"
        self.edgar, self.objects, self.artifacts = edgar, object_store, artifacts
        self.observations, self.jobs, self.clock = observations, jobs, clock
        self.stage_references = stage_references
        self.discovery = SecRecentDiscoveryHandler(edgar=edgar, object_store=object_store,
            artifacts=artifacts, observations=observations, jobs=jobs, clock=clock,
            resource_kind=self.resource_kind, occurrence_bound=True)

    def _actual_request(self, job):
        if job.kind != self.kind:
            raise ValueError("finite handler requires its scheduled producer kind")
        parameters = (validate_partial_parameters(job.parameters) if self.kind == PARTIAL_KIND
                      else validate_parameters(job.parameters))
        if parameters.producer_build_sha256 != producer_build_sha256():
            raise ValueError("finite producer build differs from immutable request")
        actual = self.jobs.scheduled_job(job_id=job.job_id, occurrence_id=job.occurrence_id,
            schedule_key=self.namespace + parameters.operation_key, scheduled_for=parameters.operation_at)
        if (actual.kind != job.kind or encode(actual.parameters) != encode(job.parameters)
                or actual.priority != job.priority or actual.due_at != job.due_at
                or actual.max_attempts != job.max_attempts or actual.deadline_at != job.deadline_at):
            raise ValueError("finite producer request differs from actual scheduled job")
        profile = dict(producer_kind=PARTIAL_KIND) if self.kind == PARTIAL_KIND else {}
        request = self.request_type(contract=self.request_contract, producer_job_id=actual.job_id,
            producer_occurrence_id=actual.occurrence_id, priority=actual.priority, parameters=parameters, **profile)
        return actual, request, hashlib.sha256(encode(request.model_dump(mode="json"))).hexdigest()

    def _read(self, ref):
        artifact, raw = read_registered_artifact(ref.artifact_sha256, self.objects, self.artifacts,
            maximum=MAX_ENVELOPE_BYTES, label="finite enumeration")
        if artifact.byte_size != ref.byte_size:
            raise ValueError("finite artifact size differs from retained reference")
        return raw

    def _source_ref(self, key, stored):
        obs, item, delivery = stored.observation, stored.item, stored.delivery
        if delivery is None:
            raise ValueError("finite acquisition requires its actual original delivery")
        artifact = self.artifacts.get(obs.artifact_sha256) if obs.artifact_sha256 is not None else None
        fields = {**asdict(delivery), "checked_at": _utc_datetime("delivery checked_at", delivery.checked_at).isoformat()}
        ref = dict(receipt_key=key, source_system=item.source_system,
            issuer_source_identifier=item.issuer_source_identifier, etag=obs.etag,
            last_modified=obs.last_modified, media_type=obs.canonical_metadata.get("http_media_type"),
            headers_captured=obs.canonical_metadata.get("http_headers_captured"),
            source=dict(observation_sha256=obs.observation_sha256, item_kind=item.item_kind,
                source_key=item.source_key, parent_source_item_id=item.parent_source_item_id, role=None,
                artifact_sha256=obs.artifact_sha256, artifact_byte_size=artifact.byte_size if artifact else None,
                captured_source=None, source_url=obs.source_url, state=obs.state, delivery=fields))
        return load(AcquisitionRef, encode(ref))

    def _recover_source(self, ref, request, digest):
        stored = self.observations.acquisition_receipt(producer_job_id=request.producer_job_id,
            receipt_key=ref.receipt_key, request_identity_sha256=digest)
        if stored is None or self._source_ref(ref.receipt_key, stored) != ref:
            raise ValueError("finite acquisition reference differs from committed receipt")
        return stored

    def _listing(self, context, digest):
        key = "sec-finite:ticker-map"
        stored = self.observations.acquisition_receipt(producer_job_id=context.job.job_id,
            receipt_key=key, request_identity_sha256=digest)
        if stored is None:
            values = dict(item=SourceItemIdentity("SEC", "inventory", "company_tickers.json"),
                item_outcome_key="finite:ticker-map", metadata={"capture": "finite-ticker-map-v1"},
                source_url=TICKER_MAP_URL)
            try:
                result = self.edgar.fetch(TICKER_MAP_URL, maximum_bytes=MAX_BODY_BYTES)
            except NoXbrlDataError:
                values.update(state="unavailable", metadata={"capture": "finite-ticker-map-v1", "reason": "not_found",
                    "http_media_type": None, "http_headers_captured": False})
            else:
                _finite_transport_texts(result, MAX_SOURCE_TEXT_BYTES)
                artifact = store_evidence(result.data, result.media_type or "application/octet-stream", self.objects, self.artifacts)
                values.update(state="present", source_url=result.url, artifact_sha256=artifact.content_sha256,
                    etag=result.etag, last_modified=result.last_modified)
                values["metadata"].update(http_media_type=result.media_type, http_headers_captured=True)
            stored = self.observations.record_with_acquisition_receipt(context.lease, receipt_key=key,
                request_identity_sha256=digest, detected_at=self.clock(), **values)
        self._admit_listing(stored, key)
        return self._source_ref(key, stored)

    @staticmethod
    def _admit_listing(stored, receipt_key):
        item, obs = stored.item, stored.observation
        expected = {"capture": "finite-ticker-map-v1", "http_media_type": obs.canonical_metadata.get("http_media_type"),
            "http_headers_captured": obs.state == "present"}
        if obs.state == "unavailable":
            expected["reason"] = "not_found"
        if (receipt_key != "sec-finite:ticker-map" or item.source_system != "SEC" or item.item_kind != "inventory"
                or item.source_key != "company_tickers.json" or item.issuer_source_identifier is not None
                or item.parent_source_item_id is not None or obs.state not in {"present", "unavailable"}
                or obs.canonical_metadata != expected or stored.delivery is None
                or stored.delivery.item_key != "finite:ticker-map"):
            raise ValueError("finite listing receipt differs from original request")

    def _ticker_records(self, listing):
        if listing.source.state == "unavailable":
            return (), ()
        if listing.source.state != "present" or listing.source.artifact_sha256 is None:
            raise ValueError("finite listing does not retain a present or unavailable acquisition")
        raw = read_registered_artifact(listing.source.artifact_sha256, self.objects, self.artifacts,
            maximum=MAX_BODY_BYTES, label="finite ticker map")[1]
        return ticker_records(raw)

    def _store_chunks(self, records, *, used_bytes):
        refs, start = [], 0
        for ordinal, raw in enumerate(pack_chunks(records, contract=self.chunk_contract)):
            if ordinal >= MAX_CHUNKS or used_bytes + len(raw) + MAX_ENVELOPE_BYTES > MAX_TOTAL_BYTES:
                raise ValueError("finite chunk/count aggregate ceiling exceeded")
            chunk = load(self.chunk_type, raw)
            artifact = store_evidence(raw, MEDIA_TYPE, self.objects, self.artifacts)
            refs.append(ChunkRef(ordinal=ordinal, start=start, count=len(chunk.records),
                byte_size=artifact.byte_size, artifact_sha256=artifact.content_sha256))
            start += len(chunk.records)
            used_bytes += len(raw)
        return tuple(refs), used_bytes

    def _read_chunks(self, refs):
        records, start = [], 0
        for ordinal, ref in enumerate(refs):
            if ref.ordinal != ordinal or ref.start != start:
                raise ValueError("finite chunks must have contiguous ordered ranges")
            chunk = load(self.chunk_type, self._read(ref))
            if len(chunk.records) != ref.count:
                raise ValueError("finite chunk retained record count differs")
            records.extend(chunk.records)
            start += ref.count
            if start > MAX_RECORDS:
                raise ValueError("finite chunks exceed record ceiling")
        return tuple(records)

    def _child_ref(self, request, filing, stored, key, child):
        immediate = datetime.combine(filing.filing_date, time.min, tzinfo=timezone.utc)
        fields = dict(job_id=child.job_id, occurrence_id=child.occurrence_id, schedule_key=key,
            scheduled_for=immediate.isoformat(), kind=child.kind, parameters=child.parameters,
            due_at=_utc_datetime("child due_at", child.due_at).isoformat(), priority=child.priority,
            max_attempts=child.max_attempts, deadline_at=None)
        ref = load(self.child_type, encode(fields))
        self._verify_child(ref, request, filing, stored)
        return ref

    def _verify_child(self, ref, request, filing, stored):
        actual = self.jobs.scheduled_job(job_id=ref.job_id, occurrence_id=ref.occurrence_id,
            schedule_key=ref.schedule_key, scheduled_for=ref.scheduled_for)
        expected_key = resource_child_key_for_occurrence(self.resource_kind, filing,
            stored.observation, request.producer_occurrence_id)
        expected_parameters = dict(accession=filing.accession, cik=filing.cik, form=filing.form,
            filename=filing.archive_filename, observation_id=stored.observation.source_observation_id)
        immediate = datetime.combine(filing.filing_date, time.min, tzinfo=timezone.utc)
        if (ref.schedule_key != expected_key or ref.scheduled_for != immediate
                or ref.kind != actual.kind or actual.kind != self.resource_kind
                or ref.parameters.model_dump() != expected_parameters
                or encode(actual.parameters) != encode(expected_parameters)
                or actual.due_at != immediate or ref.due_at != actual.due_at
                or actual.priority != request.priority or ref.priority != actual.priority
                or actual.max_attempts != 3 or ref.max_attempts != actual.max_attempts
                or actual.deadline_at is not None):
            raise ValueError("finite child immutable parameters differ from retained request")

    @staticmethod
    def _used_bytes(checkpoint):
        refs = [*checkpoint.ticker_chunks, *checkpoint.issuer_group_chunks,
                *(ref for day in checkpoint.days for ref in day.chunks)]
        if len(refs) > MAX_CHUNKS:
            raise ValueError("finite root exceeds1024 chunk references")
        total = sum(ref.byte_size for ref in refs)
        if total + MAX_ENVELOPE_BYTES > MAX_TOTAL_BYTES:
            raise ValueError("finite root/chunks exceed64MiB")
        return total

    def _verify_progress(self, checkpoint, request, digest, *, complete=False):
        if checkpoint.request_identity_sha256 != digest:
            raise ValueError("finite checkpoint differs from immutable producer request")
        self._used_bytes(checkpoint)
        listing = self._recover_source(checkpoint.listing, request, digest)
        self._admit_listing(listing, checkpoint.listing.receipt_key)
        entries, groups = self._ticker_records(checkpoint.listing)
        if (self._read_chunks(checkpoint.ticker_chunks) != entries
                or self._read_chunks(checkpoint.issuer_group_chunks) != groups
                or checkpoint.ticker_entry_count != len(entries) or checkpoint.issuer_group_count != len(groups)):
            raise ValueError("finite ticker/group records differ from retained bytes")
        group_by_cik = {group.normalized_cik: group.ordinal for group in groups}
        start, end = _date_range(request.parameters.model_dump(include={"start_date", "end_date"}))
        row_total = request_total = 0
        for ordinal, day in enumerate(checkpoint.days):
            if day.day != start + timedelta(days=ordinal) or day.day > end:
                raise ValueError("finite days are not the original contiguous date window")
            stored = self._recover_source(day.acquisition, request, digest)
            obs, item = stored.observation, stored.item
            quarter = (day.day.month - 1) // 3 + 1
            validate_finite_daily_source(stored, day.day, day.acquisition.receipt_key)
            raw = read_registered_artifact(obs.artifact_sha256, self.objects, self.artifacts,
                maximum=MAX_BODY_BYTES, label="finite daily index")[1] if obs.state == "present" else None
            rows = parse_all_form_index(raw, maximum_rows=MAX_RECORDS - row_total,
                maximum_row_bytes=MAX_ENTRY_BYTES) if raw is not None else ()
            actual_records = self._read_chunks(day.chunks)
            if len(actual_records) != len(rows):
                raise ValueError("finite day row membership differs from retained bytes")
            counters = Counter()
            for row, actual in zip(rows, actual_records):
                self._verify_recorded_row(actual, row, day, stored, request, digest, group_by_cik=group_by_cik)
                counters[actual.route] += 1
                if actual.route == "RESOURCE_V2":
                    if actual.issuer_group_ordinal is None:
                        counters["ORPHAN"] += 1
            if (day.row_count != len(rows) or day.request_count != counters["RESOURCE_V2"]
                    or day.orphan_request_count != counters["ORPHAN"]
                    or day.unsupported_route_count != counters["UNSUPPORTED_RESOURCE_ROUTE"]
                    or day.nonfinancial_count != counters["NONFINANCIAL_PREFIX"]):
                raise ValueError("finite day actual counts differ")
            row_total += len(rows)
            request_total += day.request_count
            if request_total > MAX_RECORDS:
                raise ValueError("finite requests exceed100000")
            del raw, rows, actual_records
        if complete and len(checkpoint.days) != (end - start).days + 1:
            raise ValueError("completed finite root omits dates")
        return entries, groups, row_total, request_total

    def _verify_recorded_row(self, actual, row, day, stored, request, digest, *, group_by_cik=None):
        expected = index_record(row, {} if group_by_cik is None else group_by_cik, entry_type=self.entry_type)
        compared = actual.model_copy(update={"request": None})
        if group_by_cik is None:
            compared = compared.model_copy(update={"issuer_group_ordinal": None})
        if type(actual) is not self.entry_type or compared != expected:
            raise ValueError("finite index interpretation differs from original physical row")
        if actual.route != "RESOURCE_V2":
            if actual.request is not None:
                raise ValueError("nonroute row cannot claim a request")
            return
        if actual.request is None:
            raise ValueError("routed row omits its actual filing/child request")
        key = f"sec-finite:filing:{day.day.isoformat()}:{row.entry_ordinal}"
        if actual.request.filing.receipt_key != key:
            raise ValueError("finite filing receipt differs from physical entry")
        filing_source = self._recover_source(actual.request.filing, request, digest)
        fobs, fitem = filing_source.observation, filing_source.item
        obs = stored.observation
        quarter = (day.day.month - 1) // 3 + 1
        expected_file_meta = dict(form=row.filing.form, company_name=row.filing.company_name,
            cik=row.filing.cik, filing_date=row.filing.filing_date.isoformat(),
            archive_filename=row.filing.archive_filename, accession=row.filing.accession,
            source_row=row.filing.source_row, index_date=day.day.isoformat(), quarter=f"{day.day.year}Q{quarter}",
            http_media_type=day.acquisition.media_type, http_headers_captured=True)
        if (fitem.item_kind != "filing" or fitem.source_key != row.filing.accession
                or fitem.issuer_source_identifier != row.filing.cik or fitem.parent_source_item_id is not None
                or fobs.state != "present" or fobs.canonical_metadata != expected_file_meta
                or fobs.artifact_sha256 != obs.artifact_sha256 or fobs.source_url != obs.source_url
                or fobs.etag != obs.etag or fobs.last_modified != obs.last_modified):
            raise ValueError("finite filing source differs from retained daily row")
        self._verify_child(actual.request.child, request, row.filing, filing_source)

    def recover_completed(self, job):
        actual, request, digest = self._actual_request(job)
        checkpoint = load(self.checkpoint_type, encode(actual.checkpoint))
        if actual.status != "succeeded" or checkpoint.root is None:
            raise ValueError("finite producer does not have a completed root")
        return self._completed(checkpoint, request, digest)

    @staticmethod
    def _inspection_root_stamp(job, *, allow_empty_checkpoint=False):
        if (type(job.ownership_generation) is not int or not 0 <= job.ownership_generation < 2**63
                or type(job.priority) is not int or type(job.max_attempts) is not int):
            raise ValueError("finite inspection root schedule/generation must be exact integers")
        return (job.job_id, job.occurrence_id, job.kind, job.status,
            _bounded_schedule_parameters(job.parameters)[1],
            None if allow_empty_checkpoint and job.checkpoint is None else _bounded_schedule_parameters(job.checkpoint)[1],
            _schedule_datetime("root due_at", job.due_at), job.priority, job.max_attempts,
            None if job.deadline_at is None else _schedule_datetime("root deadline", job.deadline_at),
            job.ownership_generation)

    def _inspection_resource_job(self, ref):
        actual = self.jobs.job_for_occurrence(schedule_key=ref.schedule_key, scheduled_for=ref.scheduled_for,
            kind=ref.kind, parameters=ref.parameters.model_dump(), due_at=ref.due_at,
            priority=ref.priority, max_attempts=ref.max_attempts, deadline_at=ref.deadline_at)
        if actual is None:
            raise ValueError("finite retained resource association disappeared")
        for name in ("job_id", "occurrence_id", "priority", "max_attempts"):
            if type(getattr(actual, name)) is not int or getattr(actual, name) != getattr(ref, name):
                raise ValueError("finite returned resource immutable identity differs")
        if (actual.kind != ref.kind or _bounded_schedule_parameters(actual.parameters)[1]
                != _bounded_schedule_parameters(ref.parameters.model_dump())[1]
                or _schedule_datetime("resource due_at", actual.due_at) != ref.due_at
                or actual.deadline_at != ref.deadline_at):
            raise ValueError("finite returned resource immutable declaration differs")
        return actual

    def _recorded_checkpoint(self, value, request, digest):
        raw, canonical = _bounded_schedule_parameters(value)
        if (type(raw.get("checkpoint_revision")) is not int or raw["checkpoint_revision"] != self.checkpoint_revision
                or raw.get("request_identity_sha256") != digest):
            raise ValueError("recorded checkpoint differs from actual profile/request")
        start, end = _date_range(request.parameters.model_dump(include={"start_date", "end_date"}))
        days = raw.get("days")
        if type(days) is not list or len(days) > 31 or len(days) > (end-start).days+1:
            raise ValueError("recorded checkpoint exceeds declared day prefix")
        def count(value, name):
            if type(value) is not int or not 0 <= value <= MAX_RECORDS:
                raise ValueError("recorded checkpoint count requires exact bounded integer: " + name)
            return value
        refs, byte_size = 0, MAX_ENVELOPE_BYTES
        def ranges(values, declared):
            nonlocal refs, byte_size
            if type(values) is not list or len(values) > MAX_CHUNKS-refs:
                raise ValueError("recorded checkpoint exceeds chunk-reference frontier")
            offset = 0
            for ordinal, ref in enumerate(values):
                if type(ref) is not dict:
                    raise ValueError("recorded checkpoint reference requires object")
                for name in ("ordinal", "start", "count", "byte_size"):
                    if type(ref.get(name)) is not int:
                        raise ValueError("recorded checkpoint reference scalars must be exact integers")
                if (ref["ordinal"] != ordinal or ref["start"] != offset or not 0 < ref["count"] <= MAX_RECORDS
                        or not 0 < ref["byte_size"] <= MAX_ENVELOPE_BYTES):
                    raise ValueError("recorded checkpoint references are not bounded contiguous ranges")
                offset += ref["count"]
                byte_size += ref["byte_size"]
            refs += len(values)
            if offset != declared or byte_size > MAX_TOTAL_BYTES:
                raise ValueError("recorded checkpoint range/count/byte frontier differs")
        tickers = count(raw.get("ticker_entry_count"), "tickers")
        groups = count(raw.get("issuer_group_count"), "groups")
        if groups > tickers:
            raise ValueError("recorded group count exceeds ticker declaration")
        ranges(raw.get("ticker_chunks"), tickers); ranges(raw.get("issuer_group_chunks"), groups)
        rows = requests = 0
        for ordinal, day in enumerate(days):
            if type(day) is not dict or day.get("day") != (start+timedelta(days=ordinal)).isoformat():
                raise ValueError("recorded days are not the original contiguous prefix")
            values = {name:count(day.get(name),name) for name in ("row_count", "request_count",
                "orphan_request_count", "unsupported_route_count", "nonfinancial_count")}
            if (values["request_count"]+values["unsupported_route_count"]+values["nonfinancial_count"] != values["row_count"]
                    or values["orphan_request_count"] > values["request_count"]):
                raise ValueError("recorded route-count partition differs")
            ranges(day.get("chunks"), values["row_count"])
            rows += values["row_count"]; requests += values["request_count"]
            if rows > MAX_RECORDS or requests > MAX_RECORDS:
                raise ValueError("recorded prefix exceeds row/request frontier")
        root = raw.get("root")
        if root is not None:
            if (type(root) is not dict or set(root) != {"artifact_sha256", "byte_size"}
                    or type(root["byte_size"]) is not int or not 0 < root["byte_size"] <= MAX_ENVELOPE_BYTES
                    or len(days) != (end-start).days+1):
                raise ValueError("recorded final root shape/size/date prefix differs before factory")
            _capture_sha(root["artifact_sha256"])
        checkpoint = load(self.checkpoint_type, canonical.encode("utf-8"))
        return checkpoint, dict(assessment="DECLARED", checkpointed_days=len(days), rows=rows, requests=requests,
            ticker_entries=tickers, issuer_groups=groups, root_present=checkpoint.root is not None)

    def _recorded_stage_owner(self):
        stage = self.stage_references
        engine = getattr(self.jobs, "engine", None)
        if (not isinstance(stage, SecCandidateStageHandler) or engine is None
                or self.observations.engine is not engine or self.artifacts.engine is not engine
                or stage._companies.engine is not engine or stage._companies._jobs.engine is not engine
                or stage._observations.engine is not engine or stage._artifacts.engine is not engine
                or stage._history is None or stage._history.engine is not engine):
            raise ValueError("recorded entry delegation requires genuine same-engine stage owners")
        return stage

    def inspect_recorded_entry(self, *, job_id, occurrence_id, schedule_key, scheduled_for, day, entry_ordinal):
        """Inspect one checkpointed physical entry; untouched progress and financial truth stay unassessed."""
        for name, value in (("job_id", job_id), ("occurrence_id", occurrence_id)):
            if type(value) is not int or not 0 < value < 2**63:
                raise ValueError(name + " requires an exact positive signed64 integer")
        key = _bounded_schedule_text("finite schedule key", schedule_key, 1024)
        if not key.startswith(self.namespace) or not key[len(self.namespace):]:
            raise ValueError("recorded entry requires its exact producer namespace")
        _bounded_schedule_text("finite operation key", key[len(self.namespace):], 512)
        if type(scheduled_for) is not datetime:
            raise ValueError("recorded scheduled_for requires an exact aware datetime")
        scheduled = _schedule_datetime("recorded scheduled_for", scheduled_for)
        if (type(day) is not str or len(day) != 10 or not day.isascii()
                or type(entry_ordinal) is not int or not 0 <= entry_ordinal < MAX_RECORDS):
            raise ValueError("recorded entry requires canonical day and bounded ordinal")
        selected_day = date.fromisoformat(day)
        if selected_day.isoformat() != day:
            raise ValueError("recorded day must be canonical ISO")
        identity = dict(job_id=job_id, occurrence_id=occurrence_id, schedule_key=key, scheduled_for=scheduled)
        job = self.jobs.scheduled_job(**identity)
        actual, request, digest = self._actual_request(job)
        start, end = _date_range(request.parameters.model_dump(include={"start_date", "end_date"}))
        if not start <= selected_day <= end:
            raise ValueError("recorded day lies outside original declared window")
        stamp = self._inspection_root_stamp(actual, allow_empty_checkpoint=True)
        if actual.status not in {"pending", "running", "retry_wait", "failed", "succeeded"}:
            raise ValueError("recorded producer has invalid operational status")
        reference = dict(job_id=job_id, occurrence_id=occurrence_id, schedule_key=key,
            scheduled_for=scheduled.isoformat(), kind=actual.kind, request_identity_sha256=digest,
            checkpoint_identity_sha256=None if stamp[5] is None else hashlib.sha256(stamp[5].encode()).hexdigest(),
            job_status=actual.status, ownership_generation=actual.ownership_generation)
        flags = dict(root_body_verified=False, cohort_sources_verified=False, listing_security_status="NOT_ASSESSED",
                     financial_status="NOT_ASSESSED", currentness_status="NOT_ASSESSED", collection_complete=False)
        def finish(value):
            final = self.jobs.scheduled_job(**identity)
            final, _, _ = self._actual_request(final)
            if self._inspection_root_stamp(final, allow_empty_checkpoint=True) != stamp:
                raise ValueError("producer checkpoint/status changed during recorded entry inspection")
            result = dict(operation_revision=1, producer=reference, day=day, entry_ordinal=entry_ordinal, **flags, **value)
            detached, size, exceeded = _capture_result_json(result)
            if exceeded:
                result = dict(operation_revision=1, producer=reference, day=day, entry_ordinal=entry_ordinal, **flags,
                    status="CAPACITY_UNAVAILABLE", gap=dict(phase="combined_references", required_bytes=size,
                        limit_bytes=MAX_ENVELOPE_BYTES, required_is_lower_bound=True))
                detached, _, exceeded = _capture_result_json(result)
                if exceeded: raise ValueError("compact recorded result exceeds reference ceiling")
            return detached
        if actual.checkpoint is None:
            if actual.status == "succeeded": raise ValueError("succeeded producer has no recorded checkpoint")
            return finish(dict(status="NO_RECORDED_PROGRESS", progress=None))
        checkpoint, progress = self._recorded_checkpoint(actual.checkpoint, request, digest)
        if actual.status == "succeeded" and checkpoint.root is None:
            raise ValueError("succeeded producer has no recorded final root")
        selected = next((item for item in checkpoint.days if item.day == selected_day), None)
        if selected is None:
            return finish(dict(status="NOT_CHECKPOINTED", progress=progress))
        stored = self._recover_source(selected.acquisition, request, digest)
        validate_finite_daily_source(stored, selected_day, selected.acquisition.receipt_key)
        if stored.observation.state == "unavailable":
            if selected.row_count != 0: raise ValueError("unavailable day claims recorded entries")
            return finish(dict(status="DAY_SOURCE_UNAVAILABLE", progress=progress))
        _, raw = read_registered_artifact(stored.observation.artifact_sha256, self.objects, self.artifacts,
                                          maximum=MAX_BODY_BYTES, label="recorded daily index")
        rows = parse_all_form_index(raw, maximum_rows=MAX_RECORDS, maximum_row_bytes=MAX_ENTRY_BYTES)
        if len(rows) != selected.row_count:
            raise ValueError("recorded selected day count differs from original body")
        if entry_ordinal >= len(rows):
            return finish(dict(status="ROW_NOT_IN_VERIFIED_DAY", progress=progress, selected_day_bytes_verified=True))
        ref = next(item for item in selected.chunks if item.start <= entry_ordinal < item.start+item.count)
        chunk = load(self.chunk_type, self._read(ref), record_count=ref.count, index_start=ref.start)
        entry = chunk.records[entry_ordinal-ref.start]
        if type(entry) is not self.entry_type or entry.ordinal != entry_ordinal:
            raise ValueError("selected recorded chunk entry differs from original ordinal")
        if entry.issuer_group_ordinal is not None and entry.issuer_group_ordinal >= checkpoint.issuer_group_count:
            raise ValueError("recorded group ordinal exceeds declared group count")
        self._verify_recorded_row(entry, rows[entry_ordinal], selected, stored, request, digest)
        pair = None
        if entry.request is not None:
            stage = self._recorded_stage_owner()
            child = entry.request.child
            original_child = self._inspection_resource_job(child)
            child_stamp = _capture_job_stamp(original_child)
            quartet = dict(job_id=child.job_id, occurrence_id=child.occurrence_id,
                schedule_key=child.schedule_key, scheduled_for=child.scheduled_for)
            pair = stage.recover_completed_pair_references(**quartet)
            refreshed = self._inspection_resource_job(child)
            if _capture_job_stamp(refreshed) != child_stamp:
                raise ValueError("resource changed during recorded pair delegation")
            returned = pair["resource"]
            if (set(returned) != set(quartet) or any(type(returned[n]) is not int or returned[n] != quartet[n]
                    for n in ("job_id", "occurrence_id")) or returned["schedule_key"] != child.schedule_key
                    or type(returned["scheduled_for"]) is not str
                    or _schedule_datetime("returned resource time", datetime.fromisoformat(_capture_text(returned["scheduled_for"])))
                        != _schedule_datetime("original resource time", child.scheduled_for)):
                raise ValueError("pair owner returned a different resource quartet")
        return finish(dict(status="INSPECTED", progress=progress, selected_day_bytes_verified=True,
            selected_row_verified=True, entry=entry.model_dump(mode="json"), chunk=ref.model_dump(mode="json"),
            issuer_group=dict(ordinal=entry.issuer_group_ordinal, assessment="DECLARED_NOT_VERIFIED"), pair=pair))

    def inspect_completed_entry(self, *, job_id, occurrence_id, schedule_key, scheduled_for, day, entry_ordinal):
        """Inspect one original row and downstream observations without financial qualification."""
        for name, value in (("job_id", job_id), ("occurrence_id", occurrence_id)):
            if type(value) is not int or not 0 < value < 2**63:
                raise ValueError(f"{name} requires an exact positive signed64 integer")
        key = _bounded_schedule_text("finite schedule key", schedule_key, 1024)
        if not key.startswith(self.namespace) or not key[len(self.namespace):] or len(key[len(self.namespace):].encode("utf-8")) > 512:
            raise ValueError("finite inspection requires its exact namespace key")
        _bounded_schedule_text("finite operation key", key[len(self.namespace):], 512)
        if type(scheduled_for) is not datetime:
            raise ValueError("scheduled_for requires an exact aware datetime")
        scheduled = _schedule_datetime("scheduled_for", scheduled_for)
        if (type(day) is not str or len(day) != 10 or not day.isascii()
                or type(entry_ordinal) is not int or not 0 <= entry_ordinal < MAX_RECORDS):
            raise ValueError("finite inspection requires an ISO day and exact bounded ordinal")
        selected_day = date.fromisoformat(day)
        if selected_day.isoformat() != day:
            raise ValueError("finite inspection day must be canonical ISO")
        identity = dict(job_id=job_id, occurrence_id=occurrence_id, schedule_key=key, scheduled_for=scheduled)
        original = self.jobs.scheduled_job(**identity)
        stamp = self._inspection_root_stamp(original)
        root = self.recover_completed(original)
        matched = next((item for item in root.days if item.day == selected_day), None)
        if matched is None or entry_ordinal >= matched.row_count:
            raise ValueError("finite inspection day/entry is absent from the retained root")
        ref = next(item for item in matched.chunks if item.start <= entry_ordinal < item.start + item.count)
        chunk = load(self.chunk_type, self._read(ref))
        row = chunk.records[entry_ordinal - ref.start]
        if len(chunk.records) != ref.count or type(row) is not self.entry_type or row.ordinal != entry_ordinal:
            raise ValueError("finite selected chunk/entry differs from the original range")
        root_reference = dict(**{name: value for name, value in identity.items() if name != "scheduled_for"},
            scheduled_for=scheduled.isoformat(), request_identity_sha256=hashlib.sha256(encode(root.request.model_dump(mode="json"))).hexdigest(),
            artifact=original.checkpoint["root"])
        capture = None
        stage = dict(status="NOT_APPLICABLE_TO_RETAINED_ROW")
        if row.request is not None:
            child = row.request.child
            filing = SecIndexFiling(**{name: getattr(row, name) for name in SecIndexFiling.__dataclass_fields__})
            digest = root_reference["request_identity_sha256"]
            stored = self._recover_source(row.request.filing, root.request, digest)
            self._verify_child(child, root.request, filing, stored)
            self._inspection_resource_job(child)
            resource = SecResourceFetchHandler(edgar=self.edgar, object_store=self.objects,
                artifacts=self.artifacts, observations=self.observations, jobs=self.jobs, clock=self.clock)
            capture = resource.recover_completed_capture(job_id=child.job_id, occurrence_id=child.occurrence_id,
                schedule_key=child.schedule_key, scheduled_for=child.scheduled_for)
            refreshed = self._inspection_resource_job(child)
            returned = capture["job"]
            expected = dict(job_id=child.job_id, occurrence_id=child.occurrence_id, kind=child.kind,
                schedule_key=child.schedule_key, scheduled_for=_schedule_datetime("child scheduled_for", child.scheduled_for).isoformat(),
                parameters=child.parameters.model_dump(), due_at=_schedule_datetime("child due_at", child.due_at).isoformat(),
                priority=child.priority, max_attempts=child.max_attempts, deadline_at=child.deadline_at)
            compared = dict(returned)
            for name in ("scheduled_for", "due_at"):
                if name in compared:
                    if type(compared[name]) is not str:
                        raise ValueError("finite returned schedule time requires exact text")
                    compared[name] = _schedule_datetime(name, datetime.fromisoformat(_capture_text(compared[name]))).isoformat()
            if (not all(name in returned for name in ("job_id", "occurrence_id", "kind", "schedule_key", "scheduled_for"))
                    or any(encode(compared[name]) != encode(value) for name, value in expected.items() if name in returned)):
                raise ValueError("finite capture returned a different immutable resource relation")
            stage = dict(status="NOT_INSPECTED_CAPTURE_NOT_VERIFIED")
            if capture["status"] == "VERIFIED":
                if (refreshed.status != "succeeded" or _bounded_schedule_parameters(refreshed.checkpoint)[1]
                        != _bounded_schedule_parameters(capture["checkpoint"])[1]):
                    raise ValueError("finite verified capture differs from the refreshed resource checkpoint")
                request = resource.candidate_stage_request(refreshed, capture["checkpoint"])
                actual = self.jobs.job_for_occurrence(**{name: getattr(request, name) for name in request.__dataclass_fields__})
                stage = dict(status="ABSENT_ASSOCIATION")
                if actual is not None:
                    stage = dict(status="MATCHED_ASSOCIATION", schedule_key=request.schedule_key,
                        scheduled_for=request.scheduled_for.isoformat(), job_id=actual.job_id,
                        occurrence_id=actual.occurrence_id, job_status=actual.status, attempts=actual.attempts,
                        ownership_generation=actual.ownership_generation, owner=_capture_text(actual.owner),
                        error_summary=_capture_text(actual.error_summary), checkpoint_present=actual.checkpoint is not None,
                        updated_at=actual.updated_at.isoformat())
        result = dict(operation_revision=1, status="INSPECTED", root=root_reference, day=day,
            entry_ordinal=entry_ordinal, chunk=ref.model_dump(mode="json"), entry=row.model_dump(mode="json"),
            listing=root.listing.model_dump(mode="json"), issuer_group_ordinal=row.issuer_group_ordinal,
            capture=capture, stage=stage, financial_status="NOT_ASSESSED", currentness_status="NOT_ASSESSED",
            listing_security_status="NOT_ASSESSED", universe_scope=root.universe_scope)
        detached, size, exceeded = _capture_result_json(result)
        if exceeded:
            result = dict(operation_revision=1, status="CAPACITY_UNAVAILABLE", root=root_reference,
                day=day, entry_ordinal=entry_ordinal, chunk=ref.model_dump(mode="json"),
                gap=dict(phase="combined_references", required_bytes=size, limit_bytes=MAX_ENVELOPE_BYTES,
                         required_is_lower_bound=True), financial_status="NOT_ASSESSED",
                currentness_status="NOT_ASSESSED", listing_security_status="NOT_ASSESSED")
            detached, _, exceeded = _capture_result_json(result)
            if exceeded:
                raise ValueError("finite scalar inspection references exceed capacity")
        final = self.jobs.scheduled_job(**identity)
        final, _, _ = self._actual_request(final)
        if self._inspection_root_stamp(final) != stamp or final.status != "succeeded":
            raise ValueError("finite root changed during entry inspection")
        return detached

    def _completed(self, checkpoint, request, digest):
        root = load(self.root_type, self._read(checkpoint.root))
        entries, groups, rows, requests = self._verify_progress(checkpoint, request, digest, complete=True)
        expected = self._root(checkpoint, request, rows, requests)
        if root != expected:
            raise ValueError("finite completed root differs from original checkpoint/source associations")
        if self._used_bytes(checkpoint) + checkpoint.root.byte_size > MAX_TOTAL_BYTES:
            raise ValueError("finite completed envelopes exceed64MiB")
        return root

    def _root(self, checkpoint, request, rows, requests):
        return self.root_type(contract=self.root_contract, request=request,
            listing=checkpoint.listing, ticker_chunks=checkpoint.ticker_chunks,
            issuer_group_chunks=checkpoint.issuer_group_chunks, days=checkpoint.days,
            ticker_entry_count=checkpoint.ticker_entry_count, issuer_group_count=checkpoint.issuer_group_count,
            index_row_count=rows, resource_request_count=requests, source_authority_status="NOT_ASSESSED",
            financial_status="NOT_ASSESSED", listing_security_status="NOT_ASSESSED",
            universe_scope="DECLARED_SEC_DATE_WINDOW")

    def __call__(self, context):
        actual, request, digest = self._actual_request(context.job)
        if actual.checkpoint is not None:
            checkpoint = load(self.checkpoint_type, encode(actual.checkpoint))
            if checkpoint.root is not None:
                self._completed(checkpoint, request, digest)
                return checkpoint.model_dump(mode="json")
            entries, groups, row_total, request_total = self._verify_progress(checkpoint, request, digest)
        else:
            listing = self._listing(context, digest)
            entries, groups = self._ticker_records(listing)
            ticker_chunks, used = self._store_chunks(entries, used_bytes=0)
            group_chunks, used = self._store_chunks(groups, used_bytes=used)
            checkpoint = self.checkpoint_type(checkpoint_revision=self.checkpoint_revision, request_identity_sha256=digest,
                listing=listing, ticker_chunks=ticker_chunks, issuer_group_chunks=group_chunks,
                ticker_entry_count=len(entries), issuer_group_count=len(groups), days=(), root=None)
            self._used_bytes(checkpoint)
            context.save_checkpoint(checkpoint.model_dump(mode="json"))
            row_total = request_total = 0
        group_by_cik = {group.normalized_cik: group.ordinal for group in groups}
        start, end = _date_range(request.parameters.model_dump(include={"start_date", "end_date"}))
        day = start + timedelta(days=len(checkpoint.days))
        while day <= end:
            if context.stopping.is_set():
                raise SecDiscoveryStopped("finite inventory stopped before the next day")
            acquired, rows = self.discovery.retained_day(context, day,
                request_identity_sha256=digest, maximum_bytes=MAX_BODY_BYTES,
                maximum_text_bytes=MAX_SOURCE_TEXT_BYTES, maximum_rows=MAX_RECORDS-row_total,
                maximum_row_bytes=MAX_ENTRY_BYTES)
            day_ref = self._source_ref(f"sec-finite:day:{day.isoformat()}", acquired.stored)
            records = tuple(index_record(row, group_by_cik, entry_type=self.entry_type) for row in rows)
            used = self._used_bytes(checkpoint)
            bounds = day_frontier(acquired, records, used_bytes=used,
                remaining_requests=MAX_RECORDS-request_total,
                remaining_chunks=MAX_CHUNKS-len(checkpoint.ticker_chunks)-len(checkpoint.issuer_group_chunks)
                    -sum(len(previous.chunks) for previous in checkpoint.days),
                resource_kind=self.resource_kind, chunk_contract=self.chunk_contract)
            results, counts = [], Counter()
            for (row, stored, key, child), base, bound in zip(
                    self.discovery.retained_day_requests(context, day, acquired, rows,
                        request_identity_sha256=digest), records, bounds):
                record = base
                if stored is not None:
                    filing_key = f"sec-finite:filing:{day.isoformat()}:{row.entry_ordinal}"
                    admission = self.admission_type(capture_revision=2, filing=self._source_ref(filing_key, stored),
                        child=self._child_ref(request, row.filing, stored, key, child))
                    record = base.model_copy(update={"request": admission})
                    if record.issuer_group_ordinal is None:
                        counts["ORPHAN"] += 1
                if len(encode(record.model_dump(mode="json"))) > bound:
                    raise ValueError("actual finite request exceeds pre-admitted byte frontier")
                counts[record.route] += 1
                results.append(record)
            if len(results) != len(records):
                raise ValueError("finite owner did not return every pre-admitted row")
            chunks, used = self._store_chunks(results, used_bytes=used)
            result = DayResult(day=day, acquisition=day_ref,
                chunks=chunks, row_count=len(records), request_count=counts["RESOURCE_V2"],
                orphan_request_count=counts["ORPHAN"], unsupported_route_count=counts["UNSUPPORTED_RESOURCE_ROUTE"],
                nonfinancial_count=counts["NONFINANCIAL_PREFIX"])
            checkpoint = checkpoint.model_copy(update={"days": (*checkpoint.days, result)})
            self._used_bytes(checkpoint)
            context.save_checkpoint(checkpoint.model_dump(mode="json"))
            row_total += result.row_count
            request_total += result.request_count
            del acquired, rows, records, results
            day += timedelta(days=1)
        root = self._root(checkpoint, request, row_total, request_total)
        raw = encode(root.model_dump(mode="json"))
        if len(raw) > MAX_ENVELOPE_BYTES or self._used_bytes(checkpoint) + len(raw) > MAX_TOTAL_BYTES:
            raise ValueError("finite root exceeds encoded envelope frontier")
        artifact = store_evidence(raw, MEDIA_TYPE, self.objects, self.artifacts)
        checkpoint = checkpoint.model_copy(update={"root": ArtifactRef(
            artifact_sha256=artifact.content_sha256, byte_size=artifact.byte_size)})
        context.save_checkpoint(checkpoint.model_dump(mode="json"))
        self._completed(checkpoint, request, digest)
        return checkpoint.model_dump(mode="json")
