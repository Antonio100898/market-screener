#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "api"))

from screener import evidence, store, sync  # noqa: E402
from screener.sources import inline_xbrl  # noqa: E402


CIK = "0000016868"
ANNUAL_ACCESSION = "0001104659-26-010352"
SOURCE_ACCESSION = "0000016868-26-000011"
DEFAULT_CACHE = Path.home() / ".cache/graham-screener"
DEFAULT_RETAINED = DEFAULT_CACHE / "raw-sec-statement-recovery"
OUTPUT = Path(__file__).with_name("results.json")


class RetainedEdgar:
    def __init__(self, cache_dir: Path, retained: Path):
        self.cache_dir = cache_dir
        self.retained = retained
        self.requests: list[str] = []

    def submissions(self, cik: str) -> dict:
        assert cik == CIK
        return {
            "name": "CANADIAN NATIONAL RAILWAY CO",
            "filings": {"recent": {
                "accessionNumber": [ANNUAL_ACCESSION, SOURCE_ACCESSION],
                "form": ["40-F", "6-K"],
                "filingDate": ["2026-02-04", "2026-02-04"],
                "reportDate": ["2025-12-31", "2025-12-31"],
                "primaryDocument": [
                    "tm261145d1_40f.htm", "cni-20251231_d2.htm",
                ],
            }},
        }

    def _request(self, url: str) -> SimpleNamespace:
        self.requests.append(url)
        parts = urlsplit(url).path.rstrip("/").split("/")
        return SimpleNamespace(
            content=(self.retained / parts[-2] / parts[-1]).read_bytes(),
        )


def build(cache: Path, retained: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="cni-incorporated-filename-") as temporary:
        temporary_root = Path(temporary)
        edgar = RetainedEdgar(temporary_root, retained)
        state, accession = sync._retain_inline_annual(edgar, CIK)
        manifest = inline_xbrl.read_current_manifest(temporary_root, CIK)
        assert manifest is not None

        conn = store.connect(temporary_root / "store.db")
        store.set_cover(conn, CIK, [{
            "symbol": "CNI", "title": "Common shares", "ratio": None,
        }], ANNUAL_ACCESSION)
        facts = json.loads((cache / f"companyfacts_{CIK}.json").read_text())
        bundle = evidence.EvidenceLoader(
            conn,
            SimpleNamespace(cache_dir=cache),
            inline_cache_dir=temporary_root,
        ).load(CIK, "CNI", facts)
        status, row = sync._derive_evidence(bundle)
        source = row["sources"]["total_assets"] if row else {}
        assets = row["total_assets"] if row else None
        liabilities = row["total_liabilities"] if row else None
        equity = (row.get("asset_quality") or {}).get("common_equity") if row else None
        return {
            "acquisition": {"state": state, "accession": accession},
            "manifest": {
                "relationship": manifest["relationship"],
                "annual_accession": manifest["annual"]["accession"],
                "annual_form": manifest["annual"]["form"],
                "source_accession": manifest["source"]["accession"],
                "source_form": manifest["source"]["form"],
                "source_document": manifest["source"]["document"],
                "filing_document": manifest["source"]["filing_document"],
            },
            "derivation": {
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
            },
            "retained_request_count": len(edgar.requests),
            "main_cache_mutated": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--retained", type=Path, default=DEFAULT_RETAINED)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    result = build(args.cache, args.retained)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.write:
        OUTPUT.write_text(encoded)
    else:
        assert OUTPUT.read_text() == encoded, "results.json is stale"
    print(encoded, end="")


if __name__ == "__main__":
    main()
