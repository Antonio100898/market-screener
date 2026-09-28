from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from screener import evidence, store, sync
from screener.sources import inline_xbrl


CIK = "0000016868"
ANNUAL_ACCESSION = "0001104659-26-010352"
SOURCE_ACCESSION = "0000016868-26-000011"
RAW_ROOT = Path.home() / ".cache/graham-screener/raw-sec-statement-recovery"
CACHE = Path.home() / ".cache/graham-screener"
OUTPUT = Path(__file__).with_name("results.json")


def record(accession: str, document: str) -> dict:
    raw = (RAW_ROOT / accession.replace("-", "") / document).read_bytes()
    return {
        "document": document,
        "url": (
            "https://www.sec.gov/Archives/edgar/data/16868/"
            f"{accession.replace('-', '')}/{document}"
        ),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }


def manifest() -> dict:
    annual_document = "tm261145d1_40f.htm"
    source_document = "cni-20251231.htm"
    relationship = inline_xbrl.incorporated_annual_source(
        (RAW_ROOT / ANNUAL_ACCESSION.replace("-", "") / annual_document).read_bytes(),
        CIK,
    )
    return {
        "schema": inline_xbrl.MANIFEST_SCHEMA,
        "parser_contract_revision": inline_xbrl.PARSER_CONTRACT_REVISION,
        "relationship": "incorporated_annual_exhibit",
        "cik": CIK,
        "entity_name": "CANADIAN NATIONAL RAILWAY CO",
        "annual": {
            "accession": ANNUAL_ACCESSION,
            "form": "40-F",
            "filed": "2026-02-04",
            "report_date": "2025-12-31",
            "document": annual_document,
        },
        "source": {
            "accession": SOURCE_ACCESSION,
            "form": "6-K",
            "filed": "2026-02-04",
            "report_date": "2025-12-31",
            "document": source_document,
        },
        "relationship_evidence": relationship,
        "files": {
            "annual_index": record(ANNUAL_ACCESSION, "index.json"),
            "annual_primary_document": record(ANNUAL_ACCESSION, annual_document),
            "source_index": record(SOURCE_ACCESSION, "index.json"),
            "instance": record(SOURCE_ACCESSION, "cni-20251231_d2_htm.xml"),
            "filing_summary": record(SOURCE_ACCESSION, "FilingSummary.xml"),
            "presentation": record(SOURCE_ACCESSION, "cni-20251231_pre.xml"),
            "schema": record(SOURCE_ACCESSION, "cni-20251231.xsd"),
            "primary_document": record(SOURCE_ACCESSION, source_document),
        },
    }


def main() -> None:
    current = manifest()
    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        path = inline_xbrl.current_manifest_path(temporary_root, CIK)
        path.parent.mkdir(parents=True)
        path.write_bytes(inline_xbrl.manifest_bytes(current))
        conn = store.connect(temporary_root / "store.db")
        store.set_cover(conn, CIK, [{
            "symbol": "CNI", "title": "Common shares", "ratio": None,
        }], ANNUAL_ACCESSION)
        facts = json.loads((CACHE / f"companyfacts_{CIK}.json").read_text())
        bundle = evidence.EvidenceLoader(
            conn,
            type("Edgar", (), {"cache_dir": CACHE})(),
            inline_cache_dir=temporary_root,
            inline_retained_root=RAW_ROOT,
        ).load(CIK, "CNI", facts)
        status, row = sync._derive_evidence(bundle)
        source = row["sources"]["total_assets"] if row else {}
        liabilities = row["total_liabilities"] if row else None
        equity = (row.get("asset_quality") or {}).get("common_equity") if row else None
        assets = row["total_assets"] if row else None
        result = {
            "status": status,
            "supplement_state": bundle.supplement_state,
            "annual_accession": source.get("annual_accn"),
            "annual_form": source.get("annual_form"),
            "source_accession": source.get("accn"),
            "source_form": source.get("form"),
            "source_document": source.get("document"),
            "balance": {
                "assets": assets,
                "liabilities": liabilities,
                "common_equity": equity,
                "reconciled": (
                    assets is not None and liabilities is not None and equity is not None
                    and abs(assets - liabilities - equity) <= 1
                ),
            },
            "retained_sec_bytes_in_evidence_directory": 0,
        }
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        assert OUTPUT.read_text() == encoded, "results.json is stale"
        print(encoded, end="")


if __name__ == "__main__":
    main()
