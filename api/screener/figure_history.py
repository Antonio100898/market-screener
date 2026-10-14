"""Immutable exact companion records supplied by the existing financial engine."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from functools import lru_cache
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Callable, Literal, Union
from types import MappingProxyType
from pathlib import Path
from dataclasses import replace, asdict
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, TypeAdapter, ValidationInfo, field_validator, model_serializer, model_validator
from sqlalchemy import and_, func, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from .durable_jobs import DurableJobRepository, LeaseToken
from .postgres import (company_snapshot, company_snapshot_artifact, issuer, priced_security,
    source_item, source_observation, source_check_occurrence, figure_observation,
    exact_figure_bundle, figure_selection, figure_dependency, figure_bundle_coverage, figure_bundle_source)
from .postgres import (document_evidence, figure_bundle_evidence, evidence_artifact,
    data_release, release_company, release_candidate, figure_activation_transition,
    release_company_source)


Name = Annotated[str, StringConstraints(min_length=1, pattern=r"\S", strip_whitespace=False)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveId = Annotated[int, Field(gt=0)]
View = Literal["STRICT", "ASSUMED", "CONSERVATIVE"]


def decimal_text(value: str) -> str:
    """Validate without normalizing, rounding or converting the captured spelling."""
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
        raise ValueError("exact decimal must be a decimal string")
    try:
        if not Decimal(value).is_finite():
            raise ValueError("exact decimal must be finite")
    except InvalidOperation as exc:
        raise ValueError("unsupported Decimal representation") from exc
    return value


ExactText = Annotated[str, StringConstraints(min_length=1)]


class Record(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", revalidate_instances="always")


CONTRACT3_DOCUMENT_DOMAIN = "CONTRACT3_BOUNDED_DOCUMENT_PREPARATION1"
CONTRACT3_DOCUMENT_LIMITS = MappingProxyType(dict(records=64, artifacts=32, body_bytes=8 * 1024 * 1024,
    aggregate_bytes=64 * 1024 * 1024, record_bytes=65536, name_bytes=4096))
REGISTERED_NATIVE_DOMAIN = "REGISTERED_NATIVE_SOURCE_PREPARATION1"
_NATIVE_SOURCE_FILES = ("figure_history.py", "primary_input_plan.py", "primary_input_transport.py",
    "source_observations.py", "source_validity.py", "durable_jobs.py", "postgres.py", "shared_companies.py",
    "captured_assembly.py", "artifacts.py", "models.py", "evidence.py", "company_import.py",
    "sec_ingestion.py", "sources/inline_xbrl.py", "sources/sec_json.py")
_NATIVE_IMPORTED_HASHES = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                         for name in _NATIVE_SOURCE_FILES}


def _contract3_json(value):
    def default(item):
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise ValueError("unsupported contract3 record value")
    return json.dumps(value, default=default, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


@lru_cache(maxsize=4)
def _json_field_adapters(model):
    return MappingProxyType({name: TypeAdapter(field.rebuild_annotation())
                             for name, field in model.model_fields.items()})


def _contract3_record_bound(value, maximum=65536):
    value = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    pending = [(None, value)]
    while pending:
        name, item = pending.pop()
        if isinstance(item, BaseModel):
            item = item.model_dump(mode="json")
        if isinstance(item, dict):
            pending.extend(item.items())
        elif isinstance(item, (tuple, list)):
            pending.extend((name, child) for child in item)
        elif isinstance(item, str):
            limit = 16384 if name in {"value_decimal_text", "source_lexical_text", "constant_decimal_text", "coefficient_decimal_text"} else 4096
            if len(item.encode("utf-8")) > limit:
                raise ValueError("contract3 text exceeds its byte ceiling")
    if len(_contract3_json(value)) > maximum:
        raise ValueError("contract3 record exceeds its byte ceiling")


def _contract3_bundle_bounds(value):
    value = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    fields = dict(observations=ReportedObservation, selections=FigureSelection, document_evidence=DocumentEvidence,
                  sources=BundleSource, actions=ActionEvidence, assumptions=Assumption,
                  dependencies=CalculationDependency, coverage=Coverage)
    for field, model in fields.items():
        records = value.get(field, ())
        if not isinstance(records, (tuple, list)) or any(not isinstance(record, (dict, model)) for record in records):
            raise ValueError("contract3 records require structured arrays")
    selections = value.get("selections", ())
    outputs = sum((n.purpose if isinstance(n, FigureSelection) else n.get("purpose", "OUTPUT")) == "OUTPUT" for n in selections)
    for selection in selections:
        witnesses = selection.source_witnesses if isinstance(selection, FigureSelection) else selection.get("source_witnesses", ())
        if not isinstance(witnesses, (tuple, list)) or len(witnesses) > 300:
            raise ValueError("contract3 witness count exceeds its ceiling or array shape")
    documents = value.get("document_evidence", ())
    if outputs > 128 or len(value.get("sources", ())) > 64 or len(documents) > 64:
        raise ValueError("contract3 bundle count exceeds its ceiling")
    hashes = set()
    for document in documents:
        reference = document.reference if isinstance(document, DocumentEvidence) else document.get("reference")
        if not isinstance(reference, (SourceOccurrenceRef, dict)):
            raise ValueError("contract3 document reference requires a structured record")
        digest = reference.artifact_sha256 if isinstance(reference, SourceOccurrenceRef) else reference.get("artifact_sha256")
        if type(digest) is not str or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise ValueError("contract3 document requires an exact artifact digest")
        hashes.add(digest)
    if len(hashes) > 32:
        raise ValueError("contract3 document artifact count exceeds its ceiling")
    for field in fields:
        for record in value.get(field, ()):
            _contract3_record_bound(record)
            scope = record.scope if isinstance(record, (ReportedObservation, FigureSelection)) else record.get("scope", {}) if isinstance(record, dict) else {}
            if not isinstance(scope, (Scope, dict)):
                raise ValueError("contract3 scope requires a structured record")
            dimensions = scope.dimensions if isinstance(scope, Scope) else scope.get("dimensions", ())
            if not isinstance(dimensions, (tuple, list)) or len(dimensions) > 32:
                raise ValueError("contract3 dimension count exceeds its ceiling")
    _contract3_record_bound(value, 8 * 1024 * 1024)


class SecJsonLocation(Record):
    kind: Literal["SEC_JSON"] = "SEC_JSON"
    pointer: Name

    @field_validator("pointer")
    @classmethod
    def valid_pointer(cls, value):
        if not value.startswith("/") or re.search(r"~(?![01])", value):
            raise ValueError("escaped JSON pointer required")
        return value


class DeraLocation(Record):
    kind: Literal["DERA_TSV"] = "DERA_TSV"
    member: Name
    physical_row: Annotated[int, Field(ge=2)]
    accession: Name
    tag: Name
    taxonomy: Name
    ddate: date
    qtrs: Annotated[int, Field(ge=0)]
    coreg: str
    segments: str


class Dimension(Record):
    axis: Name
    member: Name | None = None
    typed_member_xml: Name | None = None

    @model_validator(mode="after")
    def one_member(self):
        if (self.member is None) == (self.typed_member_xml is None):
            raise ValueError("dimension requires exactly one explicit or typed member")
        return self


class XmlLocation(Record):
    kind: Literal["XBRL_XML"] = "XBRL_XML"
    member: Name
    element_ordinal: Annotated[int, Field(ge=0)]
    qname: Name
    element_id: Name | None = None
    context_ref: Name
    unit_ref: Name | None = None
    decimals: str | None = None


class InlineLocation(Record):
    kind: Literal["INLINE_XBRL"] = "INLINE_XBRL"
    fact_id: Name
    context_ref: Name
    unit_ref: Name | None = None
    transformation: Name | None = None
    sign: Literal["+", "-"] | None = None
    scale: int
    continuation_ids: tuple[Name, ...] = ()


NativeQName = Annotated[str, StringConstraints(pattern=r"^\{[^{}]+\}[^{}:]+$", max_length=4096)]
NATIVE_EXTRACTION_REVISION = "sec-native-numeric-v1"
NATIVE_TRANSFORMATION = "{http://www.xbrl.org/inlineXBRL/transformation/2020-02-12}num-dot-decimal"


class NativeXmlHistoryLocation(Record):
    kind: Literal["NATIVE_XBRL_XML"] = "NATIVE_XBRL_XML"
    ordinal_domain: Literal["ORIGINAL_INSTANCE_DIRECT_CHILD_ORDER"] = "ORIGINAL_INSTANCE_DIRECT_CHILD_ORDER"
    element_ordinal: Annotated[StrictInt, Field(ge=0)]
    qname: NativeQName
    fact_id: Name | None = None
    context_ref: Name
    unit_ref: Name
    decimals: str | None = None

    @model_validator(mode="after")
    def native_record_bound(self):
        _contract3_record_bound(self)
        return self

    @field_validator("decimals")
    @classmethod
    def native_decimals(cls, value):
        if value is not None and re.fullmatch(r"(?:INF|[+-]?[0-9]+)", value) is None:
            raise ValueError("invalid native decimals")
        return value


class NativeInlineHistoryLocation(NativeXmlHistoryLocation):
    kind: Literal["NATIVE_INLINE_XBRL"] = "NATIVE_INLINE_XBRL"
    ordinal_domain: Literal["ORIGINAL_NAMESPACE_RESOLVED_NONFRACTION_START_ORDER"] = "ORIGINAL_NAMESPACE_RESOLVED_NONFRACTION_START_ORDER"
    transformation: Literal["{http://www.xbrl.org/inlineXBRL/transformation/2020-02-12}num-dot-decimal"]
    sign: Literal["-"] | None = None
    scale: StrictInt


_NATIVE_LOCATIONS = (NativeXmlHistoryLocation, NativeInlineHistoryLocation)


class DocumentSpan(Record):
    kind: Literal["DOCUMENT_BYTES"] = "DOCUMENT_BYTES"
    start_byte: Annotated[int, Field(ge=0)]
    end_byte: Annotated[int, Field(gt=0)]
    span_sha256: Digest
    encoding: Literal["utf-8", "ascii"] = "utf-8"

    @model_validator(mode="after")
    def ordered_span(self):
        if self.end_byte <= self.start_byte:
            raise ValueError("document span requires ordered nonempty byte bounds")
        return self


Location = Annotated[Union[SecJsonLocation, DeraLocation, XmlLocation, InlineLocation,
                          NativeXmlHistoryLocation, NativeInlineHistoryLocation, DocumentSpan], Field(discriminator="kind")]


class SourceOccurrenceRef(Record):
    artifact_sha256: Digest
    extraction_revision: Name
    source_system: Literal["SEC", "EDINET"]
    issuer_source_identifier: Name
    source_document: Name
    annual_wrapper: Name | None = None
    location: Location
    fidelity: Literal["EXACT_LEXICAL", "DECODED_DECIMAL", "UNKNOWN"]
    source_published_at: datetime | None = None

    @field_validator("source_published_at")
    @classmethod
    def aware_time(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("source publication time must include timezone")
        return value

    @model_validator(mode="after")
    def system_matches_location(self):
        if self.location.kind in {"SEC_JSON", "DERA_TSV"} and self.source_system != "SEC":
            raise ValueError("SEC location requires SEC source")
        if isinstance(self.location, _NATIVE_LOCATIONS) and (
                self.source_system != "SEC" or self.fidelity != "EXACT_LEXICAL"
                or self.extraction_revision != NATIVE_EXTRACTION_REVISION):
            raise ValueError("native location requires its exact SEC extraction contract")
        return self


class Period(Record):
    kind: Literal["INSTANT", "DURATION", "UNKNOWN"]
    start: date | None = None
    end: date | None = None
    origin: Literal["REPORTED", "INFERRED", "UNKNOWN"]

    @model_validator(mode="after")
    def dates_match_kind(self):
        if self.kind == "INSTANT" and (self.start is not None or self.end is None):
            raise ValueError("instant requires end only")
        if self.kind == "DURATION" and (self.start is None or self.end is None or self.start > self.end):
            raise ValueError("duration requires ordered actual dates")
        if self.kind == "UNKNOWN" and (self.start is not None or self.end is not None or self.origin != "UNKNOWN"):
            raise ValueError("unknown period cannot invent dates")
        return self


class Unit(Record):
    status: Literal["KNOWN", "UNKNOWN"]
    numerator: tuple[Name, ...] = ()
    denominator: tuple[Name, ...] = ()
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    source_unit_ref: Name | None = None

    @model_validator(mode="after")
    def known_measures(self):
        if self.status == "KNOWN" and not self.numerator:
            raise ValueError("known unit requires measures, including explicit pure")
        if self.status == "UNKNOWN" and (self.numerator or self.denominator or self.currency):
            raise ValueError("unknown unit cannot declare measures")
        if self.currency is not None and f"iso4217:{self.currency}" not in self.numerator:
            raise ValueError("currency differs from canonical unit measure")
        return self


class Scope(Record):
    entity_identifier: Name | None = None
    entity_scheme: Name | None = None
    consolidation: Literal["CONSOLIDATED", "NONCONSOLIDATED", "UNKNOWN"]
    accounting_basis: Name | None = None
    dimensions: tuple[Dimension, ...] = ()
    dimensions_assessed: bool = False

    def complete(self):
        return bool(self.entity_identifier and self.entity_scheme and self.accounting_basis
                    and self.consolidation != "UNKNOWN" and self.dimensions_assessed)


class ShareBasis(Record):
    kind: Literal["ORDINARY", "RECEIPT", "CLASS", "NONE", "UNKNOWN"]
    security_class: Name | None = None
    incorporation: Literal["KNOWN", "UNKNOWN"]
    incorporated_actions: tuple[Name, ...] = ()
    evidence_keys: tuple[Name, ...] = ()
    anchor_evidence_key: Name | None = None
    anchor_as_of: date | None = None

    @model_serializer(mode="wrap")
    def compatible_record(self, handler):
        record = handler(self)
        if self.anchor_evidence_key is None and self.anchor_as_of is None:
            record.pop("anchor_evidence_key", None)
            record.pop("anchor_as_of", None)
        return record

    @model_validator(mode="after")
    def basis_shape(self):
        if self.kind in {"ORDINARY", "RECEIPT", "CLASS"} and self.security_class is None:
            raise ValueError("share basis requires actual security class")
        if self.incorporation == "UNKNOWN" and self.incorporated_actions:
            raise ValueError("unknown incorporation cannot declare incorporated actions")
        if len(set(self.incorporated_actions)) != len(self.incorporated_actions):
            raise ValueError("duplicate incorporated legal action")
        if len(set(self.evidence_keys)) != len(self.evidence_keys):
            raise ValueError("duplicate basis evidence")
        if (self.anchor_evidence_key is None) != (self.anchor_as_of is None):
            raise ValueError("relative basis requires both anchor evidence and as-of date")
        if self.anchor_evidence_key is not None and (
                self.anchor_evidence_key not in self.evidence_keys or self.security_class is None):
            raise ValueError("relative basis requires its documentary anchor and actual share class")
        return self


EvidenceRole = Literal["LEGAL_ACTION", "INCORPORATED_BASIS", "RECEIPT_TERMS", "CORRECTION_EXPLANATION", "SECURITY_CLASS"]


class DocumentEvidence(Record):
    key: Name
    reference: SourceOccurrenceRef
    roles: tuple[EvidenceRole, ...]

    @model_validator(mode="after")
    def document_only(self):
        if not isinstance(self.reference.location, DocumentSpan) or self.reference.fidelity != "EXACT_LEXICAL":
            raise ValueError("document evidence requires exact raw byte span")
        if not self.roles or len(set(self.roles)) != len(self.roles):
            raise ValueError("document evidence requires distinct roles")
        return self


def _document_identity(document):
    return dict(reference=document.reference.model_dump(mode="json", exclude={"source_published_at"}),
                roles=sorted(document.roles))


class DateWitness(Record):
    role: Literal["EFFECTIVE", "FILED", "DISCLOSED", "REPORTED"]
    date: date
    evidence_key: Name


class ActionEvidence(Record):
    key: Name
    legal_identity: Name
    security_class: Name
    old_shares_text: ExactText
    new_shares_text: ExactText
    evidence_keys: tuple[Name, ...]
    date_witnesses: tuple[DateWitness, ...] = ()

    @field_validator("old_shares_text", "new_shares_text")
    @classmethod
    def positive_parameter(cls, value):
        if Decimal(decimal_text(value)) <= 0:
            raise ValueError("action share terms must be positive")
        return value

    @model_validator(mode="after")
    def witnesses(self):
        if not self.evidence_keys or len(set(self.evidence_keys)) != len(self.evidence_keys):
            raise ValueError("action requires distinct document witnesses")
        if any(d.evidence_key not in self.evidence_keys for d in self.date_witnesses):
            raise ValueError("action date lacks its document witness")
        return self


class BasisTransition(Record):
    input_roles: tuple[Name, ...]
    action_keys: tuple[Name, ...] = ()
    evidence_keys: tuple[Name, ...]
    reason: Literal["validator-unavailable", "unknown-incorporation"]

    @model_validator(mode="after")
    def declared_inputs(self):
        if not self.input_roles or len(set(self.input_roles)) != len(self.input_roles):
            raise ValueError("basis transition requires distinct input roles")
        if not self.evidence_keys or len(set(self.evidence_keys)) != len(self.evidence_keys):
            raise ValueError("basis transition requires distinct document witnesses")
        if len(set(self.action_keys)) != len(self.action_keys):
            raise ValueError("basis transition cannot repeat legal actions")
        return self


class Assumption(Record):
    key: Name
    definition: Name
    definition_revision: Name
    reason: Name
    affected_input_roles: tuple[Name, ...]

    @model_validator(mode="after")
    def roles_required(self):
        if not self.affected_input_roles or len(set(self.affected_input_roles)) != len(self.affected_input_roles):
            raise ValueError("assumption requires distinct affected roles")
        return self


class ReportedObservation(Record):
    key: Name
    reference: SourceOccurrenceRef
    qname: Name
    value_decimal_text: ExactText | None
    source_lexical_text: str
    nil: bool = False
    precision: str | None = None
    unit: Unit
    period: Period
    scope: Scope
    share_basis: ShareBasis | None = None

    @model_serializer(mode="wrap")
    def compatible_record(self, handler):
        record = handler(self)
        if self.share_basis is None:
            record.pop("share_basis")
        return record

    @field_validator("value_decimal_text")
    @classmethod
    def finite_value(cls, value):
        return decimal_text(value) if value is not None else value

    @model_validator(mode="after")
    def nil_value(self):
        if isinstance(self.reference.location, DocumentSpan):
            raise ValueError("nonnumeric document evidence cannot be a reported observation")
        if self.nil != (self.value_decimal_text is None):
            raise ValueError("nil and numeric value must be distinct")
        if self.reference.fidelity == "EXACT_LEXICAL" and not self.nil:
            # Inline sign/scale/transformation may change the signed value; XML/JSON/TSV do not.
            if self.reference.location.kind not in {"INLINE_XBRL", "NATIVE_INLINE_XBRL"} and Decimal(decimal_text(self.source_lexical_text.strip())) != Decimal(self.value_decimal_text):
                raise ValueError("source lexical value differs from reported value")
        location = self.reference.location
        if isinstance(location, DeraLocation):
            if (location.qtrs == 0 and self.period.kind != "INSTANT") or (location.qtrs > 0 and self.period.kind != "DURATION"):
                raise ValueError("DERA qtrs contradicts actual period kind")
            if self.period.end != location.ddate or (self.period.kind == "DURATION" and self.period.origin != "INFERRED"):
                raise ValueError("DERA reports end/qtrs; duration start must remain inferred")
        if isinstance(location, XmlLocation) and self.qname != location.qname:
            raise ValueError("XML occurrence QName differs from observation")
        if isinstance(location, _NATIVE_LOCATIONS):
            if (self.nil or self.precision is not None or self.qname != location.qname
                    or self.unit.status != "KNOWN" or self.unit.source_unit_ref != location.unit_ref
                    or self.period.kind == "UNKNOWN" or self.period.origin != "REPORTED"):
                raise ValueError("native numeric structure differs from requested occurrence")
            if not isinstance(location, NativeInlineHistoryLocation):
                raw, decoded = Decimal(self.source_lexical_text.strip()), Decimal(self.value_decimal_text)
                if raw.is_zero() and decoded.is_zero() and raw.is_signed() != decoded.is_signed():
                    raise ValueError("native XML signed zero differs")
        return self


class BundleSource(Record):
    source_item_id: PositiveId
    source_observation_id: PositiveId
    occurrence_id: PositiveId | None = None


class SourceWitness(Record):
    ordinal: Annotated[StrictInt, Field(ge=0, le=299)]
    association_status: Literal["DECLARED_NOT_VERIFIED"]
    role: Literal["ORIGINAL_COMPANY_FACTS", "ORIGINAL_NATIVE", "REPLACEMENT_NATIVE",
                  "OPERAND_SOURCE", "CORRECTION_EXPLANATION", "DOCUMENT_SUPPORT"]
    observation_key: Name | None = None
    evidence_key: Name | None = None
    source: BundleSource

    @model_validator(mode="after")
    def declared_target(self):
        if (self.observation_key is None) == (self.evidence_key is None) or self.source.occurrence_id is None:
            raise ValueError("declared witness requires one target and an exact source occurrence")
        _contract3_record_bound(self, 4096)
        return self


class FigureSelection(Record):
    node_key: Name
    metric_family: Name
    metric_path: Name
    view: View
    state: Literal["REPORTED", "DERIVED", "ASSUMED", "MISSING", "NOT_ASSESSED", "INVALIDATED"]
    value_decimal_text: ExactText | None = None
    observation_key: Name | None = None
    definition: Name | None = None
    definition_revision: Name | None = None
    required_input_roles: tuple[Name, ...] = ()
    unit: Unit
    period: Period
    scope: Scope
    security_basis: Name
    receipt_ratio_text: ExactText | None = None
    reason: Name | None = None
    purpose: Literal["OUTPUT", "INTERMEDIATE"] = "OUTPUT"
    share_basis: ShareBasis | None = None
    basis_transition: BasisTransition | None = None
    assumption_keys: tuple[Name, ...] = ()
    source_witnesses: tuple[SourceWitness, ...] = ()

    @model_serializer(mode="wrap")
    def compatible_record(self, handler):
        record = handler(self)
        for name, absent in (("purpose", self.purpose == "OUTPUT"), ("share_basis", self.share_basis is None),
                             ("basis_transition", self.basis_transition is None), ("assumption_keys", not self.assumption_keys),
                             ("source_witnesses", not self.source_witnesses)):
            if absent:
                record.pop(name)
        return record

    @field_validator("value_decimal_text", "receipt_ratio_text")
    @classmethod
    def finite_value(cls, value):
        return decimal_text(value) if value is not None else value

    @model_validator(mode="after")
    def state_fields(self):
        if self.state in {"REPORTED", "DERIVED", "ASSUMED"} and self.value_decimal_text is None:
            raise ValueError("assessed numeric selection requires exact value")
        if self.state in {"MISSING", "NOT_ASSESSED", "INVALIDATED"} and (self.value_decimal_text is not None or not self.reason):
            raise ValueError("unavailable selection requires reason and no numeric value")
        if (self.state == "REPORTED") != (self.observation_key is not None):
            raise ValueError("only REPORTED selection binds an observation directly")
        calculated = self.state in {"DERIVED", "ASSUMED"}
        if calculated != bool(self.definition and self.definition_revision):
            raise ValueError("calculated selection requires existing definition and revision")
        if not calculated and (self.definition or self.definition_revision or self.required_input_roles):
            raise ValueError("noncalculated selection cannot carry computation metadata")
        if not calculated and (self.basis_transition or self.assumption_keys):
            raise ValueError("noncalculated selection cannot carry transition/assumption metadata")
        if self.state == "ASSUMED" and self.view != "ASSUMED":
            raise ValueError("assumed output cannot overwrite strict selection")
        if self.receipt_ratio_text is not None and Decimal(self.receipt_ratio_text) <= 0:
            raise ValueError("receipt ratio must be positive")
        if len(set(self.required_input_roles)) != len(self.required_input_roles):
            raise ValueError("required roles must be unique")
        if self.source_witnesses:
            if self.state != "NOT_ASSESSED" or self.purpose != "OUTPUT":
                raise ValueError("declared witnesses require an unassessed output")
            if len(self.source_witnesses) > 300 or [w.ordinal for w in self.source_witnesses] != list(range(len(self.source_witnesses))):
                raise ValueError("declared witness ordinals must be contiguous and bounded")
            identities = [(w.role, w.observation_key, w.evidence_key, w.source) for w in self.source_witnesses]
            if len(set(identities)) != len(identities):
                raise ValueError("duplicate declared witness association")
        return self


class CalculationDependency(Record):
    parent_node_key: Name
    ordinal: Annotated[int, Field(ge=0)]
    role: Name
    observation_key: Name | None = None
    selection_node_key: Name | None = None
    constant_decimal_text: ExactText | None = None
    coefficient_decimal_text: ExactText | None = None

    @field_validator("constant_decimal_text", "coefficient_decimal_text")
    @classmethod
    def finite_value(cls, value):
        return decimal_text(value) if value is not None else value

    @model_validator(mode="after")
    def one_input(self):
        if sum(value is not None for value in (self.observation_key, self.selection_node_key, self.constant_decimal_text)) != 1:
            raise ValueError("dependency requires exactly one observation, selection or constant")
        return self


class Coverage(Record):
    metric_family: Name
    view: View
    assessment: Literal["FULL", "PARTIAL", "NOT_ASSESSED"]
    metric_paths: tuple[Name, ...]
    reason: Name | None = None
    source_fidelity: Literal["EXACT_LEXICAL", "DECODED_DECIMAL", "UNKNOWN"]
    earlier_history: Literal["CAPTURED_ONLY", "RECOVERED_PARTIAL", "UNKNOWN"]

    @model_validator(mode="after")
    def honest_manifest(self):
        if not self.metric_paths or len(set(self.metric_paths)) != len(self.metric_paths):
            raise ValueError("coverage must name unique supplied metric paths")
        if self.assessment != "FULL" and not self.reason:
            raise ValueError("incomplete coverage requires explicit reason")
        return self


class ExactFigureBundle(Record):
    security_id: PositiveId
    snapshot_id: PositiveId
    snapshot_sha256: Digest
    engine_revision: Annotated[int, Field(gt=0)]
    pipeline_run_key: Name
    contract_revision: Literal[1, 2, 3] = 1
    observations: tuple[ReportedObservation, ...]
    selections: tuple[FigureSelection, ...]
    dependencies: tuple[CalculationDependency, ...] = ()
    coverage: tuple[Coverage, ...]
    sources: tuple[BundleSource, ...] = ()
    document_evidence: tuple[DocumentEvidence, ...] = ()
    actions: tuple[ActionEvidence, ...] = ()
    assumptions: tuple[Assumption, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def contract_input(cls, value, info: ValidationInfo):
        revision = value.contract_revision if isinstance(value, cls) else value.get("contract_revision", 1) if isinstance(value, dict) else None
        if type(revision) is not int or revision not in {1, 2, 3}:
            raise ValueError("exact supported bundle contract revision required")
        if revision == 3:
            _contract3_bundle_bounds(value)
        if info.mode == "json":
            # A before-validator receives Python containers; preserve JSON's strict tuple/date admission.
            adapters = _json_field_adapters(cls)
            value = {name: adapters[name].validate_json(
                _contract3_json(item), strict=True) if name in adapters else item
                for name, item in value.items()}
        return value

    @model_serializer(mode="wrap")
    def compatible_bundle(self, handler):
        record = handler(self)
        if self.contract_revision == 1:
            for name in ("document_evidence", "actions", "assumptions"):
                record.pop(name)
        return record

    @model_validator(mode="after")
    def validate_graph(self):
        native = any(isinstance(o.reference.location, _NATIVE_LOCATIONS) for o in self.observations)
        if self.contract_revision != 3 and (native or any(n.source_witnesses for n in self.selections)):
            raise ValueError("native locations and declared witnesses require contract revision3")
        if self.contract_revision == 1 and (self.document_evidence or self.actions or self.assumptions
                or any(o.share_basis is not None for o in self.observations)
                or any(n.purpose != "OUTPUT" or n.share_basis is not None or n.basis_transition is not None or n.assumption_keys for n in self.selections)):
            raise ValueError("contract2 semantics require contract revision2")
        for field, key in (("observations", lambda r: r.key), ("selections", lambda r: r.node_key),
                           ("dependencies", lambda r: (r.parent_node_key, r.ordinal)),
                           ("coverage", lambda r: (r.metric_family, r.view)),
                           ("sources", lambda r: (r.source_item_id, r.source_observation_id)),
                           ("document_evidence", lambda r: r.key), ("actions", lambda r: r.key),
                           ("assumptions", lambda r: r.key)):
            object.__setattr__(self, field, tuple(sorted(getattr(self, field), key=key)))
        observations = {row.key: row for row in self.observations}
        nodes = {row.node_key: row for row in self.selections}
        if len(observations) != len(self.observations) or len(nodes) != len(self.selections):
            raise ValueError("duplicate observation or selection key")
        identities = [(n.metric_path, n.view, n.period.kind, n.period.start, n.period.end)
                      for n in self.selections if n.purpose == "OUTPUT"]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate metric/period/view selection")
        dependencies = {key: [] for key in nodes}
        edges = {key: [] for key in nodes}
        for row in self.dependencies:
            if row.parent_node_key not in nodes or nodes[row.parent_node_key].state not in {"DERIVED", "ASSUMED"}:
                raise ValueError("dependency parent must be a calculated node")
            parent = nodes[row.parent_node_key]
            if row.observation_key is not None and row.observation_key not in observations:
                raise ValueError("unknown observation dependency")
            if row.selection_node_key is not None:
                if row.selection_node_key not in nodes:
                    raise ValueError("unknown selection dependency")
                child = nodes[row.selection_node_key]
                if self.contract_revision == 1 and (child.view != parent.view or child.security_basis != parent.security_basis or child.receipt_ratio_text != parent.receipt_ratio_text):
                    raise ValueError("dependency view/security basis differs")
                edges[row.parent_node_key].append(row.selection_node_key)
            dependencies[row.parent_node_key].append(row)
        visiting, visited = set(), set()

        def visit(key):
            if key in visiting:
                raise ValueError("cyclic selection graph")
            if key in visited:
                return
            visiting.add(key)
            for child in edges[key]:
                visit(child)
            visiting.remove(key)
            visited.add(key)

        for key in nodes:
            visit(key)
        for key, node in nodes.items():
            inputs = dependencies[key]
            if len({d.ordinal for d in inputs}) != len(inputs) or len({d.role for d in inputs}) != len(inputs):
                raise ValueError("duplicate dependency ordinal or role")
            if node.state in {"DERIVED", "ASSUMED"} and (not node.required_input_roles or set(node.required_input_roles) != {d.role for d in inputs}):
                raise ValueError("calculated node lacks declared input roles")
            if node.state == "REPORTED":
                observed = observations.get(node.observation_key)
                if observed is None or observed.nil or any(getattr(node, field) != getattr(observed, field) for field in ("value_decimal_text", "unit", "period", "scope")):
                    raise ValueError("reported selection must preserve its exact observation")
                if node.share_basis != observed.share_basis:
                    raise ValueError("reported selection must preserve observed share basis")
        if self.contract_revision in {2, 3}:
            self._validate_contract2(nodes, observations, dependencies, edges)
        if self.contract_revision == 3:
            documents = {d.key: d for d in self.document_evidence}
            for node in nodes.values():
                for witness in node.source_witnesses:
                    if witness.source not in self.sources:
                        raise ValueError("declared witness source is outside the bundle")
                    if witness.observation_key is not None:
                        target = observations.get(witness.observation_key)
                        if target is None or witness.role in {"CORRECTION_EXPLANATION", "DOCUMENT_SUPPORT"}:
                            raise ValueError("declared numeric witness target or role differs")
                        if witness.role == "ORIGINAL_COMPANY_FACTS" and not isinstance(target.reference.location, SecJsonLocation):
                            raise ValueError("Company Facts witness requires its JSON occurrence")
                        if witness.role in {"ORIGINAL_NATIVE", "REPLACEMENT_NATIVE"} and not isinstance(target.reference.location, _NATIVE_LOCATIONS):
                            raise ValueError("native witness requires its native occurrence")
                    else:
                        target = documents.get(witness.evidence_key)
                        if target is None or witness.role not in {"CORRECTION_EXPLANATION", "DOCUMENT_SUPPORT"}:
                            raise ValueError("declared documentary witness target or role differs")
                        if witness.role == "CORRECTION_EXPLANATION" and witness.role not in target.roles:
                            raise ValueError("correction explanation requires its actual documentary role")
        coverage = {(c.metric_family, c.view): c for c in self.coverage}
        if len(coverage) != len(self.coverage):
            raise ValueError("duplicate coverage family/view")
        for node in nodes.values():
            if node.purpose == "INTERMEDIATE":
                continue
            covered = coverage.get((node.metric_family, node.view))
            if covered is None or node.metric_path not in covered.metric_paths:
                raise ValueError("selection absent from coverage manifest")
        for covered in self.coverage:
            selected = [n for n in nodes.values() if n.purpose == "OUTPUT" and (n.metric_family, n.view) == (covered.metric_family, covered.view)]
            if covered.assessment != "FULL":
                continue
            if {n.metric_path for n in selected} != set(covered.metric_paths) or not selected:
                raise ValueError("FULL coverage lacks named selections")
            if covered.source_fidelity != "EXACT_LEXICAL" or not self.sources or any(s.occurrence_id is None for s in self.sources):
                raise ValueError("FULL coverage requires exact source and accepted witnesses")
            for node in selected:
                if node.state in {"NOT_ASSESSED", "INVALIDATED"} or node.unit.status != "KNOWN" or node.period.origin == "UNKNOWN" or not node.scope.complete():
                    raise ValueError("FULL coverage has unknown selection basis")
                self._require_exact_inputs(node.node_key, nodes, observations, dependencies, set())
        if len({(s.source_item_id, s.source_observation_id) for s in self.sources}) != len(self.sources):
            raise ValueError("duplicate source binding")
        return self

    def _validate_contract2(self, nodes, observations, dependencies, edges):
        documents = {d.key: d for d in self.document_evidence}
        actions = {a.key: a for a in self.actions}
        assumptions = {a.key: a for a in self.assumptions}
        if len(documents) != len(self.document_evidence) or len(actions) != len(self.actions) or len(assumptions) != len(self.assumptions):
            raise ValueError("duplicate contract2 manifest key")
        if len({a.legal_identity for a in self.actions}) != len(self.actions):
            raise ValueError("repeated disclosure cannot create duplicate legal action")

        def evidence(keys, role=None):
            if any(k not in documents or (role and role not in documents[k].roles) for k in keys):
                raise ValueError("missing document witness or required evidence role")

        for action in self.actions:
            evidence(action.evidence_keys, "LEGAL_ACTION")
        bases = [o.share_basis for o in observations.values()] + [n.share_basis for n in nodes.values()]
        for basis in bases:
            if basis is None:
                continue
            evidence(basis.evidence_keys, "INCORPORATED_BASIS")
            if any(k not in actions or actions[k].security_class != basis.security_class for k in basis.incorporated_actions):
                raise ValueError("incorporated action differs from actual share class")
            if basis.incorporation == "KNOWN" and (basis.kind == "UNKNOWN" or (basis.kind != "NONE" and not basis.evidence_keys)):
                raise ValueError("known per-column share basis requires document evidence")
        for key, node in nodes.items():
            declared = [assumptions[k] for k in node.assumption_keys if k in assumptions]
            if len(declared) != len(node.assumption_keys) or len(set(node.assumption_keys)) != len(node.assumption_keys):
                raise ValueError("unknown or duplicate declared assumption")
            roles = {d.role for d in dependencies[key]}
            if declared and (node.view != "ASSUMED" or any(a.definition != node.definition or a.definition_revision != node.definition_revision
                    or not set(a.affected_input_roles) <= roles for a in declared)):
                raise ValueError("assumption must belong to actual assumed definition/input roles")
            if node.view == "ASSUMED" and node.state == "ASSUMED" and not declared:
                raise ValueError("assumed node requires actual declared assumptions")
            covered_roles = {role for a in declared for role in a.affected_input_roles}
            crossing = set()
            for dependency in dependencies[key]:
                child = nodes.get(dependency.selection_node_key)
                observed = observations.get(dependency.observation_key)
                input_basis = child.share_basis if child else observed.share_basis if observed else None
                if child and child.view != node.view:
                    if child.view != "STRICT" or node.view != "ASSUMED" or dependency.role not in covered_roles:
                        raise ValueError("cross-view input requires explicit strict-to-assumed role")
                if node.view == "ASSUMED" and child is None and dependency.role not in covered_roles:
                    raise ValueError("raw strict input requires explicit assumed role")
                if ((child and (child.security_basis != node.security_basis or child.receipt_ratio_text != node.receipt_ratio_text))
                        or ((child or observed) and input_basis != node.share_basis)):
                    crossing.add(dependency.role)
            transition = node.basis_transition
            if crossing and (transition is None or set(transition.input_roles) != crossing):
                raise ValueError("crossing basis requires explicit actual input roles")
            if transition:
                if not crossing or set(transition.input_roles) != crossing:
                    raise ValueError("basis transition does not match actual input bases")
                evidence(transition.evidence_keys)
                if any(k not in actions for k in transition.action_keys):
                    raise ValueError("transition references unknown legal action")
                if any(k not in transition.evidence_keys for action in transition.action_keys for k in actions[action].evidence_keys):
                    raise ValueError("transition omits legal action witnesses")
                for dependency in dependencies[key]:
                    if dependency.role not in transition.input_roles:
                        continue
                    child = nodes.get(dependency.selection_node_key)
                    observed = observations.get(dependency.observation_key)
                    input_basis = child.share_basis if child else observed.share_basis if observed else None
                    before_anchor = None if input_basis is None else input_basis.anchor_evidence_key
                    after_anchor = None if node.share_basis is None else node.share_basis.anchor_evidence_key
                    if before_anchor is not None or after_anchor is not None:
                        if (before_anchor is None or after_anchor is None
                                or input_basis.anchor_as_of != node.share_basis.anchor_as_of
                                or input_basis.security_class != node.share_basis.security_class
                                or _document_identity(documents[before_anchor])["reference"]
                                != _document_identity(documents[after_anchor])["reference"]):
                            raise ValueError("relative transition requires the same authenticated anchor/as-of/share class")
                    for action_key in transition.action_keys:
                        action = actions[action_key]
                        if (input_basis is None or node.share_basis is None
                                or input_basis.security_class != action.security_class
                                or node.share_basis.security_class != action.security_class):
                            raise ValueError("transition action differs from actual input/output share class")
                    if (input_basis is not None and node.share_basis is not None
                            and input_basis.incorporation == node.share_basis.incorporation == "KNOWN"
                            and set(input_basis.incorporated_actions) ^ set(node.share_basis.incorporated_actions)
                            != set(transition.action_keys)):
                        raise ValueError("transition actions differ from actual incorporation change")
                coverage = next((c for c in self.coverage if (c.metric_family, c.view) == (node.metric_family, node.view)), None)
                if node.purpose == "OUTPUT" and (coverage is None or coverage.assessment != "PARTIAL" or coverage.reason != transition.reason):
                    raise ValueError("unsupported crossing requires explicit PARTIAL reason")
        # Check ancestry independently of immediate view labels.
        tainted = {}
        def assumed_ancestry(key):
            if key not in tainted:
                tainted[key] = nodes[key].view == "ASSUMED" or bool(nodes[key].assumption_keys) or any(assumed_ancestry(c) for c in edges[key])
            return tainted[key]
        for key, node in nodes.items():
            if node.view == "STRICT" and assumed_ancestry(key):
                raise ValueError("assumed ancestry cannot reach strict output")
            if node.purpose == "OUTPUT":
                pending, reachable = [key], set()
                reasons = set()
                while pending:
                    current = pending.pop()
                    if current in reachable:
                        continue
                    reachable.add(current)
                    basis = nodes[current].share_basis
                    measures = (*nodes[current].unit.numerator, *nodes[current].unit.denominator)
                    share_measures = {"shares", "xbrli:shares", "{http://www.xbrl.org/2003/instance}shares"}
                    if share_measures.intersection(measures) and (basis is None or basis.kind in {"NONE", "UNKNOWN"}):
                        reasons.add("unknown-incorporation")
                    transition = nodes[current].basis_transition
                    if transition:
                        reasons.add(transition.reason)
                    if basis is not None and (basis.kind == "UNKNOWN" or basis.incorporation == "UNKNOWN"):
                        reasons.add("unknown-incorporation")
                    for d in dependencies[current]:
                        observed = observations.get(d.observation_key)
                        if observed and share_measures.intersection((*observed.unit.numerator, *observed.unit.denominator)) and (observed.share_basis is None or observed.share_basis.kind in {"NONE", "UNKNOWN"}):
                            reasons.add("unknown-incorporation")
                        if observed and observed.share_basis is not None and (observed.share_basis.kind == "UNKNOWN" or observed.share_basis.incorporation == "UNKNOWN"):
                            reasons.add("unknown-incorporation")
                    pending.extend(edges[current])
                if reasons:
                    covered = next((c for c in self.coverage if (c.metric_family, c.view) == (node.metric_family, node.view)), None)
                    if covered is None or covered.assessment != "PARTIAL" or covered.reason not in reasons:
                        raise ValueError("unsupported ancestry requires explicit PARTIAL reason")

    @staticmethod
    def _require_exact_inputs(key, nodes, observations, dependencies, visited):
        if key in visited:
            return
        visited.add(key)
        node = nodes[key]
        if node.unit.status != "KNOWN" or node.period.kind == "UNKNOWN" or node.period.origin == "UNKNOWN":
            raise ValueError("FULL dependency has unknown unit/actual period")
        if node.basis_transition is not None:
            raise ValueError("FULL cannot certify unsupported crossing math")
        if node.share_basis is not None and (node.share_basis.kind == "UNKNOWN" or node.share_basis.incorporation == "UNKNOWN"):
            raise ValueError("FULL has unknown share incorporation")
        if node.state in {"NOT_ASSESSED", "INVALIDATED", "MISSING"}:
            if node.state != "MISSING":
                raise ValueError("FULL dependency is unassessed")
            return
        refs = [node.observation_key] if node.observation_key is not None else []
        refs.extend(d.observation_key for d in dependencies[key] if d.observation_key is not None)
        for ref in refs:
            observed = observations[ref]
            if observed.share_basis is not None and (observed.share_basis.kind == "UNKNOWN" or observed.share_basis.incorporation == "UNKNOWN"):
                raise ValueError("FULL dependency has unknown share incorporation")
            if observed.reference.fidelity != "EXACT_LEXICAL" or observed.unit.status != "KNOWN" or observed.period.origin == "UNKNOWN" or not observed.scope.complete():
                raise ValueError("FULL dependency lacks exact source/basis")
            if observed.scope != node.scope:
                raise ValueError("FULL dependency reporting scope differs")
        for dependency in dependencies[key]:
            if dependency.selection_node_key:
                if nodes[dependency.selection_node_key].scope != node.scope:
                    raise ValueError("FULL dependency reporting scope differs")
                ExactFigureBundle._require_exact_inputs(dependency.selection_node_key, nodes, observations, dependencies, visited)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class FigureHistoryConflict(ValueError):
    pass


@dataclass(frozen=True)
class PreparedDocumentEvidence:
    record_sha256: str
    verification_seal: bytes


@dataclass(frozen=True)
class PreparedContract3DocumentEvidence:
    record_sha256: str
    limits_sha256: str
    verification_seal: bytes


@dataclass(frozen=True)
class PreparedRegisteredNative:
    plan_sha256: str
    cik: str
    company_facts_occurrence_id: int
    security_identifier: str
    engine_identity_sha256: str
    schema_revision: str
    plan: object
    company_facts_source: object
    artifact_metadata: tuple
    graph_gaps: tuple
    manifest: dict
    observations: tuple
    document_evidence: tuple
    sources: tuple
    artifacts: tuple
    prepared_documents: PreparedContract3DocumentEvidence
    verification_seal: bytes


@dataclass(frozen=True)
class PreparedNativeReconstruction:
    recipe_sha256: str
    snapshot_id: int
    security_id: int
    engine_identity_sha256: str
    schema_revision: str
    payload: bytes
    observations: tuple
    selections: tuple
    coverage: tuple
    sources: tuple
    document_evidence: tuple
    verification_seal: bytes


NATIVE_RECONSTRUCTION_DOMAIN = "REGISTERED_NATIVE_RECONSTRUCTION1"
NATIVE_RECONSTRUCTION_LIMITS = MappingProxyType(dict(facts=4096, edges=8192, depth=32,
    outputs=128, sources=64, documents=64, witnesses=300, artifacts=32,
    aggregate_bytes=64 * 1024 * 1024, envelope_bytes=8 * 1024 * 1024,
    recipe_bytes=1024 * 1024, control_bytes=1024 * 1024, body_bytes=8 * 1024 * 1024,
    company_facts_bytes=32 * 1024 * 1024, requests=64, xml_work_bytes=128 * 1024 * 1024,
    record_bytes=65536, witness_bytes=4096, name_bytes=4096, numeric_text_bytes=16384))
_RECONSTRUCTION_SOURCE_FILES = (*_NATIVE_SOURCE_FILES, "sync.py", "normalize.py", "exact_annual.py")
_RECONSTRUCTION_IMPORTED_HASHES = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                                  for name in _RECONSTRUCTION_SOURCE_FILES}


def _native_reconstruction_identity():
    current = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
               for name in _RECONSTRUCTION_SOURCE_FILES}
    if current != _RECONSTRUCTION_IMPORTED_HASHES:
        raise ValueError("native reconstruction executing source changed; restart frozen process")
    return _registered_native_identity()


def _native_reconstruction_headers(value):
    from .primary_input_plan import _integer, _digest as admit_sha
    from .durable_jobs import _bounded_schedule_parameters, _bounded_schedule_text
    for name in ("snapshot_id", "security_id"):
        _integer(value[name])
    for name in ("recipe_sha256", "engine_identity_sha256"):
        admit_sha(value[name])
    _bounded_schedule_text("reconstruction schema", value["schema_revision"], 32)
    return _bounded_schedule_parameters(value)[0]


CONTRACT3_DOCUMENT_LIMITS_SHA256 = _digest(dict(CONTRACT3_DOCUMENT_LIMITS))


def _contract3_document_proof_digest(record_sha256):
    return _digest(dict(domain=CONTRACT3_DOCUMENT_DOMAIN, limits_sha256=CONTRACT3_DOCUMENT_LIMITS_SHA256,
                        record_sha256=record_sha256))


def _registered_native_identity():
    from .captured_assembly import engine_identity
    current = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
               for name in _NATIVE_SOURCE_FILES}
    if current != _NATIVE_IMPORTED_HASHES:
        raise ValueError("native preparation source changed since import; restart frozen process")
    return engine_identity()


def _native_observation(value, cik):
    from .models import NativeInlineOrdinalLocation
    request, source = value.request, value.request.source
    location = dict(element_ordinal=request.location.element_ordinal, qname=request.location.fact_qname,
        fact_id=request.location.fact_id, context_ref=request.location.context_ref,
        unit_ref=request.location.unit_ref, decimals=request.decimals)
    if isinstance(request.location, NativeInlineOrdinalLocation):
        locator = NativeInlineHistoryLocation(**location, transformation=request.transformation,
            sign=request.sign, scale=request.scale)
    else:
        locator = NativeXmlHistoryLocation(**location)
    measures = lambda values: tuple("iso4217:" + name.split("}", 1)[1]
        if name.startswith("{http://www.xbrl.org/2003/iso4217}") else name for name in values)
    numerator, denominator = measures(request.unit_numerator), measures(request.unit_denominator)
    currencies = [name.split(":", 1)[1] for name in numerator if name.startswith("iso4217:")]
    unit = Unit(status="KNOWN", numerator=numerator, denominator=denominator,
        currency=currencies[0] if len(currencies) == 1 else None, source_unit_ref=request.location.unit_ref)
    context = request.context
    period = Period(kind="INSTANT" if context.instant is not None else "DURATION", start=context.start,
        end=context.instant or context.end, origin="REPORTED")
    scope = Scope(entity_identifier=context.entity_identifier, entity_scheme=context.entity_scheme,
        consolidation="UNKNOWN", dimensions_assessed=True, dimensions=tuple(Dimension(axis=d.axis,
            member=d.member if d.kind == "explicit" else None,
            typed_member_xml=d.member if d.kind == "typed" else None) for d in context.dimensions))
    reference = SourceOccurrenceRef(artifact_sha256=source.artifact_sha256,
        extraction_revision=NATIVE_EXTRACTION_REVISION, source_system="SEC", issuer_source_identifier=cik,
        source_document=source.source_document, annual_wrapper=source.annual_accession,
        location=locator, fidelity="EXACT_LEXICAL")
    key = _digest(dict(artifact_sha256=source.artifact_sha256, extraction_revision=NATIVE_EXTRACTION_REVISION,
                       location=locator.model_dump(mode="json")))
    per_share = "{http://www.xbrl.org/2003/instance}shares" in (*request.unit_numerator, *request.unit_denominator)
    observed = ReportedObservation(key=key, reference=reference, qname=locator.qname,
        value_decimal_text=value.decoded_text, source_lexical_text=value.lexical_text,
        unit=unit, period=period, scope=scope,
        share_basis=ShareBasis(kind="UNKNOWN", incorporation="UNKNOWN") if per_share else None)
    _contract3_record_bound(observed)
    return observed


def _registered_native_payload(prepared):
    from .primary_input_plan import NATIVE_PREPARATION_LIMITS, encode_primary_plan, decode_native_preparation_plan
    raw = encode_primary_plan(prepared.plan)
    decode_native_preparation_plan(raw)
    payload = dict(domain=REGISTERED_NATIVE_DOMAIN, limits=dict(NATIVE_PREPARATION_LIMITS),
        plan_sha256=prepared.plan_sha256, plan=json.loads(raw), cik=prepared.cik,
        company_facts_occurrence_id=prepared.company_facts_occurrence_id,
        security_identifier=prepared.security_identifier, engine_identity_sha256=prepared.engine_identity_sha256,
        schema_revision=prepared.schema_revision, company_facts_source=asdict(prepared.company_facts_source),
        artifact_metadata=prepared.artifact_metadata, graph_gaps=prepared.graph_gaps, manifest=prepared.manifest,
        observations=[o.model_dump(mode="json") for o in prepared.observations],
        document_evidence=[d.model_dump(mode="json") for d in prepared.document_evidence],
        sources=[s.model_dump(mode="json") for s in prepared.sources], artifacts=[asdict(a) for a in prepared.artifacts],
        prepared_documents=dict(record_sha256=prepared.prepared_documents.record_sha256,
            limits_sha256=prepared.prepared_documents.limits_sha256,
            verification_seal=prepared.prepared_documents.verification_seal.hex()))
    if len(_contract3_json(payload)) > NATIVE_PREPARATION_LIMITS["envelope_bytes"]:
        raise ValueError("native preparation envelope ceiling exceeded")
    return payload


class ActivationTransition(Record):
    release_id: PositiveId
    parent_release_id: PositiveId | None
    security_id: PositiveId
    metric_path: Name
    view: View
    period_kind: Literal["INSTANT", "DURATION", "UNKNOWN"]
    period_start: date | None
    period_end: date | None
    previous_bundle_id: PositiveId | None
    previous_node_key: Name | None
    replacement_bundle_id: PositiveId | None
    replacement_node_key: Name | None
    value_text_changed: bool
    state_changed: bool
    provenance_changed: bool
    endpoint_presence_changed: bool
    declared_context_equal: bool
    context_assessment: Literal["NOT_ASSESSED", "INCOMPARABLE"]
    reason: Name
    contract_revision: Literal[1] = 1

    @model_validator(mode="after")
    def endpoint_shape(self):
        for bundle, node in ((self.previous_bundle_id, self.previous_node_key),
                             (self.replacement_bundle_id, self.replacement_node_key)):
            if (bundle is None) != (node is None):
                raise ValueError("activation endpoint requires bundle and node")
        if self.previous_bundle_id is None and self.replacement_bundle_id is None:
            raise ValueError("activation inventory requires an actual endpoint")
        if self.parent_release_id is None and self.previous_bundle_id is not None:
            raise ValueError("initial activation cannot have a previous endpoint")
        if self.period_kind == "UNKNOWN" and (self.context_assessment != "NOT_ASSESSED" or self.reason != "unknown-period-unmatched"):
            raise ValueError("unknown period requires unmatched inventory assessment")
        Period(kind=self.period_kind, start=self.period_start, end=self.period_end,
               origin="UNKNOWN" if self.period_kind == "UNKNOWN" else "REPORTED")
        return self


class OutputGraphProjection:
    """Resolve reachable meanings once; these digests are not catalog identities."""
    def __init__(self, bundle: ExactFigureBundle):
        self.bundle = bundle
        self.nodes = {n.node_key: n for n in bundle.selections}
        self.observations = {o.key: o for o in bundle.observations}
        self.documents = {d.key: d for d in bundle.document_evidence}
        self.actions = {a.key: a for a in bundle.actions}
        self.assumptions = {a.key: a for a in bundle.assumptions}
        self.dependencies = {key: [] for key in self.nodes}
        for dependency in bundle.dependencies:
            self.dependencies[dependency.parent_node_key].append(dependency)
        for inputs in self.dependencies.values():
            inputs.sort(key=lambda d: d.ordinal)
        self._nodes, self._observations, self._documents = {}, {}, {}

    @staticmethod
    def identity(node):
        return node.metric_path, node.view, node.period.kind, node.period.start, node.period.end

    def outputs(self):
        return {self.identity(n): n for n in self.nodes.values() if n.purpose == "OUTPUT"}

    @staticmethod
    def _ordered(values):
        return sorted(values, key=lambda v: json.dumps(v, sort_keys=True, separators=(",", ":")))

    @staticmethod
    def _reference(reference):
        # Publication clocks remain available on the immutable reference itself.
        return reference.model_dump(mode="json", exclude={"source_published_at"})

    def _document(self, key):
        if key not in self._documents:
            document = self.documents[key]
            self._documents[key] = _document_identity(document)
        return self._documents[key]

    def _action(self, key):
        action = self.actions[key]
        return dict(legal_identity=action.legal_identity, security_class=action.security_class,
            old_shares_text=action.old_shares_text, new_shares_text=action.new_shares_text,
            witnesses=self._ordered([self._document(k) for k in action.evidence_keys]),
            dates=self._ordered([dict(role=w.role, date=w.date.isoformat(), evidence=self._document(w.evidence_key)) for w in action.date_witnesses]))

    def _basis(self, basis):
        if basis is None:
            return None
        record = dict(kind=basis.kind, security_class=basis.security_class, incorporation=basis.incorporation,
            actions=self._ordered([self._action(k) for k in basis.incorporated_actions]),
            evidence=self._ordered([self._document(k) for k in basis.evidence_keys]))
        if basis.anchor_evidence_key is not None:
            record["anchor"] = self._anchor(basis)
        return record

    def _anchor(self, basis):
        return dict(evidence=dict(reference=self._document(basis.anchor_evidence_key)["reference"]),
                    as_of=basis.anchor_as_of.isoformat())

    @staticmethod
    def _unit(unit):
        return {**unit.model_dump(mode="json"), "numerator": sorted(unit.numerator), "denominator": sorted(unit.denominator)}

    def _scope(self, scope):
        return {**scope.model_dump(mode="json"), "dimensions": self._ordered([d.model_dump(mode="json") for d in scope.dimensions])}

    def context(self, node):
        basis = node.share_basis
        record = dict(unit=self._unit(node.unit), period=node.period.model_dump(mode="json"), scope=self._scope(node.scope),
            security_basis=node.security_basis, receipt_ratio_text=node.receipt_ratio_text,
            share_basis=None if basis is None else dict(kind=basis.kind, security_class=basis.security_class,
                incorporation=basis.incorporation, actions=self._ordered([
                    {name: self._action(k)[name] for name in ("legal_identity", "security_class", "old_shares_text", "new_shares_text")}
                    for k in basis.incorporated_actions])))
        if basis is not None and basis.anchor_evidence_key is not None:
            record["share_basis"]["anchor"] = self._anchor(basis)
        return record

    def observation(self, key):
        if key not in self._observations:
            observed = self.observations[key]
            self._observations[key] = _digest(dict(reference=self._reference(observed.reference),
                qname=observed.qname, value_decimal_text=observed.value_decimal_text,
                source_lexical_text=observed.source_lexical_text, nil=observed.nil, precision=observed.precision,
                unit=self._unit(observed.unit), period=observed.period.model_dump(mode="json"),
                scope=self._scope(observed.scope), share_basis=self._basis(observed.share_basis)))
        return self._observations[key]

    def node(self, key):
        if key not in self._nodes:
            node = self.nodes[key]
            inputs = [dict(ordinal=d.ordinal, role=d.role, coefficient=d.coefficient_decimal_text,
                constant=d.constant_decimal_text, observation=self.observation(d.observation_key) if d.observation_key else None,
                selection=self.node(d.selection_node_key) if d.selection_node_key else None) for d in self.dependencies[key]]
            transition = node.basis_transition
            assumptions = [self.assumptions[k].model_dump(mode="json", exclude={"key"}) for k in node.assumption_keys]
            for assumption in assumptions:
                assumption["affected_input_roles"] = sorted(assumption["affected_input_roles"])
            provenance = dict(metric_family=node.metric_family, metric_path=node.metric_path, view=node.view,
                unit=self._unit(node.unit), period=node.period.model_dump(mode="json"), scope=self._scope(node.scope),
                security_basis=node.security_basis, receipt_ratio_text=node.receipt_ratio_text, share_basis=self._basis(node.share_basis),
                reason=node.reason, definition=node.definition, definition_revision=node.definition_revision,
                required_input_roles=sorted(node.required_input_roles), observation=self.observation(node.observation_key) if node.observation_key else None,
                inputs=inputs, assumptions=self._ordered(assumptions),
                transition=None if transition is None else dict(input_roles=sorted(transition.input_roles), reason=transition.reason,
                    actions=self._ordered([self._action(k) for k in transition.action_keys]),
                    evidence=self._ordered([self._document(k) for k in transition.evidence_keys])))
            if node.source_witnesses:
                provenance["source_witnesses"] = [dict(ordinal=w.ordinal, role=w.role,
                    association_status=w.association_status, source=w.source.model_dump(mode="json"),
                    observation=self.observation(w.observation_key) if w.observation_key is not None else None,
                    document=self._document(w.evidence_key) if w.evidence_key is not None else None)
                    for w in node.source_witnesses]
            self._nodes[key] = dict(provenance=_digest(provenance),
                meaning=_digest(dict(provenance=provenance, value=node.value_decimal_text, state=node.state)))
        return self._nodes[key]["meaning"]

    def provenance(self, node):
        self.node(node.node_key)
        return self._nodes[node.node_key]["provenance"]


def _context_conflicts(previous, replacement):
    """Conflicting explicit descriptors are not evidence permitting subtraction."""
    a, b = previous, replacement
    if a["unit"]["status"] == b["unit"]["status"] == "KNOWN":
        if any(a["unit"][k] != b["unit"][k] for k in ("numerator", "denominator", "currency")):
            return True
    for name in ("entity_identifier", "entity_scheme", "accounting_basis", "consolidation"):
        x, y = a["scope"][name], b["scope"][name]
        if x is not None and y is not None and x != "UNKNOWN" and y != "UNKNOWN" and x != y:
            return True
    if a["scope"]["dimensions_assessed"] and b["scope"]["dimensions_assessed"] and a["scope"]["dimensions"] != b["scope"]["dimensions"]:
        return True
    if a["security_basis"] != "UNKNOWN" and b["security_basis"] != "UNKNOWN" and a["security_basis"] != b["security_basis"]:
        return True
    if a["receipt_ratio_text"] is not None and b["receipt_ratio_text"] is not None and Decimal(a["receipt_ratio_text"]) != Decimal(b["receipt_ratio_text"]):
        return True
    x, y = a["share_basis"], b["share_basis"]
    if (x is None or y is None) and ((x or {}).get("anchor") is not None or (y or {}).get("anchor") is not None):
        return True
    if x is not None and y is not None:
        if x.get("anchor") != y.get("anchor"):
            return True
        if x["kind"] != "UNKNOWN" and y["kind"] != "UNKNOWN" and x["kind"] != y["kind"]:
            return True
        if x["security_class"] is not None and y["security_class"] is not None and x["security_class"] != y["security_class"]:
            return True
        if x["incorporation"] == y["incorporation"] == "KNOWN":
            def action_terms(actions):
                return {(a["legal_identity"], a["security_class"]):
                    (Decimal(a["old_shares_text"]), Decimal(a["new_shares_text"])) for a in actions}
            if action_terms(x["actions"]) != action_terms(y["actions"]):
                return True
    return False


def activation_inventory(*, release_id, parent_release_id, security_id,
                         previous_bundle_id, previous: OutputGraphProjection | None,
                         replacement_bundle_id, replacement: OutputGraphProjection | None):
    old, new = previous.outputs() if previous else {}, replacement.outputs() if replacement else {}
    rows = []
    for identity in sorted(old.keys() | new.keys(), key=lambda k: tuple(str(v) for v in k)):
        before, after = old.get(identity), new.get(identity)
        unknown = identity[2] == "UNKNOWN"
        presence = before is None or after is None
        value = not presence and before.value_decimal_text != after.value_decimal_text
        state = not presence and before.state != after.state
        provenance = not presence and previous.provenance(before) != replacement.provenance(after)
        if not (unknown or presence or value or state or provenance):
            continue
        equal_context = not presence and previous.context(before) == replacement.context(after)
        conflict = not unknown and not presence and _context_conflicts(previous.context(before), replacement.context(after))
        reason = "unknown-period-unmatched" if unknown else "endpoint-unavailable" if presence else "context-conflict" if conflict else "financial-comparability-unverified"
        rows.append(ActivationTransition(release_id=release_id, parent_release_id=parent_release_id, security_id=security_id,
            metric_path=identity[0], view=identity[1], period_kind=identity[2], period_start=identity[3], period_end=identity[4],
            previous_bundle_id=previous_bundle_id if before else None, previous_node_key=before.node_key if before else None,
            replacement_bundle_id=replacement_bundle_id if after else None, replacement_node_key=after.node_key if after else None,
            value_text_changed=value, state_changed=state, provenance_changed=provenance,
            endpoint_presence_changed=presence, declared_context_equal=equal_context,
            context_assessment="INCOMPARABLE" if conflict else "NOT_ASSESSED", reason=reason))
    return rows


def history_name_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf8")).hexdigest()


def public_history_text(value):
    """Shared descriptors must not disclose filesystem or credential text."""
    if not isinstance(value, str):
        return value
    decoded = unquote(value)
    if (re.search(r"(?:(?<![A-Za-z])[A-Za-z]:[\\/]|\\\\|(?:^|\s)/(?:home|Users|tmp|var|mnt)/)", decoded)
            or re.search(r"(?:password|secret|token|authorization|credential|api[_-]?key)\s*[:=]", decoded, re.I)
            or re.search(r"(?:s3://|raw/sha256/|AKIA[A-Z0-9]{16})", decoded, re.I)):
        return None
    if "://" in value:
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError:
            return None
        if (parts.scheme not in {"http", "https"} or parts.username or parts.password or parts.query or parts.fragment
                or parts.hostname not in {"www.sec.gov", "sec.gov", "data.sec.gov", "disclosure.edinet-fsa.go.jp",
                    "disclosure2.edinet-fsa.go.jp", "disclosure2dl.edinet-fsa.go.jp"} or port not in {None, 80, 443}):
            return None
    return value


def public_history_document(value):
    value = public_history_text(value)
    if value is None:
        return None
    if "://" in value or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.(?:json|xml|xhtml|html?|zip|tsv|xlsx|pdf)", value, re.I):
        return value
    if re.fullmatch(r"\d{10}-\d{2}-\d{6}", value):
        return value
    return None


def public_history_record(record):
    if isinstance(record, dict):
        result = {key: (public_history_document(value) if key in {"source_document", "annual_wrapper", "source_url"}
                      else public_history_record(value)) for key, value in record.items()}
        if "source_document" in record:
            result["public_reference_status"] = "AVAILABLE" if result["source_document"] is not None else "UNAVAILABLE"
            result["filing_identity"] = dict(status="UNAVAILABLE", accession=None, form=None, filed=None)
        if "source_url" in record:
            result["public_source_url_status"] = "AVAILABLE" if result["source_url"] is not None else "UNAVAILABLE"
        return result
    if isinstance(record, (tuple, list)):
        return [public_history_record(value) for value in record]
    return public_history_text(record)


def history_selection_record(row):
    node = FigureSelection.model_validate_json(json.dumps(dict(value_decimal_text=row["value_decimal_text"], **row["record"])))
    record = node.model_dump(mode="json")
    for key in ("node_key", "observation_key", "source_witnesses"):
        record.pop(key, None)
    return public_history_record(record)


def history_observation_record(row):
    observed = ReportedObservation.model_validate_json(json.dumps(dict(key="public-input", value_decimal_text=row["value_decimal_text"], **row["record"])))
    record = observed.model_dump(mode="json")
    record.pop("key")
    return public_history_record(record)


class FigureHistoryRepository:
    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.engine, self._clock = engine, clock
        self._jobs = DurableJobRepository(engine)
        self._preparation_key = secrets.token_bytes(32)

    def record_activation_on_connection(self, connection: Connection, *, release_id, parent_release_id,
                                       memberships, candidate_ids):
        if (connection.engine is not self.engine or not connection.in_transaction()
                or getattr(connection.connection.dbapi_connection, "autocommit", None) is not False):
            raise ValueError("activation history requires the owner's real transaction")
        release = connection.execute(select(data_release).where(data_release.c.release_id == release_id)).mappings().one()
        if release["published_at"] is not None or release["parent_release_id"] != parent_release_id or release["figure_history_revision"] != 1:
            raise ValueError("activation history requires the actual unsealed parent and marker")
        actual = connection.execute(select(release_company).where(release_company.c.release_id == release_id)).mappings().all()
        supplied = {(m["security_id"], m["snapshot_id"], m.get("exact_bundle_id")) for m in memberships}
        if supplied != {(m["security_id"], m["snapshot_id"], m["exact_bundle_id"]) for m in actual}:
            raise ValueError("activation history membership differs from inserted selection")
        if set(candidate_ids) != set(connection.scalars(select(release_candidate.c.candidate_id).where(release_candidate.c.release_id == release_id))):
            raise ValueError("activation history candidates differ from inserted selection")
        parents = {} if parent_release_id is None else {m["security_id"]: m for m in connection.execute(
            select(release_company).where(release_company.c.release_id == parent_release_id)).mappings()}
        projections = {}
        def projection(identifier):
            if identifier is not None and identifier not in projections:
                projections[identifier] = OutputGraphProjection(self.load_on_connection(connection, identifier))
            return projections.get(identifier)
        records = []
        for member in actual:
            prior = parents.get(member["security_id"])
            old_id = prior["exact_bundle_id"] if prior else None
            new_id = member["exact_bundle_id"]
            if old_id == new_id:
                continue
            records.extend(activation_inventory(release_id=release_id, parent_release_id=parent_release_id,
                security_id=member["security_id"], previous_bundle_id=old_id, previous=projection(old_id),
                replacement_bundle_id=new_id, replacement=projection(new_id)))
        if records:
            connection.execute(figure_activation_transition.insert(), [r.model_dump() for r in records])
        return tuple(records)

    def prepare_documents(self, bundle: ExactFigureBundle, object_store) -> PreparedDocumentEvidence:
        """Verify registered immutable bytes before opening the writer transaction."""
        bundle = ExactFigureBundle.model_validate(bundle)
        return self.prepare_document_records(bundle.document_evidence, object_store,
                                             contract_revision=bundle.contract_revision)

    def prepare_document_records(self, documents, object_store, *, contract_revision=2):
        if type(contract_revision) is not int or contract_revision not in {1, 2, 3}:
            raise ValueError("exact document preparation contract required")
        bounded = contract_revision == 3
        if bounded and (not isinstance(documents, tuple) or len(documents) > CONTRACT3_DOCUMENT_LIMITS["records"]):
            raise ValueError("contract3 document count exceeds its ceiling")
        if bounded:
            for document in documents:
                _contract3_record_bound(document)
        documents = tuple(DocumentEvidence.model_validate(document) for document in documents)
        if bounded:
            documents = tuple(sorted(documents, key=lambda document: document.key))
            if len({document.key for document in documents}) != len(documents):
                raise ValueError("duplicate contract3 document key")
        hashes = {d.reference.artifact_sha256 for d in documents}
        if bounded and len(hashes) > CONTRACT3_DOCUMENT_LIMITS["artifacts"]:
            raise ValueError("contract3 document artifact count exceeds its ceiling")
        with self.engine.connect() as connection:
            artifacts = {r["content_sha256"]: r for r in connection.execute(select(evidence_artifact).where(
                evidence_artifact.c.content_sha256.in_(hashes))).mappings()}
        if set(artifacts) != hashes:
            raise ValueError("document artifact is not registered verified evidence")
        if bounded:
            for digest, artifact in artifacts.items():
                if (type(artifact["byte_size"]) is not int or artifact["byte_size"] < 0
                        or artifact["byte_size"] > CONTRACT3_DOCUMENT_LIMITS["body_bytes"]
                        or artifact["object_key"] != f"raw/sha256/{digest}"):
                    raise ValueError("contract3 registered document body exceeds its ceiling or identity")
            if sum(artifact["byte_size"] for artifact in artifacts.values()) > CONTRACT3_DOCUMENT_LIMITS["aggregate_bytes"]:
                raise ValueError("contract3 registered document aggregate exceeds its ceiling")
        for digest, artifact in artifacts.items():
            body = object_store.read_verified(artifact["object_key"], digest, artifact["byte_size"])
            if bounded and (len(body) != artifact["byte_size"] or hashlib.sha256(body).hexdigest() != digest):
                raise ValueError("contract3 document body differs from registered bytes")
            for document in documents:
                if document.reference.artifact_sha256 != digest:
                    continue
                span = document.reference.location
                if span.end_byte > len(body) or hashlib.sha256(body[span.start_byte:span.end_byte]).hexdigest() != span.span_sha256:
                    raise ValueError("document byte offset/span hash differs from verified object")
                body[span.start_byte:span.end_byte].decode(span.encoding)
            del body
        digest = _digest([d.model_dump(mode="json") for d in documents])
        if bounded:
            proof = _contract3_document_proof_digest(digest)
            return PreparedContract3DocumentEvidence(digest, CONTRACT3_DOCUMENT_LIMITS_SHA256,
                hmac.digest(self._preparation_key, proof.encode(), "sha256"))
        return PreparedDocumentEvidence(digest, hmac.digest(self._preparation_key, digest.encode(), "sha256"))

    def _require_prepared(self, bundle, prepared):
        if not bundle.document_evidence:
            return
        digest = _digest([d.model_dump(mode="json") for d in bundle.document_evidence])
        if bundle.contract_revision == 3:
            proof = _contract3_document_proof_digest(digest)
            if (not isinstance(prepared, PreparedContract3DocumentEvidence) or prepared.record_sha256 != digest
                    or prepared.limits_sha256 != CONTRACT3_DOCUMENT_LIMITS_SHA256
                    or not hmac.compare_digest(prepared.verification_seal,
                        hmac.digest(self._preparation_key, proof.encode(), "sha256"))):
                raise ValueError("contract3 documents require matching bounded preparation")
            return
        if (not isinstance(prepared, PreparedDocumentEvidence) or prepared.record_sha256 != digest
                or not hmac.compare_digest(prepared.verification_seal, hmac.digest(self._preparation_key, digest.encode(), "sha256"))):
            raise ValueError("document evidence requires verified preparation outside SQL")

    def prepare_registered_native(self, plan_sha256, *, cik, company_facts_occurrence_id,
                                  security_identifier, expected_engine_identity_sha256, object_store):
        from .artifacts import SqlArtifactRepository
        from .primary_input_transport import PrimaryInputTransport
        from .source_observations import SourceObservationRepository
        from .models import AdmittedPrimaryInput
        PrimaryInputTransport.validate_native_request(plan_sha256, cik, company_facts_occurrence_id, security_identifier)
        if type(expected_engine_identity_sha256) is not str or re.fullmatch(r"[a-f0-9]{64}", expected_engine_identity_sha256) is None:
            raise ValueError("native preparation requires its exact executing identity")
        identity = _registered_native_identity()
        if expected_engine_identity_sha256 != identity:
            raise ValueError("native preparation executing identity differs")
        with self.engine.connect() as connection:
            schema = connection.scalar(text("SELECT version_num FROM alembic_version"))
        if schema != "20261009_0021":
            raise ValueError("native preparation requires supported schema21")
        transport = PrimaryInputTransport(SqlArtifactRepository(self.engine), object_store,
                                          SourceObservationRepository(self.engine))
        inputs = transport.resolve_registered_native(plan_sha256, cik=cik,
            company_facts_occurrence_id=company_facts_occurrence_id, security_identifier=security_identifier)
        observations, documents = {}, {}
        for outcome in inputs.resolved.outcomes:
            if not isinstance(outcome, AdmittedPrimaryInput):
                continue
            for value in (outcome.original, outcome.replacement):
                if value is None:
                    continue
                observed = _native_observation(value, cik)
                if observed.key in observations and observations[observed.key] != observed:
                    raise ValueError("native occurrence has conflicting projections")
                observations[observed.key] = observed
            for document in outcome.mapping.documents:
                if document.key in documents and documents[document.key] != document:
                    raise ValueError("native documentary key has conflicting records")
                documents[document.key] = document
        sources = tuple(sorted((BundleSource(source_item_id=s.source_item_id,
            source_observation_id=s.source_observation_id, occurrence_id=s.occurrence_id)
            for s in inputs.resolved.captured_sources), key=lambda s: (s.source_item_id, s.source_observation_id)))
        if len({(s.source_item_id, s.source_observation_id) for s in sources}) != len(sources):
            raise ValueError("native prerequisite deliveries cannot be represented by this bundle")
        documents = tuple(documents[key] for key in sorted(documents))
        # The existing bounded document owner performs its own second object read outside writer SQL.
        document_proof = self.prepare_document_records(documents, object_store, contract_revision=3)
        prepared = PreparedRegisteredNative(plan_sha256, cik, company_facts_occurrence_id, security_identifier,
            identity, schema, inputs.plan, inputs.company_facts_source, inputs.artifact_metadata, inputs.graph_gaps,
            inputs.resolved.manifest, tuple(observations[key] for key in sorted(observations)), documents,
            sources, inputs.resolved.artifacts, document_proof, b"")
        payload = _registered_native_payload(prepared)
        if _registered_native_identity() != identity:
            raise ValueError("native preparation source changed during admission")
        return replace(prepared, verification_seal=hmac.digest(self._preparation_key, _digest(payload).encode(), "sha256"))

    def prepare_registered_native_reconstruction(self, recipe_sha256, *, expected_engine_identity_sha256, object_store):
        from . import captured_assembly, exact_annual
        from .artifacts import SqlArtifactRepository
        from .shared_companies import SharedCompanyRepository
        from .source_observations import SourceObservationRepository
        for digest in (recipe_sha256, expected_engine_identity_sha256):
            if type(digest) is not str or len(digest) != 64 or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
                raise ValueError("native reconstruction requires exact scalar SHA256 identities")
        identity = _native_reconstruction_identity()
        if expected_engine_identity_sha256 != identity:
            raise ValueError("native reconstruction executing identity differs")
        with self.engine.connect() as connection:
            schema = connection.scalar(text("SELECT version_num FROM alembic_version"))
        if schema != "20261009_0021":
            raise ValueError("native reconstruction requires supported schema21")
        preparer = captured_assembly.CapturedCanonicalPreparer(SharedCompanyRepository(self.engine),
            SqlArtifactRepository(self.engine), object_store, SourceObservationRepository(self.engine))
        admitted = preparer._reconstruct_native(recipe_sha256, identity)
        result, company, inputs = admitted["result"], admitted["company"], admitted["inputs"]
        assumed = result.rq_result
        payload = dict(domain=NATIVE_RECONSTRUCTION_DOMAIN, limits=dict(NATIVE_RECONSTRUCTION_LIMITS),
            recipe_sha256=recipe_sha256, engine_identity_sha256=identity, schema_revision=schema,
            security_id=company.snapshot.security_id, security_identifier=company.security_identifier,
            snapshot_id=company.snapshot.snapshot_id, snapshot_sha256=company.snapshot.snapshot_sha256,
            canonical_payload_sha256=company.snapshot.payload_sha256, canonical_row=json.loads(admitted["strict_bytes"]),
            strict_status=result.status, assumed_status=None if assumed is None else assumed.status,
            assumed_row_sha256=None if assumed is None or assumed.row is None else _digest(assumed.row),
            artifact_roles=[asdict(link) for link in company.artifacts], artifact_metadata=admitted["metadata"],
            manifest=inputs.resolved.manifest, source_observations=admitted["sources"], gaps=admitted["gaps"],
            financial_status="NOT_ASSESSED", currentness_status="NOT_ASSESSED", retrospective_run=True)
        projection = exact_annual._prepare_reconstructed(result, inputs.resolved.captured_sources,
            cik=company.issuer_identifier, original_facts=inputs.resolved.original_company_facts,
            envelope_reserve=len(_contract3_json(payload)) + 2048)
        document_proof = self.prepare_document_records(projection["document_evidence"], object_store, contract_revision=3)
        serialized = {name: [record.model_dump(mode="json") for record in projection[name]] for name in
                      ("observations", "selections", "coverage", "sources", "document_evidence")}
        serialized["operand_gaps"] = projection["operand_gaps"]
        payload.update(projection=serialized, document_record_sha256=document_proof.record_sha256,
            document_limits_sha256=document_proof.limits_sha256, document_verification_seal=document_proof.verification_seal.hex())
        raw = _contract3_json(payload)
        if len(raw) > NATIVE_RECONSTRUCTION_LIMITS["envelope_bytes"]:
            raise ValueError("native reconstruction complete envelope ceiling exceeded")
        preparer._recheck_reconstruction(admitted)
        with self.engine.connect() as connection:
            if connection.scalar(text("SELECT version_num FROM alembic_version")) != schema:
                raise ValueError("native reconstruction schema changed")
        if _native_reconstruction_identity() != identity:
            raise ValueError("native reconstruction executing identity changed")
        return PreparedNativeReconstruction(recipe_sha256, company.snapshot.snapshot_id, company.snapshot.security_id,
            identity, schema, raw, projection["observations"], projection["selections"], projection["coverage"],
            projection["sources"], projection["document_evidence"], hmac.digest(self._preparation_key, raw, "sha256"))

    def verify_native_reconstruction(self, prepared):
        if type(prepared) is not PreparedNativeReconstruction:
            raise ValueError("invalid readonly native reconstruction proof type")
        headers = _native_reconstruction_headers(dict(recipe_sha256=prepared.recipe_sha256,
            engine_identity_sha256=prepared.engine_identity_sha256, snapshot_id=prepared.snapshot_id,
            security_id=prepared.security_id, schema_revision=prepared.schema_revision))
        if (type(prepared.payload) is not bytes
                or len(prepared.payload) > NATIVE_RECONSTRUCTION_LIMITS["envelope_bytes"]
                or type(prepared.verification_seal) is not bytes
                or not hmac.compare_digest(prepared.verification_seal, hmac.digest(self._preparation_key, prepared.payload, "sha256"))):
            raise ValueError("invalid readonly native reconstruction proof")
        value = json.loads(prepared.payload)
        authenticated_headers = _native_reconstruction_headers({name: value[name] for name in headers})
        if (value["domain"] != NATIVE_RECONSTRUCTION_DOMAIN or value["engine_identity_sha256"] != _native_reconstruction_identity()
                or _contract3_json(authenticated_headers) != _contract3_json(headers)
                or any(_contract3_json(value["projection"][name]) != _contract3_json([record.model_dump(mode="json") for record in getattr(prepared, name)])
                       for name in ("observations", "selections", "coverage", "sources", "document_evidence"))):
            raise ValueError("readonly native reconstruction content/build differs")
        return value

    def _require_storage_contract(self, bundle, prepared_native=None):
        native = any(isinstance(observed.reference.location, _NATIVE_LOCATIONS) for observed in bundle.observations)
        if not native and prepared_native is None:
            return
        if not isinstance(prepared_native, PreparedRegisteredNative):
            raise ValueError("registered native preparation unavailable; native persistence is closed")
        try:
            payload = _registered_native_payload(prepared_native)
            valid = type(prepared_native.verification_seal) is bytes and hmac.compare_digest(
                prepared_native.verification_seal, hmac.digest(self._preparation_key, _digest(payload).encode(), "sha256"))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("invalid registered native preparation proof") from error
        if not valid or prepared_native.engine_identity_sha256 != _registered_native_identity():
            raise ValueError("native preparation seal/build differs")
        if (bundle.contract_revision != 3 or bundle.security_id != prepared_native.plan.security_id
                or bundle.observations != prepared_native.observations
                or bundle.document_evidence != prepared_native.document_evidence or bundle.sources != prepared_native.sources):
            raise ValueError("native bundle requires its exact full prepared tuples/security")
        if (bundle.dependencies or bundle.actions or bundle.assumptions
                or any(c.assessment != "PARTIAL" for c in bundle.coverage)
                or any(n.purpose != "OUTPUT" or n.state != "NOT_ASSESSED" or n.value_decimal_text is not None
                    or n.observation_key is not None or n.definition is not None or n.definition_revision is not None
                    or n.required_input_roles or n.basis_transition is not None or n.assumption_keys
                    for n in bundle.selections)):
            raise ValueError("native preparation admits only source-only unassessed graphs")
        for record in (*bundle.observations, *bundle.selections):
            basis = record.share_basis
            if basis is not None and (basis.kind not in {"NONE", "UNKNOWN"} or basis.incorporation != "UNKNOWN"
                                      or basis.incorporated_actions or basis.evidence_keys or basis.security_class is not None):
                raise ValueError("native source-only graph cannot assert incorporation/class/actions")

    def _recheck_native_on_connection(self, connection, lease, bundle, prepared, *, _raw_work_budget=None):
        from .primary_input_transport import PrimaryInputTransport
        item_ids = sorted({dependency.captured_source.source_item_id for dependency in prepared.plan.dependencies
            if dependency.captured_source is not None and dependency.delivery is not None and dependency.observation_sha256 is not None})
        locked = connection.scalars(select(source_item.c.source_item_id).where(source_item.c.source_item_id.in_(item_ids))
            .order_by(source_item.c.source_item_id).with_for_update()).all()
        if locked != item_ids:
            raise ValueError("native preparation prerequisite source lock union is incomplete")
        self._jobs._lock_live_lease(connection, lease, self._clock,
                **({} if _raw_work_budget is None else {"_raw_work_budget": _raw_work_budget}))
        self._require_storage_contract(bundle, prepared)
        if connection.scalar(text("SELECT version_num FROM alembic_version")) != prepared.schema_revision:
            raise ValueError("native preparation schema changed after source wait")
        PrimaryInputTransport.recheck_registered_native_on_connection(connection, plan=prepared.plan,
            cik=prepared.cik, security_identifier=prepared.security_identifier, company_facts_source=prepared.company_facts_source,
            artifact_metadata=prepared.artifact_metadata, graph_gaps=prepared.graph_gaps)
        actual = set(connection.execute(select(company_snapshot_artifact.c.content_sha256,
            company_snapshot_artifact.c.role).where(company_snapshot_artifact.c.snapshot_id == bundle.snapshot_id)).tuples())
        required = {(a.content_sha256, a.role) for a in prepared.artifacts}
        plans = {pair for pair in actual if pair[1] == "primary_input_plan"}
        if not required.issubset(actual) or plans != {(prepared.plan_sha256, "primary_input_plan")}:
            raise ValueError("native snapshot plan/dependency artifact roles differ after source wait")
        self._jobs._lock_live_lease(connection, lease, self._clock,
                **({} if _raw_work_budget is None else {"_raw_work_budget": _raw_work_budget}))

    def store(self, lease: LeaseToken, *, request_key: str, bundle: ExactFigureBundle,
              prepared_documents: PreparedDocumentEvidence | None = None, prepared_native: PreparedRegisteredNative | None = None) -> int:
        bundle = ExactFigureBundle.model_validate(bundle)
        self._require_storage_contract(bundle, prepared_native)
        self._require_prepared(bundle, prepared_documents)
        with self.engine.begin() as connection:
            return self.store_on_connection(connection, lease, request_key=request_key, bundle=bundle,
                                            prepared_documents=prepared_documents, prepared_native=prepared_native)

    def store_on_connection(self, connection: Connection, lease: LeaseToken, *, request_key: str, bundle: ExactFigureBundle,
                            prepared_documents: PreparedDocumentEvidence | None = None,
                            prepared_native: PreparedRegisteredNative | None = None, _raw_work_budget=None) -> int:
        if not isinstance(request_key, str) or not request_key.strip():
            raise ValueError("request_key required")
        if _raw_work_budget is not None:
            _history_raw_store_frontier(bundle, _raw_work_budget)
        bundle = ExactFigureBundle.model_validate(bundle)
        self._require_storage_contract(bundle, prepared_native)
        self._require_prepared(bundle, prepared_documents)
        self._jobs._lock_live_lease(connection, lease, self._clock,
                **({} if _raw_work_budget is None else {"_raw_work_budget": _raw_work_budget}))
        if prepared_native is not None:
            self._recheck_native_on_connection(connection, lease, bundle, prepared_native,
                **({} if _raw_work_budget is None else {"_raw_work_budget": _raw_work_budget}))
        issuer_id = self._validate_bindings(connection, bundle, _raw_work_budget=_raw_work_budget)
        self._jobs._lock_live_lease(connection, lease, self._clock,
                **({} if _raw_work_budget is None else {"_raw_work_budget": _raw_work_budget}))
        payload = bundle.model_dump(mode="json")
        digest = _digest(payload)
        existing = connection.execute(insert(exact_figure_bundle).values(
            request_key=request_key, bundle_sha256=digest, security_id=bundle.security_id,
            snapshot_id=bundle.snapshot_id, engine_revision=bundle.engine_revision,
            snapshot_sha256=bundle.snapshot_sha256, pipeline_run_key=bundle.pipeline_run_key,
            contract_revision=bundle.contract_revision, storing_job_id=lease.job_id,
            contract_details={name: payload[name] for name in ("actions", "assumptions")} if bundle.contract_revision in {2, 3} else {},
            observation_bindings=[dict(key=o.key, artifact_sha256=o.reference.artifact_sha256,
                extraction_revision=o.reference.extraction_revision,
                location_sha256=_digest(o.reference.location.model_dump(mode="json"))) for o in bundle.observations],
        ).on_conflict_do_nothing(index_elements=["request_key"]).returning(exact_figure_bundle)).mappings().one_or_none()
        created = existing is not None
        self._jobs._lock_live_lease(connection, lease, self._clock,
                **({} if _raw_work_budget is None else {"_raw_work_budget": _raw_work_budget}))
        if existing is None:
            if _raw_work_budget is not None:
                _history_raw_row_read(connection, "exact_figure_bundle", "request_key=:key", dict(key=request_key), _raw_work_budget)
            existing = connection.execute(select(exact_figure_bundle).where(exact_figure_bundle.c.request_key == request_key)).mappings().one()
            if existing["bundle_sha256"] != digest or existing["sealed_at"] is None:
                raise FigureHistoryConflict("request_key has different exact bundle")
            previous_bundle = self.load_on_connection(connection, existing["bundle_id"], _raw_work_budget=_raw_work_budget)
            if _raw_work_budget is not None:
                from .primary_input_transport import _retained_model_dump_reserve
                left = _retained_model_dump_reserve(previous_bundle, _raw_work_budget)
                right = _retained_model_dump_reserve(bundle, _raw_work_budget)
                _raw_work_budget.equality(left, right)
            if previous_bundle.model_dump(mode="json") != payload:
                raise FigureHistoryConflict("recorded exact bundle differs")
        if created:
            bundle_id = existing["bundle_id"]
            for document in bundle.document_evidence:
                if _raw_work_budget is not None:
                    _history_raw_record_frontier(document, _raw_work_budget, locator=document.reference.location)
                record = document.model_dump(mode="json")
                record.pop("key")
                reference = document.reference
                span = reference.location
                locator = _digest(span.model_dump(mode="json"))
                identifier = connection.execute(insert(document_evidence).values(
                    artifact_sha256=reference.artifact_sha256, issuer_id=issuer_id,
                    extraction_revision=reference.extraction_revision, location_sha256=locator,
                    start_byte=span.start_byte, end_byte=span.end_byte, span_sha256=span.span_sha256, record=record,
                ).on_conflict_do_nothing(index_elements=["artifact_sha256", "extraction_revision", "location_sha256"])
                    .returning(document_evidence.c.evidence_id)).scalar_one_or_none()
                if identifier is None:
                    if _raw_work_budget is not None:
                        _history_raw_row_read(connection, "document_evidence",
                            "artifact_sha256=:sha AND extraction_revision=:revision AND location_sha256=:location",
                            dict(sha=reference.artifact_sha256, revision=reference.extraction_revision, location=locator), _raw_work_budget)
                    previous = connection.execute(select(document_evidence).where(
                        document_evidence.c.artifact_sha256 == reference.artifact_sha256,
                        document_evidence.c.extraction_revision == reference.extraction_revision,
                        document_evidence.c.location_sha256 == locator)).mappings().one()
                    if previous["record"] != record or previous["issuer_id"] != issuer_id:
                        raise FigureHistoryConflict("captured document occurrence has different evidence")
                    identifier = previous["evidence_id"]
                self._jobs._lock_live_lease(connection, lease, self._clock,
                **({} if _raw_work_budget is None else {"_raw_work_budget": _raw_work_budget}))
                connection.execute(figure_bundle_evidence.insert().values(bundle_id=bundle_id,
                    evidence_key=document.key, evidence_id=identifier))
            observation_ids = {}
            for observation in bundle.observations:
                if _raw_work_budget is not None:
                    _history_raw_record_frontier(observation, _raw_work_budget, locator=observation.reference.location)
                record = observation.model_dump(mode="json")
                record.pop("key")
                value = record.pop("value_decimal_text")
                reference = observation.reference
                locator_hash = _digest(reference.location.model_dump(mode="json"))
                row = connection.execute(insert(figure_observation).values(
                    artifact_sha256=reference.artifact_sha256, extraction_revision=reference.extraction_revision,
                    location_sha256=locator_hash, issuer_id=issuer_id,
                    value_decimal_text=value, record=record,
                ).on_conflict_do_nothing(index_elements=["artifact_sha256", "extraction_revision", "location_sha256"])
                    .returning(figure_observation.c.observation_id)).scalar_one_or_none()
                if row is None:
                    if _raw_work_budget is not None:
                        _history_raw_row_read(connection, "figure_observation",
                            "artifact_sha256=:sha AND extraction_revision=:revision AND location_sha256=:location",
                            dict(sha=reference.artifact_sha256, revision=reference.extraction_revision, location=locator_hash), _raw_work_budget)
                    previous = connection.execute(select(figure_observation).where(
                        figure_observation.c.artifact_sha256 == reference.artifact_sha256,
                        figure_observation.c.extraction_revision == reference.extraction_revision,
                        figure_observation.c.location_sha256 == locator_hash)).mappings().one()
                    if previous["record"] != record or previous["value_decimal_text"] != value or previous["issuer_id"] != issuer_id:
                        raise FigureHistoryConflict("captured artifact occurrence has different observation")
                    row = previous["observation_id"]
                observation_ids[observation.key] = row
            self._jobs._lock_live_lease(connection, lease, self._clock,
                **({} if _raw_work_budget is None else {"_raw_work_budget": _raw_work_budget}))
            for node in bundle.selections:
                if _raw_work_budget is not None:
                    _history_raw_record_frontier(node, _raw_work_budget, locator=None)
                record = node.model_dump(mode="json")
                value = record.pop("value_decimal_text")
                connection.execute(figure_selection.insert().values(bundle_id=bundle_id, node_key=node.node_key,
                    metric_family=node.metric_family, metric_path=node.metric_path, view=node.view,
                    value_decimal_text=value, observation_id=observation_ids.get(node.observation_key), record=record))
            for dependency in bundle.dependencies:
                if _raw_work_budget is not None:
                    _history_raw_record_frontier(dependency, _raw_work_budget, locator=None)
                connection.execute(figure_dependency.insert().values(bundle_id=bundle_id,
                    parent_node_key=dependency.parent_node_key, ordinal=dependency.ordinal,
                    observation_id=observation_ids.get(dependency.observation_key),
                    input_node_key=dependency.selection_node_key, record=dependency.model_dump(mode="json")))
            for covered in bundle.coverage:
                if _raw_work_budget is not None:
                    _history_raw_record_frontier(covered, _raw_work_budget, locator=None)
                connection.execute(figure_bundle_coverage.insert().values(bundle_id=bundle_id,
                    metric_family=covered.metric_family, view=covered.view, record=covered.model_dump(mode="json")))
            for source in bundle.sources:
                connection.execute(figure_bundle_source.insert().values(bundle_id=bundle_id, **source.model_dump()))
            self._jobs._lock_live_lease(connection, lease, self._clock,
                **({} if _raw_work_budget is None else {"_raw_work_budget": _raw_work_budget}))
            connection.execute(update(exact_figure_bundle).where(exact_figure_bundle.c.bundle_id == bundle_id).values(sealed_at=self._clock()))
        self._jobs._lock_live_lease(connection, lease, self._clock,
                **({} if _raw_work_budget is None else {"_raw_work_budget": _raw_work_budget}))
        return existing["bundle_id"]

    def _validate_bindings(self, connection, bundle, *, _raw_work_budget=None):
        snapshot_columns = (company_snapshot.c.security_id, company_snapshot.c.engine_revision,
            company_snapshot.c.snapshot_sha256, company_snapshot.c.security_basis, company_snapshot.c.receipt_ratio)
        snapshot_where = company_snapshot.c.snapshot_id == bundle.snapshot_id
        if _raw_work_budget is not None:
            _history_raw_projection_read(connection, company_snapshot, snapshot_columns, snapshot_where, _raw_work_budget)
        snapshot = connection.execute(select(*snapshot_columns).where(snapshot_where)).mappings().one_or_none()
        if snapshot is None or any(snapshot[k] != getattr(bundle, k) for k in ("security_id", "engine_revision", "snapshot_sha256")):
            raise ValueError("exact bundle does not match actual snapshot/security/engine")
        company = connection.execute(select(issuer).join(priced_security).where(priced_security.c.security_id == bundle.security_id)).mappings().one()
        artifacts = set(connection.scalars(select(company_snapshot_artifact.c.content_sha256).where(company_snapshot_artifact.c.snapshot_id == bundle.snapshot_id)))
        for node in bundle.selections:
            ratio = Decimal(node.receipt_ratio_text) if node.receipt_ratio_text is not None else None
            if node.purpose == "OUTPUT" and (node.security_basis != snapshot["security_basis"] or ratio != snapshot["receipt_ratio"]):
                raise ValueError("selection security basis differs from actual snapshot")
            if node.purpose == "OUTPUT" and node.share_basis is not None and node.share_basis.kind not in {"NONE", "UNKNOWN"}:
                actual = snapshot["security_basis"]
                if ((actual == "PRIMARY_DEPOSITARY_RECEIPT" and node.share_basis.kind != "RECEIPT")
                        or (actual in {"PRIMARY_COMMON_SHARE", "PRIMARY_ORDINARY_SHARE"} and node.share_basis.kind == "RECEIPT")):
                    raise ValueError("output share kind differs from actual snapshot security basis")
            full = any(c.assessment == "FULL" and (c.metric_family, c.view) == (node.metric_family, node.view) for c in bundle.coverage)
            self._validate_scope_identity(node.scope, company, require_known=full)
        for observed in bundle.observations:
            if observed.reference.artifact_sha256 not in artifacts or observed.reference.source_system != company["source_system"]:
                raise ValueError("observation artifact/source differs from actual snapshot")
            if not self._same_identifier(company["source_system"], observed.reference.issuer_source_identifier, company["source_identifier"]):
                raise ValueError("observation issuer differs from actual snapshot")
            self._validate_scope_identity(observed.scope, company)
        for document in bundle.document_evidence:
            if document.reference.artifact_sha256 not in artifacts or document.reference.source_system != company["source_system"]:
                raise ValueError("document artifact/source differs from actual snapshot")
            if not self._same_identifier(company["source_system"], document.reference.issuer_source_identifier, company["source_identifier"]):
                raise ValueError("document issuer differs from actual snapshot")
        item_ids = sorted({s.source_item_id for s in bundle.sources})
        if item_ids:
            connection.execute(select(source_item.c.source_item_id).where(source_item.c.source_item_id.in_(item_ids)).order_by(source_item.c.source_item_id).with_for_update()).all()
        source_artifacts = set()
        source_documents = set()
        bound_sources = {}
        for source in bundle.sources:
            source_columns = (source_observation.c.source_item_id, source_observation.c.state,
                source_observation.c.artifact_sha256, source_observation.c.source_url,
                source_item.c.source_system, source_item.c.issuer_source_identifier, source_item.c.item_kind)
            source_where = source_observation.c.source_observation_id == source.source_observation_id
            source_join = source_observation.join(source_item)
            if _raw_work_budget is not None:
                _history_raw_projection_read(connection, source_join, source_columns, source_where, _raw_work_budget)
            row = connection.execute(select(*source_columns).select_from(source_join)
                .where(source_where)).mappings().one_or_none()
            identifier = row["issuer_source_identifier"] if row else None
            issuer_matches = self._same_identifier(company["source_system"], identifier, company["source_identifier"])
            # Shared inventory/archive resources have no issuer; each exact occurrence supplies it.
            shared_resource = row is not None and identifier is None and row["item_kind"] in {"inventory", "resource"}
            if row is None or row["source_item_id"] != source.source_item_id or row["state"] != "present" or row["artifact_sha256"] not in artifacts or row["source_system"] != company["source_system"] or not (issuer_matches or shared_resource):
                raise ValueError("bundle source does not match captured issuer/artifact revision")
            source_artifacts.add(row["artifact_sha256"])
            source_documents.add((row["artifact_sha256"], row["source_url"]))
            bound_sources[(source.source_item_id, source.source_observation_id)] = row
            if source.occurrence_id is not None:
                witness = connection.scalar(select(source_check_occurrence.c.occurrence_id).where(
                    source_check_occurrence.c.occurrence_id == source.occurrence_id,
                    source_check_occurrence.c.source_item_id == source.source_item_id,
                    source_check_occurrence.c.source_observation_id == source.source_observation_id))
                if witness is None:
                    raise ValueError("accepted source occurrence does not match revision/item")
        if any(o.reference.artifact_sha256 not in source_artifacts for o in bundle.observations):
            raise ValueError("observation lacks captured source binding")
        if any(d.reference.artifact_sha256 not in source_artifacts for d in bundle.document_evidence):
            raise ValueError("document lacks captured source binding")
        if any((d.reference.artifact_sha256, d.reference.source_document) not in source_documents for d in bundle.document_evidence):
            raise ValueError("document identity differs from captured source URL")
        if bundle.contract_revision == 3:
            observations = {observed.key: observed for observed in bundle.observations}
            documents = {document.key: document for document in bundle.document_evidence}
            for node in bundle.selections:
                for witness in node.source_witnesses:
                    target = observations[witness.observation_key] if witness.observation_key is not None else documents[witness.evidence_key]
                    source = bound_sources[(witness.source.source_item_id, witness.source.source_observation_id)]
                    if (target.reference.artifact_sha256, target.reference.source_document) != (source["artifact_sha256"], source["source_url"]):
                        raise ValueError("declared witness differs from its exact captured source")
        return company["issuer_id"]

    @staticmethod
    def _same_identifier(system, actual, expected):
        if actual is None:
            return False
        if system == "SEC" and actual.isdigit() and expected.isdigit():
            return int(actual) == int(expected)
        return actual == expected

    @staticmethod
    def _validate_scope_identity(scope, company, *, require_known=False):
        entity = scope.entity_identifier
        if scope.entity_scheme == "http://www.sec.gov/CIK" and entity is not None:
            matches = (company["source_system"] == "SEC" and re.fullmatch(r"[0-9]{1,10}", entity)
                       and int(entity) == int(company["source_identifier"]))
        elif scope.entity_scheme == "http://disclosure.edinet-fsa.go.jp" and entity is not None:
            matches = company["source_system"] == "EDINET" and entity in {
                company["source_identifier"], company["source_identifier"] + "-000"}
        else:
            if require_known:
                raise ValueError("FULL reporting scope requires verified native issuer identity; unknown scheme remains unassessed")
            return
        if not matches:
            raise ValueError("reporting scope entity differs from actual snapshot issuer")

    def load(self, bundle_id: int) -> ExactFigureBundle:
        with self.engine.connect() as connection:
            return self.load_on_connection(connection, bundle_id)

    @staticmethod
    def _sealed_snapshot_binding(row, *, security_id, snapshot_id, expected_run_key):
        if row is None or row["security_id"] != security_id or row["snapshot_id"] != snapshot_id or row["pipeline_run_key"] != expected_run_key:
            raise ValueError("exact binding differs from sealed snapshot/security/producer run")
        if row["contract_revision"] not in {1, 2}:
            raise ValueError("declared contract3 history is not publication qualified")

    @staticmethod
    def publication_identity_on_connection(connection, bundle_id, *, security_id, snapshot_id, expected_run_key, _raw_work_budget=None):
        if type(bundle_id) is not int or bundle_id <= 0:
            raise ValueError("exact_bundle_id must be a positive integer")
        if not isinstance(expected_run_key, str) or not expected_run_key.strip():
            raise ValueError("exact binding requires expected_exact_run_key")
        header_columns = tuple(exact_figure_bundle.c[name] for name in (
            "security_id", "snapshot_id", "pipeline_run_key", "contract_revision", "bundle_sha256"))
        header_where = and_(exact_figure_bundle.c.bundle_id == bundle_id, exact_figure_bundle.c.sealed_at.is_not(None))
        if _raw_work_budget is not None:
            _history_raw_projection_read(connection, exact_figure_bundle, header_columns, header_where, _raw_work_budget)
        row = connection.execute(select(*header_columns).where(header_where)).mappings().one_or_none()
        FigureHistoryRepository._sealed_snapshot_binding(row, security_id=security_id,
            snapshot_id=snapshot_id, expected_run_key=expected_run_key)
        sources = tuple(connection.scalars(select(figure_bundle_source.c.source_observation_id).where(
            figure_bundle_source.c.bundle_id == bundle_id).order_by(figure_bundle_source.c.source_observation_id)))
        return dict(exact_bundle_id=bundle_id, exact_bundle_sha256=row["bundle_sha256"],
                    expected_exact_run_key=row["pipeline_run_key"], source_observation_ids=sources)

    @staticmethod
    def pair_reference_on_connection(connection, bundle_id, *, security_id, snapshot_id, expected_run_key,
                                     stage_job_id, snapshot_sha256, engine_revision, expected_sources):
        """Authenticate only a bounded sealed contract2 header and four source triples."""
        from .shared_companies import (_reference_frontier, _reference_id, _reference_lengths,
                                       _reference_sha, _reference_text)
        for name, value in (("bundle", bundle_id), ("security", security_id), ("snapshot", snapshot_id),
                            ("stage job", stage_job_id), ("engine revision", engine_revision)):
            _reference_id(name, value)
        _reference_sha(snapshot_sha256); _reference_text(expected_run_key)
        if type(expected_sources) is not tuple or len(expected_sources) != 4:
            raise ValueError("pair reference requires exactly four unique original source triples")
        for source in expected_sources:
            if type(source) is not tuple or len(source) != 3:
                raise ValueError("pair source requires an exact triple")
            for identifier in source:
                _reference_id("source identity", identifier)
        if len(set(expected_sources)) != 4:
            raise ValueError("pair reference requires four unique original source triples")
        where = and_(exact_figure_bundle.c.bundle_id == bundle_id, exact_figure_bundle.c.sealed_at.is_not(None))
        lengths = _reference_lengths((exact_figure_bundle.c.request_key, exact_figure_bundle.c.pipeline_run_key))
        sizes = connection.execute(select(func.greatest(*lengths).label("max_field"),
            sum(lengths).label("total_bytes")).where(where)).mappings().one_or_none()
        if sizes is None:
            raise ValueError("paired sealed history is absent")
        _reference_frontier("history_header", sizes)
        source_where = figure_bundle_source.c.bundle_id == bundle_id
        count = connection.scalar(select(func.count()).select_from(figure_bundle_source).where(source_where))
        if count != 4:
            raise ValueError("paired sealed history does not retain exactly four source triples")
        names = ("bundle_id", "request_key", "bundle_sha256", "security_id", "snapshot_id", "snapshot_sha256",
                 "engine_revision", "pipeline_run_key", "contract_revision", "storing_job_id", "sealed_at")
        row = dict(connection.execute(select(*(exact_figure_bundle.c[name] for name in names))
            .where(where)).mappings().one())
        for name in ("request_key", "pipeline_run_key"):
            _reference_text(row[name])
        if sum(len(row[name].encode("utf-8")) for name in ("request_key", "pipeline_run_key")) != sizes["total_bytes"]:
            raise ValueError("history header changed during bounded projection")
        for name in ("bundle_id", "security_id", "snapshot_id", "engine_revision", "storing_job_id", "contract_revision"):
            _reference_id(name, row[name])
        _reference_sha(row["bundle_sha256"]); _reference_sha(row["snapshot_sha256"])
        FigureHistoryRepository._sealed_snapshot_binding(row, security_id=security_id,
            snapshot_id=snapshot_id, expected_run_key=expected_run_key)
        if (row["contract_revision"] != 2 or row["request_key"] != expected_run_key
                or row["storing_job_id"] != stage_job_id or row["snapshot_sha256"] != snapshot_sha256
                or row["engine_revision"] != engine_revision):
            raise ValueError("paired history belongs to a different actual stage checkpoint")
        sources = tuple(tuple(source) for source in connection.execute(select(figure_bundle_source.c.source_item_id,
            figure_bundle_source.c.source_observation_id, figure_bundle_source.c.occurrence_id)
            .where(source_where).order_by(figure_bundle_source.c.source_item_id,
                figure_bundle_source.c.source_observation_id).limit(5)))
        if len(sources) != 4 or set(sources) != set(expected_sources):
            raise ValueError("paired history source triples differ from original capture")
        for source in sources:
            for identifier in source:
                _reference_id("stored source identity", identifier)
        if type(row["sealed_at"]) is not datetime or row["sealed_at"].tzinfo is None or row["sealed_at"].utcoffset() is None:
            raise ValueError("paired history requires an aware sealed timestamp")
        row["sealed_at"] = row["sealed_at"].isoformat()
        return dict(header=row, sources=sources)

    @staticmethod
    def require_release_contract_on_connection(connection, release_id):
        identifiers = select(release_company.c.exact_bundle_id).where(release_company.c.release_id == release_id)
        FigureHistoryRepository.require_membership_contract_on_connection(connection, identifiers)

    @staticmethod
    def require_membership_contract_on_connection(connection, bundle_ids):
        if isinstance(bundle_ids, (tuple, list)) and not bundle_ids:
            return
        unsupported = connection.scalar(select(exact_figure_bundle.c.contract_revision).where(
            exact_figure_bundle.c.bundle_id.in_(bundle_ids),
            exact_figure_bundle.c.contract_revision.not_in((1, 2))).limit(1))
        if unsupported is not None:
            raise ValueError("declared contract3 history is not publication qualified")

    def validate_publication_binding_on_connection(self, connection, bundle_id, *, security_id, snapshot_id, expected_run_key):
        """Reuse sealed graph admission after the caller locks its entire source union."""
        identity = self.publication_identity_on_connection(connection, bundle_id, security_id=security_id,
            snapshot_id=snapshot_id, expected_run_key=expected_run_key)
        self._validate_bindings(connection, self.load_on_connection(connection, bundle_id))
        return identity

    @staticmethod
    def load_on_connection(connection: Connection, bundle_id: int, *, _raw_work_budget=None) -> ExactFigureBundle:
        if _raw_work_budget is not None:
            _history_raw_load_frontier(connection, bundle_id, _raw_work_budget)
        row = connection.execute(select(exact_figure_bundle).where(exact_figure_bundle.c.bundle_id == bundle_id, exact_figure_bundle.c.sealed_at.is_not(None))).mappings().one()
        selections = connection.execute(select(figure_selection).where(figure_selection.c.bundle_id == bundle_id).order_by(figure_selection.c.node_key)).mappings().all()
        dependencies = connection.execute(select(figure_dependency).where(figure_dependency.c.bundle_id == bundle_id).order_by(figure_dependency.c.parent_node_key, figure_dependency.c.ordinal)).mappings().all()
        if _raw_work_budget is not None:
            from .primary_input_transport import _retained_json_dump_reserve
            _retained_json_dump_reserve(row["observation_bindings"], _raw_work_budget,
                                       ensure_ascii=True, default_spaces=True)
        observed_rows = connection.execute(text("""
            SELECT b.key, o.value_decimal_text, o.record
            FROM jsonb_to_recordset(CAST(:bindings AS jsonb)) AS b(
                key text, artifact_sha256 text, extraction_revision text, location_sha256 text)
            JOIN figure_observation o ON o.artifact_sha256 = b.artifact_sha256
                AND o.extraction_revision = b.extraction_revision AND o.location_sha256 = b.location_sha256
            ORDER BY b.key
        """), {"bindings": json.dumps(row["observation_bindings"])}).mappings().all()
        if len(observed_rows) != len(row["observation_bindings"]):
            raise FigureHistoryConflict("recorded bundle has missing observations")
        observations = [dict(key=o["key"], value_decimal_text=o["value_decimal_text"], **o["record"]) for o in observed_rows]
        record = dict(security_id=row["security_id"], snapshot_id=row["snapshot_id"], snapshot_sha256=row["snapshot_sha256"],
            engine_revision=row["engine_revision"], pipeline_run_key=row["pipeline_run_key"], contract_revision=row["contract_revision"],
            observations=observations, selections=[dict(value_decimal_text=n["value_decimal_text"], **n["record"]) for n in selections],
            dependencies=[d["record"] for d in dependencies],
            coverage=list(connection.scalars(select(figure_bundle_coverage.c.record).where(figure_bundle_coverage.c.bundle_id == bundle_id).order_by(figure_bundle_coverage.c.metric_family, figure_bundle_coverage.c.view))),
            sources=[dict(source_item_id=s["source_item_id"], source_observation_id=s["source_observation_id"], occurrence_id=s["occurrence_id"]) for s in connection.execute(select(figure_bundle_source).where(figure_bundle_source.c.bundle_id == bundle_id).order_by(figure_bundle_source.c.source_item_id, figure_bundle_source.c.source_observation_id)).mappings()])
        if row["contract_revision"] in {2, 3}:
            record.update(row["contract_details"])
            record["document_evidence"] = [dict(key=d["evidence_key"], **d["record"]) for d in connection.execute(
                select(figure_bundle_evidence.c.evidence_key, document_evidence.c.record).join(document_evidence)
                .where(figure_bundle_evidence.c.bundle_id == bundle_id).order_by(figure_bundle_evidence.c.evidence_key)).mappings()]
        if _raw_work_budget is not None:
            _history_raw_load_record(record, _raw_work_budget)
        return ExactFigureBundle.model_validate_json(json.dumps(record))

    @staticmethod
    def read_outputs_on_connection(connection, bundle_id, selector):
        query = select(figure_selection).where(figure_selection.c.bundle_id == bundle_id,
            figure_selection.c.metric_path == selector["metric_path"], figure_selection.c.view == selector["view"],
            func.coalesce(figure_selection.c.record["purpose"].astext, "OUTPUT") == "OUTPUT")
        for key, value in selector["period"].items():
            if key != "origin":
                field = figure_selection.c.record["period"][key].astext
                query = query.where(field.is_(None) if value is None else field == str(value))
        rows = connection.execute(query.limit(2)).mappings().all()
        if len(rows) > 1:
            raise FigureHistoryConflict("ambiguous selected output")
        return rows[0] if rows else None

    @staticmethod
    def read_catalog_on_connection(connection, bundle_id, *, view, after, limit):
        anchor = None
        if after is not None:
            actual = FigureHistoryRepository.read_node_digest_on_connection(connection, bundle_id, after)
            if actual is None or actual["view"] != view or actual["record"].get("purpose", "OUTPUT") != "OUTPUT":
                raise FigureHistoryConflict("catalog continuation unavailable")
            anchor = actual["node_key"]
        query = select(figure_selection).where(figure_selection.c.bundle_id == bundle_id,
            figure_selection.c.view == view,
            func.coalesce(figure_selection.c.record["purpose"].astext, "OUTPUT") == "OUTPUT")
        if anchor is not None:
            query = query.where(figure_selection.c.node_key.collate("C") > anchor)
        return connection.execute(query.order_by(figure_selection.c.node_key.collate("C")).limit(limit + 1)).mappings().all()

    @staticmethod
    def read_catalog_coverage_on_connection(connection, bundle_id, nodes):
        if not nodes:
            return {}
        if len(nodes) > 300:
            raise ValueError("catalog coverage exceeds page bound")
        # Project only the requested paths; the family manifest stays in SQL.
        query = select(figure_selection.c.node_key,
            figure_bundle_coverage.c.record["assessment"].astext.label("assessment"),
            figure_bundle_coverage.c.record["reason"].astext.label("reason"),
            figure_bundle_coverage.c.record["source_fidelity"].astext.label("source_fidelity"),
            figure_bundle_coverage.c.record["earlier_history"].astext.label("earlier_history")).join(
                figure_bundle_coverage, and_(figure_bundle_coverage.c.bundle_id == figure_selection.c.bundle_id,
                    figure_bundle_coverage.c.metric_family == figure_selection.c.metric_family,
                    figure_bundle_coverage.c.view == figure_selection.c.view,
                    figure_bundle_coverage.c.record["metric_paths"].has_key(figure_selection.c.metric_path))).where(
                figure_selection.c.bundle_id == bundle_id,
                figure_selection.c.node_key.in_([node["node_key"] for node in nodes]))
        grouped = {}
        for row in connection.execute(query).mappings():
            grouped.setdefault(row["node_key"], []).append(dict(row))
        result = {}
        for node in nodes:
            rows = grouped.get(node["node_key"], [])
            if len(rows) != 1:
                value = dict(assessment="NOT_ASSESSED", reason="coverage-unavailable",
                    source_fidelity="UNKNOWN", earlier_history="UNKNOWN")
            else:
                value = rows[0]; value.pop("node_key")
            result[node["node_key"]] = dict(public_history_record(value), scope="SUPPLIED_GRAPH_ONLY")
        return result

    @staticmethod
    def read_nodes_on_connection(connection, references):
        refs = tuple(set(references))
        if len(refs) > 601:
            raise ValueError("endpoint batch exceeds bounded history page")
        if not refs:
            return {}
        rows = connection.execute(select(figure_selection).where(
            tuple_(figure_selection.c.bundle_id, figure_selection.c.node_key).in_(refs))).mappings()
        return {(row["bundle_id"], row["node_key"]): row for row in rows}

    @staticmethod
    def read_node_digest_on_connection(connection, bundle_id, digest):
        # ponytail: scans one admitted bundle; add a measured immutable seek key if that scan exceeds the read budget.
        hashed = func.encode(func.sha256(func.convert_to(figure_selection.c.node_key, "UTF8")), "hex")
        rows = connection.execute(select(figure_selection).where(
            figure_selection.c.bundle_id == bundle_id, hashed == digest).limit(2)).mappings().all()
        return rows[0] if len(rows) == 1 else None

    @staticmethod
    def read_coverage_on_connection(connection, bundle_id, node, metric_path, view):
        # Project one requested path, never the entire family path manifest.
        query = select(figure_bundle_coverage.c.record["assessment"].astext.label("assessment"),
            figure_bundle_coverage.c.record["reason"].astext.label("reason"),
            figure_bundle_coverage.c.record["source_fidelity"].astext.label("source_fidelity"),
            figure_bundle_coverage.c.record["earlier_history"].astext.label("earlier_history")).where(
            figure_bundle_coverage.c.bundle_id == bundle_id, figure_bundle_coverage.c.view == view,
            figure_bundle_coverage.c.record["metric_paths"].contains([metric_path]))
        if node is not None:
            query = query.where(figure_bundle_coverage.c.metric_family == node["metric_family"])
        rows = connection.execute(query.limit(2)).mappings().all()
        if len(rows) != 1:
            return dict(assessment="NOT_ASSESSED", reason="coverage-unavailable", earlier_history="UNKNOWN",
                        source_fidelity="UNKNOWN", scope="SUPPLIED_GRAPH_ONLY")
        return dict(public_history_record(dict(rows[0])), scope="SUPPLIED_GRAPH_ONLY")

    @staticmethod
    def read_inputs_on_connection(connection, node, *, after, limit):
        inputs = []
        if after < -1 and node["observation_id"] is not None:
            inputs.append(dict(ordinal=-1, role="reported-occurrence", observation_id=node["observation_id"],
                               input_node_key=None, record={}))
        query = select(figure_dependency).where(figure_dependency.c.bundle_id == node["bundle_id"],
            figure_dependency.c.parent_node_key == node["node_key"], figure_dependency.c.ordinal > after)
        inputs.extend(dict(row) for row in connection.execute(query.order_by(figure_dependency.c.ordinal).limit(limit + 1)).mappings())
        return inputs[:limit + 1]

    @staticmethod
    def read_observations_on_connection(connection, identifiers):
        identifiers = tuple(set(identifiers))
        if len(identifiers) > 300:
            raise ValueError("observation batch exceeds direct input page")
        return {row["observation_id"]: row for row in connection.execute(select(figure_observation).where(
            figure_observation.c.observation_id.in_(identifiers))).mappings()} if identifiers else {}

    @staticmethod
    def read_metadata_on_connection(connection, bundle_id, target, *, kind, after, limit):
        """Key projection scans one immutable manifest; it is not a key index."""
        record = target["record"]
        parameters = dict(bundle=bundle_id, target=json.dumps(record), after=after, limit=limit + 1)
        prefix = """WITH target AS (SELECT CAST(:target AS jsonb) record), actions AS (
            SELECT a.record FROM exact_figure_bundle b,
            LATERAL jsonb_array_elements(COALESCE(b.contract_details->'actions','[]')) a(record), target t
            WHERE b.bundle_id=:bundle AND (
                a.record->>'key' IN (SELECT jsonb_array_elements_text(COALESCE(t.record->'share_basis'->'incorporated_actions','[]')))
                OR a.record->>'key' IN (SELECT jsonb_array_elements_text(COALESCE(t.record->'basis_transition'->'action_keys','[]'))))) """
        if kind == "actions":
            body = "SELECT record->>'key' AS key, record FROM actions"
        elif kind == "assumptions":
            body = """SELECT a.record->>'key' AS key,a.record FROM exact_figure_bundle b,
                LATERAL jsonb_array_elements(COALESCE(b.contract_details->'assumptions','[]')) a(record), target t
                WHERE b.bundle_id=:bundle AND a.record->>'key' IN
                    (SELECT jsonb_array_elements_text(COALESCE(t.record->'assumption_keys','[]')))"""
        elif kind == "documents":
            body = """SELECT e.evidence_key AS key,d.record FROM figure_bundle_evidence e
                JOIN document_evidence d USING(evidence_id) WHERE e.bundle_id=:bundle AND e.evidence_key IN (
                SELECT jsonb_array_elements_text(COALESCE(record->'share_basis'->'evidence_keys','[]')) FROM target
                UNION SELECT jsonb_array_elements_text(COALESCE(record->'basis_transition'->'evidence_keys','[]')) FROM target
                UNION SELECT jsonb_array_elements_text(record->'evidence_keys') FROM actions)"""
        else:
            raise ValueError("unsupported metadata page")
        prefix += ", records AS (" + body + ") "
        anchor = None
        if after is not None:
            matches = connection.execute(text(prefix + "SELECT key FROM records WHERE encode(sha256(convert_to(key,'UTF8')),'hex')=:after LIMIT 2"), parameters).scalars().all()
            if len(matches) != 1:
                raise FigureHistoryConflict("metadata continuation anchor unavailable")
            anchor = matches[0]
        parameters["anchor"] = anchor
        rows = connection.execute(text(prefix + "SELECT key,record FROM records WHERE (CAST(:anchor AS text) IS NULL OR key>:anchor) ORDER BY key LIMIT :limit"), parameters).mappings().all()
        model = {"actions": ActionEvidence, "assumptions": Assumption, "documents": DocumentEvidence}[kind]
        output = []
        for row in rows:
            record = model.model_validate_json(json.dumps(dict(row["record"], key=row["key"]))).model_dump(mode="json")
            record.pop("key")
            output.append(dict(key=row["key"], record=public_history_record(record)))
        return output

    @staticmethod
    def read_sources_on_connection(connection, *, bundle_id, release_id, security_id, after, limit):
        capture = source_check_occurrence.alias("capture")
        accepted = source_check_occurrence.alias("accepted")
        query = select(figure_bundle_source.c.source_item_id, figure_bundle_source.c.source_observation_id,
            figure_bundle_source.c.occurrence_id.label("captured_occurrence_id"), capture.c.checked_at.label("captured_checked_at"),
            release_company_source.c.accepted_occurrence_id, accepted.c.checked_at.label("accepted_checked_at"),
            source_observation.c.artifact_sha256, source_observation.c.state,
            source_observation.c.source_url, source_observation.c.source_published_at,
            source_observation.c.detected_at.label("first_detected_at")).select_from(figure_bundle_source).join(
                source_observation, source_observation.c.source_observation_id == figure_bundle_source.c.source_observation_id).outerjoin(
                capture, capture.c.occurrence_id == figure_bundle_source.c.occurrence_id).outerjoin(release_company_source,
                and_(release_company_source.c.release_id == release_id, release_company_source.c.security_id == security_id,
                     release_company_source.c.source_observation_id == figure_bundle_source.c.source_observation_id)).outerjoin(
                accepted, accepted.c.occurrence_id == release_company_source.c.accepted_occurrence_id).where(
                figure_bundle_source.c.bundle_id == bundle_id)
        if after is not None:
            query = query.where(tuple_(figure_bundle_source.c.source_item_id, figure_bundle_source.c.source_observation_id) > tuple(after))
        rows = connection.execute(query.order_by(figure_bundle_source.c.source_item_id,
            figure_bundle_source.c.source_observation_id).limit(limit + 1)).mappings().all()
        return [dict(public_history_record(dict(row)), scope="BUNDLE_DEPENDENCIES",
                     affected_figures_assessed=False) for row in rows]

    @staticmethod
    def read_coverage_paths_on_connection(connection, bundle_id, node, *, after, limit):
        parameters = dict(bundle=bundle_id, family=node["metric_family"], view=node["view"], after=after, limit=limit + 1)
        prefix = """WITH paths AS (SELECT p.key,c.record FROM figure_bundle_coverage c,
            LATERAL jsonb_array_elements_text(c.record->'metric_paths') p(key)
            WHERE c.bundle_id=:bundle AND c.metric_family=:family AND c.view=:view) """
        anchor = None
        if after is not None:
            matches = connection.execute(text(prefix + "SELECT key FROM paths WHERE encode(sha256(convert_to(key,'UTF8')),'hex')=:after LIMIT 2"), parameters).scalars().all()
            if len(matches) != 1:
                raise FigureHistoryConflict("coverage continuation unavailable")
            anchor = matches[0]
        parameters["anchor"] = anchor
        rows = connection.execute(text(prefix + """SELECT key,jsonb_build_object(
                'metric_family',record->'metric_family','view',record->'view','assessment',record->'assessment',
                'reason',record->'reason','source_fidelity',record->'source_fidelity',
                'earlier_history',record->'earlier_history') AS record FROM paths
            WHERE (CAST(:anchor AS text) IS NULL OR key>:anchor) ORDER BY key LIMIT :limit"""), parameters).mappings()
        return [dict(key=row["key"], record=dict(public_history_record(row["record"]), metric_path=public_history_text(row["key"]), scope="SUPPLIED_GRAPH_ONLY")) for row in rows]

    @staticmethod
    def read_journal_binding_on_connection(connection, release_id, security_id, item_id, bundle_id):
        accepted = source_check_occurrence.alias("accepted_journal")
        rows = connection.execute(select(release_company_source.c.source_observation_id,
            release_company_source.c.accepted_occurrence_id, accepted.c.checked_at.label("accepted_checked_at"),
            source_observation.c.source_url, source_observation.c.source_published_at,
            source_observation.c.detected_at.label("first_detected_at")).select_from(release_company_source).join(source_observation,
                source_observation.c.source_observation_id == release_company_source.c.source_observation_id).outerjoin(
                accepted, accepted.c.occurrence_id == release_company_source.c.accepted_occurrence_id).where(
                release_company_source.c.release_id == release_id, release_company_source.c.security_id == security_id,
                source_observation.c.source_item_id == item_id).limit(2)).mappings().all()
        if not rows:
            return None
        if len(rows) != 1:
            raise FigureHistoryConflict("ambiguous release source journal binding")
        binding = dict(rows[0])
        capture = None
        if bundle_id is not None:
            capture = connection.execute(select(figure_bundle_source.c.occurrence_id,
                source_check_occurrence.c.checked_at).outerjoin(source_check_occurrence,
                source_check_occurrence.c.occurrence_id == figure_bundle_source.c.occurrence_id).where(
                figure_bundle_source.c.bundle_id == bundle_id, figure_bundle_source.c.source_item_id == item_id,
                figure_bundle_source.c.source_observation_id == binding["source_observation_id"])).mappings().one_or_none()
        binding.update(captured_occurrence_id=capture["occurrence_id"] if capture else None,
                       captured_checked_at=capture["checked_at"] if capture else None)
        return public_history_record(binding)

    @staticmethod
    def read_journal_on_connection(connection, item_id, *, after, limit):
        query = select(source_check_occurrence.c.occurrence_id, source_check_occurrence.c.source_observation_id,
            source_check_occurrence.c.checked_at, source_observation.c.state,
            source_observation.c.canonical_metadata["reason"].astext.label("reason"),
            source_observation.c.artifact_sha256, source_observation.c.source_url,
            source_observation.c.source_published_at, source_observation.c.detected_at.label("first_detected_at")).select_from(source_check_occurrence).join(
                source_observation, source_observation.c.source_observation_id == source_check_occurrence.c.source_observation_id).where(
                source_check_occurrence.c.source_item_id == item_id)
        if after is not None:
            query = query.where(source_check_occurrence.c.occurrence_id < after)
        rows = connection.execute(query.order_by(source_check_occurrence.c.occurrence_id.desc()).limit(limit + 1)).mappings()
        return [dict(public_history_record(dict(row)), capture_is_acceptance=False, affected_figures_assessed=False) for row in rows]


def _history_raw_store_frontier(bundle, budget):
    from .primary_input_transport import (_retained_model_dump_reserve,
        _retained_json_dump_reserve, _retained_driver_reserve)
    # Before existing validation/dump/digest. Superset fields only over-reserve removed defaults.
    _retained_model_dump_reserve(bundle, budget)
    _retained_model_dump_reserve(bundle, budget)
    _retained_json_dump_reserve(bundle, budget, sha=True)
    if bundle.document_evidence:
        _retained_model_dump_reserve(bundle.document_evidence, budget)
        _retained_json_dump_reserve(bundle.document_evidence, budget, sha=True)
    bindings = []
    for observed in bundle.observations:
        _retained_model_dump_reserve(observed.reference.location, budget)
        _retained_json_dump_reserve(observed.reference.location, budget, sha=True)
        bindings.append(dict(key=observed.key, artifact_sha256=observed.reference.artifact_sha256,
            extraction_revision=observed.reference.extraction_revision, location_sha256="0" * 64))
    _retained_driver_reserve(bindings, budget, returned=True)
    _retained_driver_reserve(dict(actions=bundle.actions, assumptions=bundle.assumptions), budget, returned=True)


def _history_raw_record_frontier(record, budget, *, locator=None):
    from .primary_input_transport import (_retained_model_dump_reserve,
        _retained_json_dump_reserve, _retained_driver_reserve)
    _retained_model_dump_reserve(record, budget)
    if locator is not None:
        _retained_model_dump_reserve(locator, budget)
        _retained_json_dump_reserve(locator, budget, sha=True)
    # Existing record emitter removes some fields; its unchanged JSONB encoder remains owner.
    _retained_driver_reserve(record, budget)


def _history_raw_load_frontier(connection, bundle_id, budget):
    # to_jsonb(row) supplies a conservative UTF-8 envelope for the actual selected columns.
    # This preflight transfers only two integers per disjoint group, never its numerical records.
    queries = (
        "SELECT count(*) AS count, COALESCE(sum(octet_length(to_jsonb(e)::text)),0) AS bytes FROM exact_figure_bundle e WHERE e.bundle_id=:bundle AND e.sealed_at IS NOT NULL",
        "SELECT count(*) AS count, COALESCE(sum(octet_length(to_jsonb(n)::text)),0) AS bytes FROM figure_selection n WHERE n.bundle_id=:bundle",
        "SELECT count(*) AS count, COALESCE(sum(octet_length(to_jsonb(d)::text)),0) AS bytes FROM figure_dependency d WHERE d.bundle_id=:bundle",
        "SELECT count(*) AS count, COALESCE(sum(octet_length(to_jsonb(c)::text)),0) AS bytes FROM figure_bundle_coverage c WHERE c.bundle_id=:bundle",
        "SELECT count(*) AS count, COALESCE(sum(octet_length(to_jsonb(s)::text)),0) AS bytes FROM figure_bundle_source s WHERE s.bundle_id=:bundle",
        "SELECT count(*) AS count, COALESCE(sum(octet_length(to_jsonb(d)::text)+octet_length(b.evidence_key)),0) AS bytes FROM figure_bundle_evidence b JOIN document_evidence d ON d.evidence_id=b.evidence_id WHERE b.bundle_id=:bundle",
        "SELECT count(*) AS count, COALESCE(sum(octet_length(to_jsonb(o)::text)+octet_length(b.key)),0) AS bytes FROM exact_figure_bundle e CROSS JOIN LATERAL jsonb_to_recordset(e.observation_bindings) AS b(key text, artifact_sha256 text, extraction_revision text, location_sha256 text) JOIN figure_observation o ON o.artifact_sha256=b.artifact_sha256 AND o.extraction_revision=b.extraction_revision AND o.location_sha256=b.location_sha256 WHERE e.bundle_id=:bundle AND e.sealed_at IS NOT NULL",
    )
    total = 0
    for query in queries:
        row = connection.execute(text(query), dict(bundle=bundle_id)).mappings().one()
        from .sec_ingestion import _size
        _size(row["count"])
        total += _size(row["bytes"])
    # DBAPI UTF-8 input and JSON/number materialization occur in existing selects next.
    budget.reserve(reads=total, work=3 * total)


def _history_raw_load_record(record, budget):
    from .primary_input_transport import (_retained_json_dump_reserve, _retained_model_work_sizes)
    # load's default ASCII/spaced dump and the initial strict JSON materialization.
    size = _retained_json_dump_reserve(record, budget, ensure_ascii=True, default_spaces=True)
    budget.json_decode(size)
    # ExactFigureBundle's existing JSON before-validator emits and adapts each disjoint field.
    for value in record.values():
        field_size, _ = _retained_model_work_sizes(value, budget)
        budget.reserve(work=2 * field_size)
        budget.json_decode(field_size)


def _history_raw_row_read(connection, table_name, predicate, parameters, budget):
    if table_name not in {"exact_figure_bundle", "document_evidence", "figure_observation"}:
        raise ValueError("owning history row required")
    # Names and predicate are private constants; actual identifiers remain bound parameters.
    row = connection.execute(text("SELECT COALESCE(sum(octet_length(to_jsonb(t)::text)),0) AS bytes FROM "
        + table_name + " t WHERE " + predicate), parameters).mappings().one()
    from .sec_ingestion import _size
    size = _size(row["bytes"])
    budget.reserve(reads=size, work=3 * size)

def _history_raw_projection_read(connection, from_clause, columns, predicate, budget):
    from sqlalchemy import Text, cast
    from .sec_ingestion import _size
    lengths = tuple(func.coalesce(func.octet_length(cast(column, Text)), 0) for column in columns)
    size = connection.scalar(select(func.coalesce(func.sum(sum(lengths)), 0)).select_from(from_clause).where(predicate))
    # Plain projected text: DBAPI UTF-8 decode, the owning checks' UTF-8 work, comparisons.
    size = _size(size)
    budget.reserve(reads=size, work=3 * size)
