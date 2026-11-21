"""EDINET filing index and XBRL facts from Japan's Financial Services Agency."""
from __future__ import annotations

import io
import json
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx


API_ROOT = "https://api.edinet-fsa.go.jp/api/v2"
_DOCUMENT_ID = re.compile(r"S[0-9A-Z]{7}")
_XBRLI = "{http://www.xbrl.org/2003/instance}"
_XBRLDI = "{http://xbrl.org/2006/xbrldi}"
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_JST = timezone(timedelta(hours=9))


class EdinetError(Exception):
    pass


class EdinetClient:
    """Retains every filing list and archive it fetches, so a copy of the cache
    replays imports without the API key."""

    def __init__(self, api_key: str | None = None, http: httpx.Client | None = None,
                 cache_dir: Path | None = None):
        self.api_key = api_key or os.environ.get("EDINET_API_KEY")
        self.http = http or httpx.Client(timeout=45, follow_redirects=True)
        self.cache = Path(cache_dir or Path.home() / ".cache" / "graham-screener") / "edinet"

    def _get(self, path: str, **params) -> httpx.Response:
        if not self.api_key:
            raise EdinetError("EDINET_API_KEY is required")
        try:
            response = self.http.get(
                f"{API_ROOT}/{path}",
                params={**params, "Subscription-Key": self.api_key},
            )
        except httpx.RequestError as exc:
            # httpx exception URLs contain the subscription key.
            raise EdinetError(f"EDINET request failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise EdinetError(f"EDINET returned HTTP {response.status_code} for {path}")
        return response

    def documents_on(self, filing_date: str) -> list[dict]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", filing_date):
            raise ValueError("filing_date must be YYYY-MM-DD")
        path = self.cache / "lists" / f"{filing_date}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        payload = self._get("documents.json", date=filing_date, type=2).json()
        if payload.get("metadata", {}).get("status") != "200":
            raise EdinetError(f"EDINET list unavailable for {filing_date}")
        results = payload.get("results") or []
        # Today's list is still growing in Japan; only closed days are retained.
        if date.fromisoformat(filing_date) < datetime.now(_JST).date():
            _write(path, json.dumps(results, ensure_ascii=False).encode("utf-8"))
        return results

    def xbrl_archive(self, document_id: str) -> bytes:
        if not _DOCUMENT_ID.fullmatch(document_id):
            raise ValueError("invalid EDINET document ID")
        path = self.cache / f"{document_id}.zip"
        if path.exists():
            return path.read_bytes()
        content = self._get(f"documents/{document_id}", type=1).content
        if zipfile.is_zipfile(io.BytesIO(content)):
            _write(path, content)
        return content


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def annual_filings(rows: list[dict]) -> list[dict]:
    """Filed annual reports and corrections with an XBRL primary document."""
    return [row for row in rows
            if row.get("docTypeCode") in {"120", "130"} and row.get("xbrlFlag") == "1"
            and row.get("edinetCode") and row.get("secCode")]


def _unit(unit: ET.Element) -> str | None:
    measure = unit.find(f"{_XBRLI}measure")
    if measure is not None:
        return (measure.text or "").split(":")[-1]
    division = unit.find(f"{_XBRLI}divide")
    if division is None:
        return None
    numerator = division.find(f"{_XBRLI}unitNumerator/{_XBRLI}measure")
    denominator = division.find(f"{_XBRLI}unitDenominator/{_XBRLI}measure")
    if numerator is None or denominator is None:
        return None
    return f"{(numerator.text or '').split(':')[-1]}/{(denominator.text or '').split(':')[-1]}"


def _context(context: ET.Element) -> dict:
    period = context.find(f"{_XBRLI}period")
    identifier = context.find(f"{_XBRLI}entity/{_XBRLI}identifier")
    out = {"id": context.get("id"), "entity": identifier.text if identifier is not None else None,
           "start": None, "end": None, "dimensions": []}
    if period is not None:
        instant = period.find(f"{_XBRLI}instant")
        start = period.find(f"{_XBRLI}startDate")
        end = period.find(f"{_XBRLI}endDate")
        out["start"] = start.text if start is not None else None
        out["end"] = (instant.text if instant is not None else
                      end.text if end is not None else None)
    for member in context.findall(f".//{_XBRLDI}explicitMember"):
        out["dimensions"].append({"axis": member.get("dimension"),
                                  "member": (member.text or "").strip()})
    for member in context.findall(f".//{_XBRLDI}typedMember"):
        out["dimensions"].append({"axis": member.get("dimension"),
                                  "member": " ".join(member.itertext()).strip()})
    return out


def xbrl_facts(archive: bytes, document_id: str) -> list[dict]:
    """Keep numeric filing facts with their exact concept, period, unit and axes."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            names = [name for name in bundle.namelist()
                     if name.startswith("XBRL/PublicDoc/") and name.endswith(".xbrl")]
            if not names:
                raise EdinetError(f"{document_id} has no PublicDoc XBRL instance")
            instances = [(name, bundle.read(name)) for name in names]
    except zipfile.BadZipFile:
        raise EdinetError(f"{document_id} is not an EDINET XBRL archive") from None

    facts = []
    for filename, content in instances:
        root = ET.fromstring(content)
        contexts = {element.get("id"): _context(element)
                    for element in root.findall(f"{_XBRLI}context")}
        units = {element.get("id"): _unit(element)
                 for element in root.findall(f"{_XBRLI}unit")}
        for element in root:
            context = contexts.get(element.get("contextRef"))
            unit = units.get(element.get("unitRef"))
            if context is None or unit is None or element.get(_XSI_NIL) == "true":
                continue
            try:
                value = Decimal(element.text or "")
            except InvalidOperation:
                continue
            if not value.is_finite():
                continue
            facts.append({
                "namespace": element.tag.partition("}")[0].lstrip("{"),
                "tag": element.tag.partition("}")[2],
                "value": value,
                "unit": unit,
                "entity": context["entity"],
                "start": context["start"],
                "end": context["end"],
                "dimensions": context["dimensions"],
                "document_id": document_id,
                "source_file": filename,
            })
    return facts


def fetch(start: date, end: date, client: EdinetClient | None = None) -> int:
    """Retain every annual report filed in a date range; returns failures."""
    client = client or EdinetClient()
    failures = 0
    day = start
    while day <= end:
        try:
            records = annual_filings(client.documents_on(day.isoformat()))
        except EdinetError as exc:
            records, failures = [], failures + 1
            print(f"{day}: {exc}", flush=True)
        for record in records:
            try:
                client.xbrl_archive(record["docID"])
            except EdinetError as exc:
                failures += 1
                print(f"{record['docID']}: {exc}", flush=True)
        print(f"{day}: {len(records)} annual reports", flush=True)
        day += timedelta(days=1)
    return failures


if __name__ == "__main__":
    import sys

    raise SystemExit(1 if fetch(date.fromisoformat(sys.argv[1]),
                                date.fromisoformat(sys.argv[2])) else 0)
