"""EDINET filing index and XBRL facts from Japan's Financial Services Agency."""
from __future__ import annotations

import io
import json
import os
import re
import hashlib
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, timedelta

import httpx


API_ROOT = "https://api.edinet-fsa.go.jp/api/v2"
_DOCUMENT_ID = re.compile(r"S[0-9A-Z]{7}")
_XBRLI = "{http://www.xbrl.org/2003/instance}"
_XBRLDI = "{http://xbrl.org/2006/xbrldi}"
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_XBRL_INPUT_LIMIT = 16 * 1024 * 1024
_ARCHIVE_EXPANSION_LIMIT = 32 * 1024 * 1024
_ARCHIVE_MEMBER_LIMIT = 4096


class EdinetError(Exception):
    pass


class EdinetInputCeilingError(EdinetError):
    """Retained input exceeds a supported finite processing ceiling."""


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
    if len(archive) > _XBRL_INPUT_LIMIT:
        raise EdinetInputCeilingError("EDINET archive exceeds the 16 MiB source ceiling")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            _archive_members(bundle)
            bad_member = bundle.testzip()
    except zipfile.BadZipFile:
        raise EdinetError(f"{document_id} is not an EDINET XBRL archive") from None
    if bad_member is not None:
        raise EdinetError(
            f"{document_id} has a corrupt archive member: {bad_member}"
        )


def _archive_members(bundle: zipfile.ZipFile):
    """Reject ambiguous identities and excessive declared work before expansion."""
    infos = bundle.infolist()
    if len({item.filename for item in infos}) != len(infos):
        raise EdinetError("EDINET duplicate archive member identities")
    if len(infos) > _ARCHIVE_MEMBER_LIMIT:
        raise EdinetInputCeilingError("EDINET archive exceeds the 4096 member source ceiling")
    if sum(item.file_size for item in infos) > _ARCHIVE_EXPANSION_LIMIT:
        raise EdinetInputCeilingError("EDINET archive expansion exceeds the 32 MiB aggregate source ceiling")
    for info in infos:
        if ".." in info.filename.split("/") or "\\" in info.filename or info.filename.startswith("/"):
            raise EdinetError("EDINET unsafe original member path")
    selected = [item for item in infos if item.filename.startswith("XBRL/PublicDoc/")
                and item.filename.endswith(".xbrl")]
    if sum(item.file_size for item in selected) > _XBRL_INPUT_LIMIT:
        raise EdinetInputCeilingError("EDINET PublicDoc expansion exceeds the 16 MiB source ceiling")
    return selected


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
           "entity_scheme": identifier.get("scheme") if identifier is not None else None,
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


def _instances(archive: bytes, document_id: str):
    """Read one bounded, unambiguous original PublicDoc source frontier."""
    from .inline_xbrl import _xml
    if len(archive) > _XBRL_INPUT_LIMIT:
        raise EdinetInputCeilingError("EDINET archive exceeds the 16 MiB source ceiling")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            selected = _archive_members(bundle)
            if not selected:
                raise EdinetError(f"{document_id} has no PublicDoc XBRL instance")
            for info in selected:
                if ".." in info.filename.split("/") or "\\" in info.filename:
                    raise EdinetError("EDINET unsafe original member path")
                with bundle.open(info) as stream:
                    content = stream.read(_XBRL_INPUT_LIMIT + 1)
                if len(content) != info.file_size or len(content) > _XBRL_INPUT_LIMIT:
                    raise EdinetError("EDINET bounded member read differs from declared size")
                if b"\0" in content or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", content, re.I):
                    raise EdinetError("EDINET unsupported XML declaration or encoding")
                root = _xml(content, "EDINET instance")
                if root.tag != f"{_XBRLI}xbrl":
                    raise EdinetError("EDINET original XBRL root required")
                ids = [node.get("id") for node in root if node.get("id") is not None]
                if len(ids) != len(set(ids)):
                    raise EdinetError("EDINET duplicate context, unit or element id")
                yield info.filename, content, root
    except zipfile.BadZipFile:
        raise EdinetError(f"{document_id} is not an EDINET XBRL archive") from None


def xbrl_facts(archive: bytes, document_id: str) -> list[dict]:
    """Keep numeric facts and unreadable debt disclosures with exact source scope."""
    from ..normalize import debt_evidence_roles
    from .inline_xbrl import _native_context, _native_namespace_scopes, _native_qname
    facts = []
    for filename, content, root in _instances(archive, document_id):
        scopes = None
        context_nodes = {element.get("id"): element for element in root.findall(f"{_XBRLI}context")}
        unit_nodes = {element.get("id"): element for element in root.findall(f"{_XBRLI}unit")}
        contexts = {element.get("id"): _context(element)
                    for element in root.findall(f"{_XBRLI}context")}
        units = {element.get("id"): _unit(element)
                 for element in root.findall(f"{_XBRLI}unit")}
        for ordinal, element in enumerate(root):
            context = contexts.get(element.get("contextRef"))
            unit = units.get(element.get("unitRef"))
            if context is None or unit is None:
                continue
            nil = element.get(_XSI_NIL) in {"true", "1"}
            try:
                value = None if nil else Decimal(element.text or "")
            except InvalidOperation:
                value = None
            if value is None or not value.is_finite():
                if not debt_evidence_roles(element.tag.partition("}")[2]):
                    continue
                value = None
            source_qualification = {}
            if debt_evidence_roles(element.tag.partition("}")[2]):
                if scopes is None:
                    scopes = dict(zip(map(id, root.iter()), _native_namespace_scopes(content), strict=True))
                native_context = _native_context(context_nodes[element.get("contextRef")], scopes)
                unit_node = unit_nodes[element.get("unitRef")]
                measures = unit_node.findall(f"{_XBRLI}measure")
                source_qualification = {
                    "native_context": native_context,
                    "unit_measures": (tuple(_native_qname(node.text or "", scopes[id(node)]) for node in measures)
                                      if len(unit_node) == 1 and len(measures) == 1 and not list(measures[0]) else ()),
                }
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
                "context_ref": context["id"],
                "entity_scheme": context["entity_scheme"],
                "element_ordinal": ordinal,
                "fact_qname": element.tag,
                "unit_ref": element.get("unitRef"),
                "source_lexical": element.text or "",
                "nil": nil,
                **source_qualification,
            })
    return facts


def issuer_class_evidence(archive: bytes, document_id: str, entity_id: str) -> list:
    """Qualify exhaustive issuer class tables at their actual annual endpoints."""
    from ..models import IssuerClassEvidence, XmlTextBlockTableLocation
    from .statements import _Tables, _grid

    def normalized(text):
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))

    def count(text):
        value = normalized(text).replace(",", "")
        return int(value) if re.fullmatch(r"\d+", value) else None

    digest = hashlib.sha256(archive).hexdigest()
    result = []
    for filename, member, root in _instances(archive, document_id):
        contexts = {node.get("id"): _context(node) for node in root.findall(f"{_XBRLI}context")}
        for ordinal, node in enumerate(root):
            namespace, _, tag = node.tag.lstrip("{").partition("}")
            if (not namespace.rstrip("/").endswith("/jpcrp_cor") or tag not in {
                    "IssuedSharesTotalNumberOfSharesEtcTextBlock",
                    "NotesRegardingIssuedSharesAndTreasurySharesTextBlock"}):
                continue
            context = contexts.get(node.get("contextRef"))
            if (not context or context["entity"] != f"{entity_id}-000"
                    or context["entity_scheme"] != "http://disclosure.edinet-fsa.go.jp"
                    or context["dimensions"] or not context["end"]):
                continue
            parser = _Tables()
            parser.feed(node.text or "")
            for frame in sorted(parser.tables, key=lambda table: table["position"]):
                grid = _grid(frame["rows"])
                if not grid:
                    continue
                header = grid[0]
                columns = {}
                for column, cell in header.items():
                    if column == 0:
                        continue
                    text = normalized(cell["text"])
                    if "株" not in text or any(scale in text for scale in ("千株", "万株")) or "議決権" in text:
                        continue
                    if tag == "IssuedSharesTotalNumberOfSharesEtcTextBlock":
                        matches = re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
                        if len(matches) == 1 and "事業年度末" in text:
                            columns[column] = date(*map(int, matches[0])).isoformat()
                    elif context["start"]:
                        if "期首" in text:
                            columns[column] = (date.fromisoformat(context["start"]) - timedelta(days=1)).isoformat()
                        elif "年度末" in text:
                            columns[column] = context["end"]
                if not columns:
                    continue
                active = (tag == "IssuedSharesTotalNumberOfSharesEtcTextBlock"
                          or "発行済株式" in normalized(frame["caption"]))
                rows = []
                for row_index, row in enumerate(grid[1:], 1):
                    label = row.get(0, {}).get("text", "")
                    short = normalized(label)
                    if short == "発行済株式":
                        active, rows = True, []
                        continue
                    if short == "自己株式":
                        active = False
                    if not active:
                        continue
                    if short in {"計", "合計"}:
                        for column, as_of in columns.items():
                            total = row.get(column, {}).get("text", "")
                            values = [(index, name, source.get(column, {}).get("text", ""))
                                      for index, name, source in rows]
                            numeric = [count(value) for _, _, value in values]
                            ordinary = [i for i, (_, name, _) in enumerate(values)
                                        if re.sub(r"\(注.*", "", normalized(name)) == "普通株式"]
                            known_classes = all(normalized(name).startswith("普通株式")
                                                or "優先株式" in normalized(name)
                                                for _, name, _ in values)
                            outcome = "UNRESOLVED"
                            if (values and len(ordinary) == 1 and known_classes
                                    and count(total) is not None and all(n is not None for n in numeric)
                                    and sum(numeric) == count(total)):
                                outcome = ("ORDINARY_ONLY" if all(n == 0 for i, n in enumerate(numeric)
                                            if i not in ordinary) else "PREFERRED_MONETARY_CLAIM_REQUIRED")
                            if values:
                                result.append(IssuerClassEvidence(
                                    digest, hashlib.sha256(member).hexdigest(), document_id, context["entity"], context["entity_scheme"],
                                    context["start"], context["end"], as_of, outcome,
                                    tuple((name, value) for _, name, value in values), total,
                                    header[column]["text"], XmlTextBlockTableLocation(
                                        filename, node.tag, ordinal, node.get("contextRef"),
                                        frame["position"], 0, tuple(index for index, _, _ in values),
                                        row_index, column)))
                        active, rows = False, []
                    elif short or any(cell["text"] for column, cell in row.items() if column):
                        rows.append((row_index, label, row))
    return result
