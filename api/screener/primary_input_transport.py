"""Registered immutable private declarations; financial consumers remain separate."""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
import hashlib
import json
from types import MappingProxyType, SimpleNamespace
from .sources import sec_json

from sqlalchemy import select

from . import evidence
from .artifacts import read_registered_artifact
from .models import CapturedSource, UnavailablePrimaryInput
from .postgres import issuer, priced_security, evidence_artifact
from .primary_input_plan import MAXIMUM_BYTES, NATIVE_PREPARATION_LIMITS, decode_primary_plan, decode_native_preparation_plan
from .shared_companies import SnapshotArtifact
from .source_validity import load_delivery


PLAN_ROLE = "primary_input_plan"
DEPENDENCY_ROLE = "primary_input_dependency"


@dataclass(frozen=True)
class ResolvedPrimaryInputs:
    plan_sha256: str
    security_id: int
    original_company_facts: dict
    outcomes: tuple
    captured_sources: tuple[CapturedSource, ...]
    artifacts: tuple[SnapshotArtifact, ...]
    manifest: dict


@dataclass(frozen=True)
class RegisteredNativeInputs:
    plan: object
    resolved: ResolvedPrimaryInputs
    company_facts_source: CapturedSource
    artifact_metadata: tuple[dict, ...]
    graph_gaps: tuple[str | None, ...]
    xml_work_bytes: int
    retained_graph: object | None = None


class PrimaryInputTransport:
    def __init__(self, artifacts, objects, observations):
        self.artifacts, self.objects, self.observations = artifacts, objects, observations

    def resolve(self, plan_sha256, *, cik, company_facts_source, company_facts_payload,
                security_id=None, security_identifier=None, payloads=None, company_facts_occurrence_id=None):
        _, raw = read_registered_artifact(plan_sha256, self.objects, self.artifacts, maximum=MAXIMUM_BYTES,
                                          verified_payloads=payloads)
        plan = decode_primary_plan(raw)
        if plan.cik != cik:
            raise ValueError("registered plan identifies another issuer")
        if company_facts_source is None:
            candidates = [dependency.captured_source for dependency in plan.dependencies
                          if dependency.captured_source is not None and dependency.captured_source.role == "company_facts"
                          and dependency.captured_source.artifact_sha256 == hashlib.sha256(company_facts_payload).hexdigest()
                          and dependency.captured_source.occurrence_id == company_facts_occurrence_id]
            if len(candidates) != 1:
                raise ValueError("captured plan has no unique declared Company Facts occurrence")
            company_facts_source = candidates[0]
        if security_id is not None:
            if plan.security_id != security_id:
                raise ValueError("registered plan identifies another security")
        else:
            with self.observations.engine.connect() as connection:
                security = connection.execute(select(priced_security.c.security_identifier,
                    issuer.c.source_system, issuer.c.source_identifier).join(issuer,
                    issuer.c.issuer_id == priced_security.c.issuer_id).where(
                    priced_security.c.security_id == plan.security_id)).mappings().one_or_none()
            if (security is None or security["source_system"] != "SEC"
                    or security["source_identifier"] != cik
                    or security["security_identifier"] != security_identifier):
                raise ValueError("registered plan security does not bind this SEC issuer")
        supplied = payloads if payloads is not None else {}
        if hashlib.sha256(company_facts_payload).hexdigest() != company_facts_source.artifact_sha256:
            raise ValueError("pinned Company Facts bytes differ")
        if company_facts_source.artifact_sha256 in supplied and supplied[company_facts_source.artifact_sha256] != company_facts_payload:
            raise ValueError("pinned Company Facts differs from verified request bytes")
        verified = {}
        dependencies, sources, records, links = [], [], [], [SnapshotArtifact(plan_sha256, PLAN_ROLE)]
        for dependency in plan.dependencies:
            if dependency.artifact_sha256 not in verified:
                artifact, payload = read_registered_artifact(dependency.artifact_sha256, self.objects, self.artifacts,
                                                             verified_payloads=supplied)
                if artifact.byte_size != dependency.byte_size:
                    raise ValueError("registered dependency byte size differs")
                if dependency.artifact_sha256 in supplied and supplied[dependency.artifact_sha256] != payload:
                    raise ValueError("registered dependency differs from supplied bytes")
                verified[dependency.artifact_sha256] = payload
                links.append(SnapshotArtifact(dependency.artifact_sha256, DEPENDENCY_ROLE))
            status = "REGISTERED_ARTIFACT_ONLY"
            captured, delivery, digest = dependency.captured_source, dependency.delivery, dependency.observation_sha256
            stored = None
            if captured is not None and delivery is not None and digest is not None:
                stored = self.observations.by_sha256(digest)
                self._verify_dependency_metadata(dependency, stored, cik)
                actual = self.observations.delivery(delivery.occurrence_id,
                    source_item_id=delivery.source_item_id, source_observation_id=delivery.source_observation_id)
                self._verify_dependency_delivery(dependency, actual)
                status = "REGISTERED_SOURCE_DELIVERY"
                sources.append(captured)
                records.append((dependency, stored))
            dependencies.append(dict(artifact_sha256=dependency.artifact_sha256,
                byte_size=dependency.byte_size, roles=list(dependency.roles), status=status,
                observation_sha256=digest, source_item_id=None if captured is None else captured.source_item_id,
                source_observation_id=None if captured is None else captured.source_observation_id,
                occurrence_id=None if delivery is None else delivery.occurrence_id,
                captured_source=None if captured is None else asdict(captured),
                delivery=None if delivery is None else {**asdict(delivery), "checked_at": delivery.checked_at.isoformat()}))
        if company_facts_source not in sources:
            raise ValueError("plan omits the exact pinned Company Facts delivery")
        reasons = tuple(self._missing_witness(mapping, records, company_facts_source) for mapping in plan.declarations)
        original_facts, admitted_outcomes = evidence.decode_and_admit_primary_inputs(cik,
            tuple(mapping for mapping, reason in zip(plan.declarations, reasons, strict=True) if reason is None), verified,
            company_facts_source=company_facts_source)
        admitted = iter(admitted_outcomes)
        results = [next(admitted) if reason is None else UnavailablePrimaryInput(
            mapping.key, mapping.canonical_namespace, mapping.canonical_tag, reason,
            copy.deepcopy(mapping.original), copy.deepcopy(mapping.target_origin))
            for mapping, reason in zip(plan.declarations, reasons, strict=True)]
        outcomes = tuple(results)
        admission = [dict(key=value.mapping.key if hasattr(value, "mapping") else value.key,
            status="ADMITTED" if hasattr(value, "mapping") else "UNAVAILABLE",
            reason=None if hasattr(value, "mapping") else value.reason) for value in outcomes]
        manifest = dict(primary_input_revision=1, primary_input_plan_sha256=plan_sha256,
            dependencies=dependencies, admission=admission,
            financial_consumer="NOT_IMPLEMENTED", native_history="NOT_ASSESSED")
        return ResolvedPrimaryInputs(plan_sha256, plan.security_id, original_facts, outcomes,
            tuple(dict.fromkeys(sources)), tuple(dict.fromkeys(links)), manifest)

    @staticmethod
    def _verify_dependency_metadata(dependency, stored, cik):
        captured = dependency.captured_source
        item, observation = stored.item, stored.observation
        if (item.source_system != "SEC" or item.issuer_source_identifier != cik
                or item.source_item_id != captured.source_item_id
                or observation.source_observation_id != captured.source_observation_id
                or observation.state != "present" or observation.artifact_sha256 != dependency.artifact_sha256
                or observation.source_url != captured.source_document
                or observation.canonical_metadata.get("cik", cik) != cik
                or (observation.canonical_metadata.get("role") != captured.role
                    and not (item.item_kind == "filing" and captured.role == "filing"))):
            raise ValueError("registered source dependency identity or role differs")
        if captured.role == "company_facts" and (captured.fidelity != "EXACT_LEXICAL"
                or type(observation.canonical_metadata.get("capture_revision")) is not int
                or observation.canonical_metadata.get("capture_revision") != 2
                or observation.canonical_metadata.get("capture_fidelity") != "RAW_HTTP_BYTES"
                or item.parent_source_item_id is not None):
            raise ValueError("Company Facts delivery or raw HTTP admission differs")

    @staticmethod
    def _verify_dependency_delivery(dependency, actual):
        captured, delivery = dependency.captured_source, dependency.delivery
        if actual != delivery or (captured.source_item_id, captured.source_observation_id, captured.occurrence_id) != (
                actual.source_item_id, actual.source_observation_id, actual.occurrence_id):
            raise ValueError("registered source delivery differs")

    @staticmethod
    def _artifact_identity(artifact):
        return dict(content_sha256=artifact.content_sha256, object_key=artifact.object_key,
                    byte_size=artifact.byte_size, media_type=artifact.media_type)

    @staticmethod
    def validate_native_request(plan_sha256, cik, company_facts_occurrence_id, security_identifier):
        if (type(cik) is not str or len(cik) != 10 or not cik.isascii() or not cik.isdigit()
                or type(company_facts_occurrence_id) is not int or not 0 < company_facts_occurrence_id <= 9223372036854775807
                or type(security_identifier) is not str or len(security_identifier) > 4096
                or len(security_identifier.encode("utf-8")) > 4096 or not security_identifier.strip()):
            raise ValueError("native preparation requires exact scalar issuer/security/CF occurrence")
        if type(plan_sha256) is not str or len(plan_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in plan_sha256):
            raise ValueError("native preparation requires an exact plan digest")

    @staticmethod
    def _native_parent_context(records):
        for _, stored in records:
            parent = stored.item.parent_source_item_id
            metadata = stored.observation.canonical_metadata
            if parent is None or not any(name in metadata for name in
                    ("parent_source_item_id", "parent_observation_id", "parent_observation_sha256")):
                continue
            for name in ("parent_source_item_id", "parent_observation_id"):
                if name in metadata and (type(metadata[name]) is not int or not 0 < metadata[name] <= 9223372036854775807):
                    raise ValueError("native registered parent context requires exact signed64 IDs")
            if "parent_observation_sha256" in metadata:
                digest = metadata["parent_observation_sha256"]
                if type(digest) is not str or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                    raise ValueError("native registered parent context requires exact digest")
            if "parent_source_item_id" in metadata and metadata["parent_source_item_id"] != parent:
                raise ValueError("native registered parent item context differs")
            witnesses = [record for _, record in records if record.item.item_kind == "filing"
                         and record.item.source_item_id == parent]
            if not any(("parent_observation_id" not in metadata or metadata["parent_observation_id"] == record.observation.source_observation_id)
                       and ("parent_observation_sha256" not in metadata or metadata["parent_observation_sha256"] == record.observation.observation_sha256)
                       for record in witnesses):
                raise ValueError("native registered original parent revision context differs")

    def _admit_registered_native_graph(self, plan_sha256, *, cik, company_facts_occurrence_id,
                                       security_identifier, parameters):
        return _admit_registered_native_graph(self, plan_sha256, cik=cik,
            company_facts_occurrence_id=company_facts_occurrence_id,
            security_identifier=security_identifier, parameters=parameters)

    def _recheck_retained_graph(self, graph, parameters, *, connection=None):
        from . import sec_ingestion as sec
        from .source_validity import current_observation_id
        owner = _transport_resource_owner(self)
        current = sec._submission_part_execution(graph.budget)
        graph.budget.equality(len(current), len(graph.execution))
        if current != graph.execution:
            raise ValueError("retained stage executing source changed")
        for metadata in graph.producer_metadata:
            sec._recheck_submission_part_source_metadata(owner, metadata)
        for raw in parameters["retained_stage"]["acquisitions"]:
            sec._reserve_submission_part_receipt(graph.budget, phase="codec", raw_value=raw)
            from .source_observations import SourceAcquisitionReceipt
            receipt = SourceAcquisitionReceipt.from_outcome(raw)
            actual = sec._submission_part_actual_receipt(owner, receipt, graph.budget)
            expected = next((stored for _, stored in graph.records
                if stored.observation.observation_sha256 == receipt.observation_sha256), None)
            if expected is None:
                expected = next((stored for stored in graph.root_sources.values()
                    if stored.observation.observation_sha256 == receipt.observation_sha256), None)
            if expected is None and receipt.observation_sha256 == graph.parent.observation.observation_sha256:
                expected = graph.parent
            if expected is None:
                raise ValueError("retained recheck contains an unrelated acquisition")
            before = sec._submission_part_source_pin(expected, graph.budget)
            after = sec._submission_part_source_pin(actual, graph.budget)
            graph.budget.equality(len(before), len(after))
            if before != after:
                raise ValueError("retained source/full delivery changed")
        def recheck(active):
            if current_observation_id(active, graph.parent.item.source_item_id) != graph.parent.observation.source_observation_id:
                raise ValueError("retained stage current filing parent changed")
            self.recheck_registered_native_on_connection(active, plan=graph.plan, cik=graph.plan.cik,
                security_identifier=f"sec:{graph.plan.cik}:primary-listed-security",
                company_facts_source=graph.company_facts_source,
                _raw_work_budget=graph.budget, artifact_metadata=tuple(self._artifact_identity(value) for value in graph.artifacts.values()),
                graph_gaps=graph.graph_gaps)
        if connection is not None:
            recheck(connection)
        else:
            with self.observations.engine.connect() as active:
                recheck(active)
    def resolve_registered_native(self, plan_sha256, *, cik, company_facts_occurrence_id,
                                  security_identifier, retained_stage=None):
        if retained_stage is not None:
            graph = self._admit_registered_native_graph(plan_sha256, cik=cik,
                company_facts_occurrence_id=company_facts_occurrence_id,
                security_identifier=security_identifier, parameters=retained_stage)
            resolved = _resolve_registered_native_graph(graph)
            return RegisteredNativeInputs(graph.plan, resolved, graph.company_facts_source,
                tuple(self._artifact_identity(graph.artifacts[key]) for key in sorted(graph.artifacts)),
                graph.graph_gaps, graph.xml_work_bytes, graph)
        self.validate_native_request(plan_sha256, cik, company_facts_occurrence_id, security_identifier)
        limits = NATIVE_PREPARATION_LIMITS
        payloads = {}
        artifact, raw = read_registered_artifact(plan_sha256, self.objects, self.artifacts,
            maximum=MAXIMUM_BYTES, verified_payloads=payloads)
        plan = decode_native_preparation_plan(raw)
        if plan.cik != cik:
            raise ValueError("native preparation plan issuer differs")
        choices = [d.captured_source for d in plan.dependencies if d.captured_source is not None
                   and d.captured_source.role == "company_facts" and d.captured_source.occurrence_id == company_facts_occurrence_id]
        if len(choices) != 1:
            raise ValueError("native preparation requires one exact declared CF occurrence")
        company_facts_source = choices[0]
        records = []
        for dependency in plan.dependencies:
            if dependency.captured_source is None or dependency.delivery is None or dependency.observation_sha256 is None:
                continue
            stored = self.observations.by_sha256(dependency.observation_sha256)
            self._verify_dependency_metadata(dependency, stored, cik)
            delivery = dependency.delivery
            actual = self.observations.delivery(delivery.occurrence_id, source_item_id=delivery.source_item_id,
                                               source_observation_id=delivery.source_observation_id)
            self._verify_dependency_delivery(dependency, actual)
            records.append((dependency, stored))
        self._native_parent_context(records)
        authenticated_cf = {dependency.artifact_sha256 for dependency, _ in records
                            if dependency.captured_source.role == "company_facts"}
        if company_facts_source not in [dependency.captured_source for dependency, _ in records]:
            raise ValueError("native preparation CF occurrence lacks registered source admission")
        requests = [request for mapping in plan.declarations for request in (mapping.original, mapping.replacement) if request is not None]
        restricted = {request.source.artifact_sha256 for request in requests}
        restricted.update(document.reference.artifact_sha256 for mapping in plan.declarations for document in mapping.documents)
        restricted.update(dependency.artifact_sha256 for dependency, stored in records if stored.item.item_kind == "filing")
        maxima, sizes, metadata = {}, {}, {plan_sha256: self._artifact_identity(artifact)}
        for dependency in plan.dependencies:
            maximum = limits["company_facts_bytes"] if (dependency.captured_source is not None
                and dependency.captured_source.role == "company_facts"
                and dependency.delivery is not None and dependency.observation_sha256 is not None
                and dependency.artifact_sha256 in authenticated_cf
                and set(dependency.roles).issubset({"company_facts", "target"})
                and dependency.artifact_sha256 not in restricted) else limits["body_bytes"]
            digest = dependency.artifact_sha256
            maxima[digest] = min(maxima.get(digest, maximum), maximum)
            sizes[digest] = dependency.byte_size
        for digest, maximum in maxima.items():
            actual = self.artifacts.get(digest)
            if (actual.content_sha256 != digest or actual.object_key != f"raw/sha256/{digest}"
                    or type(actual.byte_size) is not int or actual.byte_size != sizes[digest]
                    or actual.byte_size > maximum):
                raise ValueError("native registered dependency size/address/ceiling differs")
            metadata[digest] = self._artifact_identity(actual)
        if sum(sizes.values()) > limits["aggregate_bytes"]:
            raise ValueError("native registered aggregate ceiling exceeded")
        work = sum(2 * sizes.get(request.source.artifact_sha256, request.source.byte_size) for request in requests)
        if work > limits["xml_work_bytes"]:
            raise ValueError("native registered two-pass work ceiling exceeded")
        for digest, maximum in maxima.items():
            read_registered_artifact(digest, self.objects, self.artifacts, maximum=maximum, verified_payloads=payloads)
        resolved = self.resolve(plan_sha256, cik=cik, company_facts_source=None,
            company_facts_payload=payloads[company_facts_source.artifact_sha256],
            company_facts_occurrence_id=company_facts_occurrence_id, security_identifier=security_identifier, payloads=payloads)
        gaps = tuple(self._missing_witness(mapping, records, company_facts_source) for mapping in plan.declarations)
        return RegisteredNativeInputs(plan, resolved, company_facts_source,
            tuple(metadata[key] for key in sorted(metadata)), gaps, work)

    @staticmethod
    def recheck_registered_native_on_connection(connection, *, plan, cik, security_identifier,
                                                company_facts_source, artifact_metadata, graph_gaps, _raw_work_budget=None):
        from .source_observations import SourceObservationRepository
        security = connection.execute(select(priced_security.c.security_identifier,
            issuer.c.source_system, issuer.c.source_identifier).join(issuer,
            issuer.c.issuer_id == priced_security.c.issuer_id).where(priced_security.c.security_id == plan.security_id)).mappings().one_or_none()
        if security is None or dict(security) != dict(security_identifier=security_identifier, source_system="SEC", source_identifier=cik):
            raise ValueError("native prepared security/issuer changed after source wait")
        records = []
        for dependency in plan.dependencies:
            if dependency.captured_source is None or dependency.delivery is None or dependency.observation_sha256 is None:
                continue
            stored = SourceObservationRepository.by_sha256_on_connection(connection, dependency.observation_sha256,
                **({} if _raw_work_budget is None else {"_raw_work_budget": _raw_work_budget}))
            PrimaryInputTransport._verify_dependency_metadata(dependency, stored, cik)
            delivery = dependency.delivery
            actual = load_delivery(connection, delivery.occurrence_id,
                source_item_id=delivery.source_item_id, source_observation_id=delivery.source_observation_id)
            PrimaryInputTransport._verify_dependency_delivery(dependency, actual)
            records.append((dependency, stored))
        PrimaryInputTransport._native_parent_context(records)
        current_gaps = tuple(PrimaryInputTransport._missing_witness(mapping, records, company_facts_source) for mapping in plan.declarations)
        if current_gaps != graph_gaps:
            raise ValueError("native prepared filing/wrapper associations changed")
        for expected in artifact_metadata:
            actual = connection.execute(select(evidence_artifact).where(evidence_artifact.c.content_sha256 == expected["content_sha256"])).mappings().one_or_none()
            if actual is None or any(actual[name] != value for name, value in expected.items()):
                raise ValueError("native registered artifact metadata changed")

    @staticmethod
    def _missing_witness(mapping, records, company_facts_source):
        if mapping.target_origin is not None and mapping.target_origin.source != company_facts_source:
            raise ValueError("primary target does not identify the pinned Company Facts root")
        by_source = {dependency.captured_source: stored for dependency, stored in records}
        for request in (mapping.original, mapping.replacement):
            if request is None:
                continue
            source = request.source
            stored = by_source.get(source.captured_source)
            if stored is None:
                return "NATIVE_REGISTERED_SOURCE_DELIVERY_UNAVAILABLE"
            metadata = stored.observation.canonical_metadata
            if metadata.get("accession") != source.accession:
                raise ValueError("native source accession differs from registered resource")
            for accession, form, filed in ((source.accession, source.form, source.filed),
                                           (source.annual_accession, source.annual_form, source.annual_filed)):
                witnesses = [record for _, record in records if record.item.item_kind == "filing"
                             and record.observation.canonical_metadata.get("accession") == accession]
                if not witnesses:
                    return "PINNED_REGISTERED_FILING_WITNESS_UNAVAILABLE"
                if not any(record.observation.canonical_metadata.get("form") == form
                           and record.observation.canonical_metadata.get("filing_date") == filed.isoformat()
                           and (accession != source.accession or stored.item.parent_source_item_id is None
                                or stored.item.parent_source_item_id == record.item.source_item_id)
                           for record in witnesses):
                    raise ValueError("native filing/wrapper metadata or parent differs")
        for document in mapping.documents:
            reference = document.reference
            witnesses = [stored for dependency, stored in records
                         if dependency.artifact_sha256 == reference.artifact_sha256
                         and stored.observation.source_url == reference.source_document]
            if not witnesses:
                return "DOCUMENT_REGISTERED_SOURCE_DELIVERY_UNAVAILABLE"
            if reference.source_system != "SEC" or reference.issuer_source_identifier != mapping.original.source.cik:
                raise ValueError("document issuer differs")
            if reference.source_published_at is not None and not any(
                    value.observation.source_published_at == reference.source_published_at for value in witnesses):
                raise ValueError("document source publication time differs")
            if reference.annual_wrapper is not None and not any(record.item.item_kind == "filing"
                    and record.observation.canonical_metadata.get("accession") == reference.annual_wrapper
                    for _, record in records):
                return "DOCUMENT_REGISTERED_ANNUAL_WRAPPER_UNAVAILABLE"
        return None


@dataclass(frozen=True)
class _RetainedLeafReference:
    document: str
    source_role: str
    projected_roles: tuple[str, ...]
    captured_source: CapturedSource
    observation_sha256: str
    delivery: object
    artifact: object
    origin_json: bytes | None


@dataclass(frozen=True)
class _RegisteredNativeGraph:
    plan_sha256: str
    plan: object
    company_facts_source: CapturedSource
    artifacts: object
    maxima: object
    bodies: dict
    budget: object
    records: tuple
    sources: tuple
    links: tuple
    dependency_manifest: tuple
    graph_gaps: tuple
    xml_work_bytes: int
    parent: object
    roots: object
    root_sources: object
    inventory: dict
    submissions: dict
    required: object
    leaves: tuple
    producer_metadata: tuple
    execution: bytes
    retained_manifest: dict


def _transport_resource_owner(transport):
    from .sec_ingestion import SecResourceFetchHandler
    return SecResourceFetchHandler(edgar=None, object_store=transport.objects,
        artifacts=transport.artifacts, observations=transport.observations,
        jobs=transport.observations._jobs, clock=transport.observations._clock)


def _graph_source(stored, role):
    delivery = stored.delivery
    if delivery is None:
        raise ValueError("retained source lacks its authenticated original delivery")
    return CapturedSource(role, stored.observation.artifact_sha256, stored.observation.source_url,
        stored.item.source_item_id, stored.observation.source_observation_id,
        delivery.occurrence_id, "EXACT_LEXICAL")


def _graph_dependency_manifest(dependency):
    captured, delivery = dependency.captured_source, dependency.delivery
    return dict(artifact_sha256=dependency.artifact_sha256, byte_size=dependency.byte_size,
        roles=list(dependency.roles), status="REGISTERED_SOURCE_DELIVERY" if captured is not None else "REGISTERED_ARTIFACT_ONLY",
        observation_sha256=dependency.observation_sha256,
        source_item_id=None if captured is None else captured.source_item_id,
        source_observation_id=None if captured is None else captured.source_observation_id,
        occurrence_id=None if delivery is None else delivery.occurrence_id,
        captured_source=None if captured is None else asdict(captured),
        delivery=None if delivery is None else {**asdict(delivery), "checked_at": delivery.checked_at.isoformat()})


def _admit_registered_native_graph(transport, plan_sha256, *, cik,
                                    company_facts_occurrence_id, security_identifier, parameters):
    from . import sec_ingestion as sec
    from .source_observations import SourceAcquisitionReceipt
    transport.validate_native_request(plan_sha256, cik, company_facts_occurrence_id, security_identifier)
    owner, budget = _transport_resource_owner(transport), sec._AdmissionBudget()
    copied, _ = sec._retained_schedule_codec(dict(parameters), budget)
    accession, supplied_cik, form, filename, observation_id, roots = sec._candidate_parameters(copied)
    if (supplied_cik != cik or copied.get("primary_input_plan_sha256") != plan_sha256
            or type(copied.get("capture_revision")) is not int or copied["capture_revision"] != 2):
        raise ValueError("retained stage parameters differ from its native request")
    retained = copied.get("retained_stage")
    if type(retained) is not dict or set(retained) != {"revision", "execution", "resource", "acquisitions"}:
        raise ValueError("exact retained stage envelope required")
    if type(retained["revision"]) is not int or retained["revision"] != 1:
        raise ValueError("retained stage revision differs")
    _, supplied_execution = sec._submission_part_codec(retained["execution"], budget)
    execution = sec._submission_part_execution(budget)
    budget.equality(len(execution), len(supplied_execution))
    if execution != supplied_execution:
        raise ValueError("retained stage executing build differs")

    plan_artifact = transport.artifacts.get(plan_sha256)
    if (plan_artifact.content_sha256 != plan_sha256 or plan_artifact.object_key != "raw/sha256/" + plan_sha256
            or type(plan_artifact.byte_size) is not int or not 0 <= plan_artifact.byte_size <= MAXIMUM_BYTES):
        raise ValueError("retained plan registered metadata differs")
    budget.artifact_read(plan_artifact.byte_size)
    actual_plan_artifact, raw = read_registered_artifact(plan_sha256, transport.objects, transport.artifacts,
                                                        maximum=MAXIMUM_BYTES)
    if actual_plan_artifact != plan_artifact:
        raise ValueError("retained plan changed during its verified read")
    budget.json_decode(len(raw))
    value = sec_json.decode_object(raw, "native plan metadata sizing")
    _reserve_native_plan_codec(value, budget, len(raw))
    plan = decode_native_preparation_plan(raw)
    if plan.cik != cik:
        raise ValueError("retained native plan issuer differs")
    with transport.observations.engine.connect() as connection:
        security = connection.execute(select(priced_security.c.security_identifier,
            issuer.c.source_system, issuer.c.source_identifier).join(issuer,
            issuer.c.issuer_id == priced_security.c.issuer_id).where(
            priced_security.c.security_id == plan.security_id)).mappings().one_or_none()
    if security is None or dict(security) != dict(security_identifier=security_identifier,
                                                source_system="SEC", source_identifier=cik):
        raise ValueError("retained plan does not bind the normalized issuer/security")

    acquisitions = retained["acquisitions"]
    if type(acquisitions) is not list or not 0 < len(acquisitions) <= 64:
        raise ValueError("retained acquisitions require a bounded exact union")
    acquired, receipt_values, order = {}, [], []
    for receipt_value in acquisitions:
        sec._reserve_submission_part_receipt(budget, phase="codec", raw_value=receipt_value)
        receipt = SourceAcquisitionReceipt.from_outcome(receipt_value)
        stored = sec._submission_part_actual_receipt(owner, receipt, budget)
        key = (stored.item.source_item_id, stored.observation.source_observation_id, stored.delivery.occurrence_id)
        if key in acquired:
            raise ValueError("retained acquisitions repeat a source occurrence")
        acquired[key] = stored
        receipt_values.append(receipt_value)
        order.append(key)
    if order != sorted(order):
        raise ValueError("retained acquisitions must be sorted by actual source occurrence")
    by_digest = {}
    for stored in acquired.values():
        digest = stored.observation.observation_sha256
        if digest in by_digest:
            raise ValueError("retained source observation has ambiguous delivery")
        by_digest[digest] = stored
    parent = owner._observations.latest_sec_filing(observation_id, _raw_work_budget=budget)
    sec._validate_parent_filing(parent, accession=accession, cik=cik, form=form,
                               filename=filename, observation_id=observation_id)
    parent_actual = by_digest.get(parent.observation.observation_sha256)
    if parent_actual is None or parent_actual.item != parent.item or parent_actual.observation != parent.observation:
        raise ValueError("retained stage omits its exact current parent receipt")

    records, links = [], [SnapshotArtifact(plan_sha256, PLAN_ROLE)]
    for dependency in plan.dependencies:
        captured = dependency.captured_source
        if captured is not None:
            if dependency.delivery is None or dependency.observation_sha256 is None:
                raise ValueError("retained dependency omits registered source/full delivery")
            stored = by_digest.get(dependency.observation_sha256)
            if stored is None:
                raise ValueError("retained dependency lacks its actual acquisition")
            transport._verify_dependency_metadata(dependency, stored, cik)
            transport._verify_dependency_delivery(dependency, stored.delivery)
            records.append((dependency, stored))
        elif dependency.delivery is not None or dependency.observation_sha256 is not None:
            raise ValueError("retained artifact-only dependency has partial source credentials")
        links.append(SnapshotArtifact(dependency.artifact_sha256, DEPENDENCY_ROLE))
    transport._native_parent_context(records)
    choices = [dependency.captured_source for dependency, _ in records
        if dependency.captured_source.role == "company_facts"
        and dependency.captured_source.occurrence_id == company_facts_occurrence_id]
    if len(choices) != 1:
        raise ValueError("retained native request lacks its exact CF occurrence")
    cf_source = choices[0]
    root_sources = {}
    for role, digest in roots.items():
        stored = by_digest.get(digest)
        if stored is None:
            raise ValueError("retained root lacks its actual acquisition")
        checked = sec._retained_root_source(owner._observations, digest, role=role,
                                           parent=parent, accession=accession, cik=cik, budget=budget)
        delivery = sec._retained_root_delivery(owner._observations, checked,
            copied["root_deliveries"][role], compare_instants=True, strict_revision=True)
        if checked.item != stored.item or checked.observation != stored.observation or delivery != stored.delivery:
            raise ValueError("retained root/source/full delivery differs")
        root_sources[role] = stored
        links.append(SnapshotArtifact(stored.observation.artifact_sha256, "sec_root_" + role))
    if _graph_source(root_sources["company_facts"], "company_facts") != cf_source:
        raise ValueError("retained stage CF root differs from its declared native source")

    producer_metadata = {}
    for _, stored in records:
        origin = stored.observation.canonical_metadata.get("derived_document")
        if origin is None:
            continue
        if type(origin) is not dict:
            raise ValueError("retained derived source has no exact origin record")
        association = origin.get("extraction_job")
        _, association_bytes = sec._submission_part_codec(association, budget)
        if association_bytes not in producer_metadata:
            producer_metadata[association_bytes] = sec._admit_submission_part_source_metadata(owner, association, budget)
    if not producer_metadata:
        raise ValueError("retained stage has no authenticated auxiliary producer")
    resource_fields = retained["resource"]
    if type(resource_fields) is not dict or set(resource_fields) != {
            "job_id", "occurrence_id", "schedule_key", "scheduled_for", "job_stamp_sha256"}:
        raise ValueError("retained original resource association differs")
    sec._capture_sha(resource_fields["job_stamp_sha256"])
    for metadata in producer_metadata.values():
        expected_association = sec._submission_part_association({key: resource_fields[key] for key in
            ("job_id", "occurrence_id", "schedule_key", "scheduled_for")})
        if (dict(metadata.resource_association) != expected_association or metadata.cik != cik
                or metadata.accession != accession or metadata.form != form or metadata.filename != filename
                or metadata.parent.item != parent.item or metadata.parent.observation != parent.observation):
            raise ValueError("retained auxiliary producer belongs to another original graph")
        budget.reserve(work=len(metadata.resource_stamp))
        if hashlib.sha256(metadata.resource_stamp).hexdigest() != resource_fields["job_stamp_sha256"]:
            raise ValueError("retained original resource stamp differs")
        for role, stored, _ in metadata.sources:
            expected = parent_actual if role == "filing" else root_sources[role]
            if stored.item != expected.item or stored.observation != expected.observation or stored.delivery != expected.delivery:
                raise ValueError("retained producer original source union differs")

    limits = NATIVE_PREPARATION_LIMITS
    requests = [request for mapping in plan.declarations for request in (mapping.original, mapping.replacement)
                if request is not None]
    restricted = {request.source.artifact_sha256 for request in requests}
    restricted.update(document.reference.artifact_sha256 for mapping in plan.declarations for document in mapping.documents)
    restricted.update(dependency.artifact_sha256 for dependency, stored in records if stored.item.item_kind == "filing")
    maxima, sizes, artifacts = {}, {}, {plan_sha256: plan_artifact}
    for dependency in plan.dependencies:
        maximum = limits["company_facts_bytes"] if (dependency.captured_source == cf_source
            and set(dependency.roles).issubset({"company_facts", "target"})
            and dependency.artifact_sha256 not in restricted) else limits["body_bytes"]
        digest = dependency.artifact_sha256
        maxima[digest] = min(maxima.get(digest, maximum), maximum)
        sizes[digest] = dependency.byte_size
    for role, stored in root_sources.items():
        digest = stored.observation.artifact_sha256
        maximum = limits["company_facts_bytes"] if role == "company_facts" and digest not in restricted else limits["body_bytes"]
        maxima[digest] = min(maxima.get(digest, maximum), maximum)
    for metadata in producer_metadata.values():
        for digest, artifact in metadata.artifacts.items():
            maxima[digest] = min(maxima.get(digest, limits["body_bytes"]), limits["body_bytes"])
    for digest, maximum in maxima.items():
        artifact = transport.artifacts.get(digest)
        if (artifact.content_sha256 != digest or artifact.object_key != "raw/sha256/" + digest
                or type(artifact.byte_size) is not int or not 0 <= artifact.byte_size <= maximum
                or digest in sizes and sizes[digest] != artifact.byte_size):
            raise ValueError("retained registered metadata/union ceiling differs")
        sizes[digest], artifacts[digest] = artifact.byte_size, artifact
    if len(sizes) > limits["artifacts"] or sum(sizes.values()) > limits["aggregate_bytes"]:
        raise ValueError("retained native unique union exceeds its existing frontier")
    native_work = sum(2 * sizes.get(request.source.artifact_sha256, request.source.byte_size) for request in requests)
    if native_work > limits["xml_work_bytes"]:
        raise ValueError("retained native two-pass frontier differs")
    for mapping in plan.declarations:
        for document in mapping.documents:
            span = document.reference.location
            size = sizes.get(document.reference.artifact_sha256)
            if size is None or not 0 <= span.start_byte < span.end_byte <= size:
                raise ValueError("retained documentary span exceeds its admitted body")

    bodies = {plan_sha256: raw}
    reader = SimpleNamespace(artifacts=artifacts, maxima=MappingProxyType(maxima), bodies=bodies, budget=budget)
    inventory_body = sec._read_retained_body(owner, reader, root_sources["accession_inventory"].observation.artifact_sha256)
    submissions_body = sec._read_retained_body(owner, reader, root_sources["submissions"].observation.artifact_sha256)
    budget.json_decode(len(inventory_body)); inventory = sec._strict_json_object(inventory_body, "retained accession inventory")
    budget.json_decode(len(submissions_body)); submissions = sec._strict_json_object(submissions_body, "retained submissions")
    required = sec._required_retained_documents(inventory, submissions, parent=parent,
                                               accession=accession, cik=cik, form=form)
    for metadata in producer_metadata.values():
        if metadata.auxiliaries != required.auxiliaries:
            raise ValueError("retained extraction names/roles differ from the verified owning graph")
    by_document, used = {}, {key for key, stored in acquired.items()
        if stored.observation.observation_sha256 in set(roots.values()) or stored == parent_actual}
    for dependency, stored in records:
        used.add((stored.item.source_item_id, stored.observation.source_observation_id, stored.delivery.occurrence_id))
    if used != set(acquired):
        raise ValueError("retained acquisitions are not the exact root/plan source union")
    projections = dict(required.projections)
    for dependency, stored in records:
        if stored.observation.canonical_metadata.get("derived_document") is None:
            continue
        document = stored.observation.canonical_metadata.get("document")
        if document in by_document or document not in projections:
            raise ValueError("retained auxiliary is ambiguous or outside the verified graph")
        origin = stored.observation.canonical_metadata["derived_document"]
        _, association_bytes = sec._submission_part_codec(origin["extraction_job"], budget)
        metadata = producer_metadata[association_bytes]
        role = dict(metadata.auxiliaries)[document]
        expected = metadata.receipts.get(document)
        if expected is None or expected.item != stored.item or expected.observation != stored.observation or expected.delivery != stored.delivery:
            raise ValueError("retained auxiliary differs from actual producing receipt")
        by_document[document] = (dependency, stored, metadata, role)
    if set(by_document) != set(projections):
        raise ValueError("retained required auxiliary source is missing")
    primary_choices = [(dependency, stored) for dependency, stored in records
        if dependency.captured_source.role == "primary_document"
        and stored.item.parent_source_item_id == parent.item.source_item_id
        and stored.observation.source_url == sec.sync._archive_base(cik, accession) + required.primary
        and stored.observation.canonical_metadata.get("derived_document") is None]
    if len(primary_choices) != 1:
        raise ValueError("retained graph lacks its unique genuine named primary")
    primary_dependency, primary_stored = primary_choices[0]
    leaves = [_RetainedLeafReference(required.primary, "primary_document", ("primary_document",),
        primary_dependency.captured_source, primary_dependency.observation_sha256,
        primary_stored.delivery, artifacts[primary_dependency.artifact_sha256], None)]
    containers = {}
    for document, (dependency, stored, metadata, role) in by_document.items():
        digest = metadata.container_artifact.content_sha256
        containers.setdefault(digest, []).append((document, dependency, stored, metadata, role))
    for digest, members in containers.items():
        container = sec._read_retained_body(owner, reader, digest)
        budget.parser(len(container), 6 * len(container))
        decoded = sec.sec_submission.decode_named_documents(container, tuple(sorted(member[0] for member in members)))
        if decoded.container_sha256 != digest or decoded.work_bytes > 6 * len(container):
            raise ValueError("retained container parser differs from registered origin")
        parts = {part.filename: part for part in decoded.documents}
        for document, dependency, stored, metadata, role in members:
            part = parts[document]
            sec._submission_part_verify_decoded_child(metadata, stored, part)
            child = sec._read_retained_body(owner, reader, dependency.artifact_sha256)
            budget.equality(len(child), len(part.payload))
            if child != part.payload:
                raise ValueError("retained auxiliary bytes differ from exact container part")
            _, origin = sec._submission_part_codec(stored.observation.canonical_metadata["derived_document"], budget)
            leaves.append(_RetainedLeafReference(document, role, projections[document], dependency.captured_source,
                dependency.observation_sha256, stored.delivery, artifacts[dependency.artifact_sha256], origin))
    for digest in maxima:
        sec._read_retained_body(owner, reader, digest)
    gaps = tuple(transport._missing_witness(mapping, records, cf_source) for mapping in plan.declarations)
    sources = tuple(dict.fromkeys([_graph_source(stored, role) for role, stored in root_sources.items()]
                                 + [dependency.captured_source for dependency, _ in records]))
    _, acquisition_bytes = sec._retained_schedule_codec(dict(acquisitions=receipt_values), budget)
    budget.reserve(work=len(acquisition_bytes))
    retained_manifest = dict(revision=1, execution=retained["execution"], resource=resource_fields,
        acquisition_receipts_sha256=hashlib.sha256(acquisition_bytes).hexdigest(), leaves=[dict(
            document=leaf.document, source_role=leaf.source_role, projected_roles=list(leaf.projected_roles),
            captured_source=asdict(leaf.captured_source), observation_sha256=leaf.observation_sha256,
            delivery={**asdict(leaf.delivery), "checked_at": leaf.delivery.checked_at.isoformat()},
            artifact_metadata=transport._artifact_identity(leaf.artifact),
            derived_document=None if leaf.origin_json is None else _retained_origin_value(leaf.origin_json, budget))
            for leaf in sorted(leaves, key=lambda leaf: leaf.document)])
    return _RegisteredNativeGraph(plan_sha256, plan, cf_source, MappingProxyType(artifacts),
        MappingProxyType(maxima), bodies, budget, tuple(records), sources, tuple(dict.fromkeys(links)),
        tuple(_graph_dependency_manifest(dependency) for dependency in plan.dependencies), gaps, native_work,
        parent_actual, MappingProxyType(roots), MappingProxyType(root_sources), inventory, submissions,
        required, tuple(sorted(leaves, key=lambda leaf: leaf.document)), tuple(producer_metadata.values()),
        execution, retained_manifest)


def _resolve_registered_native_graph(graph):
    from . import sec_ingestion as sec
    facts = graph.bodies[graph.company_facts_source.artifact_sha256]
    _reserve_company_facts_origins(graph.budget, facts)
    graph.budget.reserve(work=len(facts)); graph.budget.json_decode(len(facts))
    selected = tuple(mapping for mapping, gap in zip(graph.plan.declarations, graph.graph_gaps, strict=True) if gap is None)
    for mapping in selected:
        for document in mapping.documents:
            span = document.reference.location
            graph.budget.document(len(graph.bodies[document.reference.artifact_sha256]), span.start_byte, span.end_byte)
        for request in (mapping.original, mapping.replacement):
            if request is not None:
                graph.budget.native_reader(len(graph.bodies[request.source.artifact_sha256]))
    original_facts, admitted_outcomes = evidence.decode_and_admit_primary_inputs(graph.plan.cik, selected,
        graph.bodies, company_facts_source=graph.company_facts_source)
    admitted = iter(admitted_outcomes)
    outcomes = tuple(next(admitted) if gap is None else UnavailablePrimaryInput(mapping.key,
        mapping.canonical_namespace, mapping.canonical_tag, gap, copy.deepcopy(mapping.original),
        copy.deepcopy(mapping.target_origin)) for mapping, gap in zip(graph.plan.declarations, graph.graph_gaps, strict=True))
    admission = [dict(key=value.mapping.key if hasattr(value, "mapping") else value.key,
        status="ADMITTED" if hasattr(value, "mapping") else "UNAVAILABLE",
        reason=None if hasattr(value, "mapping") else value.reason) for value in outcomes]
    manifest = dict(primary_input_revision=1, primary_input_plan_sha256=graph.plan_sha256,
        dependencies=list(graph.dependency_manifest), admission=admission,
        financial_consumer="NOT_IMPLEMENTED", native_history="NOT_ASSESSED", retained_stage=graph.retained_manifest)
    return ResolvedPrimaryInputs(graph.plan_sha256, graph.plan.security_id, original_facts,
                                 outcomes, graph.sources, graph.links, manifest)


def _reserve_company_facts_origins(budget, raw):
    """Existing lexical JSON owner; never materialize numerical values in this preflight."""
    budget.json_decode(len(raw))
    tree = sec_json._decode(raw, "retained CF provenance sizing")
    facts = tree.get("facts", {})
    if type(facts) is not dict:
        return
    for namespace, taxonomy in facts.items():
        if type(taxonomy) is not dict:
            continue
        for concept, definition in taxonomy.items():
            units = definition.get("units") if type(definition) is dict else None
            if type(units) is not dict:
                continue
            for unit, entries in units.items():
                if type(entries) is not list:
                    continue
                for index, entry in enumerate(entries):
                    if type(entry) is not dict or not isinstance(entry.get("val"), sec_json._Number):
                        continue
                    # Existing _escape has two string replacement scans, then pointer joins.
                    escaped_upper = 0
                    for token in (namespace, concept, unit):
                        budget.reserve(work=12 * len(token))
                        escaped_upper += len(token) + token.count("~") + token.count("/")
                    budget.reserve(work=20)
                    pointer_upper = len("/facts///units///val") + 4 * escaped_upper + len(str(index))
                    budget.reserve(work=pointer_upper)


def _native_plan_json_size(value, budget, *, ensure_ascii=False, default_spaces=False):
    """Work sizing of an already decoded JSON tree; no native schema admission."""
    pending, encoded_size, utf8_size = [value], 0, 0
    while pending:
        item = pending.pop()
        budget.reserve(work=1)
        kind = type(item)
        if kind is dict:
            count = len(item)
            encoded_size += 2 + max(0, count - 1) + count
            if default_spaces:
                encoded_size += max(0, count - 1) + count
            for key, child in item.items():
                pending.extend((_json_key_work_text(key, budget), child))
        elif kind is list:
            encoded_size += 2 + max(0, len(item) - 1)
            if default_spaces:
                encoded_size += max(0, len(item) - 1)
            pending.extend(item)
        elif kind is str:
            budget.reserve(work=4 * len(item))
            encoded_size += 2
            for character in item:
                code = ord(character)
                raw = 1 if code < 128 else 2 if code < 2048 else 3 if code < 65536 else 4
                utf8_size += raw
                encoded_size += (2 if character in '"\\\b\f\n\r\t' else
                                 6 if code < 32 or ensure_ascii and 127 <= code <= 65535 else
                                 12 if ensure_ascii and code > 65535 else raw)
        elif kind is int:
            # Upper decimal width permits every integer accepted by the actual decoder.
            decimal_upper = item.bit_length() * 30103 // 100000 + 1 + (item < 0)
            budget.reserve(work=decimal_upper)
            encoded_size += len(str(item))
        elif kind is float:
            budget.reserve(work=32)
            encoded_size += len(repr(item))
        elif kind is bool or item is None:
            encoded_size += 4 if item is None or item is True else 5
        else:
            raise TypeError("work sizing requires the existing JSON decoder's plain tree")
    return encoded_size, utf8_size


def _reserve_native_plan_codec(value, budget, raw_bytes):
    """Reserve the unchanged typed decoder from actual disjoint subrecord groups."""
    from .sec_ingestion import _size, _AdmissionCapacity
    raw_bytes = _size(raw_bytes)
    if raw_bytes > MAXIMUM_BYTES:
        raise _AdmissionCapacity("existing native plan byte ceiling")
    # Caller has already charged its extra metadata-only decode before reaching here.
    _, whole_utf8 = _native_plan_json_size(value, budget)
    frontier, nested, mapping, captured, delivery = [], [], [], [], []
    declarations = value.get("declarations")
    declarations = declarations if type(declarations) is list else ()
    for declaration in declarations:
        if type(declaration) is not dict:
            continue
        frontier.append(declaration)
        mapping.append(declaration)
        documents = declaration.get("documents")
        if type(documents) is list:
            nested.extend(documents)
        target = declaration.get("target_origin")
        if target is not None:
            nested.append(target)
            if type(target) is dict and "source" in target:
                captured.append(target["source"])
        for name in ("original", "replacement"):
            request = declaration.get(name)
            if request is not None:
                nested.append(request)
                source = request.get("source") if type(request) is dict else None
                if type(source) is dict and source.get("captured_source") is not None:
                    captured.append(source["captured_source"])
    dependencies = value.get("dependencies")
    dependencies = dependencies if type(dependencies) is list else ()
    for dependency in dependencies:
        if type(dependency) is not dict:
            continue
        frontier.append(dependency)
        if dependency.get("captured_source") is not None:
            captured.append(dependency["captured_source"])
        if dependency.get("delivery") is not None:
            delivery.append(dependency["delivery"])
    sizes = {}
    for name, records in (("frontier", frontier), ("nested", nested),
                          ("mapping", mapping), ("captured", captured), ("delivery", delivery)):
        sizes[name] = sum(_native_plan_json_size(record, budget)[0] for record in records)
    canonical = sum(sizes.values())
    adapters = sizes["mapping"] + sizes["captured"] + sizes["delivery"]
    # Decoder JSON input passes; canonical emission/UTF-8; adapter input; text UTF-8 walk.
    budget.reserve(work=3 * raw_bytes + 2 * canonical + adapters + whole_utf8)
    return dict(groups=sizes, canonical_upper=canonical, adapter_upper=adapters,
                utf8_walk_upper=whole_utf8, decoder_input_bytes=raw_bytes)


def _retained_origin_value(raw, budget):
    budget.json_decode(len(raw))
    return sec_json.decode_object(raw, "retained origin manifest")

def _retained_model_work_sizes(value, budget, *, ensure_ascii=False, default_spaces=False):
    from datetime import date, datetime
    from pydantic import BaseModel
    def plain(item):
        budget.reserve(work=1)
        if isinstance(item, BaseModel):
            return {key: plain(item.__dict__[key]) for key in type(item).model_fields if key in item.__dict__}
        if isinstance(item, dict):
            return {key: plain(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [plain(child) for child in item]
        if isinstance(item, datetime):
            budget.reserve(work=42)
            return item.isoformat()
        if isinstance(item, date):
            budget.reserve(work=10)
            return item.isoformat()
        if type(item) in {str, int, float, bool} or item is None:
            return item
        raise TypeError("existing typed history JSON scalar required for work sizing")
    return _native_plan_json_size(plain(value), budget, ensure_ascii=ensure_ascii, default_spaces=default_spaces)


def _retained_model_dump_reserve(value, budget):
    size, utf8 = _retained_model_work_sizes(value, budget)
    budget.reserve(work=utf8)
    return size


def _retained_driver_reserve(value, budget, *, returned=False):
    size, _ = _retained_model_work_sizes(value, budget, ensure_ascii=True, default_spaces=True)
    # SQLAlchemy's unchanged JSONB encoder emits text; psycopg then encodes UTF-8.
    budget.reserve(work=2 * size)
    if returned:
        # The returned JSONB text is no larger than its escaped/default-space input.
        budget.reserve(reads=size, work=3 * size)
    return size


def _retained_json_dump_reserve(value, budget, *, ensure_ascii=False, default_spaces=False, sha=False):
    size, _ = _retained_model_work_sizes(value, budget, ensure_ascii=ensure_ascii, default_spaces=default_spaces)
    budget.reserve(work=2 * size + (size if sha else 0))
    return size


def _json_key_work_text(key, budget):
    """Existing JSON encoder key spelling, for size estimation only."""
    if isinstance(key, str):
        if type(key) is str:
            return key
        budget.reserve(work=4 * str.__len__(key))
        return str.__str__(key)
    if key is True:
        return "true"
    if key is False:
        return "false"
    if key is None:
        return "null"
    if isinstance(key, int):
        width = int.bit_length(key) * 30103 // 100000 + 1 + int.__lt__(key, 0)
        budget.reserve(work=width)
        return int.__repr__(key)
    if isinstance(key, float):
        budget.reserve(work=32)
        text = float.__repr__(key)
        return {"nan": "NaN", "inf": "Infinity", "-inf": "-Infinity"}.get(text, text)
    raise TypeError("existing JSON encoder key type required for work sizing")