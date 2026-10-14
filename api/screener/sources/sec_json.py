"""Strict captured JSON admission and original Company Facts occurrences."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

from ..models import CapturedSource, RawFactOrigin


ORIGIN_KEY = "_exact_sec_origin"


@dataclass(frozen=True)
class _Number:
    text: str
    integer: bool


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object keys")
        if key == ORIGIN_KEY:
            raise ValueError("reserved source occurrence field")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"invalid numeric constant: {value}")


def _decode(data, label):
    if not isinstance(data, bytes):
        raise ValueError(f"{label} must be exact response bytes")
    try:
        tree = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique,
                          parse_constant=_constant,
                          parse_int=lambda v: _Number(v, True),
                          parse_float=lambda v: _Number(v, False))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(tree, dict):
        raise ValueError(f"{label} must be a JSON object")
    return tree


def _materialize(value):
    if isinstance(value, _Number):
        number = int(value.text) if value.integer else float(value.text)
        if not value.integer and not math.isfinite(number):
            raise ValueError("nonfinite compatible JSON number")
        return number
    if isinstance(value, dict):
        return {key: _materialize(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_materialize(child) for child in value]
    return value


def decode_object(data: bytes, label: str = "captured input") -> dict:
    return _materialize(_decode(data, label))


def _escape(value):
    return value.replace("~", "~0").replace("/", "~1")


def decode_company_facts(data: bytes, *, source: CapturedSource) -> dict:
    if not isinstance(source, CapturedSource) or source.role != "company_facts":
        raise ValueError("Company Facts requires its captured source")
    if (not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != source.artifact_sha256
            or source.fidelity not in {"EXACT_LEXICAL", "DECODED_DECIMAL", "UNKNOWN"}
            or any(type(identifier) is not int or identifier <= 0
                   for identifier in (source.source_item_id, source.source_observation_id))
            or (source.occurrence_id is not None
                and (type(source.occurrence_id) is not int or source.occurrence_id <= 0))
            or not isinstance(source.source_document, str) or not source.source_document):
        raise ValueError("Company Facts captured identity differs from verified bytes")
    tree = _decode(data, "SEC Company Facts")
    origins = []
    facts = tree.get("facts", {})
    if isinstance(facts, dict):
        for namespace, taxonomy in facts.items():
            if not isinstance(taxonomy, dict):
                continue
            for concept, definition in taxonomy.items():
                units = definition.get("units") if isinstance(definition, dict) else None
                if not isinstance(units, dict):
                    continue
                for unit, entries in units.items():
                    if not isinstance(entries, list):
                        continue
                    for index, entry in enumerate(entries):
                        if not isinstance(entry, dict) or not isinstance(entry.get("val"), _Number):
                            continue
                        origins.append((namespace, concept, unit, index, RawFactOrigin(
                            source, "/facts/" + "/".join(map(_escape, (namespace, concept)))
                            + "/units/" + _escape(unit) + f"/{index}/val",
                            entry["val"].text, namespace, concept, unit,
                            entry.get("start"), entry.get("end"), entry.get("accn"),
                            entry.get("form"), entry.get("filed"))))
    output = _materialize(tree)
    for namespace, concept, unit, index, origin in origins:
        output["facts"][namespace][concept]["units"][unit][index][ORIGIN_KEY] = origin
    return output


def matches_entry(origin: RawFactOrigin, entry: dict, *, namespace: str,
                  concept: str) -> bool:
    return (origin.namespace == entry.get("_source_namespace", namespace)
            and origin.concept == entry.get("_source_tag", concept)
            and origin.unit == entry.get("_source_unit")
            and origin.start == entry.get("start") and origin.end == entry.get("end")
            and origin.accession == entry.get("_source_accession", entry.get("accn"))
            and origin.form == entry.get("_source_form", entry.get("form"))
            and origin.filed == entry.get("_source_filed", entry.get("filed"))
            and not entry.get("_annual_accession"))


def verify_origin(origin: RawFactOrigin, original_facts: dict):
    """Check the selected carrier against the original decoder-owned occurrence."""
    try:
        value = original_facts
        tokens = origin.pointer.split("/")
        if tokens[0] != "" or tokens[-1] != "val":
            raise ValueError("invalid selected occurrence pointer")
        for token in tokens[1:-1]:
            token = token.replace("~1", "/").replace("~0", "~")
            value = value[int(token)] if isinstance(value, list) else value[token]
        if not isinstance(value, dict) or value.get(ORIGIN_KEY) != origin:
            raise ValueError("selected occurrence differs from original verified input")
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise ValueError("selected occurrence is absent from original verified input") from error
