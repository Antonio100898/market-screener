"""Bounded private declarations; source resolution and financial use are separate."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import date, datetime
import re
from types import MappingProxyType

from pydantic import TypeAdapter

from .company_import import _canonical_json
from .figure_history import ActionEvidence, DateWitness, DocumentEvidence, DocumentSpan, ShareBasis, SourceOccurrenceRef, decimal_text
from .models import (CapturedSource, NativeContext, NativeDimension, NativeInlineOrdinalLocation,
                     NativeNumericRequest, NativeSource, NativeXmlOrdinalLocation, PrimaryInputMapping,
                     RawFactOrigin)
from .source_validity import SourceDeliveryRecord
from .sources.sec_json import decode_object

MAXIMUM_BYTES = 1024 * 1024
NATIVE_PREPARATION_LIMITS = MappingProxyType(dict(declarations=32, requests=64, dependencies=64,
    artifacts=32, documents=64, dimensions=32, body_bytes=8 * 1024 * 1024,
    company_facts_bytes=32 * 1024 * 1024, aggregate_bytes=64 * 1024 * 1024,
    xml_work_bytes=128 * 1024 * 1024, name_bytes=4096, numeric_text_bytes=16384,
    record_bytes=65536, envelope_bytes=8 * 1024 * 1024))
_KINDS = {"NATIVE_INLINE_ORDINAL": NativeInlineOrdinalLocation,
          "NATIVE_XML_CHILD_ORDINAL": NativeXmlOrdinalLocation}
_MAPPING = TypeAdapter(PrimaryInputMapping)
_MAPPING.rebuild(_types_namespace=globals())
_CAPTURED = TypeAdapter(CapturedSource)
_DELIVERY = TypeAdapter(SourceDeliveryRecord)


class PlanCodecError(ValueError):
    pass


@dataclass(frozen=True)
class PlanDependency:
    artifact_sha256: str
    byte_size: int
    roles: tuple[str, ...]
    captured_source: CapturedSource | None = None
    observation_sha256: str | None = None
    delivery: SourceDeliveryRecord | None = None

    def __post_init__(self):
        object.__setattr__(self, "roles", tuple(self.roles))


@dataclass(frozen=True)
class PrimaryInputPlan:
    cik: str
    security_id: int
    declarations: tuple[PrimaryInputMapping, ...]
    dependencies: tuple[PlanDependency, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "declarations", tuple(self.declarations))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))


def _keys(value, record, *, exclude=()):
    expected = set(record.model_fields) if hasattr(record, "model_fields") else {field.name for field in fields(record)}
    expected.difference_update(exclude)
    if type(value) is not dict or set(value) != expected:
        raise PlanCodecError(f"exact {record.__name__} fields required")


def _text(value):
    if type(value) is not str or not value.strip():
        raise PlanCodecError("immutable nonempty text required")


def _integer(value, minimum=1):
    if type(value) is not int or value < minimum:
        raise PlanCodecError("exact integer required")


def _digest(value):
    if type(value) is not str or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise PlanCodecError("exact artifact/observation digest required")


def _calendar(value):
    if value is not None and (type(value) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)):
        raise PlanCodecError("ISO calendar date required")
    if value is not None:
        date.fromisoformat(value)


def _timestamp(value):
    if (type(value) is not str or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})", value)):
        raise PlanCodecError("exact timezone-aware delivery/publication timestamp required")
    if datetime.fromisoformat(value).utcoffset() is None:
        raise PlanCodecError("timestamp requires timezone")


def _array(value):
    if type(value) is not list:
        raise PlanCodecError("ordered JSON array required")


def _captured(value):
    _keys(value, CapturedSource)
    for field in ("role", "source_document"):
        _text(value[field])
    _digest(value["artifact_sha256"])
    for field in ("source_item_id", "source_observation_id"):
        _integer(value[field])
    if value["occurrence_id"] is not None:
        _integer(value["occurrence_id"])
    if value["fidelity"] not in {"EXACT_LEXICAL", "DECODED_DECIMAL", "UNKNOWN"}:
        raise PlanCodecError("unknown source fidelity")
    return _CAPTURED.validate_json(_canonical_json(value), strict=True)


def _request(value, cik):
    _keys(value, NativeNumericRequest)
    source, context, location = value["source"], value["context"], value["location"]
    _keys(source, NativeSource)
    _digest(source["artifact_sha256"])
    _integer(source["byte_size"], 0)
    for field in ("cik", "accession", "form", "source_document", "annual_accession", "annual_form"):
        _text(source[field])
    if source["cik"] != cik:
        raise PlanCodecError("native declaration identifies another plan issuer")
    for field in ("filed", "annual_filed"):
        _calendar(source[field])
        if source[field] is None:
            raise PlanCodecError("source filed calendar date required")
    if source["captured_source"] is not None:
        captured = _captured(source["captured_source"])
        if captured.artifact_sha256 != source["artifact_sha256"] or captured.source_document != source["source_document"]:
            raise PlanCodecError("native captured source differs from declared bytes/document")
    _keys(context, NativeContext)
    _text(context["entity_identifier"])
    _text(context["entity_scheme"])
    for field in ("instant", "start", "end"):
        _calendar(context[field])
    _array(context["dimensions"])
    for dimension in context["dimensions"]:
        _keys(dimension, NativeDimension)
        _text(dimension["axis"])
        _text(dimension["member"])
        if dimension["kind"] not in {"explicit", "typed"}:
            raise PlanCodecError("unknown native dimension kind")
    for field in ("unit_numerator", "unit_denominator"):
        _array(value[field])
        for measure in value[field]:
            _text(measure)
    if type(location) is not dict or type(location.get("kind")) is not str or location["kind"] not in _KINDS:
        raise PlanCodecError("distinct private native locator kind required")
    kind = _KINDS[location["kind"]]
    plain = {key: item for key, item in location.items() if key != "kind"}
    _keys(plain, kind)
    _integer(plain["element_ordinal"], 0)
    for field in ("fact_qname", "context_ref", "unit_ref"):
        _text(plain[field])
    value = {**value, "location": plain}
    return value, kind


def _mapping(value, cik, revision):
    _keys(value, PrimaryInputMapping)
    original, original_kind = _request(value["original"], cik)
    replacement, replacement_kind = None, None
    if value["replacement"] is not None:
        replacement, replacement_kind = _request(value["replacement"], cik)
    if value["replacement"] is not None and value["unary_factor_text"] is not None:
        raise PlanCodecError("replacement and unary declarations are distinct operations")
    if value["unary_factor_text"] is not None:
        decimal_text(value["unary_factor_text"])
    if value["target_origin"] is not None:
        target = value["target_origin"]
        _keys(target, RawFactOrigin)
        _captured(target["source"])
        for field in ("pointer", "lexical_text", "namespace", "concept", "unit"):
            _text(target[field])
        decimal_text(target["lexical_text"])
        for field in ("start", "end", "filed"):
            if target[field] is not None and type(target[field]) is not str:
                raise PlanCodecError("raw optional origin text required")
    for collection in ("documents", "actions"):
        _array(value[collection])
    for document in value["documents"]:
        _keys(document, DocumentEvidence)
        _keys(document["reference"], SourceOccurrenceRef)
        _keys(document["reference"]["location"], DocumentSpan)
        published = document["reference"]["source_published_at"]
        if published is not None:
            _timestamp(published)
    for action in value["actions"]:
        _keys(action, ActionEvidence)
        _array(action["date_witnesses"])
        for witness in action["date_witnesses"]:
            _keys(witness, DateWitness)
            _calendar(witness["date"])
    if value["share_basis"] is not None:
        basis = value["share_basis"]
        anchors = {"anchor_evidence_key", "anchor_as_of"}
        relative = revision == 2 and type(basis) is dict and bool(anchors.intersection(basis))
        _keys(basis, ShareBasis, exclude=() if relative else anchors)
        if relative:
            _text(basis["anchor_evidence_key"])
            _calendar(basis["anchor_as_of"])
            if basis["anchor_as_of"] is None:
                raise PlanCodecError("relative plan requires an actual anchor as-of date")
    result = _MAPPING.validate_json(_canonical_json({**value, "original": original, "replacement": replacement}), strict=True)
    result = replace(result, original=replace(result.original, location=original_kind(**original["location"])),
                     replacement=None if result.replacement is None else replace(result.replacement, location=replacement_kind(**replacement["location"])))
    _text(result.key)
    _text(result.canonical_tag)
    if result.canonical_namespace not in {"us-gaap", "ifrs-full"}:
        raise PlanCodecError("unknown declared canonical namespace")
    return result


def _dependency(value):
    _keys(value, PlanDependency)
    _digest(value["artifact_sha256"])
    _integer(value["byte_size"], 0)
    _array(value["roles"])
    for role in value["roles"]:
        _text(role)
    if not value["roles"] or len(set(value["roles"])) != len(value["roles"]):
        raise PlanCodecError("distinct ordered dependency roles required")
    captured = None if value["captured_source"] is None else _captured(value["captured_source"])
    if captured is not None and captured.artifact_sha256 != value["artifact_sha256"]:
        raise PlanCodecError("dependency captured artifact differs")
    if value["observation_sha256"] is not None:
        _digest(value["observation_sha256"])
    delivery = None
    if value["delivery"] is not None:
        supplied = value["delivery"]
        _keys(supplied, SourceDeliveryRecord)
        for field in ("occurrence_id", "source_item_id", "source_observation_id", "job_id", "ownership_generation"):
            _integer(supplied[field])
        _text(supplied["item_key"])
        _timestamp(supplied["checked_at"])
        delivery = _DELIVERY.validate_json(_canonical_json(supplied), strict=True)
        if captured is not None and (captured.source_item_id, captured.source_observation_id, captured.occurrence_id) != (
                delivery.source_item_id, delivery.source_observation_id, delivery.occurrence_id):
            raise PlanCodecError("dependency source/delivery tuple differs")
    return PlanDependency(value["artifact_sha256"], value["byte_size"], tuple(value["roles"]), captured, value["observation_sha256"], delivery)


def _native_preparation_bounds(value):
    limits = NATIVE_PREPARATION_LIMITS
    pending = [(None, value)]
    while pending:
        name, item = pending.pop()
        if name in {"security_id", "source_item_id", "source_observation_id", "occurrence_id", "job_id", "ownership_generation"}:
            if not (name == "occurrence_id" and item is None) and (type(item) is not int or not 0 < item < 2**63):
                raise PlanCodecError("native SQL identifier requires an exact positive signed64 integer")
        if type(item) is dict:
            if name == "delivery" and type(item.get("occurrence_id")) is not int:
                raise PlanCodecError("native delivery occurrence requires an exact SQL identifier")
            pending.extend(item.items())
            pending.extend((None, key) for key in item)
        elif type(item) is list:
            pending.extend((name, child) for child in item)
        elif type(item) is str:
            maximum = limits["numeric_text_bytes"] if name in {"lexical_text", "value_decimal_text", "unary_factor_text"} else limits["name_bytes"]
            if len(item) > maximum or len(item.encode("utf-8")) > maximum:
                raise PlanCodecError("native preparation text ceiling exceeded")
    if len(value["declarations"]) > limits["declarations"] or len(value["dependencies"]) > limits["dependencies"]:
        raise PlanCodecError("native preparation declaration/dependency ceiling exceeded")
    requests, documents, work = 0, 0, 0
    for declaration in value["declarations"]:
        if type(declaration) is not dict:
            raise PlanCodecError("native declaration requires a record")
        if len(_canonical_json(declaration)) > limits["record_bytes"]:
            raise PlanCodecError("native declaration record ceiling exceeded")
        entries = declaration.get("documents")
        _array(entries)
        documents += len(entries)
        for document in entries:
            if type(document) is not dict or len(_canonical_json(document)) > limits["record_bytes"]:
                raise PlanCodecError("native document record ceiling exceeded")
        target = declaration.get("target_origin")
        if target is not None and (type(target) is not dict or len(_canonical_json(target)) > limits["record_bytes"]):
            raise PlanCodecError("native target origin record ceiling exceeded")
        for name in ("original", "replacement"):
            request = declaration.get(name)
            if name == "replacement" and request is None:
                continue
            if type(request) is not dict or type(request.get("source")) is not dict or type(request.get("context")) is not dict:
                raise PlanCodecError("native request requires source/context records")
            size = request["source"].get("byte_size")
            _integer(size)
            if size > limits["body_bytes"]:
                raise PlanCodecError("native body ceiling exceeded")
            dimensions = request["context"].get("dimensions")
            _array(dimensions)
            if len(dimensions) > limits["dimensions"]:
                raise PlanCodecError("native dimension ceiling exceeded")
            requests += 1
            work += 2 * size
            if len(_canonical_json(request)) > limits["record_bytes"]:
                raise PlanCodecError("native request record ceiling exceeded")
    if requests > limits["requests"] or documents > limits["documents"] or work > limits["xml_work_bytes"]:
        raise PlanCodecError("native request/document/two-pass work ceiling exceeded")
    hashes, total = {}, 0
    for dependency in value["dependencies"]:
        if type(dependency) is not dict:
            raise PlanCodecError("native dependency requires a record")
        if len(_canonical_json(dependency)) > limits["record_bytes"]:
            raise PlanCodecError("native dependency record ceiling exceeded")
        digest, size = dependency.get("artifact_sha256"), dependency.get("byte_size")
        _digest(digest); _integer(size, 0)
        if digest in hashes and hashes[digest] != size:
            raise PlanCodecError("native dependency size conflicts")
        if digest not in hashes:
            total += size; hashes[digest] = size
    if len(hashes) > limits["artifacts"] or total > limits["aggregate_bytes"]:
        raise PlanCodecError("native unique artifact/aggregate ceiling exceeded")


def decode_primary_plan(raw: bytes) -> PrimaryInputPlan:
    return _decode_primary_plan(raw)


def decode_native_preparation_plan(raw: bytes) -> PrimaryInputPlan:
    return _decode_primary_plan(raw, native_preparation=True)


def _decode_primary_plan(raw: bytes, *, native_preparation=False) -> PrimaryInputPlan:
    try:
        if type(raw) is not bytes or len(raw) > MAXIMUM_BYTES:
            raise PlanCodecError("plan requires immutable bytes no larger than 1MiB")
        value = decode_object(raw, "primary input plan")
        if set(value) != {"kind", "revision", "cik", "security_id", "declarations", "dependencies"}:
            raise PlanCodecError("exact plan header and fields required")
        if value["kind"] != "PRIMARY_INPUT_PLAN" or type(value["revision"]) is not int or value["revision"] not in {1, 2}:
            raise PlanCodecError("unknown private plan contract")
        if type(value["cik"]) is not str or not re.fullmatch(r"[0-9]{10}", value["cik"]):
            raise PlanCodecError("plan CIK requires ten ASCII digits")
        _integer(value["security_id"])
        _array(value["declarations"])
        _array(value["dependencies"])
        if native_preparation:
            _native_preparation_bounds(value)
        declarations = tuple(_mapping(item, value["cik"], value["revision"]) for item in value["declarations"])
        if value["revision"] == 2 and not any(
                d.share_basis is not None and d.share_basis.anchor_evidence_key is not None for d in declarations):
            raise PlanCodecError("plan revision2 requires an explicit relative anchor")
        dependencies = tuple(_dependency(item) for item in value["dependencies"])
        keys, originals, targets = set(), set(), set()
        for declaration in declarations:
            original = (declaration.original.source.artifact_sha256, declaration.original.location)
            target = None if declaration.target_origin is None else (declaration.target_origin.source.artifact_sha256, declaration.target_origin.pointer)
            if declaration.key in keys or original in originals or target is not None and target in targets:
                raise PlanCodecError("duplicate declaration key/original/target")
            keys.add(declaration.key); originals.add(original)
            if target is not None:
                targets.add(target)
        identities, sizes = set(), {}
        for dependency in dependencies:
            if dependency.artifact_sha256 in sizes and sizes[dependency.artifact_sha256] != dependency.byte_size:
                raise PlanCodecError("same dependency artifact has conflicting sizes")
            sizes[dependency.artifact_sha256] = dependency.byte_size
            source = dependency.captured_source
            delivery = dependency.delivery
            identity = (dependency.artifact_sha256,
                        None if source is None else (source.source_item_id, source.source_observation_id, source.occurrence_id),
                        dependency.observation_sha256,
                        None if delivery is None else (delivery.source_item_id, delivery.source_observation_id, delivery.occurrence_id))
            if identity in identities:
                raise PlanCodecError("duplicate dependency binding")
            identities.add(identity)
        for declaration in declarations:
            for request in (declaration.original, declaration.replacement):
                if request is not None and request.source.artifact_sha256 in sizes and sizes[request.source.artifact_sha256] != request.source.byte_size:
                    raise PlanCodecError("native declaration/dependency byte size differs")
        return PrimaryInputPlan(value["cik"], value["security_id"], declarations, dependencies)
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise PlanCodecError(str(exc)) from exc


def encode_primary_plan(plan: PrimaryInputPlan) -> bytes:
    try:
        if type(plan) is not PrimaryInputPlan:
            raise PlanCodecError("typed private plan required")
        header = dict(kind="PRIMARY_INPUT_PLAN", revision=1, cik=plan.cik,
                      security_id=plan.security_id, declarations=[], dependencies=[])
        encoded_size = len(_canonical_json(header))
        if encoded_size > MAXIMUM_BYTES:
            raise PlanCodecError("plan requires immutable bytes no larger than 1MiB")
        declarations = []
        for declaration in plan.declarations:
            if type(declaration) is not PrimaryInputMapping:
                raise PlanCodecError("full original declaration required, not an admission outcome")
            if declaration.share_basis is not None and declaration.share_basis.anchor_evidence_key is not None:
                header["revision"] = 2
            value = _MAPPING.dump_python(declaration, mode="json", warnings="error")
            for name in ("original", "replacement"):
                request = getattr(declaration, name)
                if request is not None:
                    kinds = {kind: name for name, kind in _KINDS.items()}
                    if type(request.location) not in kinds:
                        raise PlanCodecError("private original locator required")
                    value[name]["location"]["kind"] = kinds[type(request.location)]
            encoded_size += len(_canonical_json(value)) + bool(declarations)
            if encoded_size > MAXIMUM_BYTES:
                raise PlanCodecError("plan requires immutable bytes no larger than 1MiB")
            declarations.append(value)
        dependencies = []
        for dependency in plan.dependencies:
            if type(dependency) is not PlanDependency:
                raise PlanCodecError("typed dependency required")
            value = dict(artifact_sha256=dependency.artifact_sha256, byte_size=dependency.byte_size,
                         roles=list(dependency.roles), observation_sha256=dependency.observation_sha256,
                         captured_source=None if dependency.captured_source is None else _CAPTURED.dump_python(dependency.captured_source, mode="json", warnings="error"),
                         delivery=None if dependency.delivery is None else _DELIVERY.dump_python(dependency.delivery, mode="json", warnings="error"))
            encoded_size += len(_canonical_json(value)) + bool(dependencies)
            if encoded_size > MAXIMUM_BYTES:
                raise PlanCodecError("plan requires immutable bytes no larger than 1MiB")
            dependencies.append(value)
        raw = _canonical_json({**header, "declarations": declarations, "dependencies": dependencies})
        decode_primary_plan(raw)
        return raw
    except (ValueError, TypeError, RecursionError, OverflowError, AttributeError) as exc:
        raise PlanCodecError(str(exc)) from exc
