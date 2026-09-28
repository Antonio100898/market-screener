"""Fetch fixed SEC filings and record direct depositary-ratio parser output."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from screener.sources import cover
from screener.sources.edgar import EdgarClient


REPORT_CASES = {
    "CDLR": ("0001978867", "0001978867-26-000022", "4"),
    "ERIC": ("0000717826", "0001193125-26-104149", "1"),
    "GMAB": ("0001434265", "0001434265-26-000014", "0.1"),
    "GOTU": ("0001768259", "0001193125-26-168211", "0.6666666666666666666666666667"),
    "HDB": ("0001144967", "0001193125-25-158722", "3"),
    "JG": ("0001737339", "0001104659-26-035657", "13.33333333333333333333333333"),
    "NCNA": ("0001709626", "0001193125-26-116030", "5000"),
    "NVO": ("0000353278", "0000353278-26-000012", "1"),
    "POM": ("0001877971", "0001213900-26-056576", "0.1666666666666666666666666667"),
    "RELX": ("0000929869", "0001104659-26-017277", "1"),
    "RERE": ("0001838957", "0001193125-26-146260", "0.6666666666666666666666666667"),
    "SNY": ("0001121404", "0001628280-26-008403", "0.5"),
    "SOGP": ("0001783407", "0001493152-26-019716", "200"),
    "SUZ": ("0000909327", "0000909327-26-000045", "1"),
    "WKEY": ("0001738699", "0001104659-26-053033", "0.5"),
}

PRIMARY_CASES = {
    "ADAG": ("0001818838", "0001104659-26-038612", "adag-20251231x20f.htm", None),
    "BBD": ("0001160330", "0001292814-26-001753", "bbdform20f_2025.htm", None),
    "BMA": ("0001347426", "0001193125-25-087225", "d865064d20f.htm", None),
    "FMS": ("0001333141", "0001104659-26-019005", "fms-20251231x20f.htm", None),
    "FMX": ("0001061736", "0001628280-25-019714", "fmx-20241231.htm", None),
    "ING": ("0001039765", "0001628280-26-011979", "ing-20251231.htm", "1"),
    "KOF": ("0000910631", "0001628280-25-017225", "kof-20241231.htm", None),
    "PSNY": ("0001884082", "0001884082-25-000012", "psny-20241231.htm", "1"),
    "SSL": ("0000314590", "0001410578-25-001910", "ssl-20250630x20f.htm", "1"),
    "SY": ("0001758530", "0001104659-26-047129", "sy-20251231x20f.htm", "0.7692307692307692307692307692"),
    "VLRS": ("0001520504", "0001292814-25-001765", "vlrsform20f_2024.htm", None),
    "WAVE": ("0001846715", "0001213900-26-026655", "ea0280691-20f_ecowave.htm", None),
    "WDS": ("0000844551", "0001628280-26-010858", "wds-20251231.htm", None),
}


def _record(ticker, cik, accession, source, raw, title, ratio, expected):
    actual = str(ratio) if ratio is not None else None
    if actual != expected:
        raise AssertionError(f"{ticker}: expected {expected}, got {actual}")
    return {
        "ticker": ticker,
        "cik": cik,
        "accession": accession,
        "source": source,
        "byte_size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "title": title,
        "ratio": actual,
    }


def main() -> None:
    edgar = EdgarClient()
    output = []
    for ticker, (cik, accession, expected) in REPORT_CASES.items():
        source = cover.R_URL.format(
            cik=int(cik), accn=accession.replace("-", ""), n=1
        )
        raw = edgar._request(source).content
        rows = cover.securities(raw.decode("utf-8", errors="replace"))
        exact = [
            row for row in rows
            if cover.symbol_matches(row["symbol"], row["title"], ticker)
        ]
        if len(exact) != 1:
            raise AssertionError(f"{ticker}: expected one exact R-report class")
        title = exact[0]["title"]
        output.append(_record(
            ticker,
            cik,
            accession,
            source,
            raw,
            title,
            cover.depositary_ratio(title),
            expected,
        ))

    for ticker, (cik, accession, document, expected) in PRIMARY_CASES.items():
        source = (
            "https://www.sec.gov/Archives/edgar/data/"
            f"{int(cik)}/{accession.replace('-', '')}/{document}"
        )
        raw = edgar._request(source).content
        output.append(_record(
            ticker,
            cik,
            accession,
            source,
            raw,
            None,
            cover.depositary_ratio(
                cover.text_of(raw.decode("utf-8", errors="replace"))
            ),
            expected,
        ))

    recovered = [row["ticker"] for row in output if row["ratio"] is not None]
    if len(recovered) != 19:
        raise AssertionError(f"expected 19 current recoveries, got {len(recovered)}")
    path = Path(__file__).with_name("real-ratio-output.json")
    path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {path}: {len(output)} cases, {len(recovered)} direct recoveries")


if __name__ == "__main__":
    main()
