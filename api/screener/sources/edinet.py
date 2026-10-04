"""EDINET filing index and XBRL facts from Japan's Financial Services Agency."""
from __future__ import annotations

import io
import json
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx


API_ROOT = "https://api.edinet-fsa.go.jp/api/v2"
_DOCUMENT_ID = re.compile(r"S[0-9A-Z]{7}")
_XBRLI = "{http://www.xbrl.org/2003/instance}"
_XBRLDI = "{http://xbrl.org/2006/xbrldi}"
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


class EdinetError(Exception):
    pass


class EdinetHttpError(EdinetError):
    def __init__(self, status_code: int, path: str, url: str):
        self.status_code = status_code
        self.url = url
        super().__init__(f"EDINET returned HTTP {status_code} for {path}")


@dataclass(frozen=True)
class EdinetTransportResult:
    data: bytes
    url: str
    media_type: str | None
    etag: str | None
    last_modified: str | None


class EdinetClient:
    def __init__(self, api_key: str | None = None, http: httpx.Client | None = None):
        self.api_key = api_key or os.environ.get("EDINET_API_KEY")
        self.http = http or httpx.Client(timeout=45, follow_redirects=True)

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
            raise EdinetHttpError(
                response.status_code,
                path,
                str(response.request.url.copy_remove_param("Subscription-Key")),
            )
        return response

    def documents_on(self, filing_date: str) -> list[dict]:
        result = self.fetch_documents_on(filing_date)
        payload = json.loads(result.data)
        if payload.get("metadata", {}).get("status") != "200":
            raise EdinetError(f"EDINET list unavailable for {filing_date}")
        return payload.get("results") or []

    def fetch_documents_on(self, filing_date: str) -> EdinetTransportResult:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", filing_date):
            raise ValueError("filing_date must be YYYY-MM-DD")
        response = self._get("documents.json", date=filing_date, type=2)
        return EdinetTransportResult(
            response.content,
            f"{API_ROOT}/documents.json?date={filing_date}&type=2",
            response.headers.get("content-type"),
            response.headers.get("etag"),
            response.headers.get("last-modified"),
        )

    def xbrl_archive(self, document_id: str) -> bytes:
        return self.fetch_xbrl_archive(document_id).data

    def fetch_xbrl_archive(self, document_id: str) -> EdinetTransportResult:
        if not _DOCUMENT_ID.fullmatch(document_id):
            raise ValueError("invalid EDINET document ID")
        response = self._get(f"documents/{document_id}", type=1)
        return EdinetTransportResult(
            response.content,
            f"{API_ROOT}/documents/{document_id}?type=1",
            response.headers.get("content-type"),
            response.headers.get("etag"),
            response.headers.get("last-modified"),
        )


def annual_filings(rows: list[dict]) -> list[dict]:
    """Filed annual reports and corrections with an XBRL primary document."""
    return [row for row in rows
            if row.get("docTypeCode") in {"120", "130"} and row.get("xbrlFlag") == "1"
            and row.get("edinetCode") and row.get("secCode")]


def validate_xbrl_archive(archive: bytes, document_id: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            bad_member = bundle.testzip()
    except zipfile.BadZipFile:
        raise EdinetError(f"{document_id} is not an EDINET XBRL archive") from None
    if bad_member is not None:
        raise EdinetError(
            f"{document_id} has a corrupt archive member: {bad_member}"
        )


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
