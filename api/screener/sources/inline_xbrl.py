"""Parse retained SEC Inline-XBRL into the existing Company Facts shape."""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Mapping


XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
XSI = "http://www.w3.org/2001/XMLSchema-instance"

_FILE_FIELDS = (
    "instance", "filing_summary", "presentation", "schema", "primary_document",
)


@dataclass(frozen=True)
class InlineXbrlMetadata:
    cik: str
    entity_name: str
    source_accession: str
    source_form: str
    source_filed: str
    source_document: str
    report_date: str
    annual_accession: str
    annual_form: str
    annual_filed: str


@dataclass(frozen=True)
class InlineXbrlPaths:
    instance: Path
    filing_summary: Path
    presentation: Path
    schema: Path
    primary_document: Path


@dataclass(frozen=True)
class _Dimension:
    axis: str
    kind: str
    member: str


@dataclass(frozen=True)
class _Context:
    identifier: str
    instant: str | None
    start: str | None
    end: str | None
    dimensions: tuple[_Dimension, ...]


class _InlineAnchors(HTMLParser):
    def __init__(self, wanted: set[str]):
        super().__init__(convert_charrefs=False)
        self.wanted = wanted
        self.facts: dict[str, dict[str, str]] = {}
        self.duplicates: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "ix:nonfraction":
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        fact_id = values.get("id")
        if fact_id not in self.wanted:
            return
        if fact_id in self.facts:
            self.duplicates.add(fact_id)
        self.facts[fact_id] = values


def parse_inline_xbrl(
    metadata: InlineXbrlMetadata,
    paths: InlineXbrlPaths,
    expected_sha256: Mapping[str, str],
) -> dict:
    """Return standard numeric facts without selecting or normalizing them."""
    cik = _validate_metadata(metadata, paths)
    source_bytes, hashes = _read_verified(paths, expected_sha256)

    instance = _xml(source_bytes["instance"], "instance")
    _xml(source_bytes["schema"], "schema")
    summary = _xml(source_bytes["filing_summary"], "filing summary")
    presentation = _xml(source_bytes["presentation"], "presentation linkbase")

    contexts = _contexts(instance)
    units = _units(instance)
    roles = _presentation_roles(summary, presentation)
    raw_facts = _numeric_facts(instance, contexts, units, cik)

    wanted_ids = {fact["_source_fact_id"] for fact in raw_facts}
    anchors = _anchors(source_bytes["primary_document"], wanted_ids)
    facts = _attach_anchors(raw_facts, anchors, roles, metadata)
    facts = _deduplicate(facts)

    grouped: dict[str, dict] = {}
    for fact in sorted(facts, key=_fact_order):
        namespace = fact.pop("_output_namespace")
        concept = fact.pop("_output_concept")
        unit = fact.pop("_output_unit")
        (grouped.setdefault(namespace, {})
         .setdefault(concept, {"units": {}})
         .setdefault("units", {})
         .setdefault(unit, [])
         .append(fact))

    return {
        "cik": cik,
        "entityName": metadata.entity_name,
        "facts": grouped,
        "_inline_xbrl": {
            "kind": "sec_inline_xbrl",
            "source_accession": metadata.source_accession,
            "source_form": metadata.source_form,
            "source_filed": metadata.source_filed,
            "source_document": metadata.source_document,
            "annual_accession": metadata.annual_accession,
            "annual_form": metadata.annual_form,
            "annual_filed": metadata.annual_filed,
            "report_date": metadata.report_date,
            "files": {
                field: {
                    "document": getattr(paths, field).name,
                    "sha256": hashes[field],
                }
                for field in _FILE_FIELDS
            },
            "standard_fact_count": len(facts),
        },
    }


def _validate_metadata(metadata: InlineXbrlMetadata, paths: InlineXbrlPaths) -> str:
    cik = _cik(metadata.cik)
    for field in (
        "entity_name", "source_accession", "source_form", "source_document",
        "annual_accession", "annual_form",
    ):
        if not getattr(metadata, field).strip():
            raise ValueError(f"missing Inline-XBRL metadata: {field}")
    for field in ("source_filed", "report_date", "annual_filed"):
        try:
            date.fromisoformat(getattr(metadata, field))
        except ValueError as exc:
            raise ValueError(f"invalid Inline-XBRL date: {field}") from exc
    if paths.primary_document.name != metadata.source_document:
        raise ValueError("primary document path does not match source document")
    return cik


def _read_verified(
    paths: InlineXbrlPaths, expected_sha256: Mapping[str, str],
) -> tuple[dict[str, bytes], dict[str, str]]:
    if set(expected_sha256) != set(_FILE_FIELDS):
        raise ValueError("expected SHA-256 map must name every retained Inline-XBRL file")
    content: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    for field in _FILE_FIELDS:
        path = getattr(paths, field)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"missing retained Inline-XBRL file: {field}") from exc
        expected = expected_sha256[field].casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(f"invalid expected SHA-256: {field}")
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise ValueError(f"retained Inline-XBRL hash mismatch: {field}")
        content[field] = payload
        hashes[field] = actual
    return content, hashes


def _xml(payload: bytes, label: str) -> ET.Element:
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"invalid Inline-XBRL {label}") from exc


def _cik(value: str) -> str:
    if not value.isdigit():
        raise ValueError("SEC issuer identifier must be a numeric CIK")
    return str(int(value)).zfill(10)


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _namespace(value: str) -> str:
    return value[1:].split("}", 1)[0] if value.startswith("{") else ""


def _taxonomy(uri: str) -> str | None:
    if "xbrl.ifrs.org/" in uri and uri.rstrip("/").endswith("/ifrs-full"):
        return "ifrs-full"
    if "fasb.org/us-gaap/" in uri:
        return "us-gaap"
    return None


def _contexts(root: ET.Element) -> dict[str, _Context]:
    result: dict[str, _Context] = {}
    for item in root.findall(f"{{{XBRLI}}}context"):
        context_id = item.get("id") or ""
        identifier = item.findtext(f".//{{{XBRLI}}}identifier") or ""
        dimensions: list[_Dimension] = []
        for member in item.findall(f".//{{{XBRLDI}}}explicitMember"):
            dimensions.append(_Dimension(
                axis=member.get("dimension") or "",
                kind="explicit",
                member=(member.text or "").strip(),
            ))
        for member in item.findall(f".//{{{XBRLDI}}}typedMember"):
            children = list(member)
            value = "".join(ET.tostring(child, encoding="unicode") for child in children)
            dimensions.append(_Dimension(
                axis=member.get("dimension") or "",
                kind="typed",
                member=value.strip() or (member.text or "").strip(),
            ))
        if not context_id or not identifier:
            raise ValueError("Inline-XBRL context lacks identity")
        context = _Context(
            identifier=identifier,
            instant=item.findtext(f".//{{{XBRLI}}}instant"),
            start=item.findtext(f".//{{{XBRLI}}}startDate"),
            end=item.findtext(f".//{{{XBRLI}}}endDate"),
            dimensions=tuple(sorted(dimensions, key=lambda value: (
                value.axis, value.kind, value.member,
            ))),
        )
        if context_id in result and result[context_id] != context:
            raise ValueError(f"conflicting Inline-XBRL context: {context_id}")
        result[context_id] = context
    return result


def _units(root: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in root.findall(f"{{{XBRLI}}}unit"):
        unit_id = item.get("id") or ""
        direct = item.findall(f"{{{XBRLI}}}measure")
        if direct:
            unit = "*".join(_measure(value.text or "") for value in direct)
        else:
            numerator = item.findall(
                f"{{{XBRLI}}}divide/{{{XBRLI}}}unitNumerator/{{{XBRLI}}}measure"
            )
            denominator = item.findall(
                f"{{{XBRLI}}}divide/{{{XBRLI}}}unitDenominator/{{{XBRLI}}}measure"
            )
            if not numerator or not denominator:
                raise ValueError(f"invalid Inline-XBRL unit: {unit_id}")
            unit = ("*".join(_measure(value.text or "") for value in numerator)
                    + "/"
                    + "*".join(_measure(value.text or "") for value in denominator))
        if not unit_id or not unit:
            raise ValueError("Inline-XBRL unit lacks identity")
        if unit_id in result and result[unit_id] != unit:
            raise ValueError(f"conflicting Inline-XBRL unit: {unit_id}")
        result[unit_id] = unit
    return result


def _measure(value: str) -> str:
    value = value.strip()
    prefix, separator, local = value.partition(":")
    if separator and prefix.casefold() == "iso4217":
        return local
    if (local if separator else prefix).casefold() in {"pure", "shares"}:
        return (local if separator else prefix).casefold()
    return value


def _presentation_roles(summary: ET.Element, presentation: ET.Element) -> dict[str, list[str]]:
    reports: dict[str, str] = {}
    for report in summary.iter():
        if _local_name(report.tag) != "Report":
            continue
        values = {_local_name(child.tag): (child.text or "").strip() for child in report}
        if values.get("MenuCategory") != "Statements" or not values.get("Role"):
            continue
        name = values.get("ShortName") or values.get("LongName")
        if name:
            reports[values["Role"]] = name

    roles: dict[str, set[str]] = {}
    for link in presentation.iter(f"{{{LINK}}}presentationLink"):
        role = link.get(f"{{{XLINK}}}role") or ""
        report = reports.get(role)
        if not report:
            continue
        for locator in link.findall(f"{{{LINK}}}loc"):
            fragment = (locator.get(f"{{{XLINK}}}href") or "").partition("#")[2]
            concept = fragment.rsplit("_", 1)[-1]
            if concept:
                roles.setdefault(concept, set()).add(report)
    return {concept: sorted(names) for concept, names in sorted(roles.items())}


def _dimension_payload(dimensions: tuple[_Dimension, ...]) -> tuple[str, list[dict]]:
    source = [
        {"axis": value.axis, "kind": value.kind, "member": value.member}
        for value in dimensions
    ]
    segments = ";".join(
        f"{value.axis}={value.member}" if value.kind == "explicit"
        else f"{value.axis}=<typed:{value.member}>"
        for value in dimensions
    )
    return segments, source


def _numeric_facts(
    root: ET.Element,
    contexts: Mapping[str, _Context],
    units: Mapping[str, str],
    cik: str,
) -> list[dict]:
    result = []
    ids: set[str] = set()
    for item in root:
        uri = _namespace(item.tag)
        taxonomy = _taxonomy(uri)
        context_id = item.get("contextRef")
        unit_id = item.get("unitRef")
        if taxonomy is None or not context_id or not unit_id:
            continue
        if item.get(f"{{{XSI}}}nil", "").casefold() == "true":
            continue
        context = contexts.get(context_id)
        unit = units.get(unit_id)
        if context is None or unit is None:
            raise ValueError("Inline-XBRL fact references missing context or unit")
        if _cik(context.identifier) != cik:
            raise ValueError(f"Inline-XBRL issuer mismatch in context: {context_id}")
        fact_id = item.get("id") or ""
        if not fact_id or fact_id in ids:
            raise ValueError(f"missing or duplicate Inline-XBRL fact ID: {fact_id}")
        ids.add(fact_id)
        value = "".join(item.itertext()).strip()
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"invalid transformed Inline-XBRL value: {fact_id}") from exc
        if not number.is_finite():
            raise ValueError(f"invalid transformed Inline-XBRL value: {fact_id}")
        decimals = item.get("decimals")
        if decimals is None or (decimals != "INF" and not re.fullmatch(r"-?\d+", decimals)):
            raise ValueError(f"invalid Inline-XBRL decimals: {fact_id}")
        segments, dimensions = _dimension_payload(context.dimensions)
        result.append({
            "_output_namespace": taxonomy,
            "_output_concept": _local_name(item.tag),
            "_output_unit": unit,
            "val": value,
            "start": context.start,
            "end": context.instant or context.end,
            "segments": segments,
            "_source_dimensions": dimensions,
            "_source_namespace": taxonomy,
            "_source_namespace_uri": uri,
            "_source_tag": _local_name(item.tag),
            "_source_unit": unit,
            "_source_unit_ref": unit_id,
            "_source_context_id": context_id,
            "_source_fact_id": fact_id,
            "_source_decimals": decimals,
        })
    return result


def _anchors(payload: bytes, wanted_ids: set[str]) -> dict[str, dict[str, str]]:
    parser = _InlineAnchors(wanted_ids)
    parser.feed(payload.decode("utf-8", errors="replace"))
    parser.close()
    if parser.duplicates:
        raise ValueError(f"duplicate Inline-XBRL source anchor: {min(parser.duplicates)}")
    missing = wanted_ids - parser.facts.keys()
    if missing:
        raise ValueError(f"unresolved Inline-XBRL source anchor: {min(missing)}")
    return parser.facts


def _attach_anchors(
    facts: list[dict],
    anchors: Mapping[str, Mapping[str, str]],
    roles: Mapping[str, list[str]],
    metadata: InlineXbrlMetadata,
) -> list[dict]:
    result = []
    fiscal_year = int(metadata.report_date[:4])
    for fact in facts:
        anchor = anchors[fact["_source_fact_id"]]
        if (anchor.get("name", "").rsplit(":", 1)[-1] != fact["_output_concept"]
                or anchor.get("contextref") != fact["_source_context_id"]
                or anchor.get("unitref") != fact["_source_unit_ref"]):
            raise ValueError(f"Inline-XBRL source anchor mismatch: {fact['_source_fact_id']}")
        scale = anchor.get("scale") or None
        sign = anchor.get("sign") or None
        if scale is not None and not re.fullmatch(r"-?\d+", scale):
            raise ValueError(f"invalid Inline-XBRL scale: {fact['_source_fact_id']}")
        if sign not in {None, "-"}:
            raise ValueError(f"invalid Inline-XBRL sign: {fact['_source_fact_id']}")
        negative = Decimal(fact["val"]).is_signed()
        if (sign == "-") != negative:
            raise ValueError(f"Inline-XBRL sign disagrees with transformed value: {fact['_source_fact_id']}")
        entry = {
            **fact,
            "accn": metadata.source_accession,
            "form": metadata.annual_form,
            "filed": metadata.annual_filed,
            "fy": fiscal_year,
            "fp": "FY",
            "_source_document": metadata.source_document,
            "_source_accession": metadata.source_accession,
            "_source_form": metadata.source_form,
            "_source_filed": metadata.source_filed,
            "_source_scale": scale,
            "_source_sign": sign,
            "_source_statement_roles": roles.get(fact["_output_concept"], []),
            "_annual_accession": metadata.annual_accession,
            "_annual_form": metadata.annual_form,
            "_annual_filed": metadata.annual_filed,
        }
        if entry["start"] is None:
            entry.pop("start")
        result.append(entry)
    return result


def _precision(decimals: str) -> int | None:
    return None if decimals == "INF" else int(decimals)


def _compatible(left: dict, right: dict) -> bool:
    left_value = Decimal(left["val"])
    right_value = Decimal(right["val"])
    left_precision = _precision(left["_source_decimals"])
    right_precision = _precision(right["_source_decimals"])
    if left_precision is None and right_precision is None:
        return left_value == right_value
    tolerances = [
        Decimal(0) if precision is None else Decimal("0.5") * (Decimal(10) ** -precision)
        for precision in (left_precision, right_precision)
    ]
    return abs(left_value - right_value) <= max(tolerances)


def _deduplicate(facts: list[dict]) -> list[dict]:
    selected: dict[tuple, dict] = {}
    for fact in sorted(facts, key=_fact_order):
        key = (
            fact["_output_namespace"], fact["_output_concept"], fact["_output_unit"],
            fact["_source_context_id"],
        )
        previous = selected.get(key)
        if previous is None:
            selected[key] = fact
            continue
        if not _compatible(previous, fact):
            raise ValueError(
                "conflicting duplicate Inline-XBRL facts: "
                f"{fact['_output_namespace']}:{fact['_output_concept']} "
                f"{fact['_source_context_id']}"
            )
        previous_precision = _precision(previous["_source_decimals"])
        fact_precision = _precision(fact["_source_decimals"])
        previous_rank = float("inf") if previous_precision is None else previous_precision
        fact_rank = float("inf") if fact_precision is None else fact_precision
        if fact_rank > previous_rank or (
            fact_rank == previous_rank
            and _natural(fact["_source_fact_id"]) < _natural(previous["_source_fact_id"])
        ):
            selected[key] = fact
    return list(selected.values())


def _natural(value: str) -> tuple:
    return tuple(int(part) if part.isdigit() else part.casefold()
                 for part in re.split(r"(\d+)", value))


def _fact_order(fact: dict) -> tuple:
    return (
        fact["_output_namespace"], fact["_output_concept"], fact["_output_unit"],
        fact.get("end") or "", fact.get("start") or "", fact["_source_context_id"],
        _natural(fact["_source_fact_id"]),
    )
