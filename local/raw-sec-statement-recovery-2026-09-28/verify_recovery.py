#!/usr/bin/env python3
"""Build and verify the deterministic SEC statement-recovery audit.

This script reads only official SEC files from the separate local cache. It does
not read provider credentials, call a model, or change application data.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SOURCE_INVENTORY = REPO / "local/foreign-coverage-investigation-2026-09-28/inventory.csv"
OUTPUT_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE = Path.home() / ".cache/graham-screener/raw-sec-statement-recovery"

XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"

EXPECTED_TICKERS = {
    "AERO", "AGMB", "AHNRF", "ALPS", "AUGO", "BRBI", "CIB", "CNI", "DAVI",
    "GCDT", "GMTL", "HBNB", "NXAT", "PAYP", "PICS", "TMCR", "VMET", "YMAT",
}
EXPECTED_CATEGORIES = {
    "generic_inline_xbrl": 17,
    "generic_incorporated_exhibit": 1,
    "extension_mapping": 0,
    "unstructured_llm_candidate": 0,
    "unsupported": 0,
}

CANONICAL = {
    "AssetsCurrent": ("CurrentAssets", "AssetsCurrent"),
    "Assets": ("Assets",),
    "LiabilitiesCurrent": ("CurrentLiabilities", "LiabilitiesCurrent"),
    "Liabilities": ("Liabilities",),
    "Equity": (
        "Equity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "StockholdersEquity",
    ),
    "EquityAndLiabilities": ("EquityAndLiabilities", "LiabilitiesAndStockholdersEquity"),
    "NetIncomeLoss": ("ProfitLossAttributableToOwnersOfParent", "NetIncomeLoss", "ProfitLoss"),
    "NetCashProvidedByUsedInOperatingActivities": (
        "CashFlowsFromUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivities",
    ),
}

FIELD_STATEMENT_WORDS = {
    "AssetsCurrent": ("position", "balance"),
    "Assets": ("position", "balance"),
    "LiabilitiesCurrent": ("position", "balance"),
    "Liabilities": ("position", "balance"),
    "Equity": ("position", "balance"),
    "EquityAndLiabilities": ("position", "balance"),
    "NetIncomeLoss": ("income", "operations", "profit", "loss"),
    "NetCashProvidedByUsedInOperatingActivities": ("cash flow",),
}


@dataclass(frozen=True)
class Context:
    identifier: str | None
    instant: str | None
    start: str | None
    end: str | None
    dimensions: tuple[str, ...]


class InlineAttributes(HTMLParser):
    def __init__(self, wanted: set[str]):
        super().__init__(convert_charrefs=False)
        self.wanted = wanted
        self.facts: dict[str, dict[str, str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() not in {"ix:nonfraction", "ix:nonnumeric"}:
            return
        values = {key.lower(): value or "" for key, value in attrs}
        fact_id = values.get("id")
        if fact_id in self.wanted:
            self.facts[fact_id] = values


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].rsplit("_", 1)[-1]


def namespace(name: str) -> str:
    return name[1:].split("}", 1)[0] if name.startswith("{") else ""


def taxonomy(uri: str) -> str:
    if "xbrl.ifrs.org" in uri:
        return "ifrs-full"
    if "fasb.org/us-gaap" in uri:
        return "us-gaap"
    if "xbrl.sec.gov/dei" in uri:
        return "dei"
    return "extension"


def load_rows() -> list[dict[str, str]]:
    with SOURCE_INVENTORY.open(newline="") as handle:
        rows = [row for row in csv.DictReader(handle)
                if row["subclass"] == "incomplete_company_facts"]
    assert len(rows) == 18
    assert {row["ticker"] for row in rows} == EXPECTED_TICKERS
    return sorted(rows, key=lambda row: row["ticker"])


def metadata(cache: Path, accession: str) -> tuple[Path, dict]:
    directory = cache / accession.replace("-", "")
    path = directory / "metadata.json"
    if not path.exists():
        raise FileNotFoundError(f"missing SEC cache metadata: {path}")
    return directory, json.loads(path.read_text())


def find_file(directory: Path, files: dict, suffix: str) -> Path | None:
    names = sorted(name for name in files if name.endswith(suffix))
    return directory / names[0] if names else None


def contexts_and_units(root: ET.Element) -> tuple[dict[str, Context], dict[str, str]]:
    contexts: dict[str, Context] = {}
    for item in root.findall(f"{{{XBRLI}}}context"):
        dimensions = []
        for member in item.findall(f".//{{{XBRLDI}}}explicitMember"):
            dimensions.append(f"{member.get('dimension')}={member.text}")
        for member in item.findall(f".//{{{XBRLDI}}}typedMember"):
            dimensions.append(f"{member.get('dimension')}=<typed>")
        contexts[item.get("id", "")] = Context(
            identifier=item.findtext(f".//{{{XBRLI}}}identifier"),
            instant=item.findtext(f".//{{{XBRLI}}}instant"),
            start=item.findtext(f".//{{{XBRLI}}}startDate"),
            end=item.findtext(f".//{{{XBRLI}}}endDate"),
            dimensions=tuple(dimensions),
        )
    units = {}
    for item in root.findall(f"{{{XBRLI}}}unit"):
        measures = [local_name(value.text or "")
                    for value in item.findall(f".//{{{XBRLI}}}measure")]
        units[item.get("id", "")] = "*".join(measures)
    return contexts, units


def statement_reports(summary: Path) -> dict[str, str]:
    reports = {}
    root = ET.parse(summary).getroot()
    for report in root.findall(".//Report"):
        if report.findtext("MenuCategory") == "Statements":
            role = report.findtext("Role")
            name = report.findtext("ShortName") or report.findtext("LongName")
            if role and name:
                reports[role] = name
    return reports


def presentation_roles(paths: list[Path], reports: dict[str, str]) -> dict[str, list[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        root = ET.parse(path).getroot()
        for link in root.findall(f".//{{{LINK}}}presentationLink"):
            role = link.get(f"{{{XLINK}}}role")
            report = reports.get(role or "")
            if not report:
                continue
            for locator in link.findall(f"{{{LINK}}}loc"):
                href = locator.get(f"{{{XLINK}}}href", "")
                concept = local_name(href.partition("#")[2])
                if concept:
                    result[concept].add(report)
    return {concept: sorted(names) for concept, names in result.items()}


def choose_fact(
    field: str,
    facts: list[dict],
    report_date: str,
    roles: dict[str, list[str]],
) -> dict | None:
    names = CANONICAL[field]
    candidates = []
    for fact in facts:
        if fact["concept"] not in names or fact["taxonomy"] not in {"ifrs-full", "us-gaap"}:
            continue
        if fact["dimensions"] or (fact["instant"] or fact["end"]) != report_date:
            continue
        fact_roles = roles.get(fact["concept"], [])
        wanted = FIELD_STATEMENT_WORDS[field]
        primary = any(any(word in role.lower() for word in wanted) for role in fact_roles)
        natural_id = tuple(
            int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", fact["id"])
        )
        candidates.append((names.index(fact["concept"]), not primary, natural_id, fact))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[:3])[3]


def parse_filing(directory: Path, meta: dict) -> dict:
    files = meta["files"]
    instance = find_file(directory, files, "_htm.xml")
    summary = directory / "FilingSummary.xml"
    if instance is None or not summary.exists():
        raise AssertionError(f"structured SEC files absent in {directory}")
    reports = statement_reports(summary)
    presentation_files = [directory / name for name in files
                          if name.endswith("_pre.xml") or name.endswith(".xsd")]
    roles = presentation_roles(presentation_files, reports)

    root = ET.parse(instance).getroot()
    contexts, units = contexts_and_units(root)
    facts = []
    for item in root:
        context_ref = item.get("contextRef")
        if not context_ref or context_ref not in contexts:
            continue
        context = contexts[context_ref]
        concept = local_name(item.tag)
        if concept not in {name for names in CANONICAL.values() for name in names}:
            continue
        facts.append({
            "id": item.get("id", ""),
            "concept": concept,
            "namespace": namespace(item.tag),
            "taxonomy": taxonomy(namespace(item.tag)),
            "value": (item.text or "").strip(),
            "unit": units.get(item.get("unitRef", "")),
            "decimals": item.get("decimals"),
            "context_id": context_ref,
            "identifier": context.identifier,
            "instant": context.instant,
            "start": context.start,
            "end": context.end,
            "dimensions": list(context.dimensions),
        })

    report_date = meta["submission"]["reportDate"]
    selected = {field: choose_fact(field, facts, report_date, roles) for field in CANONICAL}
    wanted_ids = {fact["id"] for fact in selected.values() if fact and fact["id"]}
    primary = directory / meta["submission"]["primaryDocument"]
    parser = InlineAttributes(wanted_ids)
    parser.feed(primary.read_text(encoding="utf-8", errors="replace"))
    for field, fact in selected.items():
        if fact is None:
            continue
        attrs = parser.facts.get(fact["id"])
        if attrs is None:
            raise AssertionError(f"{meta['ticker']} {field} source anchor {fact['id']} missing")
        fact["source_anchor"] = fact["id"]
        fact["source_scale"] = attrs.get("scale")
        fact["source_sign"] = attrs.get("sign")
        fact["source_document"] = primary.name
        fact["source_accession"] = meta["submission"]["accessionNumber"]
        fact["source_form"] = meta["submission"]["form"]
        unit = fact["unit"] or ""
        currency = unit.rsplit(":", 1)[-1]
        fact["currency"] = currency if re.fullmatch(r"[A-Z]{3}", currency) else None
        fact["statement_roles"] = roles.get(fact["concept"], [])
    return {
        "instance_document": instance.name,
        "instance_sha256": sha256(instance),
        "filing_summary_sha256": sha256(summary),
        "primary_document": primary.name,
        "primary_document_sha256": sha256(primary),
        "statement_reports": sorted(reports.values()),
        "selected": selected,
    }


def decimal(fact: dict | None) -> Decimal | None:
    return Decimal(fact["value"]) if fact else None


def reconciliation(selected: dict[str, dict | None]) -> dict:
    assets = decimal(selected["Assets"])
    liabilities = decimal(selected["Liabilities"])
    equity = decimal(selected["Equity"])
    total = decimal(selected["EquityAndLiabilities"])
    current_assets = decimal(selected["AssetsCurrent"])
    current_liabilities = decimal(selected["LiabilitiesCurrent"])
    equation = None
    equation_basis = None
    if assets is not None and total is not None:
        equation = assets == total
        equation_basis = "Assets == EquityAndLiabilities"
    elif assets is not None and liabilities is not None and equity is not None:
        equation = assets == liabilities + equity
        equation_basis = "Assets == Liabilities + Equity"
    return {
        "equation_basis": equation_basis,
        "equation_reconciles": equation,
        "current_assets_within_assets": (
            0 <= current_assets <= assets
            if current_assets is not None and assets is not None else None
        ),
        "current_liabilities_within_liabilities": (
            0 <= current_liabilities <= liabilities
            if current_liabilities is not None and liabilities is not None else None
        ),
    }


def artifact_rows(directory: Path, meta: dict) -> list[dict]:
    result = []
    for name, recorded in sorted(meta["files"].items()):
        path = directory / name
        if not path.exists():
            raise FileNotFoundError(path)
        actual = {"sha256": sha256(path), "bytes": path.stat().st_size}
        assert actual == {"sha256": recorded["sha256"], "bytes": recorded["bytes"]}
        if name in {meta["submission"]["primaryDocument"], "index.json", "FilingSummary.xml"} \
                or name.endswith(("_htm.xml", "_pre.xml", ".xsd")):
            result.append({"document": name, "url": recorded["url"], **actual})
    return result


def company_record(row: dict[str, str], cache: Path) -> dict:
    annual_dir, annual_meta = metadata(cache, row["annual_accession"])
    annual_artifacts = artifact_rows(annual_dir, annual_meta)
    source_dir, source_meta = annual_dir, annual_meta
    incorporated = []
    category = "generic_inline_xbrl"
    if row["ticker"] == "CNI":
        exhibit_accession = "0000016868-26-000011"
        exhibit_document = "cni-20251231.htm"
        wrapper = annual_dir / annual_meta["submission"]["primaryDocument"]
        wrapper_text = wrapper.read_text(encoding="utf-8", errors="replace")
        exhibit_url = (
            "http://www.sec.gov/Archives/edgar/data/16868/"
            "000001686826000011/cni-20251231.htm"
        )
        assert exhibit_url in wrapper_text
        assert "Incorporated by reference from the Registrant" in wrapper_text
        source_dir, source_meta = metadata(cache, exhibit_accession)
        incorporated = [{
            "accession": exhibit_accession,
            "form": "6-K",
            "document": exhibit_document,
            "description": "Exhibit 99.2, incorporated by the current 40-F",
            "relationship_anchor": exhibit_url,
        }]
        category = "generic_incorporated_exhibit"
    parsed = parse_filing(source_dir, source_meta)
    source_artifacts = artifact_rows(source_dir, source_meta)
    selected = parsed.pop("selected")
    assert selected["Assets"] is not None
    assert selected["EquityAndLiabilities"] is not None or (
        selected["Liabilities"] is not None and selected["Equity"] is not None)
    assert {fact["taxonomy"] for fact in selected.values() if fact} <= {"ifrs-full", "us-gaap"}
    check = reconciliation(selected)
    assert check["equation_reconciles"] is True
    assert check["current_assets_within_assets"] is not False
    assert check["current_liabilities_within_liabilities"] is not False

    companyfacts = Path.home() / ".cache/graham-screener" / f"companyfacts_{row['cik']}.json"
    sidecar = Path.home() / ".cache/graham-screener" / f"dimensioned_{row['cik']}.json"
    companyfacts_payload = json.loads(companyfacts.read_text())
    current_companyfacts = sum(
        entry.get("accn") == row["annual_accession"]
        for taxonomy_data in companyfacts_payload.get("facts", {}).values()
        for concept_data in taxonomy_data.values()
        for entries in concept_data.get("units", {}).values()
        for entry in entries
    )
    current_sidecar = 0
    if sidecar.exists():
        sidecar_payload = json.loads(sidecar.read_text())
        current_sidecar = sum(
            entry.get("accn") == row["annual_accession"]
            for taxonomy_data in sidecar_payload.get("facts", {}).values()
            for concept_data in taxonomy_data.values()
            for entries in concept_data.get("units", {}).values()
            for entry in entries
        )
    statement_names = parsed["statement_reports"]
    scope = "consolidated" if any("consolidated" in name.lower() for name in statement_names) \
        else "undimensioned reporting entity"
    present = [field for field, fact in selected.items() if fact]
    missing = [field for field, fact in selected.items() if not fact]
    return {
        "ticker": row["ticker"],
        "cik": row["cik"],
        "company": row["company"],
        "exchange": row["exchange"] or None,
        "annual_accession": row["annual_accession"],
        "annual_form": annual_meta["submission"]["form"],
        "annual_filed": annual_meta["submission"]["filingDate"],
        "report_date": annual_meta["submission"]["reportDate"],
        "annual_primary_document": annual_meta["submission"]["primaryDocument"],
        "incorporated_official_exhibits": incorporated,
        "source_accession": source_meta["submission"]["accessionNumber"],
        "source_form": source_meta["submission"]["form"],
        "category": category,
        "accounting_basis": next(fact["taxonomy"] for fact in selected.values() if fact),
        "scope": scope,
        "available_canonical_anchors": present,
        "missing_canonical_anchors": missing,
        "facts": selected,
        "reconciliation": check,
        "local_availability": {
            "company_facts": companyfacts.exists(),
            "company_facts_sha256": sha256(companyfacts) if companyfacts.exists() else None,
            "company_facts_current_accession_fact_count": current_companyfacts,
            "dera_sidecar": sidecar.exists(),
            "dera_sidecar_sha256": sha256(sidecar) if sidecar.exists() else None,
            "dera_sidecar_current_accession_fact_count": current_sidecar,
            "annual_sec_files": True,
            "source_sec_files": True,
        },
        "annual_artifacts": annual_artifacts,
        "source_artifacts": source_artifacts,
        **parsed,
    }


def build(cache: Path) -> dict:
    companies = [company_record(row, cache) for row in load_rows()]
    counts = Counter(company["category"] for company in companies)
    for category in EXPECTED_CATEGORIES:
        counts.setdefault(category, 0)
    assert dict(sorted(counts.items())) == dict(sorted(EXPECTED_CATEGORIES.items()))
    assert len(companies) == len({company["ticker"] for company in companies}) == 18
    return {
        "schema": "raw_sec_statement_recovery_v1",
        "source_inventory": str(SOURCE_INVENTORY.relative_to(REPO)),
        "company_count": len(companies),
        "category_counts": dict(sorted(counts.items())),
        "companies": companies,
    }


def write_csv(payload: dict) -> None:
    columns = [
        "ticker", "cik", "annual_accession", "annual_form", "report_date",
        "annual_primary_document", "source_accession", "source_form", "category",
        "accounting_basis", "scope", "available_canonical_anchors",
        "missing_canonical_anchors", "equation_basis", "equation_reconciles",
        "current_assets_within_assets", "current_liabilities_within_liabilities",
        "company_facts_available", "dera_sidecar_available",
        "company_facts_current_accession_fact_count", "dera_sidecar_current_accession_fact_count",
    ]
    with (OUTPUT_DIR / "inventory.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for company in payload["companies"]:
            check = company["reconciliation"]
            writer.writerow({
                **{key: company.get(key) for key in columns},
                "available_canonical_anchors": ";".join(company["available_canonical_anchors"]),
                "missing_canonical_anchors": ";".join(company["missing_canonical_anchors"]),
                "equation_basis": check["equation_basis"],
                "equation_reconciles": check["equation_reconciles"],
                "current_assets_within_assets": check["current_assets_within_assets"],
                "current_liabilities_within_liabilities": check["current_liabilities_within_liabilities"],
                "company_facts_available": company["local_availability"]["company_facts"],
                "dera_sidecar_available": company["local_availability"]["dera_sidecar"],
                "company_facts_current_accession_fact_count": (
                    company["local_availability"]["company_facts_current_accession_fact_count"]
                ),
                "dera_sidecar_current_accession_fact_count": (
                    company["local_availability"]["dera_sidecar_current_accession_fact_count"]
                ),
            })


def write_hashes(payload: dict) -> None:
    rows = []
    seen = set()
    for company in payload["companies"]:
        for artifact in company["annual_artifacts"] + company["source_artifacts"]:
            key = (artifact["sha256"], artifact["url"])
            if key not in seen:
                rows.append((company["ticker"], artifact["sha256"], artifact["bytes"], artifact["url"]))
                seen.add(key)
    text = "\n".join(f"{ticker}  {digest}  {size}  {url}" for ticker, digest, size, url in rows)
    (OUTPUT_DIR / "source-hashes.txt").write_text(text + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    payload = build(args.cache)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    inventory = OUTPUT_DIR / "inventory.json"
    if args.write:
        inventory.write_text(encoded)
        write_csv(payload)
        write_hashes(payload)
    else:
        assert inventory.read_text() == encoded, "inventory.json is stale"
    print(json.dumps({
        "companies": payload["company_count"],
        "categories": payload["category_counts"],
        "all_source_anchors_resolved": True,
        "all_balance_equations_reconcile": True,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
