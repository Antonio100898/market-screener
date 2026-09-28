"""Fetch fixed current SEC cover accessions and record exact parser output."""

from __future__ import annotations

import json
from pathlib import Path

from screener.sources import cover
from screener.sources.edgar import EdgarClient


CASES = {
    "BBVA": ("0000842180", "0001628280-26-010001"),
    "BEP": ("0001533232", "0001533232-26-000011"),
    "AZN": ("0000901832", "0001104659-26-019130"),
    "SUZ": ("0000909327", "0000909327-26-000045"),
    "AURE": ("0001765850", "0001213900-25-013536"),
    "ADAG": ("0001818838", "0001104659-26-038612"),
    "HON": ("0000773840", "0000773840-26-000124"),
    "PPG": ("0000079879", "0000079879-26-000170"),
    "GLP": ("0001323468", "0001104659-26-092496"),
    "LX": ("0001708259", "0001193125-26-189256"),
    "TM": ("0001094517", "0001193125-26-264811"),
}


def main() -> None:
    edgar = EdgarClient()
    output = []
    for ticker, (cik, accession) in CASES.items():
        rows = []
        source = None
        for report in cover.COVER_REPORTS:
            url = cover.R_URL.format(
                cik=int(cik), accn=accession.replace("-", ""), n=report
            )
            try:
                rows = cover.securities(edgar._get_text(url))
            except Exception:
                continue
            if rows:
                source = url
                break
        exact = next((row for row in rows if cover.symbol_matches(
            row["symbol"], row["title"], ticker
        )), None)
        output.append({
            "ticker": ticker,
            "cik": cik,
            "accession": accession,
            "source": source,
            "rows": rows,
            "exact": exact,
            "exact_is_common_equity": (
                cover.is_common_equity_security(exact["title"]) if exact else False
            ),
            "exact_ratio": (
                str(cover.depositary_ratio(exact["title"]))
                if exact and cover.depositary_ratio(exact["title"]) is not None
                else None
            ),
        })
    path = Path(__file__).with_name("real-cover-output.json")
    path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {path}: {len(output)} cases")


if __name__ == "__main__":
    main()
