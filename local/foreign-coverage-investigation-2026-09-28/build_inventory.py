#!/usr/bin/env python3
"""Reproduce and classify every listed snapshot whose current status is foreign."""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path

from screener import evidence, normalize
from screener.sources.edgar import EdgarClient


ROOT = Path(__file__).resolve().parent
CACHE = Path.home() / ".cache" / "graham-screener"
DB = CACHE / "screener.db"

COVER_SUBCLASSES = {
    "empty_parser_failure": set("""
        AURE AZN AZUL BBVA BEP BSAC CCJ CLWT CNEY CRESY DAVA DOX DPU DSGX DSX EHLD EMA
        EPWKF FAMI FURY GGAL GLAS GLBS GNS ICON IMMP IRS KAZR LKNCY MCRP MOGU MUFG NA
        PAVS PLTYF PSHG RBNE SAN SGRX SUZ SXTC TANH TORO USAS USEA VOXR ZCMD
    """.split()),
    "symbol_mismatch": set("""
        AKO-A ATTT BRNX BVC EOCN GMEX GRSD HELP HERE LKFT LYG MF NIKI OGG ORIO PBK PLGO
        QTEX SLMT SVRN UZX VIVO YFOR ZTG
    """.split()),
    "wrong_security_class": set("""
        ATMP BIPH BPYPP CEF CIG GGB GLDI ITUB PHYS PSLV SPPP
    """.split()),
    "stale_otc_mapping": set("""
        ABLZF AMBIQ AMLIF ATEYY BETRF BGICF BHATF BRCNF CAJPY CASIF CHKIF CILJF CKDXF
        CPTAF EGLXF FNCTF FTRKF GRTUF GVHGF HTHIY ILLMF KYOCF MAXNQ MDNAF MMTZF NMPGY
        NTTYY ORISF PREJF PYRGF REEAF RMTHF SELXF SLAIY SNNRF SPTJF TARSF TCGLF TEFOF
        TIRXF TNCAF UOKAF VQSSF WILCF YGMZF ZTEKF
    """.split()),
    "no_registered_class": set("AGRZ ALMMF ARRKF ASAIY LYTHF RAJAF SNPMF".split()),
    "missing_raw_cover_evidence": set("""
        AHL-PD AIJTY ALRTF ALYAF AMUB AVCRF AVLNF BRQSF CRLBF CSCIF CWLXF DAZSD EGG
        EGMCF EHVVF ERLFF FFMGF FRFHF GDRZF GEBRF GIGGF GLOP-PA GNOLF GNTOF GRVT
        HAMVF HMELF HRNNF ICTSF ITMSF KDOZF KIQSF LINMF LVRLF MMTIF MTLK NSFDF NYXH
        PCCYF PHOS QZMRF RLNDF RTCJF SEAL-PA SGBAF SPOWF STNDF TAC TANAF TCPA TGB TLIH
        TRTN-PA UBS WEBNF XTGRF XTXXF
    """.split()),
}

RATIO_SUBCLASSES = {
    "ratio_present_in_cover_title": set("""
        ADAG CDLR ERIC FMX GMAB GOTU HDB JG KOF NCNA NVO POM RELX RERE SNY SOGP SY WKEY
    """.split()),
    "primary_document_footnote_or_prose": set("FMS ING PSNY SSL VLRS WAVE".split()),
    "tagged_dei_ratio_fact": set(),
    "wrong_class_pairing": {"BBD"},
    "no_current_filing_backed_ratio": {"BMA", "WDS"},
}

RATIO_VALUES = {
    "ADAG": "1.25", "BBD": "1 preferred share", "CDLR": "4", "ERIC": "1",
    "FMS": "0.5", "FMX": "10 BD units", "GMAB": "0.1", "GOTU": "2/3",
    "HDB": "3", "ING": "1", "JG": "40/3", "KOF": "10 units", "NCNA": "5000",
    "NVO": "1", "POM": "1/6", "PSNY": "1", "RELX": "1", "RERE": "2/3",
    "SNY": "0.5", "SOGP": "200", "SSL": "1", "SY": "10/13", "VLRS": "10 CPOs",
    "WAVE": "8", "WKEY": "0.5",
}

RATIO_SOURCE_URLS = {
    "ADAG": "https://www.sec.gov/Archives/edgar/data/1818838/000110465926038612/adag-20251231x20f.htm",
    "BBD": "https://www.sec.gov/Archives/edgar/data/1160330/000129281426001753/bbdform20f_2025.htm",
    "BMA": "https://www.sec.gov/Archives/edgar/data/1347426/000119312525087225/d865064d20f.htm",
    "CDLR": "https://www.sec.gov/Archives/edgar/data/1978867/000197886726000022/cdlr-20251231.htm",
    "ERIC": "https://www.sec.gov/Archives/edgar/data/717826/000119312526104149/d948057d20f.htm",
    "FMS": "https://www.sec.gov/Archives/edgar/data/1333141/000110465926019005/fms-20251231x20f.htm",
    "FMX": "https://www.sec.gov/Archives/edgar/data/1061736/000162828025019714/fmx-20241231.htm",
    "GMAB": "https://www.sec.gov/Archives/edgar/data/1434265/000143426526000014/gmab-20251231_d2.htm",
    "GOTU": "https://www.sec.gov/Archives/edgar/data/1768259/000119312526168211/gotu-20251231.htm",
    "HDB": "https://www.sec.gov/Archives/edgar/data/1144967/000119312525158722/d854075d20f.htm",
    "ING": "https://www.sec.gov/Archives/edgar/data/1039765/000162828026011979/ing-20251231.htm",
    "JG": "https://www.sec.gov/Archives/edgar/data/1737339/000110465926035657/jg-20251231x20f.htm",
    "KOF": "https://www.sec.gov/Archives/edgar/data/910631/000162828025017225/kof-20241231.htm",
    "NCNA": "https://www.sec.gov/Archives/edgar/data/1709626/000119312526116030/ncna-20251231.htm",
    "NVO": "https://www.sec.gov/Archives/edgar/data/353278/000035327826000012/nvo-20251231.htm",
    "POM": "https://www.sec.gov/Archives/edgar/data/1877971/000121390026056576/ea0286069-20f_pomdoc.htm",
    "PSNY": "https://www.sec.gov/Archives/edgar/data/1884082/000188408225000012/psny-20241231.htm",
    "RELX": "https://www.sec.gov/Archives/edgar/data/929869/000110465926017277/relx-20251231x20f.htm",
    "RERE": "https://www.sec.gov/Archives/edgar/data/1838957/000119312526146260/rere-20251231.htm",
    "SNY": "https://www.sec.gov/Archives/edgar/data/1121404/000162828026008403/sny-20251231.htm",
    "SOGP": "https://www.sec.gov/Archives/edgar/data/1783407/000149315226019716/form20-f.htm",
    "SSL": "https://www.sec.gov/Archives/edgar/data/314590/000141057825001910/ssl-20250630x20f.htm",
    "SY": "https://www.sec.gov/Archives/edgar/data/1758530/000110465926047129/sy-20251231x20f.htm",
    "VLRS": "https://www.sec.gov/Archives/edgar/data/1520504/000129281425001765/vlrsform20f_2024.htm",
    "WAVE": "https://www.sec.gov/Archives/edgar/data/1846715/000121390026026655/ea0280691-20f_ecowave.htm",
    "WDS": "https://www.sec.gov/Archives/edgar/data/844551/000162828026010858/wds-20251231.htm",
    "WKEY": "https://www.sec.gov/Archives/edgar/data/1738699/000110465926053033/tmb-20251231x20f.htm",
}


def only_subclass(ticker: str, groups: dict[str, set[str]]) -> str:
    found = [name for name, tickers in groups.items() if ticker in tickers]
    assert len(found) == 1, (ticker, found)
    return found[0]


def current_filing_detail(facts: dict, newest: tuple[str, str] | None) -> dict:
    if newest is None:
        return {}
    detail = {}
    for namespace, tags in facts.items():
        current_tags = set()
        entries = 0
        units = Counter()
        for tag, body in tags.items():
            for unit, rows in (body.get("units") or {}).items():
                matches = [row for row in rows if (row.get("filed"), row.get("accn", "")) == newest]
                if matches:
                    current_tags.add(tag)
                    entries += len(matches)
                    units[unit] += len(matches)
        if entries:
            detail[namespace] = {
                "tag_count": len(current_tags),
                "entry_count": entries,
                "units": dict(sorted(units.items())),
            }
    return detail


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    loader = evidence.EvidenceLoader(conn, EdgarClient(cache_dir=CACHE))
    db_rows = conn.execute(
        """SELECT c.cik, c.ticker, c.name, c.exchange, c.listed,
                  s.engine_version, s.computed_at
             FROM snapshot s JOIN company c USING (cik)
            WHERE s.status = 'foreign' AND c.listed = 'y' AND c.ticker IS NOT NULL
            ORDER BY c.ticker"""
    ).fetchall()

    rows = []
    for db_row in db_rows:
        ticker = db_row["ticker"]
        facts_doc = json.loads((CACHE / f"companyfacts_{db_row['cik']}.json").read_text())
        facts = facts_doc.get("facts") or {}
        newest, basis = normalize._current_supported_foreign_annual(facts)
        _, receipt = loader.identity(db_row["cik"], ticker)
        try:
            normalize.build_snapshot(ticker, db_row["cik"], facts_doc, receipt=receipt)
        except Exception as exc:  # exact observed normalization outcome is the inventory subject
            exception = f"{type(exc).__name__}: {exc}"
        else:
            exception = "NO_EXCEPTION"

        raw_covers = [dict(row) for row in conn.execute(
            "SELECT symbol, accn, title, ratio, read_at FROM security_cover WHERE cik = ? ORDER BY symbol",
            (db_row["cik"],),
        )]
        exact_raw = next((row for row in raw_covers if row["symbol"] == ticker), None)
        filed, accession = newest or (None, None)
        archive = (
            f"https://www.sec.gov/Archives/edgar/data/{int(db_row['cik'])}/"
            f"{accession.replace('-', '')}" if accession else None
        )

        if "no exact filing-cover security title" in exception:
            top_group = "cover_identity"
            subclass = only_subclass(ticker, COVER_SUBCLASSES)
            source_location = f"{archive}/R1.htm through R3.htm" if archive else None
        elif "no resolved positive cover-page ratio" in exception:
            top_group = "depositary_ratio"
            subclass = only_subclass(ticker, RATIO_SUBCLASSES)
            source_location = RATIO_SOURCE_URLS[ticker]
        elif "coherent standard US-GAAP or IFRS" in exception:
            top_group = "statement_coherence"
            subclass = "incomplete_company_facts"
            source_location = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{db_row['cik']}.json"
        else:
            raise AssertionError((ticker, exception))

        currency = None
        if newest and basis:
            anchors = (normalize._FOREIGN_BALANCE_ANCHORS if basis == "us-gaap"
                       else normalize._IFRS_BALANCE_ANCHORS)
            currency = normalize._balance_currency_in_filing(facts.get(basis, {}), newest, anchors)

        rows.append({
            "ticker": ticker,
            "cik": db_row["cik"],
            "company": db_row["name"],
            "exchange": db_row["exchange"],
            "engine_version": db_row["engine_version"],
            "computed_at": db_row["computed_at"],
            "annual_filed": filed,
            "annual_accession": accession,
            "annual_basis": basis,
            "reporting_currency": currency,
            "selected_cover": receipt,
            "exact_raw_cover": exact_raw,
            "all_raw_cover_rows": raw_covers,
            "exception": exception,
            "top_group": top_group,
            "subclass": subclass,
            "ratio_evidence_value": RATIO_VALUES.get(ticker),
            "source_location": source_location,
            "current_filing_fact_detail": current_filing_detail(facts, newest),
        })

    assert len(rows) == 237
    assert Counter(row["top_group"] for row in rows) == {
        "cover_identity": 192, "depositary_ratio": 27, "statement_coherence": 18,
    }
    assert sum(map(len, COVER_SUBCLASSES.values())) == 192
    assert len(set().union(*COVER_SUBCLASSES.values())) == 192
    assert sum(map(len, RATIO_SUBCLASSES.values())) == 27
    assert len(set().union(*RATIO_SUBCLASSES.values())) == 27

    (ROOT / "inventory.json").write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")
    fields = [
        "ticker", "cik", "company", "exchange", "engine_version", "computed_at",
        "annual_filed", "annual_accession", "annual_basis", "reporting_currency",
        "top_group", "subclass", "ratio_evidence_value", "exception", "source_location",
    ]
    with (ROOT / "inventory.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print("rows=237 cover_identity=192 depositary_ratio=27 statement_coherence=18")
    for group, groups in (("cover", COVER_SUBCLASSES), ("ratio", RATIO_SUBCLASSES)):
        for name, tickers in groups.items():
            print(f"{group}.{name}={len(tickers)}")


if __name__ == "__main__":
    main()
