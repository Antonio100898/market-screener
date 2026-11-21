"""SEC Insider Transactions Data Sets — Forms 3, 4 and 5 as quarterly tables.

Each director's and officer's current holding is the common stock their latest
ownership filing reports after its last transaction. SEC never records when a
person leaves, so the caller judges staleness from the filing dates kept here.

Layer 1 only: this fetches and reshapes one person's latest filing per issuer.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

PAGE_URL = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
FIRST_QUARTER = "2016q1"
STALE_AFTER = timedelta(days=730)
# Non-derivative rows also carry preferred stock, units and depositary receipts;
# only the issuer's common or ordinary shares compare with shares outstanding.
_COMMON = re.compile(r"common|ordinary|capital stock", re.I)
_NOT_COMMON = re.compile(r"preferred|unit|depositary", re.I)
_QUARTER_ZIP = re.compile(r'href="([^"]*/(\d{4}q[1-4])_form345\.zip)"')


def quarter_urls(user_agent: str, first: str = FIRST_QUARTER) -> dict[str, str]:
    """Published quarters, read from SEC's own index: the path has moved before."""
    page = httpx.get(PAGE_URL, headers={"User-Agent": user_agent},
                     timeout=30, follow_redirects=True)
    page.raise_for_status()
    return {quarter: f"https://www.sec.gov{href}" if href.startswith("/") else href
            for href, quarter in _QUARTER_ZIP.findall(page.text) if quarter >= first}


def download(quarter: str, url: str, cache_dir: Path, user_agent: str) -> Path:
    """One quarter's zip, cached; a published quarter does not change."""
    path = cache_dir / f"insiders_{quarter}.zip"
    if path.exists() and path.stat().st_size > 0:
        return path
    tmp = path.with_suffix(".part")
    with httpx.stream("GET", url, headers={"User-Agent": user_agent},
                      timeout=None, follow_redirects=True) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)
    tmp.replace(path)
    return path


def _rows(bundle: zipfile.ZipFile, name: str):
    with bundle.open(name) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, "utf-8", errors="replace"),
                                  delimiter="\t", quoting=csv.QUOTE_NONE)


def _iso(text: str) -> str:
    return datetime.strptime(text, "%d-%b-%Y").date().isoformat()


def _shares(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def fold(zip_path: Path, issuers: set[str], latest: dict) -> None:
    """Merge one quarter into `latest`, keyed (issuer CIK, owner CIK).

    Each entry keeps the person's newest filing (role, title, date) and, apart,
    the newest filing that reported their common stock — a Form 4 that only
    grants options says nothing about the shares they hold.
    """
    with zipfile.ZipFile(zip_path) as bundle:
        filings = {}
        for r in _rows(bundle, "SUBMISSION.tsv"):
            if r["ISSUERCIK"] in issuers:
                filings[r["ACCESSION_NUMBER"]] = {
                    "issuer": r["ISSUERCIK"], "filed": _iso(r["FILING_DATE"]),
                    "form": r["DOCUMENT_TYPE"], "positions": {},
                    "none_owned": r["NO_SECURITIES_OWNED"] in ("1", "true")}
        # Holdings first, then transactions in filing order: for one position the
        # amount after the last transaction is what the person holds now.
        trans = sorted((r for r in _rows(bundle, "NONDERIV_TRANS.tsv")
                        if r["ACCESSION_NUMBER"] in filings),
                       key=lambda r: (_iso(r["TRANS_DATE"]) if r["TRANS_DATE"] else "",
                                      int(r["NONDERIV_TRANS_SK"] or 0)))
        for r in [*_rows(bundle, "NONDERIV_HOLDING.tsv"), *trans]:
            filing = filings.get(r["ACCESSION_NUMBER"])
            title = r["SECURITY_TITLE"]
            if filing is None or not _COMMON.search(title) or _NOT_COMMON.search(title):
                continue
            key = (title.strip().lower(), r["DIRECT_INDIRECT_OWNERSHIP"],
                   r["NATURE_OF_OWNERSHIP"].strip().lower())
            filing["positions"][key] = _shares(r["SHRS_OWND_FOLWNG_TRANS"])
        for r in _rows(bundle, "REPORTINGOWNER.tsv"):
            filing = filings.get(accn := r["ACCESSION_NUMBER"])
            if filing is None:
                continue
            entry = latest.setdefault((filing["issuer"], r["RPTOWNERCIK"]), {})
            order = (filing["filed"], accn)
            if order >= entry.get("last_order", ("",)):
                entry.update(last_order=order, last_filed=filing["filed"],
                             name=r["RPTOWNERNAME"], title=r["RPTOWNER_TITLE"] or None,
                             relationship=r["RPTOWNER_RELATIONSHIP"])
            if ((filing["positions"] or filing["none_owned"])
                    and order >= entry.get("holding_order", ("",))):
                entry.update(holding_order=order, filed=filing["filed"], accn=accn,
                             form=filing["form"], **_holding(filing["positions"]))


def _holding(positions: dict) -> dict:
    """Direct and indirect positions are summed; the indirect part is stated.
    A position reported without a share amount leaves the holding unknown."""
    if any(v is None for v in positions.values()):
        return {"shares": None, "indirect_shares": None}
    indirect = sum(v for (_, kind, _), v in positions.items() if kind == "I")
    return {"shares": sum(positions.values()), "indirect_shares": indirect}


def management(latest: dict) -> list[dict]:
    """Directors and officers only; pure ten-percent owners are not management."""
    out = []
    for (issuer, owner), e in latest.items():
        roles = set((e.get("relationship") or "").split(","))
        if not roles & {"Director", "Officer"}:
            continue
        title = e["title"] or ("Director" if "Director" in roles else "Officer")
        out.append({"cik": issuer, "source": "SEC", "person": owner, "name": e["name"],
                    "title": title,
                    "shares": e.get("shares"), "indirect_shares": e.get("indirect_shares"),
                    "filed": e.get("filed"), "accn": e.get("accn"), "form": e.get("form"),
                    "last_filed": e["last_filed"]})
    return out


def summary(people: list[dict], shares_outstanding: float | None,
            today: date | None = None) -> dict | None:
    """The company-page record: counted people, possibly former people, total.

    A US person whose latest filing is over two years old may have left, so is
    listed apart and not counted. An unknown holding is never added as zero:
    the total covers known holdings and says how many are unknown.

    SEC filings report the same shares more than once: a joint filing lists
    one set of shares for all its owners, and several insiders each report
    the block an entity they share holds (JBS at Pilgrim's Pride) as indirect.
    So a filing's direct shares are added once and an indirect block of the
    same size is added once. An EDINET report states each person's own holding.
    A total above shares outstanding still overlaps, or predates a share split,
    so its percent is withheld rather than shown.
    """
    if not people:
        return None
    shown = ("name", "title", "shares", "indirect_shares", "filed", "accn", "form",
             "last_filed")
    cutoff = ((today or date.today()) - STALE_AFTER).isoformat()
    current, former = [], []
    for p in sorted(people, key=lambda p: (-(p["shares"] or 0), p["name"])):
        stale = p["source"] == "SEC" and p["last_filed"] < cutoff
        (former if stale else current).append(p)
    counted = {}
    for p in current:
        if p["shares"] is None:
            continue
        if p["source"] != "SEC":
            counted[(p["accn"], p["person"])] = p["shares"]
            continue
        counted[p["accn"]] = p["shares"] - p["indirect_shares"]
        if p["indirect_shares"]:
            counted[("indirect", p["indirect_shares"])] = p["indirect_shares"]
    total = sum(counted.values()) if counted else None
    percent = (100 * total / shares_outstanding
               if total is not None and shares_outstanding else None)
    return {
        "source": people[0]["source"],
        "people": [{k: p[k] for k in shown} for p in current],
        "possibly_former": [{k: p[k] for k in shown} for p in former],
        "total_shares": total,
        "unknown_holdings": sum(p["shares"] is None for p in current),
        "percent_of_outstanding": percent if percent is None or percent <= 100 else None,
        "percent_withheld": percent is not None and percent > 100,
    }
