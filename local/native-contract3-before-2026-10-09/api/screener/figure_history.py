"""Immutable exact companion records supplied by the existing financial engine."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Callable, Literal, Union
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_serializer, model_validator
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


Location = Annotated[Union[SecJsonLocation, DeraLocation, XmlLocation, InlineLocation, DocumentSpan], Field(discriminator="kind")]


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
        return self


EvidenceRole = Literal["LEGAL_ACTION", "INCORPORATED_BASIS", "RECEIPT_TERMS", "CORRECTION_EXPLANATION"]


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
            if self.reference.location.kind != "INLINE_XBRL" and Decimal(decimal_text(self.source_lexical_text.strip())) != Decimal(self.value_decimal_text):
                raise ValueError("source lexical value differs from reported value")
        location = self.reference.location
        if isinstance(location, DeraLocation):
            if (location.qtrs == 0 and self.period.kind != "INSTANT") or (location.qtrs > 0 and self.period.kind != "DURATION"):
                raise ValueError("DERA qtrs contradicts actual period kind")
            if self.period.end != location.ddate or (self.period.kind == "DURATION" and self.period.origin != "INFERRED"):
                raise ValueError("DERA reports end/qtrs; duration start must remain inferred")
        if isinstance(location, XmlLocation) and self.qname != location.qname:
            raise ValueError("XML occurrence QName differs from observation")
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

    @model_serializer(mode="wrap")
    def compatible_record(self, handler):
        record = handler(self)
        for name, absent in (("purpose", self.purpose == "OUTPUT"), ("share_basis", self.share_basis is None),
                             ("basis_transition", self.basis_transition is None), ("assumption_keys", not self.assumption_keys)):
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


class BundleSource(Record):
    source_item_id: PositiveId
    source_observation_id: PositiveId
    occurrence_id: PositiveId | None = None


class ExactFigureBundle(Record):
    security_id: PositiveId
    snapshot_id: PositiveId
    snapshot_sha256: Digest
    engine_revision: Annotated[int, Field(gt=0)]
    pipeline_run_key: Name
    contract_revision: Literal[1, 2] = 1
    observations: tuple[ReportedObservation, ...]
    selections: tuple[FigureSelection, ...]
    dependencies: tuple[CalculationDependency, ...] = ()
    coverage: tuple[Coverage, ...]
    sources: tuple[BundleSource, ...] = ()
    document_evidence: tuple[DocumentEvidence, ...] = ()
    actions: tuple[ActionEvidence, ...] = ()
    assumptions: tuple[Assumption, ...] = ()

    @model_serializer(mode="wrap")
    def compatible_bundle(self, handler):
        record = handler(self)
        if self.contract_revision == 1:
            for name in ("document_evidence", "actions", "assumptions"):
                record.pop(name)
        return record

    @model_validator(mode="after")
    def validate_graph(self):
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
        if self.contract_revision == 2:
            self._validate_contract2(nodes, observations, dependencies, edges)
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
            self._documents[key] = dict(reference=self._reference(document.reference), roles=sorted(document.roles))
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
        return dict(kind=basis.kind, security_class=basis.security_class, incorporation=basis.incorporation,
            actions=self._ordered([self._action(k) for k in basis.incorporated_actions]),
            evidence=self._ordered([self._document(k) for k in basis.evidence_keys]))

    @staticmethod
    def _unit(unit):
        return {**unit.model_dump(mode="json"), "numerator": sorted(unit.numerator), "denominator": sorted(unit.denominator)}

    def _scope(self, scope):
        return {**scope.model_dump(mode="json"), "dimensions": self._ordered([d.model_dump(mode="json") for d in scope.dimensions])}

    def context(self, node):
        basis = node.share_basis
        return dict(unit=self._unit(node.unit), period=node.period.model_dump(mode="json"), scope=self._scope(node.scope),
            security_basis=node.security_basis, receipt_ratio_text=node.receipt_ratio_text,
            share_basis=None if basis is None else dict(kind=basis.kind, security_class=basis.security_class,
                incorporation=basis.incorporation, actions=self._ordered([
                    {name: self._action(k)[name] for name in ("legal_identity", "security_class", "old_shares_text", "new_shares_text")}
                    for k in basis.incorporated_actions])))

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
    if x is not None and y is not None:
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
    for key in ("node_key", "observation_key"):
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
        hashes = {d.reference.artifact_sha256 for d in bundle.document_evidence}
        with self.engine.connect() as connection:
            artifacts = {r["content_sha256"]: r for r in connection.execute(select(evidence_artifact).where(
                evidence_artifact.c.content_sha256.in_(hashes))).mappings()}
        if set(artifacts) != hashes:
            raise ValueError("document artifact is not registered verified evidence")
        for digest, artifact in artifacts.items():
            body = object_store.read_verified(artifact["object_key"], digest, artifact["byte_size"])
            for document in bundle.document_evidence:
                if document.reference.artifact_sha256 != digest:
                    continue
                span = document.reference.location
                if span.end_byte > len(body) or hashlib.sha256(body[span.start_byte:span.end_byte]).hexdigest() != span.span_sha256:
                    raise ValueError("document byte offset/span hash differs from verified object")
                body[span.start_byte:span.end_byte].decode(span.encoding)
        digest = _digest([d.model_dump(mode="json") for d in bundle.document_evidence])
        return PreparedDocumentEvidence(digest, hmac.digest(self._preparation_key, digest.encode(), "sha256"))

    def _require_prepared(self, bundle, prepared):
        if not bundle.document_evidence:
            return
        digest = _digest([d.model_dump(mode="json") for d in bundle.document_evidence])
        if (not isinstance(prepared, PreparedDocumentEvidence) or prepared.record_sha256 != digest
                or not hmac.compare_digest(prepared.verification_seal, hmac.digest(self._preparation_key, digest.encode(), "sha256"))):
            raise ValueError("document evidence requires verified preparation outside SQL")

    def store(self, lease: LeaseToken, *, request_key: str, bundle: ExactFigureBundle,
              prepared_documents: PreparedDocumentEvidence | None = None) -> int:
        bundle = ExactFigureBundle.model_validate(bundle)
        self._require_prepared(bundle, prepared_documents)
        with self.engine.begin() as connection:
            return self.store_on_connection(connection, lease, request_key=request_key, bundle=bundle,
                                            prepared_documents=prepared_documents)

    def store_on_connection(self, connection: Connection, lease: LeaseToken, *, request_key: str, bundle: ExactFigureBundle,
                            prepared_documents: PreparedDocumentEvidence | None = None) -> int:
        if not isinstance(request_key, str) or not request_key.strip():
            raise ValueError("request_key required")
        bundle = ExactFigureBundle.model_validate(bundle)
        self._require_prepared(bundle, prepared_documents)
        self._jobs._lock_live_lease(connection, lease, self._clock)
        issuer_id = self._validate_bindings(connection, bundle)
        self._jobs._lock_live_lease(connection, lease, self._clock)
        payload = bundle.model_dump(mode="json")
        digest = _digest(payload)
        existing = connection.execute(insert(exact_figure_bundle).values(
            request_key=request_key, bundle_sha256=digest, security_id=bundle.security_id,
            snapshot_id=bundle.snapshot_id, engine_revision=bundle.engine_revision,
            snapshot_sha256=bundle.snapshot_sha256, pipeline_run_key=bundle.pipeline_run_key,
            contract_revision=bundle.contract_revision, storing_job_id=lease.job_id,
            contract_details={name: payload[name] for name in ("actions", "assumptions")} if bundle.contract_revision == 2 else {},
            observation_bindings=[dict(key=o.key, artifact_sha256=o.reference.artifact_sha256,
                extraction_revision=o.reference.extraction_revision,
                location_sha256=_digest(o.reference.location.model_dump(mode="json"))) for o in bundle.observations],
        ).on_conflict_do_nothing(index_elements=["request_key"]).returning(exact_figure_bundle)).mappings().one_or_none()
        created = existing is not None
        self._jobs._lock_live_lease(connection, lease, self._clock)
        if existing is None:
            existing = connection.execute(select(exact_figure_bundle).where(exact_figure_bundle.c.request_key == request_key)).mappings().one()
            if existing["bundle_sha256"] != digest or existing["sealed_at"] is None:
                raise FigureHistoryConflict("request_key has different exact bundle")
            if self.load_on_connection(connection, existing["bundle_id"]).model_dump(mode="json") != payload:
                raise FigureHistoryConflict("recorded exact bundle differs")
        if created:
            bundle_id = existing["bundle_id"]
            for document in bundle.document_evidence:
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
                    previous = connection.execute(select(document_evidence).where(
                        document_evidence.c.artifact_sha256 == reference.artifact_sha256,
                        document_evidence.c.extraction_revision == reference.extraction_revision,
                        document_evidence.c.location_sha256 == locator)).mappings().one()
                    if previous["record"] != record or previous["issuer_id"] != issuer_id:
                        raise FigureHistoryConflict("captured document occurrence has different evidence")
                    identifier = previous["evidence_id"]
                self._jobs._lock_live_lease(connection, lease, self._clock)
                connection.execute(figure_bundle_evidence.insert().values(bundle_id=bundle_id,
                    evidence_key=document.key, evidence_id=identifier))
            observation_ids = {}
            for observation in bundle.observations:
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
                    previous = connection.execute(select(figure_observation).where(
                        figure_observation.c.artifact_sha256 == reference.artifact_sha256,
                        figure_observation.c.extraction_revision == reference.extraction_revision,
                        figure_observation.c.location_sha256 == locator_hash)).mappings().one()
                    if previous["record"] != record or previous["value_decimal_text"] != value or previous["issuer_id"] != issuer_id:
                        raise FigureHistoryConflict("captured artifact occurrence has different observation")
                    row = previous["observation_id"]
                observation_ids[observation.key] = row
            self._jobs._lock_live_lease(connection, lease, self._clock)
            for node in bundle.selections:
                record = node.model_dump(mode="json")
                value = record.pop("value_decimal_text")
                connection.execute(figure_selection.insert().values(bundle_id=bundle_id, node_key=node.node_key,
                    metric_family=node.metric_family, metric_path=node.metric_path, view=node.view,
                    value_decimal_text=value, observation_id=observation_ids.get(node.observation_key), record=record))
            for dependency in bundle.dependencies:
                connection.execute(figure_dependency.insert().values(bundle_id=bundle_id,
                    parent_node_key=dependency.parent_node_key, ordinal=dependency.ordinal,
                    observation_id=observation_ids.get(dependency.observation_key),
                    input_node_key=dependency.selection_node_key, record=dependency.model_dump(mode="json")))
            for covered in bundle.coverage:
                connection.execute(figure_bundle_coverage.insert().values(bundle_id=bundle_id,
                    metric_family=covered.metric_family, view=covered.view, record=covered.model_dump(mode="json")))
            for source in bundle.sources:
                connection.execute(figure_bundle_source.insert().values(bundle_id=bundle_id, **source.model_dump()))
            self._jobs._lock_live_lease(connection, lease, self._clock)
            connection.execute(update(exact_figure_bundle).where(exact_figure_bundle.c.bundle_id == bundle_id).values(sealed_at=self._clock()))
        self._jobs._lock_live_lease(connection, lease, self._clock)
        return existing["bundle_id"]

    def _validate_bindings(self, connection, bundle):
        snapshot = connection.execute(select(company_snapshot).where(company_snapshot.c.snapshot_id == bundle.snapshot_id)).mappings().one_or_none()
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
        for source in bundle.sources:
            row = connection.execute(select(source_observation, source_item.c.source_system, source_item.c.issuer_source_identifier, source_item.c.item_kind)
                .join(source_item).where(source_observation.c.source_observation_id == source.source_observation_id)).mappings().one_or_none()
            identifier = row["issuer_source_identifier"] if row else None
            issuer_matches = self._same_identifier(company["source_system"], identifier, company["source_identifier"])
            # Shared inventory/archive resources have no issuer; each exact occurrence supplies it.
            shared_resource = row is not None and identifier is None and row["item_kind"] in {"inventory", "resource"}
            if row is None or row["source_item_id"] != source.source_item_id or row["state"] != "present" or row["artifact_sha256"] not in artifacts or row["source_system"] != company["source_system"] or not (issuer_matches or shared_resource):
                raise ValueError("bundle source does not match captured issuer/artifact revision")
            source_artifacts.add(row["artifact_sha256"])
            source_documents.add((row["artifact_sha256"], row["source_url"]))
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
    def publication_identity_on_connection(connection, bundle_id, *, security_id, snapshot_id, expected_run_key):
        if type(bundle_id) is not int or bundle_id <= 0:
            raise ValueError("exact_bundle_id must be a positive integer")
        if not isinstance(expected_run_key, str) or not expected_run_key.strip():
            raise ValueError("exact binding requires expected_exact_run_key")
        row = connection.execute(select(exact_figure_bundle).where(
            exact_figure_bundle.c.bundle_id == bundle_id,
            exact_figure_bundle.c.sealed_at.is_not(None))).mappings().one_or_none()
        if row is None or row["security_id"] != security_id or row["snapshot_id"] != snapshot_id or row["pipeline_run_key"] != expected_run_key:
            raise ValueError("exact binding differs from sealed snapshot/security/producer run")
        sources = tuple(connection.scalars(select(figure_bundle_source.c.source_observation_id).where(
            figure_bundle_source.c.bundle_id == bundle_id).order_by(figure_bundle_source.c.source_observation_id)))
        return dict(exact_bundle_id=bundle_id, exact_bundle_sha256=row["bundle_sha256"],
                    expected_exact_run_key=row["pipeline_run_key"], source_observation_ids=sources)

    def validate_publication_binding_on_connection(self, connection, bundle_id, *, security_id, snapshot_id, expected_run_key):
        """Reuse sealed graph admission after the caller locks its entire source union."""
        identity = self.publication_identity_on_connection(connection, bundle_id, security_id=security_id,
            snapshot_id=snapshot_id, expected_run_key=expected_run_key)
        self._validate_bindings(connection, self.load_on_connection(connection, bundle_id))
        return identity

    @staticmethod
    def load_on_connection(connection: Connection, bundle_id: int) -> ExactFigureBundle:
        row = connection.execute(select(exact_figure_bundle).where(exact_figure_bundle.c.bundle_id == bundle_id, exact_figure_bundle.c.sealed_at.is_not(None))).mappings().one()
        selections = connection.execute(select(figure_selection).where(figure_selection.c.bundle_id == bundle_id).order_by(figure_selection.c.node_key)).mappings().all()
        dependencies = connection.execute(select(figure_dependency).where(figure_dependency.c.bundle_id == bundle_id).order_by(figure_dependency.c.parent_node_key, figure_dependency.c.ordinal)).mappings().all()
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
        if row["contract_revision"] == 2:
            record.update(row["contract_details"])
            record["document_evidence"] = [dict(key=d["evidence_key"], **d["record"]) for d in connection.execute(
                select(figure_bundle_evidence.c.evidence_key, document_evidence.c.record).join(document_evidence)
                .where(figure_bundle_evidence.c.bundle_id == bundle_id).order_by(figure_bundle_evidence.c.evidence_key)).mappings()]
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
