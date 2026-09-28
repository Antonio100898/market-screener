#!/usr/bin/env python3
"""Exercise direct annual acquisition from retained SEC bytes only."""
from __future__ import annotations

import json
import runpy
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "api"))

from screener import sync  # noqa: E402
from screener.sources import inline_xbrl  # noqa: E402


RETAINED = Path.home() / ".cache/graham-screener/raw-sec-statement-recovery"
CASES = {
    "GCDT": ("0001926293", "000149315226038530"),
    "HBNB": ("0002054507", "000121390026049771"),
    "NXAT": ("0002000756", "000182912626005357"),
}


class RetainedEdgar:
    def __init__(self, cache_dir: Path, cik: str, accession_digits: str):
        self.cache_dir = cache_dir
        self.cik = cik
        self.accession_digits = accession_digits
        self.directory = RETAINED / accession_digits
        self.metadata = json.loads((self.directory / "metadata.json").read_text())

    def submissions(self, cik: str) -> dict:
        assert cik == self.cik
        filing = self.metadata["submission"]
        return {
            "name": self.metadata["company"],
            "filings": {"recent": {
                "accessionNumber": [filing["accessionNumber"]],
                "form": [filing["form"]],
                "filingDate": [filing["filingDate"]],
                "reportDate": [filing["reportDate"]],
                "primaryDocument": [filing["primaryDocument"]],
            }},
        }

    def _request(self, url: str) -> SimpleNamespace:
        parts = urlsplit(url).path.rstrip("/").split("/")
        assert parts[-2] == self.accession_digits
        return SimpleNamespace(content=(self.directory / parts[-1]).read_bytes())


def main() -> None:
    direct = []
    with tempfile.TemporaryDirectory(prefix="direct-before-incorporation-") as temporary:
        cache = Path(temporary)
        for ticker, (cik, accession_digits) in CASES.items():
            state, accession = sync._retain_inline_annual(
                RetainedEdgar(cache, cik, accession_digits), cik,
            )
            manifest = inline_xbrl.read_current_manifest(cache, cik)
            assert manifest is not None
            assert state == "activated"
            assert manifest["relationship"] == "direct_annual"
            direct.append({
                "ticker": ticker,
                "cik": cik,
                "state": state,
                "accession": accession,
                "relationship": manifest["relationship"],
            })
    cni_verifier = runpy.run_path(
        REPO / "local/raw-sec-incorporated-filename-fix-2026-09-28/verify_cni.py"
    )
    cni = cni_verifier["build"](RETAINED.parent, RETAINED)
    assert cni["acquisition"]["state"] == "activated"
    assert cni["manifest"]["relationship"] == "incorporated_annual_exhibit"
    assert cni["derivation"]["status"] == "ok"
    assert cni["derivation"]["balance"]["reconciled"] is True
    print(json.dumps({
        "direct": direct,
        "cni": {
            "state": cni["acquisition"]["state"],
            "relationship": cni["manifest"]["relationship"],
            "derivation": cni["derivation"]["status"],
            "balance_reconciled": cni["derivation"]["balance"]["reconciled"],
        },
    }, sort_keys=True))


if __name__ == "__main__":
    main()
