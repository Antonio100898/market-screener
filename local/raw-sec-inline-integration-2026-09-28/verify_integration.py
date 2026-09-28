#!/usr/bin/env python3
"""Derive the 17 direct annual cases from external retained SEC bytes."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "api"))

from screener import evidence, normalize, store, sync  # noqa: E402
from screener.sources import dera, inline_xbrl  # noqa: E402


INVENTORY = REPO / "local/raw-sec-statement-recovery-2026-09-28/inventory.json"
OUTPUT = Path(__file__).with_name("results.json")
DEFAULT_CACHE = Path.home() / ".cache/graham-screener"
DEFAULT_RETAINED = DEFAULT_CACHE / "raw-sec-statement-recovery"


def _one_name(files: dict, suffix: str) -> str | None:
    return next((name for name in sorted(files) if name.endswith(suffix)), None)


def _manifest(company: dict, retained: Path) -> dict:
    directory = retained / company["source_accession"].replace("-", "")
    metadata = json.loads((directory / "metadata.json").read_text())
    files = metadata["files"]
    schema = _one_name(files, ".xsd")
    roles = {
        "index": "index.json",
        "instance": _one_name(files, "_htm.xml"),
        "filing_summary": "FilingSummary.xml",
        "presentation": _one_name(files, "_pre.xml") or schema,
        "schema": schema,
        "primary_document": metadata["submission"]["primaryDocument"],
    }
    records = {}
    for role, document in roles.items():
        record = files[document]
        records[role] = {
            "document": document,
            "url": record["url"],
            "sha256": record["sha256"],
            "size": record["bytes"],
        }
    annual = {
        "accession": company["annual_accession"],
        "form": company["annual_form"],
        "filed": company["annual_filed"],
        "report_date": company["report_date"],
        "document": company["primary_document"],
    }
    return {
        "schema": inline_xbrl.MANIFEST_SCHEMA,
        "parser_contract_revision": inline_xbrl.PARSER_CONTRACT_REVISION,
        "relationship": "direct_annual",
        "cik": company["cik"],
        "entity_name": company["company"],
        "annual": annual,
        "source": {
            "accession": company["source_accession"],
            "form": company["source_form"],
            "filed": metadata["submission"]["filingDate"],
            "document": company["primary_document"],
        },
        "files": records,
    }


def _summary(status: str, data: dict | None) -> dict:
    data = data or {}
    source = (data.get("sources") or {}).get("total_assets") or {}
    return {
        "status": status,
        "balance_sheet_date": data.get("balance_sheet_date"),
        "source_accession": source.get("accn"),
        "error": data.get("error"),
        "pending": (data.get("data_pending") or {}).get("accession"),
    }


def _failure(bundle: evidence.EvidenceBundle) -> str | None:
    try:
        normalize.build_snapshot(
            bundle.ticker, bundle.cik, bundle.facts,
            dimensioned=bundle.dimensioned, receipt=bundle.receipt,
        )
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def build(cache: Path, retained: Path, database: Path) -> dict:
    inventory = json.loads(INVENTORY.read_text())
    companies = [
        company for company in inventory["companies"]
        if company["category"] == "generic_inline_xbrl"
    ]
    uri = f"file:{database}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    rows = {
        row["cik"]: row
        for row in conn.execute("SELECT cik, ticker FROM company")
    }
    results = []
    with tempfile.TemporaryDirectory(prefix="sec-inline-manifests-") as temporary:
        manifest_cache = Path(temporary)
        for company in companies:
            cik = company["cik"]
            manifest = _manifest(company, retained)
            path = inline_xbrl.current_manifest_path(manifest_cache, cik)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(inline_xbrl.manifest_bytes(manifest))

            facts = json.loads((cache / f"companyfacts_{cik}.json").read_text())
            loader = evidence.EvidenceLoader(
                conn,
                SimpleNamespace(cache_dir=cache),
                inline_cache_dir=manifest_cache,
                inline_retained_root=retained,
            )
            ticker, receipt = loader.identity(cik, rows[cik]["ticker"])
            baseline = evidence.EvidenceBundle(
                cik, ticker, facts, dera.load_sidecar(cache, cik), receipt,
            )
            before_status, before_data = sync._derive_evidence(baseline)
            bundle = loader.load(cik, ticker, facts)
            after_status, after_data = sync._derive_evidence(bundle)
            before = _summary(before_status, before_data)
            after = _summary(after_status, after_data)
            results.append({
                "ticker": ticker,
                "cik": cik,
                "annual_accession": company["annual_accession"],
                "supplement_state": bundle.supplement_state,
                "before": before,
                "after": after,
                "changed": before != after,
                "remaining_failure": (
                    None if after_status == "ok" else _failure(bundle)
                ),
            })
    return {
        "schema": "sec_inline_integration_verification_v1",
        "engine_version": store.ENGINE_VERSION,
        "company_count": len(results),
        "ok_after": sum(row["after"]["status"] == "ok" for row in results),
        "remaining_failures": sum(row["remaining_failure"] is not None for row in results),
        "companies": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--retained", type=Path, default=DEFAULT_RETAINED)
    parser.add_argument("--database", type=Path, default=store.DEFAULT_DB)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    payload = build(args.cache, args.retained, args.database)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.write:
        OUTPUT.write_text(encoded)
    else:
        assert OUTPUT.read_text() == encoded, "results.json is stale"
    print(json.dumps({
        "companies": payload["company_count"],
        "ok_after": payload["ok_after"],
        "remaining_failures": payload["remaining_failures"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
