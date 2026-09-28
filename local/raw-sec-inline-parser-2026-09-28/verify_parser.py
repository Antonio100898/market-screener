#!/usr/bin/env python3
"""Verify the pure parser against the 17 accepted direct SEC filings."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "api"))

from screener.sources.inline_xbrl import (  # noqa: E402
    InlineXbrlMetadata,
    InlineXbrlPaths,
    parse_inline_xbrl,
)


SOURCE = REPO / "local/raw-sec-statement-recovery-2026-09-28/inventory.json"
OUTPUT = Path(__file__).with_name("results.json")
DEFAULT_CACHE = Path.home() / ".cache/graham-screener/raw-sec-statement-recovery"
FILE_FIELDS = ("instance", "filing_summary", "presentation", "schema", "primary_document")


def _find(directory: Path, files: dict, suffix: str) -> Path | None:
    return next((directory / name for name in sorted(files) if name.endswith(suffix)), None)


def _expected_unit(fact: dict) -> str:
    if fact.get("currency"):
        return fact["currency"]
    parts = fact["unit"].split("/")
    return "/".join(part.rsplit(":", 1)[-1] for part in parts)


def _entries(payload: dict, fact: dict) -> list[dict]:
    return (payload["facts"].get(fact["taxonomy"], {})
            .get(fact["concept"], {}).get("units", {})
            .get(_expected_unit(fact), []))


def _one_company(company: dict, cache: Path) -> dict:
    directory = cache / company["source_accession"].replace("-", "")
    retained = json.loads((directory / "metadata.json").read_text())
    files = retained["files"]
    schema = _find(directory, files, ".xsd")
    presentation = _find(directory, files, "_pre.xml") or schema
    paths = InlineXbrlPaths(
        instance=_find(directory, files, "_htm.xml"),
        filing_summary=directory / "FilingSummary.xml",
        presentation=presentation,
        schema=schema,
        primary_document=directory / retained["submission"]["primaryDocument"],
    )
    expected_hashes = {
        field: files[getattr(paths, field).name]["sha256"] for field in FILE_FIELDS
    }
    metadata = InlineXbrlMetadata(
        cik=company["cik"],
        entity_name=company["company"],
        source_accession=company["source_accession"],
        source_form=company["source_form"],
        source_filed=retained["submission"]["filingDate"],
        source_document=retained["submission"]["primaryDocument"],
        report_date=company["report_date"],
        annual_accession=company["annual_accession"],
        annual_form=company["annual_form"],
        annual_filed=company["annual_filed"],
    )
    payload = parse_inline_xbrl(metadata, paths, expected_hashes)

    checked = []
    for field, expected in sorted(company["facts"].items()):
        if expected is None:
            continue
        matches = [
            entry for entry in _entries(payload, expected)
            if entry["_source_fact_id"] == expected["id"]
        ]
        assert len(matches) == 1, f"{company['ticker']} {field}: source anchor not reproduced"
        actual = matches[0]
        assert actual["val"] == expected["value"]
        assert actual.get("start") == expected["start"]
        assert actual["end"] == (expected["instant"] or expected["end"])
        assert actual["_source_decimals"] == expected["decimals"]
        assert actual["_source_scale"] == expected["source_scale"]
        assert actual["_source_sign"] == expected["source_sign"]
        assert actual["_source_statement_roles"] == expected["statement_roles"]
        assert actual["_source_dimensions"] == []
        assert actual["_source_accession"] == company["source_accession"]
        assert actual["_source_document"] == company["primary_document"]
        assert actual["_annual_accession"] == company["annual_accession"]
        assert actual["_annual_form"] == company["annual_form"]
        checked.append({
            "field": field,
            "fact_id": actual["_source_fact_id"],
            "value": actual["val"],
            "unit": _expected_unit(expected),
            "start": actual.get("start"),
            "end": actual["end"],
        })

    parser_files = payload["_inline_xbrl"]["files"]
    assert {field: parser_files[field]["sha256"] for field in FILE_FIELDS} == expected_hashes
    return {
        "ticker": company["ticker"],
        "cik": company["cik"],
        "source_accession": company["source_accession"],
        "standard_fact_count": payload["_inline_xbrl"]["standard_fact_count"],
        "selected_anchors_checked": checked,
        "source_hashes": parser_files,
    }


def build(cache: Path) -> dict:
    inventory = json.loads(SOURCE.read_text())
    direct = [
        company for company in inventory["companies"]
        if company["category"] == "generic_inline_xbrl"
    ]
    assert len(direct) == 17
    companies = [_one_company(company, cache) for company in direct]
    return {
        "schema": "raw_sec_inline_parser_v1",
        "company_count": len(companies),
        "total_standard_fact_count": sum(row["standard_fact_count"] for row in companies),
        "selected_anchor_count": sum(len(row["selected_anchors_checked"]) for row in companies),
        "all_selected_anchors_reproduced": True,
        "all_source_hashes_preserved": True,
        "companies": companies,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    payload = build(args.cache)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.write:
        OUTPUT.write_text(encoded)
    else:
        assert OUTPUT.read_text() == encoded, "results.json is stale"
    print(json.dumps({
        "companies": payload["company_count"],
        "standard_facts": payload["total_standard_fact_count"],
        "selected_anchors": payload["selected_anchor_count"],
        "all_selected_anchors_reproduced": payload["all_selected_anchors_reproduced"],
        "all_source_hashes_preserved": payload["all_source_hashes_preserved"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
