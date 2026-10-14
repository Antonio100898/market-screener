"""Check every number the panel shows against the filing it claims to come from.

Re-running the engine proves nothing: a wrong rule is wrong twice. No extraction
or normalization rule is rerun as the audit oracle. Each figure is checked two
ways, independently of how it was produced:

  SOURCED   the value is really in the filing its own provenance names. The row
            says `total_assets` came from tag `Assets` in accession X for period
            ending Y; this opens raw Company Facts (or the SEC DERA sidecar for an
            exact dimensioned fact), finds that exact fact, and compares the
            number. Catches a value that drifted away from its recorded evidence.

  DERIVED   the figures computed from those facts are the arithmetic they claim.
            The current ratio is current assets over current liabilities and
            nothing else; book value per share is common equity over the share
            count. Written out here as one line of plain arithmetic per figure, so
            the check is obviously right by reading it.

A third class is reported but never failed: figures whose inputs the payload does
not carry (the trailing EPS composite, the ten-year statistics). They are listed as
UNCHECKED rather than silently counted as passing.

    python -m screener.audit                    # a spread of 20 companies
    python -m screener.audit --ticker KO,MSFT
    python -m screener.audit --sample 200 --quiet
    python -m screener.audit --all --quiet
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import unicodedata
import zipfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .normalize import _is_annual_form, _is_financial_form
from .sources import inline_xbrl, statements
from .sources.edgar import EdgarClient
from .sync import DASHBOARD_JSON, _fx_rate, _valuation_fx_record

# A displayed figure and the sources entry that must vouch for it. Only figures
# that come straight from one tagged fact appear here; anything summed or chosen
# between tags is checked as an identity below instead.
SOURCED = (
    ("total_assets", "total_assets"),
    ("current_assets", "current_assets"),
    ("current_liabilities", "current_liabilities"),
    ("total_liabilities", "total_liabilities"),
    ("goodwill", "goodwill"),
    ("intangibles", "intangibles"),
    ("shares", "shares"),
    ("options", "options"),
    ("noncontrolling_interest", "noncontrolling_interest"),
    ("operating_lease_liability", "operating_lease_liability"),
    ("lease_cost", "lease_cost"),
    ("asset_quality.inventory", "inventory"),
    ("asset_quality.receivables", "receivables"),
    ("asset_quality.cash", "cash"),
    ("asset_quality.short_term_investments", "short_term_investments"),
)
TOLERANCE = 0.005          # a rounded display figure, not a different number


def _facts(cik: str, cache: Path) -> dict:
    path = cache / f"companyfacts_{cik}.json"
    facts = json.loads(path.read_text()) if path.exists() else {"facts": {}}
    if re.fullmatch(r"E\d{5}", cik):
        facts["_audit_edinet"] = {"issuer": cik, "cache": cache, "documents": {}, "sources": []}
    # Company Facts omits dimensioned observations. The independent SEC DERA
    # sidecar retains them, so an exact share-class source can still be checked
    # against accession, period, unit and dimension instead of being waived.
    sidecar = cache / f"dimensioned_{cik}.json"
    if not sidecar.exists():
        return facts
    try:
        dimensioned = json.loads(sidecar.read_text())
    except ValueError:
        return facts
    for namespace, tags in (dimensioned.get("facts") or {}).items():
        for tag, data in tags.items():
            for unit, entries in (data.get("units") or {}).items():
                target = (facts.setdefault("facts", {}).setdefault(namespace, {})
                          .setdefault(tag, {"units": {}}).setdefault("units", {})
                          .setdefault(unit, []))
                target.extend(entries)
    return facts


# These are source accounting meanings, not a copy of the mapper's alias table.
_EDINET_CASH_PAYMENTS = {
    "PurchaseOfPropertyPlantAndEquipmentInvCF": "PaymentsToAcquirePropertyPlantAndEquipment",
    "PurchaseOfPropertyPlantAndEquipmentInvCFIFRS": "PaymentsToAcquirePropertyPlantAndEquipment",
    "PurchaseOfIntangibleAssetsInvCF": "PaymentsToAcquireIntangibleAssets",
    "PurchaseOfIntangibleAssetsInvCFIFRS": "PaymentsToAcquireIntangibleAssets",
    "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCF": "PaymentsToAcquireProductiveAssets",
    "CashDividendsPaidFinCF": "PaymentsOfOrdinaryDividends",
    "DividendsPaidToOwnersOfParentFinCFIFRS": "PaymentsOfDividendsCommonStock",
}
# ponytail: 16 MiB inputs and one parsed document; stream native traversal if larger filings are needed.
_EDINET_AUDIT_INPUT_LIMIT = 16 * 1024 * 1024


def _edinet_source_values(facts: dict, source: dict) -> list[float]:
    """Check the named raw occurrence, without rebuilding canonical facts."""
    state = facts.get("_audit_edinet")
    if state is None:
        return []
    document = source.get("document")
    try:
        if (not isinstance(document, str) or not re.fullmatch(r"S[0-9A-Z]+", document)
                or document != source.get("accn") or source.get("form") not in {"JP-AR", "JP-AR/A"}
                or source.get("segments")):
            raise ValueError("no exact supported EDINET document/scope")
        start = date.fromisoformat(source["start"]) if source.get("start") else None
        end = date.fromisoformat(source["end"])
        unit = source.get("unit") or ""
        if not re.fullmatch(r"[A-Z]{3}(?:/shares)?|shares", unit):
            raise ValueError("no exact EDINET unit")
        namespace, tag = _source_qname(source.get("tag") or "")
        if namespace not in {"jppfs_cor", "jpigp_cor", "jpcrp_cor"}:
            raise ValueError("unsupported EDINET source taxonomy")
        if document not in state["documents"]:
            state["documents"].clear()
            archive_path = state["cache"] / "edinet" / (document + ".zip")
            with archive_path.open("rb") as stream:
                archive = stream.read(_EDINET_AUDIT_INPUT_LIMIT + 1)
            if len(archive) > _EDINET_AUDIT_INPUT_LIMIT:
                raise ValueError("EDINET compressed input exceeds 16 MiB audit ceiling")
            members = []
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                names = [item.filename for item in bundle.infolist()]
                if len(names) > 4096 or len(set(names)) != len(names):
                    raise ValueError("duplicate archive member identity")
                selected = [item for item in bundle.infolist()
                            if item.filename.startswith("XBRL/PublicDoc/") and item.filename.endswith(".xbrl")]
                if sum(item.file_size for item in selected) > _EDINET_AUDIT_INPUT_LIMIT:
                    raise ValueError("EDINET PublicDoc expansion exceeds 16 MiB aggregate audit ceiling")
                for info in selected:
                    name = info.filename
                    if ".." in name.split("/") or "\\" in name:
                        raise ValueError("unsafe original archive member path")
                    with bundle.open(info) as stream:
                        payload = stream.read(_EDINET_AUDIT_INPUT_LIMIT + 1)
                    if len(payload) != info.file_size or len(payload) > _EDINET_AUDIT_INPUT_LIMIT:
                        raise ValueError("EDINET bounded member read differs from declared size")
                    if b"\0" in payload or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", payload, re.I):
                        raise ValueError("unsupported native XML declaration")
                    root = inline_xbrl._xml(payload, "EDINET audit instance")
                    if root.tag != "{http://www.xbrl.org/2003/instance}xbrl":
                        raise ValueError("native XBRL instance required")
                    scopes = inline_xbrl._native_namespace_scopes(payload)
                    namespaces = dict(zip(map(id, root.iter()), scopes, strict=True))
                    identities = [node.get("id") for node in root if node.get("id") is not None]
                    if len(identities) != len(set(identities)):
                        raise ValueError("duplicate original XML identity")
                    members.append((name, hashlib.sha256(payload).hexdigest(), root, namespaces))
            state["documents"][document] = (str(archive_path), hashlib.sha256(archive).hexdigest(), members)
        archive_path, archive_sha, members = state["documents"][document]
        if source.get("source_artifact_sha256") and source["source_artifact_sha256"] != archive_sha:
            raise ValueError("source archive digest differs from retained bytes")
        found = []
        x = "{http://www.xbrl.org/2003/instance}"
        for member, member_sha, root, scopes in members:
            if source.get("source_file") and member != source["source_file"]:
                continue
            ids = {}
            for item in root:
                if item.get("id"):
                    ids.setdefault(item.get("id"), []).append(item)
            for ordinal, item in enumerate(root):
                if (not item.tag.startswith("{http://disclosure.edinet-fsa.go.jp/taxonomy/" + namespace.split("_")[0] + "/")
                        or item.tag.rsplit("}", 1)[-1] != tag
                        or not item.tag.split("}", 1)[0].endswith("/" + namespace)):
                    continue
                locator = source.get("native_xml_location")
                if locator and (locator.get("element_ordinal") != ordinal
                        or locator.get("fact_qname") != item.tag
                        or locator.get("context_ref") != item.get("contextRef")
                        or locator.get("unit_ref") != item.get("unitRef")):
                    continue
                context_items, unit_items = ids.get(item.get("contextRef"), []), ids.get(item.get("unitRef"), [])
                if len(context_items) != 1 or len(unit_items) != 1:
                    raise ValueError("missing or duplicate native context/unit")
                if context_items[0].tag != x + "context":
                    raise ValueError("native context resource required")
                context = inline_xbrl._native_context(context_items[0], scopes)
                if (source.get("source_entity") and source["source_entity"] != context.entity_identifier
                        or source.get("source_entity_scheme") and source["source_entity_scheme"] != context.entity_scheme):
                    continue
                if (context.entity_identifier not in {state["issuer"], state["issuer"] + "-000"}
                        or context.entity_scheme != "http://disclosure.edinet-fsa.go.jp"
                        or context.dimensions or context.start != start or (context.instant or context.end) != end):
                    continue
                native_unit = unit_items[0]
                if native_unit.tag != x + "unit":
                    raise ValueError("native unit resource required")
                direct = native_unit.findall(x + "measure")
                numerator = direct or native_unit.findall(x + "divide/" + x + "unitNumerator/" + x + "measure")
                denominator = native_unit.findall(x + "divide/" + x + "unitDenominator/" + x + "measure")
                divide = native_unit.find(x + "divide")
                if (direct and (len(native_unit) != 1 or len(direct) != 1)
                        or not direct and (len(native_unit) != 1 or divide is None
                            or [node.tag for node in divide] != [x + "unitNumerator", x + "unitDenominator"]
                            or any(len(side) != 1 or side[0].tag != x + "measure" for side in divide))
                        or any(list(node) for node in (*numerator, *denominator))):
                    raise ValueError("ambiguous native unit shape")
                measures = tuple(inline_xbrl._native_qname(n.text or "", scopes[id(n)]) for n in numerator)
                divided = tuple(inline_xbrl._native_qname(n.text or "", scopes[id(n)]) for n in denominator)
                expected = x + "shares" if unit == "shares" else "{http://www.xbrl.org/2003/iso4217}" + unit.split("/")[0]
                if measures != (expected,) or divided != ((x + "shares",) if unit.endswith("/shares") else ()):
                    continue
                if (item.get("{http://www.w3.org/2001/XMLSchema-instance}nil") not in {None, "false", "0"}
                        or list(item) or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", (item.text or "").strip())):
                    continue
                value = Decimal(item.text.strip())
                if not value.is_finite():
                    continue
                if "cash outflow magnitude; source signed negative" in (source.get("concept") or ""):
                    if value >= 0 or _EDINET_CASH_PAYMENTS.get(tag) != source.get("canonical_tag"):
                        raise ValueError("unproved cash magnitude transformation")
                    value = abs(value)
                found.append((value, member, member_sha, ordinal, item.get("contextRef"), item.text))
        if (not found or len({value[0] for value in found}) != 1
                or len({value[1] for value in found}) != 1):
            raise ValueError("absent or ambiguous exact native occurrence")
        state["sources"].append({"document": document, "archive": archive_path, "archive_sha256": archive_sha,
            "member": found[0][1], "member_sha256": found[0][2], "tag": source["tag"],
            "start": source.get("start"), "end": source["end"], "unit": unit,
            "occurrences": [{"ordinal": v[3], "context_ref": v[4], "lexical": v[5]} for v in found]})
        return [float(found[0][0])]
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile) as exc:
        state["sources"].append({"document": document, "status": "UNCHECKED", "reason": str(exc)})
        return []


def _edinet_class_tables(facts, source):
    """Independent physical issued-class rows at the selected equity endpoint."""
    state = facts["_audit_edinet"]
    _, archive_sha, members = state["documents"][source["document"]]
    endpoint = date.fromisoformat(source["end"])
    out = []
    compact = lambda text: re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    x = "{http://www.xbrl.org/2003/instance}"
    for member, member_sha, root, scopes in members:
        if member != source["source_file"]:
            continue
        contexts = {node.get("id"): node for node in root if node.tag == x + "context"}
        for ordinal, block in enumerate(root):
            tag = block.tag.rsplit("}", 1)[-1]
            if (not block.tag.startswith("{http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/")
                    or not block.tag.split("}", 1)[0].endswith("/jpcrp_cor")
                    or tag not in {"IssuedSharesTotalNumberOfSharesEtcTextBlock",
                                   "NotesRegardingIssuedSharesAndTreasurySharesTextBlock"}):
                continue
            context_node = contexts.get(block.get("contextRef"))
            if context_node is None:
                continue
            context = inline_xbrl._native_context(context_node, scopes)
            if (context.entity_identifier != source["source_entity"]
                    or context.entity_scheme != source["source_entity_scheme"] or context.dimensions):
                continue
            parser = statements._Tables()
            parser.feed(block.text or "")
            for frame in parser.tables:
                grid = statements._grid(frame["rows"])
                if not grid:
                    continue
                classes, active = [], (tag == "IssuedSharesTotalNumberOfSharesEtcTextBlock"
                                      or "発行済株式" in compact(frame["caption"]))
                for row_index, cells in enumerate(grid[1:], 1):
                    label = cells.get(0, {}).get("text", "")
                    name = compact(label)
                    if name == "発行済株式":
                        classes, active = [], True
                        continue
                    if name == "自己株式":
                        active = False
                    if not active:
                        continue
                    if name not in {"計", "合計"}:
                        if name or any(cell["text"] for column, cell in cells.items() if column):
                            classes.append((row_index, label, cells))
                        continue
                    for column, header in grid[0].items():
                        text = compact(header["text"])
                        if column == 0 or "株" not in text or "千株" in text or "議決権" in text:
                            continue
                        as_of = None
                        if tag == "IssuedSharesTotalNumberOfSharesEtcTextBlock" and "事業年度末" in text:
                            dates = re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
                            if len(dates) == 1:
                                as_of = date(*map(int, dates[0]))
                        elif context.start and "期首" in text:
                            as_of = context.start - timedelta(days=1)
                        elif context.start and "年度末" in text:
                            as_of = context.end
                        if as_of != endpoint:
                            continue
                        reported = [(label, row.get(column, {}).get("text", "")) for _, label, row in classes]
                        names = [re.sub(r"\(注.*", "", compact(label)) for label, _ in reported]
                        values = [int(value) if re.fullmatch(r"\d+", value := compact(raw).replace(",", "")) else None
                                  for _, raw in reported]
                        total = cells.get(column, {}).get("text", "")
                        total_value = compact(total).replace(",", "")
                        ordinary = [index for index, name in enumerate(names) if name == "普通株式"]
                        known = all(name == "普通株式" or "優先株式" in name for name in names)
                        outcome = "UNRESOLVED"
                        if (reported and len(ordinary) == 1 and known and all(value is not None for value in values)
                                and total_value.isdigit() and sum(values) == int(total_value)):
                            outcome = ("PREFERRED_MONETARY_CLAIM_REQUIRED" if any(
                                value > 0 for index, value in enumerate(values) if index not in ordinary) else "ORDINARY_ONLY")
                        out.append(dict(artifact_sha256=archive_sha, member_sha256=member_sha,
                            document=source["document"], entity=context.entity_identifier, entity_scheme=context.entity_scheme,
                            context_start=context.start.isoformat() if context.start else None,
                            context_end=(context.instant or context.end).isoformat(), as_of=as_of.isoformat(),
                            outcome=outcome, classes=[list(item) for item in reported], total_lexical=total,
                            header_lexical=header["text"], registration_status="UNREGISTERED_NATIVE_MEMBER",
                            location=dict(source_file=member, fact_qname=block.tag, element_ordinal=ordinal,
                                context_ref=block.get("contextRef"), table=frame["position"], header_row=0,
                                class_rows=[index for index, _, _ in classes], total_row=row_index, column=column)))
                    classes, active = [], False
    return out


def _equity_source_value(facts, item, *, common):
    """Capital plus parent OCI, then separately proved issuer-class qualification."""
    if not isinstance(item, dict) or not isinstance(item.get("source"), dict):
        return None, "no serialized equity Fact"
    source = item["source"]
    parts = source.get("components") or []
    expected_tags = {"jppfs_cor:ShareholdersEquity", "jppfs_cor:ValuationAndTranslationAdjustments"}
    if (len(parts) != 2 or {part.get("tag") for part in parts} != expected_tags
            or source.get("canonical_tag") or source.get("tag") != " + ".join(part["tag"] for part in parts)):
        return None, "equity lacks its exact capital plus parent OCI operation"
    keys = ("end", "unit", "accn", "document", "source_file", "source_entity", "source_entity_scheme", "source_artifact_sha256")
    if (any(parts[0].get(key) is None or parts[0].get(key) != parts[1].get(key) for key in keys)
            or source.get("end") != parts[0]["end"] or source.get("unit") != parts[0]["unit"]
            or source.get("document") != parts[0]["document"] or any(part.get("start") for part in parts)
            or any(not part.get("native_xml_location") for part in parts)):
        return None, "equity component source contexts differ or are incomplete"
    values = [_source_values(facts, part, exact_identity=True) for part in parts]
    if not all(len(value) == 1 for value in values):
        return None, "equity primitive source occurrence unavailable or ambiguous"
    if common:
        actual = _edinet_class_tables(facts, parts[0])
        witnesses = source.get("equity_class_evidence") or []
        if any(_report_key(witness) not in {_report_key(item) for item in actual} for witness in witnesses):
            return None, "issuer class witness differs from original member/table/endpoint"
        if (not witnesses or not any(witness["outcome"] == "ORDINARY_ONLY" for witness in witnesses)
                or any(witness["outcome"] == "PREFERRED_MONETARY_CLAIM_REQUIRED" for witness in actual)):
            return None, "ordinary-only endpoint is unproved or preferred monetary claim remains unknown"
    return values[0][0] + values[1][0], None


def _equity_checks(row, facts):
    evidence = row.get("equity_evidence")
    if evidence is None:
        return
    ratios = _annual_ratio_series(row)
    entries = [("parent", evidence.get("parent"), False), ("current", evidence.get("current"), True)]
    entries.extend((f"annual {year}", item, True) for year, item in (evidence.get("annual") or {}).items())
    verified = {}
    for name, item, common in entries:
        if item is None:
            continue
        field = "equity_evidence." + name
        shown = item.get("value")
        endpoint = (row.get("balance_sheet_date") if name in {"current", "parent"} else
                    (ratios.get(name.removeprefix("annual ")) or {}).get("end"))
        if not endpoint and name.startswith("annual "):
            year = name.removeprefix("annual ")
            flow_ends = set()
            for metric in ("annual_net_income", "annual_revenue"):
                source = ((row.get("annual_sources") or {}).get(metric) or {}).get(year) or {}
                amount = (row.get(metric) or {}).get(year)
                if (source.get("start") and source.get("end") and amount is not None
                        and not source.get("operation_unassessed_reason")
                        and any(_close(amount, value) for value in _source_values(facts, source, exact_identity=True))):
                    flow_ends.add(source["end"])
            if len(flow_ends) == 1:
                endpoint = flow_ends.pop()
        source_end = (item.get("source") or {}).get("end")
        if not endpoint:
            yield "UNCHECKED", field, shown, None, "consumer equity slot has no exact selected endpoint"
            continue
        if source_end != endpoint:
            yield "DERIVED", field, shown, None, "equity source period differs from its selected consumer endpoint"
            continue
        try:
            expected, why = _equity_source_value(facts, item, common=common)
        except (KeyError, TypeError, ValueError, IndexError, ArithmeticError) as exc:
            expected, why = None, f"unreadable equity source context: {type(exc).__name__}"
        if expected is None:
            yield "UNCHECKED", field, shown, None, why
        elif _close(shown, expected):
            verified[name] = expected
            yield "DERIVED-OK", field, shown, expected, "exact capital plus parent accumulated OCI; class evidence separate"
        else:
            yield "DERIVED", field, shown, expected, "exact capital plus parent accumulated OCI"
    if evidence.get("current") is None:
        quality = row.get("asset_quality")
        if not isinstance(quality, dict) or "common_equity" not in quality:
            yield "DERIVED", "asset_quality.common_equity", None, None, "unavailable required common equity must retain an explicit null to prevent book fallback"
        for field in ("bvps", "tbvps", "debt_to_equity", "profitability.book_value", "profitability.on_book",
                      "profitability.net_tangible_assets", "profitability.on_net_tangible_assets",
                      "asset_quality.common_equity", "asset_quality.goodwill_to_common_equity",
                      "asset_quality.intangibles_to_common_equity", "asset_quality.goodwill_and_intangibles_to_common_equity"):
            if (value := _payload_field(row, field)) is not None:
                yield "DERIVED", field, value, None, "common-equity qualification is unavailable"
    for year, cell in ratios.items():
        for metric in ("bvps", "tbvps", "pb", "ptbv"):
            if cell.get(metric) is not None:
                yield "UNCHECKED", f"annual_ratios.{year}.{metric}", cell[metric], None, "exact historical share denominator/intangible/price dependencies are not serialized"
        closing, opening = verified.get(f"annual {year}"), verified.get(f"annual {int(year) - 1}")
        if cell.get("common_equity") is not None and closing is None:
            yield "UNCHECKED", f"FY{year} common_equity", cell["common_equity"], None, "no proved endpoint common-equity source"
        elif cell.get("common_equity") is not None:
            yield ("DERIVED-OK" if _close(cell["common_equity"], closing) else "DERIVED"), f"FY{year} common_equity", cell["common_equity"], closing, "exact annual common-equity endpoint"
        income = (row.get("annual_net_income") or {}).get(year)
        expected_book_return = income / closing * 100 if closing is not None and closing > 0 and income is not None else None
        if expected_book_return is not None or cell.get("return_on_book") is not None:
            yield ("DERIVED-OK" if _close(cell.get("return_on_book"), expected_book_return) else "DERIVED"), f"FY{year} return_on_book", cell.get("return_on_book"), expected_book_return, "annual net income over source-qualified closing common equity"
        if cell.get("average_common_equity") is not None:
            if closing is None or opening is None:
                yield "UNCHECKED", f"FY{year} average_common_equity", cell["average_common_equity"], None, "both annual common-equity endpoint sources are required"
            else:
                expected = (opening + closing) / 2
                yield ("DERIVED-OK" if _close(cell["average_common_equity"], expected) else "DERIVED"), f"FY{year} average_common_equity", cell["average_common_equity"], expected, "opening plus closing common equity divided by two"
    profitability = row.get("profitability") or {}
    if profitability.get("average_common_equity") is not None:
        year = int(profitability["fiscal_year"])
        closing, opening = verified.get(f"annual {year}"), verified.get(f"annual {year - 1}")
        expected = (opening + closing) / 2 if closing is not None and opening is not None else None
        yield ("UNCHECKED" if expected is None else "DERIVED-OK" if _close(profitability["average_common_equity"], expected) else "DERIVED"), "profitability.average_common_equity", profitability["average_common_equity"], expected, "both exact annual common-equity endpoints"
    yield from _equity_book_consumer_checks(row, facts, verified)


def _equity_book_consumer_checks(row, facts, verified):
    """Presence and arithmetic of the disclosed current common-book consumers."""
    equity = verified.get("current")
    if equity is None:
        return
    def check(field, expected, why):
        shown = _payload_field(row, field)
        return ("DERIVED-OK" if _close(shown, expected) else "DERIVED"), field, shown, expected, why
    shares = row.get("shares")
    yield check("bvps", equity / shares if shares is not None and shares > 0 else None,
                "source-qualified current common equity over positive outstanding shares")
    goodwill, intangibles = row.get("goodwill"), row.get("intangibles")
    tangible = equity - goodwill - intangibles if goodwill is not None and intangibles is not None else None
    yield check("tbvps", tangible / shares if tangible is not None and shares is not None and shares > 0 else None,
                "current tangible common equity over positive outstanding shares")
    debt, assets = row.get("debt"), row.get("total_assets")
    expected_debt_ratio = (round(debt / equity, 4) if debt is not None and debt >= 0 and equity > 0
                           and (assets is None or debt <= assets) else None)
    yield check("debt_to_equity", expected_debt_ratio, "settled debt over positive common equity within reported asset bounds")
    quality = row.get("asset_quality")
    if quality is not None:
        yield check("asset_quality.common_equity", equity, "source-qualified current common equity")
        for field, value in (("goodwill_to_common_equity", row.get("goodwill")),
                             ("intangibles_to_common_equity", row.get("intangibles"))):
            expected = value / equity * 100 if value is not None and equity > 0 else None
            yield check("asset_quality." + field, expected, "reported intangible over source-qualified current common equity")
        goodwill, intangibles = row.get("goodwill"), row.get("intangibles")
        expected = (goodwill + intangibles) / equity * 100 if goodwill is not None and intangibles is not None and equity > 0 else None
        yield check("asset_quality.goodwill_and_intangibles_to_common_equity", expected, "reported goodwill plus intangibles over source-qualified current common equity")
    profitability = row.get("profitability")
    if profitability is None:
        return
    yield check("profitability.book_value", equity, "source-qualified current common equity")
    revenue, income = row.get("annual_revenue") or {}, row.get("annual_net_income") or {}
    years = sorted(year for year in set(revenue) & set(income) if revenue[year] >= 1_000_000)
    if not years:
        for field in ("on_book", "on_net_tangible_assets", "on_equity"):
            yield check("profitability." + field, None, "no qualifying common annual income/revenue year")
        return
    year = years[-1]
    shown_year = profitability.get("fiscal_year")
    yield ("DERIVED-OK" if shown_year == int(year) else "DERIVED"), "profitability.fiscal_year", shown_year, int(year), "exact latest qualifying annual income/revenue year identity"
    source = ((row.get("annual_sources") or {}).get("annual_net_income") or {}).get(year) or {}
    if (not source.get("end") or source.get("operation_unassessed_reason")
            or not any(_close(income[year], value) for value in _source_values(facts, source, exact_identity=True))):
        yield "UNCHECKED", "profitability.on_book", profitability.get("on_book"), None, "selected annual income lacks its exact source/end"
        return
    try:
        stale = (date.fromisoformat(row["balance_sheet_date"]) - date.fromisoformat(source["end"])).days > 800
    except (ValueError, TypeError, KeyError):
        yield "UNCHECKED", "profitability.on_book", profitability.get("on_book"), None, "selected annual income/balance endpoint is missing"
        return
    expected = round(income[year] / equity * 100, 2) if equity > 0 and not stale else None
    yield check("profitability.on_book", expected, "selected annual net income over current common equity; 800-day maximum lag")
    goodwill, intangibles = row.get("goodwill"), row.get("intangibles")
    tangible = equity - goodwill - intangibles if goodwill is not None and intangibles is not None else None
    yield check("profitability.net_tangible_assets", tangible, "current common equity less reported goodwill and intangibles")
    expected = round(income[year] / tangible * 100, 2) if tangible is not None and tangible > 0 and not stale else None
    yield check("profitability.on_net_tangible_assets", expected, "selected annual net income over current tangible equity; 800-day maximum lag")
    average = profitability.get("average_common_equity")
    expected = round(income[year] / average * 100, 2) if average is not None and average > 0 else None
    yield check("profitability.on_equity", expected, "selected annual net income over disclosed proved annual average common equity")


def _payload_field(row: dict, path: str):
    value = row
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _annual_ratio_series(row):
    """In-memory ratio keys are ints; JSON payload keys are strings."""
    return {str(year): cell for year, cell in (row.get("annual_ratios") or {}).items()}


def _ratio(row: dict) -> float:
    """How many ordinary shares one listed receipt stands for, or 1.

    Zai Lab's share count is filed in ordinary shares and displayed in the American
    depositary shares the price belongs to, ten to one. The provenance still names
    the ordinary fact, so the check has to divide before comparing or every
    depositary filer reads as wrong by its own ratio."""
    value = ((row.get("receipt") or {}).get("ratio"))
    try:
        return float(value) if value else 1.0
    except (TypeError, ValueError):
        return 1.0


def _source_qname(tag_text: str) -> tuple[str, str]:
    """Native extension namespaces may include an accession and a colon."""
    namespace, separator, name = tag_text.rpartition(":")
    return (namespace, name) if separator else ("us-gaap", tag_text)


def _at_period_end(facts: dict, tag_text: str, end: str | None) -> float | None:
    """One concept's value at one balance-sheet date, latest-filed, any accession."""
    ns, tag = _source_qname(tag_text)
    best = None
    for units in (facts.get("facts", {}).get(ns or "us-gaap", {}).get(tag) or {}).get("units", {}).values():
        for e in units:
            if e.get("end") == end and "start" not in e:
                if best is None or e.get("filed", "") > best.get("filed", ""):
                    best = e
    return float(best["val"]) if best else None


def _fact_in_filing(facts: dict, source: dict) -> float | None:
    """The value the named accession actually reports for the named period.

    A provenance naming two tags is a figure the engine derived by subtraction or
    addition (total liabilities from the balance-sheet identity, a minority interest
    summed with its redeemable half). Those are resolved from their own components
    rather than reported as unfindable."""
    tag_text = source.get("tag") or ""
    # A summed figure records where each half came from, and the halves may be
    # filed separately: Cohen & Steers tags its permanent minority interest every
    # quarter and its redeemable half only in the 10-K, so the two carry different
    # accessions and dates. Each is looked up in its own filing — forcing them into
    # the parent's reads the figure as wrong when it is merely composite.
    parts = source.get("components") or ()
    if len(parts) > 1 and " + " in tag_text:
        found = [_fact_in_filing(facts, part) for part in parts]
        return None if any(v is None for v in found) else sum(found)
    for operator, combine in ((" - ", lambda a, b: a - b), (" + ", lambda a, b: a + b)):
        if operator in tag_text:
            # Each half at the same balance-sheet date, from whichever filing states
            # it: Isabella Bank's goodwill for 2025-12-31 is carried by an earlier
            # accession than the combined line, and a half that cannot be found is
            # NOT zero — treating it so read the whole combined figure as intangible
            # and called a correct 0 an error.
            halves = [_at_period_end(facts, part.strip(), source.get("end"))
                      for part in tag_text.split(operator, 1)]
            if any(h is None for h in halves):
                return None
            return combine(*halves)
    ns, tag = _source_qname(tag_text)
    found = _values_in_filing(facts, ns or "us-gaap", tag, source)
    return found[0] if found else None


def _values_in_filing(facts: dict, ns: str, tag: str, source: dict, *, exact_identity=False) -> list[float]:
    """Every value the named filing reports for that exact period context.

    Usually one. But a weighted-average share count is a DURATION fact, and a
    quarterly report states several for the same end date — the three months, the
    six, the year to date. Existing compact sources record only the end, so for
    those the check asks whether the figure is one of the numbers in the filing.
    New duration sources that carry a start are matched to that exact context.
    """
    if source.get("canonical_tag") and "_audit_edinet" in facts:
        return _edinet_source_values(facts, source)
    # A canonical adapter displays its exact workbook row as the provenance tag.
    # The normalized concept is retained separately so the audit can locate the
    # cached value without pretending that row was a US-GAAP element.
    if source.get("canonical_tag"):
        ns, tag = "canonical", source["canonical_tag"]
    out = []
    available_units = (facts.get("facts", {}).get(ns, {}).get(tag) or {}).get("units", {})
    selected_units = ({source["unit"]: available_units.get(source["unit"], [])}
                      if source.get("unit") else available_units)
    for units in selected_units.values():
        for e in units:
            if (e.get("accn") == source.get("accn")
                    and (not exact_identity or source.get("form") and source.get("filed")
                         and e.get("_source_form", e.get("form")) == source["form"]
                         and e.get("_source_filed", e.get("filed")) == source["filed"])
                    and e.get("end") == source.get("end")
                    and (source.get("start") is None
                         or e.get("start") == source.get("start"))
                    and (e.get("segments") or "")
                    == (source.get("segments") or "")
                    and (not source.get("document")
                         or e.get("_source_document") == source.get("document"))
                    and (not source.get("canonical_tag")
                         or e.get("_source_tag")
                         == (source.get("tag") or "").partition(":")[2])):
                out.append(float(e["val"]))
    if exact_identity and len(set(out)) > 1:
        return []
    return out


def _source_values(facts: dict, source: dict, *, exact_identity=False) -> list[float]:
    """Every filed value a provenance tree can represent.

    Foreign reports commonly tag the same concept in both their presentation
    currency and USD. A summed figure therefore has several legitimate component
    combinations; retaining all candidates lets the audit verify the USD value the
    engine selected instead of silently taking whichever unit appears first.
    """
    tag_text = source.get("tag") or ""
    if source.get("canonical_tag"):
        return _values_in_filing(facts, "canonical", source["canonical_tag"], source, exact_identity=exact_identity)
    parts = source.get("components") or ()
    operator = " + " if " + " in tag_text else " - " if " - " in tag_text else None
    operands = list(parts)
    if operator and len(operands) < 2:
        operands = [
            {"tag": tag, "accn": source.get("accn"), "end": source.get("end")}
            for tag in tag_text.split(operator, 1)
        ]
    if len(operands) > 1 and operator:
        totals = [0.0]
        for index, part in enumerate(operands):
            values = _source_values(facts, part, exact_identity=exact_identity)
            if not values:
                return []
            sign = -1 if operator == " - " and index else 1
            totals = [total + sign * value for total in totals for value in values]
        return totals
    if " - " not in tag_text and " + " not in tag_text:
        ns, tag = _source_qname(tag_text)
        return _values_in_filing(facts, ns or "us-gaap", tag, source, exact_identity=exact_identity)
    if exact_identity:
        return []
    value = _fact_in_filing(facts, source)
    return [] if value is None else [value]


def _reported_check(facts, field, shown, source, *, exact_identity=False):
    values = _source_values(facts, source, exact_identity=exact_identity) if source and source.get("accn") else []
    if not values:
        return "UNCHECKED", field, shown, None, "no exact retained source occurrence"
    matched = next((value for value in values if _close(shown, value)), None)
    if matched is not None:
        return "SOURCED-OK", field, shown, matched, None
    return "SOURCED", field, shown, values[0], "value differs from its exact retained source"


def _edinet_cash_checks(row, facts):
    earnings = row.get("owner_earnings") or {}
    components = dict(earnings.get("components") or [])
    components.update(dict(earnings.get("free_cash_flow_components") or []))
    for label, key in (("reported earnings available to common", "reported_earnings"),
                       ("operating cash flow", "operating_cash_flow"),
                       ("+ depreciation & amortisation", "depreciation_and_amortisation"),
                       ("- total capital expenditure", "total_capex")):
        if label not in components:
            continue
        source = (earnings.get("sources") or {}).get(key) or {}
        shown = components[label]
        if key == "total_capex":
            tag = _source_qname(source.get("tag") or "")[1]
            if _EDINET_CASH_PAYMENTS.get(tag) != source.get("canonical_tag"):
                yield "UNCHECKED", "cash " + key, shown, None, "unproved cash purchase scope"
                continue
            shown = abs(shown)
        yield _reported_check(facts, "cash " + key, shown, source)
    for year, cell in (earnings.get("annual_per_share") or {}).items():
        for metric, point in (cell.get("cash_flow_bridge") or {}).items():
            if not isinstance(point, list):
                continue  # The residual and FCF are derived, not reported leaves.
            field = f"FY{year} cash {metric}"
            if len(point) < 8 or not isinstance(point[7], dict):
                yield "UNCHECKED", field, point[0], None, "legacy cash point lacks full exact source context"
                continue
            source = point[7]
            if len(point) < 7 or point[1:7] != [source.get(key) for key in ("tag", "form", "accn", "end", "unit", "document")]:
                yield "UNCHECKED", field, point[0], None, "cash point and full source identities differ"
                continue
            yield _reported_check(facts, field, point[0], source)


def _prior_history_checks(row, facts):
    """Reported prior-basis leaves and their own FCF parents, without rebasing."""
    for group in row.get("prior_accounting_history") or []:
        basis, currency = group.get("basis"), group.get("currency")
        namespace = {"US-GAAP": "us-gaap", "IFRS": "ifrs-full"}.get(basis)
        if (namespace is None or basis == group.get("current_basis")
                or not re.fullmatch(r"[A-Z]{3}", currency or "")
                or not group.get("share_basis") or group.get("unavailable_reason")):
            yield "UNCHECKED", "prior accounting history", None, None, "unqualified prior-basis group"
            continue
        series = group.get("series") or {}
        for metric, years in series.items():
            if metric == "free_cash_flow":
                continue
            for year, item in years.items():
                source = item.get("source") or {}
                unit = "shares" if metric == "weighted_shares" else currency + ("/shares" if metric == "eps" else "")
                field = f"prior {basis} FY{year} {metric}"
                if (_source_qname(source.get("tag") or "")[0] != namespace
                        or source.get("unit", unit) != unit or not source.get("start")
                        or not _is_annual_form(source.get("form") or "")):
                    yield "UNCHECKED", field, item.get("value"), None, "prior source basis/unit/period missing or incompatible"
                    continue
                if metric == "capital_expenditure" and source["tag"].replace(":", "_", 1) not in CASH_FLOW["- total capital expenditure"]:
                    yield "UNCHECKED", field, item.get("value"), None, "source is not an established cash capital-purchase concept"
                    continue
                yield _reported_check(facts, field, item.get("value"), {**source, "unit": unit}, exact_identity=True)
        for year, item in (series.get("free_cash_flow") or {}).items():
            field = f"prior {basis} FY{year} free_cash_flow"
            cash = (series.get("operating_cash_flow") or {}).get(year) or {}
            capex = (series.get("capital_expenditure") or {}).get(year) or {}
            parents = [(value.get("source") or {}) for value in (cash, capex)]
            source = item.get("source") or {}
            if (not all(parent.get("start") and parent.get("end") for parent in parents)
                    or (parents[0]["start"], parents[0]["end"]) != (parents[1]["start"], parents[1]["end"])
                    or (source.get("start"), source.get("end")) != (parents[0]["start"], parents[0]["end"])
                    or source.get("components") != parents):
                yield "UNCHECKED", field, item.get("value"), None, "FCF lacks its exact same-period source parents"
                continue
            values = [_source_values(facts, {**parent, "unit": currency}, exact_identity=True) for parent in parents]
            if any(_source_qname(parent.get("tag") or "")[0] != namespace
                   or parent.get("unit", currency) != currency for parent in parents) or not all(values):
                yield "UNCHECKED", field, item.get("value"), None, "FCF parent source unavailable or incompatible"
                continue
            if parents[1]["tag"].replace(":", "_", 1) not in CASH_FLOW["- total capital expenditure"]:
                yield "UNCHECKED", field, item.get("value"), None, "FCF capital parent is not an established cash purchase"
                continue
            expected = values[0][0] - abs(values[1][0])
            yield ("DERIVED-OK" if _close(item.get("value"), expected) else "DERIVED"), field, item.get("value"), expected, "exact prior-source OCF less cash capex magnitude"


def _edinet_filing_checks(row, facts):
    """Retained native XML evidence, explicitly distinct from rendered tables."""
    sources = row.get("sources") or {}
    checks = [_reported_check(facts, field, _payload_field(row, field), sources.get(key))
              for field, key in SOURCED if _payload_field(row, field) is not None]
    checks.extend(_edinet_cash_checks(row, facts))
    for metric in ("revenue", "gross_profit", "net_income", "eps", "operating_income", "weighted_shares"):
        for year, value in (row.get("annual_" + metric) or {}).items():
            source = ((row.get("annual_sources") or {}).get("annual_" + metric) or {}).get(str(year)) or {}
            field = f"FY{year} {metric}"
            if not source.get("start") or not source.get("end"):
                checks.append(("UNCHECKED", field, value, None, "annual value lacks its own exact serialized source"))
            elif reason := source.get("operation_unassessed_reason"):
                checks.append(("UNCHECKED", field, value, None, reason))
            else:
                checks.append(_reported_check(facts, field, value, source))
    for kind, field, shown, expected, why in checks:
        yield ({"SOURCED-OK": "FILING-OK", "SOURCED": "FILING", "UNCHECKED": "FILING?"}[kind],
               field, shown, expected, why or "retained EDINET native XML; rendered-table semantics not assessed")


# The panel rounds a displayed ratio to two decimals, so 0.0669 is shown as 0.07.
# On a small figure that is a 5% relative gap and nothing is wrong, which a purely
# relative tolerance calls a defect.
ROUNDING = 0.005


def _close(a, b, slack: float = 0.0) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= max(ROUNDING, slack, TOLERANCE * max(abs(a), abs(b), 1e-9))


def _identities(row: dict) -> list[tuple]:
    """(name, shown, expected, formula) for every figure the panel computes.

    Each is the whole of its arithmetic. Where an input is missing the check is
    skipped rather than assumed — that is the same rule the engine follows, and a
    check that treats missing as zero would pass exactly where the engine is
    wrong.
    """
    out = []

    def check(name, shown, expected, formula):
        if shown is not None and expected is not None:
            out.append((name, shown, expected, formula))

    ca, cl = row.get("current_assets"), row.get("current_liabilities")
    shares, quote_price = row.get("shares"), row.get("price")
    reporting_currency = (row.get("reporting_currency") or row.get("currency")
                          or "USD").upper()
    quote_currency = (row.get("quote_currency") or row.get("currency")
                      or "USD").upper()
    if quote_currency != reporting_currency:
        price = None
        rate = _fx_rate(_valuation_fx_record(row), quote_currency, reporting_currency)
        if quote_price is not None and rate is not None:
            price = row.get("price_reporting_currency")
            check("price_reporting_currency", price,
                  float(Decimal(str(quote_price)) * rate),
                  f"{quote_currency} quote x {quote_currency}/{reporting_currency} rate")
    else:
        price = quote_price
    assets, liabilities = row.get("total_assets"), row.get("total_liabilities")
    # the same three deductions `_common_equity` makes — what is left is the
    # common's, and omitting the preferred overstates it by exactly that claim
    senior = ((row.get("noncontrolling_interest") or 0) + (row.get("preferred_stock") or 0)
              + (row.get("temporary_equity") or 0))
    equity = None if assets is None or liabilities is None else assets - liabilities - senior
    if "equity_evidence" in row:
        equity = ((row["equity_evidence"].get("current") or {}).get("value"))

    # where one tag carries both, the two fields must still add up to it
    goodwill_src = ((row.get("sources") or {}).get("goodwill") or {})
    if "contained in" in goodwill_src.get("concept", ""):
        check("goodwill + intangibles", (row.get("goodwill") or 0) + (row.get("intangibles") or 0),
              row.get("intangibles"), "the combined line, split as 0 + all of it")
    if ca is not None and cl:
        check("current_ratio", _c(row, 2), ca / cl, "current assets / current liabilities")
    if equity is not None and shares:
        check("bvps", row.get("bvps"), equity / shares,
              ("source-qualified current common equity / shares" if "equity_evidence" in row else
               "(assets - liabilities - preferred - minority - temporary equity) / shares"))
        intangible = (row.get("goodwill") or 0) + (row.get("intangibles") or 0)
        check("tbvps", row.get("tbvps"), (equity - intangible) / shares,
              "(common equity - goodwill - intangibles) / shares")
    if ca is not None and liabilities is not None and shares:
        check("ncavps", row.get("ncavps"), (ca - liabilities - senior) / shares,
              "(current assets - all liabilities - preferred - minority) / shares")
    if price and row.get("ttm_eps"):
        check("pe", _c(row, 1), price / row["ttm_eps"], "price / trailing EPS")
    if price and row.get("tbvps"):
        check("ptbv", _c(row, 7), price / row["tbvps"], "price / tangible book per share")
    # Graham's chapter-13 margin is struck on a fiscal year, not on trailing
    # quarters: a year of sales against the same year's profit. The payload carries
    # both series, so the check uses the newest year they share.
    # ...on a year whose sales are large enough for the percentage to mean anything.
    # The engine skips a year under $1M of revenue, because a company with $40,000
    # of sales and a $500,000 loss has a margin of -1,250% and no margin at all.
    revenue, income = row.get("annual_revenue") or {}, row.get("annual_net_income") or {}
    shared = [y for y in sorted(set(revenue) & set(income), reverse=True)
              if (revenue[y] or 0) >= 1_000_000]
    if shared:
        year = shared[0]
        check("net_margin", (row.get("profitability") or {}).get("net"),
              income[year] / revenue[year] * 100, f"FY{year} net income / FY{year} revenue x 100")
    profitability = row.get("profitability") or {}
    goodwill, intangibles = row.get("goodwill"), row.get("intangibles")
    if equity is not None and goodwill is not None and intangibles is not None:
        tangible = equity - goodwill - intangibles
        check("profitability.net_tangible_assets", profitability.get("net_tangible_assets"),
              tangible, "common equity - goodwill - intangibles")
        profit_year = str(profitability.get("fiscal_year"))
        if tangible > 0 and income.get(profit_year) is not None:
            check("return_on_net_tangible_assets",
                  profitability.get("on_net_tangible_assets"),
                  income[profit_year] / tangible * 100,
                  f"FY{profit_year} net income / current net tangible assets x 100")
    average_equity = profitability.get("average_common_equity")
    profit_year = str(profitability.get("fiscal_year"))
    if average_equity is not None and average_equity > 0 and income.get(profit_year) is not None:
        check("return_on_equity", profitability.get("on_equity"),
              income[profit_year] / average_equity * 100,
              f"FY{profit_year} net income / average common equity x 100")
    combined_debt = row.get("debt")
    if equity is not None and equity > 0 and combined_debt is not None:
        check("debt_to_equity", row.get("debt_to_equity"), combined_debt / equity,
              "combined interest-bearing debt / common equity")
    if row.get("options") is not None and row.get("rsus") is None and shares:
        check("equity_awards", row.get("equity_awards"), row["options"],
              "options alone, no restricted stock on file")
    if row.get("options") is not None and row.get("rsus") is not None:
        check("equity_awards", row.get("equity_awards"), row["options"] + row["rsus"],
              "options + restricted stock")
    # The debt criterion 3 weighed. A newer representation wins because mixing
    # dates is not conservative, it is incoherent; only representations struck
    # on the same date are reconciled by taking the larger.
    lt, st, total = row.get("long_term_debt"), row.get("short_term_debt"), row.get("total_debt")
    parts = sum(v for v in (lt, st) if v is not None)
    if row.get("debt") is not None and (total is not None or lt is not None or st is not None):
        sources = row.get("sources") or {}
        total_end = (sources.get("total_debt") or {}).get("end")
        part_ends = [
            (sources.get(name) or {}).get("end")
            for name, value in (("long_term_debt", lt), ("short_term_debt", st))
            if value is not None and (sources.get(name) or {}).get("end")
        ]
        parts_end = max(part_ends, default=None)
        if total is None:
            expected = parts
        elif total_end and parts_end and total_end != parts_end:
            expected = parts if parts_end > total_end else total
        else:
            expected = max(total, parts)
        check("debt", row["debt"], expected, "max(total-debt tag, long + short) at one date")
    return out


# What each figure is called on a printed balance sheet, and what it may be
# derived from where the company prints no such line. Coca-Cola states no "total
# liabilities" at all — it prints total equity and the identity closes the gap.
PRINTED = (
    ("total_assets", ("total assets",), None),
    ("current_assets", ("total current assets",), None),
    ("current_liabilities", ("total current liabilities",), None),
    ("total_liabilities", ("total liabilities",), ("total assets", "total equity")),
    ("goodwill", ("goodwill", "goodwill, net"), None),
    ("intangibles", ("intangible assets, net", "other intangible assets, net",
                     "intangible assets", "other intangibles, net",
                     "intangibles, net", "other assets, intangible, net"), None),
    ("noncontrolling_interest", ("noncontrolling interests", "noncontrolling interest",
                                 "total noncontrolling interests",
                                 "redeemable noncontrolling interests"), None),
)
# Every "total ..." form before any bare one. An income statement often opens with
# a bare "Revenues:" as the heading of the section that follows, and a heading row
# can carry a stray figure — Robinhood's read $2,628M against a $4,473M year.
PRINTED_INCOME = (
    ("revenue", ("total net revenues", "total net revenue", "total revenues",
                 # Authid prints gross "Revenues" above "Revenues, net"; the net
                 # line is the one its own XBRL tags and the one a reader wants
                 "revenues, net", "revenue, net",
                 # Phillips 66's sales line, beneath a total that adds equity
                 # earnings and disposal gains — neither of which is a sale
                 "sales and other operating revenues",
                 # Expand Energy totals to "Total revenues and other", which adds
                 # derivative gains to the sales line
                 "total revenues and other", "revenues and other income",
                 "total revenue", "total operating revenues", "total net sales",
                 "total revenues and other income", "net operating revenues",
                 "revenues and other income", "net revenues", "net sales",
                 "revenues", "sales")),
    ("gross_profit", ("gross profit", "gross profit (loss)", "gross margin")),
    # the parent's share first: Apollo prints consolidated net income of $5,401M
    # and $3,492M attributable to itself, and `NetIncomeLoss` is the second
    # The parent company by name first, then the after-preferred line, then the
    # consolidated one. Cohen & Steers calls the parent's share "attributable to
    # common stockholders" while Uniti uses that same phrase for the figure after
    # preferred dividends, so the label alone cannot separate them — but a line
    # naming the company outranks both wherever one exists.
    ("net_income", ("net income attributable to*", "net income (loss) attributable to*",
                    # "attributed to" is the same line under a different participle;
                    # AEP heads its own share "EARNINGS ATTRIBUTABLE TO AEP MEMBER"
                    "net income attributed to*", "net loss attributed to*",
                    "earnings attributable to*", "earnings attributed to*",
                    "net income (loss) attributed to*",
                    # utilities write "available to": Edison's parent line is
                    # "Net income available to Edison International common shareholders"
                    "net income available to*", "net income (loss) available to*",
                    "net earnings available to*",
                    "net earnings attributable to*", "net loss attributable to*",
                    # insurers write it this way; Markel's is "Net Income to Shareholders"
                    # small filers merge the two statements into one caption
                    "net loss and comprehensive loss", "net income and comprehensive income",
                    "net income (loss) and comprehensive income (loss)",
                    "net income to shareholders", "net earnings to shareholders",
                    "net income (loss) to shareholders",
                    # Cummins heads the parent's share "Cummins share of net income"
                    "*share of net income", "*share of net earnings",
                    "net income attributable to common stockholders",
                    "net income (loss) attributable to common shareholders",
                    "net loss attributable to common shareholders",
                    "net income", "net income (loss)", "net earnings", "net loss",
                    "consolidated net income")),
    # the printed per-share line: the only part of the earnings chain a statement
    # can confirm, since the trailing figure is stitched from quarters
    ("eps", ("diluted net income per share", "diluted earnings per share",
             "diluted net income (loss) per share", "diluted net loss per share",
             "net income per share diluted", "earnings per share diluted",
             "diluted income (loss) per share", "net income (loss) per share diluted",
             # Berkshire states one per-share line and names the shareholders in it
             "net earnings per share attributable to*", "net income per share attributable to*",
             "earnings per share attributable to*", "net earnings per share",
             "net income per share", "earnings per share",
             # Apple names the line simply "Diluted", under a unit suffix the
             # renderer adds; the bare word is only reached after every fuller form
             "diluted")),
    ("operating_income", ("total operating income", "operating income",
                          "operating income (loss)", "income from operations",
                          "operating profit", "operating income (expense)")),
)
FILING_TOLERANCE = 0.01     # the printed figure is rounded to the header's scale


def _the_finite_part_only(facts: dict, sources: dict, printed_values: list) -> bool:
    """Whether the printed intangibles line is the filer's own finite-lived figure."""
    source = sources.get("intangibles") or {}
    if "IntangibleAssetsNetExcludingGoodwill" not in (source.get("tag") or ""):
        return False
    finite = _at_period_end(facts, "us-gaap:FiniteLivedIntangibleAssetsNet", source.get("end"))
    if finite is None:
        return False
    return any(abs(finite - v) <= FILING_TOLERANCE * max(abs(finite), abs(v), 1e-9)
               for v in printed_values)


def _only_the_scale_differs(shown: float, printed: float) -> bool:
    """Whether the two are the same number under a different declared scale.

    A rendered statement states its own units in its header, and a small filer
    often leaves a template's "$ in Millions" above figures reported in whole
    dollars: ABVC heads its balance sheet that way and then prints cash of
    "$ 31,944", which at millions would be $31.9 trillion. The tagged values carry
    no such ambiguity, so where the panel and the page differ by exactly a
    thousand, a million or a billion it is the header that is wrong and the figure
    that agrees.
    """
    if not shown or not printed:
        return False
    ratio = abs(printed) / abs(shown)
    return any(abs(ratio - power) <= 0.01 * power or abs(ratio - 1 / power) <= 0.01 / power
               for power in (1e3, 1e6, 1e9))


def _absent_concept(tagged: dict, provenance_tag: str | None) -> bool:
    """Whether a plain concept the panel names is simply not on this statement."""
    if not provenance_tag or any(op in provenance_tag for op in (" - ", " + ")):
        return False                    # assembled figures have no single concept
    return provenance_tag.replace(":", "_") not in tagged


def _by_element(tagged: dict, provenance_tag: str | None, columns: list) -> list:
    """The printed values for the exact concept the panel's provenance names."""
    if not provenance_tag or not tagged or any(op in provenance_tag for op in (" - ", " + ")):
        return []
    key = provenance_tag.strip().replace(":", "_")
    values = tagged.get(key) or []
    return [(key, values[col]) for col in columns
            if col < len(values) and values[col] is not None]


def _reconciled_operating_income(printed, source: dict,
                                 column: int) -> list[tuple[str, float]]:
    """Independently rebuild a derived operating-income statement subtotal.

    The extractor accepts gross profit less operating expenses only when the
    filed nonoperating lines bridge that subtotal exactly to pretax income. The
    payload records those component tags; verify both equations from the rendered
    statement rather than looking for an operating-income line that does not
    exist.
    """
    if "derived and reconciled" not in (source.get("concept") or "").lower():
        return []

    by_name: dict[str, list[float]] = {}
    for component in source.get("components") or []:
        _, component_tagged, why = _qualified_views(printed or [], component, duration=True, document_source=source)
        if why:
            return []
        tag = component.get("tag") or ""
        local = tag.rsplit(":", 1)[-1]
        by_name.setdefault(local, []).extend(
            float(value) for _, value in _by_element(component_tagged, tag, [column])
        )

    gross = by_name.get("GrossProfit") or []
    expenses = [
        *by_name.get("OperatingExpenses", []),
        *by_name.get("SellingGeneralAndAdministrativeExpense", []),
    ]
    pretax = [
        value for name, values in by_name.items()
        if "BeforeIncomeTax" in name
        for value in values
    ]
    aggregate = by_name.get("NonoperatingIncomeExpense") or []
    interest = by_name.get("InterestIncomeExpenseNonoperatingNet") or []
    other = by_name.get("OtherNonoperatingIncomeExpense") or []
    separate_interest_income = [
        *by_name.get("InvestmentIncomeInterest", []),
        *by_name.get("InterestIncomeNonoperating", []),
    ]
    separate_interest_expense = [
        *by_name.get("InterestExpenseNonoperating", []),
        *by_name.get("InterestExpense", []),
    ]
    research = [
        *by_name.get("ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost", []),
    ]
    restructuring = by_name.get("RestructuringCharges") or []

    def close(left: float, right: float) -> bool:
        return abs(left - right) <= FILING_TOLERANCE * max(
            abs(left), abs(right), 1e-9)

    out = []
    if "pretax less complete nonoperating lines" in (
            source.get("concept") or "").lower():
        selling = by_name.get("SellingGeneralAndAdministrativeExpense") or []
        for pretax_value in pretax:
            for income_value in separate_interest_income:
                for expense_value in separate_interest_expense:
                    for other_value in other:
                        # Rendered income statements present the three rows on an
                        # expense basis: income is parenthesized/negative and an
                        # expense is positive. Adding those printed values back to
                        # pretax therefore removes the complete nonoperating bridge.
                        operating = (pretax_value + income_value + expense_value
                                     + other_value)
                        for gross_value in gross:
                            for selling_value in selling:
                                for research_value in research:
                                    restructuring_values = restructuring or [0.0]
                                    for restructuring_value in restructuring_values:
                                        known_costs = (selling_value + research_value
                                                       + restructuring_value)
                                        residual = gross_value - operating - known_costs
                                        if (gross_value > 0 and residual >= 0
                                                and residual <= gross_value * 0.05):
                                            out.append((
                                                "tagged pretax less complete "
                                                "nonoperating lines", operating))
        return out

    for gross_value in gross:
        for expense_value in expenses:
            operating = gross_value - expense_value
            # SEC's rendered statement applies the presentation/calculation sign:
            # NIKE's Company Facts value for interest income is +50m while the
            # published expense row displays (50m), parsed here as -50m. Preserve
            # the filed magnitude and allow either statement orientation for each
            # bridge component.
            bridges = ([signed * value for value in aggregate for signed in (1, -1)]
                       if aggregate else
                       [a_sign * a + b_sign * b
                        for a in interest for b in other
                        for a_sign in (1, -1) for b_sign in (1, -1)])
            if any(close(operating + bridge, pretax_value)
                   for bridge in bridges for pretax_value in pretax):
                out.append(("tagged gross profit less operating expenses", operating))
    return out


def _read_statement(row: dict, edgar, kind: str, source_override: dict | None = None):
    """The published statement's rows and the column the panel's date belongs to.

    Not every filer prints the newest period first, and one emerging from Chapter 11
    prints successor and predecessor periods side by side, so the column is chosen
    by its own heading rather than by position.
    """
    source = source_override or (
        (row.get("sources") or {}).get(
            "total_assets" if kind == "balance_sheet" else "eps") or {})
    if re.fullmatch(r"E\d{5}", str(row.get("cik") or "")):
        return (None, {}), None, None, "EDINET uses retained native XML; no SEC rendered-table source"
    accn, end = source.get("accn"), source.get("end")
    if not accn:
        return (None, {}), None, None, "no provenance for the statement"
    try:
        wanted = date.fromisoformat(end)
    except (TypeError, ValueError):
        return (None, {}), None, None, "no identifiable source period for the statement"
    bare, cik = accn.replace("-", ""), int(row["cik"])
    summary = edgar._get_text(statements.SUMMARY_URL.format(cik=cik, accn=bare))
    file = statements.find(summary, kind)
    if not file:
        # closed-end funds publish a statement of assets and liabilities, not a
        # balance sheet; nothing is wrong, there is simply nothing to compare
        return (None, {}), None, None, f"no {kind} rendered in {accn}"
    document = edgar._get_text(statements.REPORT_URL.format(cik=cik, accn=bare, file=file))
    parsed = statements.read(document)
    eligible = [table for table in parsed.tables
                if any(table.headers[position].end == wanted for position in table.active)
                and any(cell.raw is not None for printed_row in table.rows for cell in printed_row.cells)]
    if not eligible:
        return (None, {}), None, None, f"{end} is not among the identifiable printed table periods"
    if len(eligible) != 1:
        return (None, {}), None, None, f"multiple printed tables contain {end}; no exact table witness"
    parsed = statements.Statement((eligible[0],))
    printed, headings = statements.lines(parsed), statements.columns(parsed)
    tagged = statements.elements(parsed)
    if not printed:
        return (None, {}), None, None, f"{file} held no numbered rows"
    if not headings:
        return (None, {}), None, None, f"{file} held no identifiable printed period"
    # A rendered table can carry the same period twice — Great Wall's runs
    # 2025, 2024, 2025, 2024, two blocks side by side — and the figure may sit in
    # either. Every column bearing the date is a candidate.
    matching = [i for i, heading in enumerate(headings) if heading == wanted]
    if not matching:
        return (None, {}), None, None, f"{end} is not among the printed columns {headings}"
    return (printed, tagged), matching, headings, None


def _selected_context(source: dict, *, duration: bool = False):
    try:
        end = date.fromisoformat(source.get("end"))
        start = date.fromisoformat(source["start"]) if source.get("start") else None
    except (TypeError, ValueError):
        return None, "no exact selected source period"
    if duration and start is None:
        return None, "no exact selected source duration"
    if start is not None and start > end:
        return None, "invalid selected source duration"
    unit = source.get("unit") or ""
    if unit == "shares":
        measurement, currency = "SHARES", None
    elif re.fullmatch(r"[A-Z]{3}(?:/shares)?", unit):
        measurement = "PER_SHARE" if unit.endswith("/shares") else "MONEY"
        currency = unit.split("/", 1)[0]
    else:
        return None, "no exact selected source unit/measurement"
    if source.get("segments"):
        return None, "selected class/dimension has no exact printed context witness"
    return statements.SourceContext(end, start, measurement, currency), None


def _qualified_views(printed, source: dict, *, duration: bool = False, document_source: dict | None = None):
    if document_source is not None and any(source.get(key) != document_source.get(key) for key in ("accn", "document")):
        return [], {}, "component source does not identify this printed document"
    context, why = _selected_context(source, duration=duration)
    if why:
        return [], {}, why
    for component in source.get("components") or []:
        component_context, component_why = _selected_context(component, duration=duration)
        if component_why or component_context != context:
            return [], {}, component_why or "component source does not share the selected context"
        _, component_tagged, component_why = _qualified_views(printed, component, duration=duration, document_source=source)
        if component_why:
            return [], {}, component_why
        columns = range(len(printed[0].headers)) if printed else ()
        if not _by_element(component_tagged, component.get("tag"), columns):
            return [], {}, "component has no numeric selected concept in the printed context"
    if not printed or any(not isinstance(line, statements.PrintedLine) for line in printed):
        return [], {}, "no physical source context for the printed input"
    if context.measurement in {"SHARES", "PER_SHARE"} and any(
            line.row.measurement == context.measurement and line.row.basis is not None
            for line in printed):
        return [], {}, "printed share basis has no exact selected-basis witness"
    qualified = statements.qualify(printed, context)
    tagged = statements.elements_from_lines(qualified)
    if any(op in (source.get("tag") or "") for op in (" - ", " + ")):
        # A compound output is corroborated by its arithmetic, never an operand caption.
        return [], tagged, None
    return qualified, tagged, None


def _report_key(source: dict):
    return json.dumps(source, sort_keys=True, separators=(",", ":"))


def against_filing(row: dict, edgar, facts: dict | None = None) -> list[tuple]:
    """Compare the displayed balance-sheet figures against the published statement.

    The only check here that does not share an ancestor with what it is checking:
    every other test compares one reading of the XBRL against another, and a
    mis-tagged fact passes them all. This one reads the document a person opens and
    asks whether the number on the page is the number on the panel.
    """
    sources = row.get("sources") or {}
    anchor_source = sources.get("total_assets") or {}
    try:
        (printed, tagged), wanted_columns, headings, why = _read_statement(
            row, edgar, "balance_sheet")
    except Exception as exc:
        return [("FILING?", "balance_sheet", None, None, f"could not read: {exc!r}"[:110])]
    if printed is None:
        return [("FILING?", "balance_sheet", None, None, why)]

    out = []
    facts = facts or {}
    ratio = _ratio(row)
    reports = {
        _report_key(anchor_source): (
            (printed, tagged), wanted_columns, headings, why),
    }
    for field, phrases, identity in PRINTED:
        shown = row.get(field)
        if shown is None:
            continue
        # the same abstention the sourced check makes: where one tag carries both
        # goodwill and intangibles, the engine puts all of it in the second and
        # zeroes the first, and says so. Comparing the zero against the combined
        # line it names is comparing a bookkeeping choice against a figure.
        field_source = sources.get(field) or {}
        if "contained in" in (field_source.get("concept") or ""):
            continue
        # A snapshot can legitimately combine exact-date facts from different
        # filings. Verify each figure against the accession its own provenance
        # names, not whichever accession happened to supply total assets. FOXF's
        # later 10-Q reuses the intangibles tag for a narrower row and otherwise
        # makes its correct 10-K total look wrong.
        source_accn = _report_key(field_source)
        if source_accn not in reports:
            try:
                reports[source_accn] = _read_statement(
                    row, edgar, "balance_sheet", field_source)
            except Exception as exc:
                out.append(("FILING?", field, shown, None,
                            f"could not read source accession: {exc!r}"[:110]))
                continue
        ((field_printed, field_tagged), field_columns,
         field_headings, field_why) = reports[source_accn]
        if field_printed is None:
            out.append(("FILING?", field, shown, None, field_why))
            continue
        field_raw_printed = field_printed
        field_printed, field_tagged, context_why = _qualified_views(field_printed, field_source)
        if context_why:
            out.append(("FILING?", field, shown, None, context_why))
            continue
        # each figure against the column of ITS OWN balance-sheet date: a filer may
        # carry one line from a later filing than another, and Fervent's current
        # assets are stated a quarter behind its total assets
        own = field_source.get("end")
        here = field_columns
        if own:
            here = [i for i, heading in enumerate(field_headings)
                    if heading is not None and heading.isoformat() == own]
            if not here:
                # The figure belongs to a balance sheet this filing does not print.
                # ProtoKinetix states current assets a year behind its total assets,
                # because both legs of a current ratio must come from one moment and
                # its current liabilities were last tagged then. Correct, disclosed
                # by the provenance, and not answerable from this document.
                out.append(("FILING?", field, shown, None,
                            f"stated at {own}, which this filing does not print"))
                continue
        # A statement can print the same caption twice — Creatd states "Total
        # Liabilities" as a zeroed section line and again as the real total — so the
        # question is whether the panel's figure is one of the values printed under
        # that caption, not whether it is the first of them.
        # The concept the panel names, looked up as the concept the statement tagged.
        # Labels were only ever a proxy for this: Apple prints its NON-CURRENT
        # intangibles under a caption reading like the whole, and Hercules its GROSS
        # ones, and no reading of the words separates those from a disagreement.
        exact = _by_element(field_tagged, field_source.get("tag"), here)
        if (not exact and field_tagged
                and _absent_concept(field_tagged, field_source.get("tag"))):
            # The statement does not state this concept at all, and a caption that
            # reads like it is a different figure: Apple prints
            # `aapl_...NoncurrentIntangibleAssets` under "Intangible assets, net" and
            # Hercules prints `FiniteLivedIntangibleAssetsGross` under "Intangible
            # assets". Falling back to the words is what turned those into
            # disagreements.
            out.append(("FILING?", field, shown, None,
                        "the statement tags no such concept; its nearest caption is "
                        "a different one"))
            continue
        # A malformed renderer can attach the element link to only one comparative
        # cell. Keep the exact concept first, but also admit the visible caption
        # when it independently contains the shown value (EXDW FY2025).
        labelled = [hit for col in here for hit in statements.all_matching(
            field_printed, *phrases, column=col)]
        options = exact + labelled
        deduction = None
        if field == "current_liabilities" and not field_source.get("components"):
            context, _ = _selected_context(field_source)
            if context is not None:
                deduction = statements.liability_deduction_witness(
                    field_raw_printed, context, concept=field_source.get("tag"))
            if deduction is not None:
                options = [(deduction.concept, deduction.comparand)]
        scaled = [float(v) / (ratio if field == "shares" else 1) for _, v in options]
        note = f"printed as {options[0][0]!r}" if options else None
        if deduction is not None:
            note = (f"{deduction.kind}; {deduction.assessment}; literal {deduction.cell.lexical!r}, "
                    f"raw {deduction.cell.raw} x scale {deduction.cell.multiplier} "
                    f"= signed {deduction.cell.value}; quantity {deduction.comparand}; "
                    f"table {deduction.table.position}, column {deduction.cell.column}, "
                    f"physical column {deduction.cell.physical_column}, "
                    f"{deduction.context.end} {deduction.context.currency}; "
                    f"supporting rows {[(row.concept, row.position, row.cells[deduction.cell.column].lexical, row.cells[deduction.cell.column].value) for row in deduction.supporting_rows]}; "
                    f"native scope/sign {deduction.native_scope}/{deduction.native_sign_role}")
        if not scaled and identity and len(field_source.get("components") or []) == 2:
            for column in here:
                halves = []
                for component in field_source["components"]:
                    _, component_tagged, component_why = _qualified_views(field_raw_printed, component, document_source=field_source)
                    values = [] if component_why else _by_element(component_tagged, component.get("tag"), [column])
                    halves.append(values[0] if len(values) == 1 else None)
                if all(halves):
                    scaled.append(float(halves[0][1] - halves[1][1]))
                    note = f"not printed; {identity[0]} - {identity[1]}"
        if not scaled:
            out.append(("FILING?", field, shown, None, "no matching line on the statement"))
            continue
        agree = [v for v in scaled if abs(shown - v)
                 <= FILING_TOLERANCE * max(abs(shown), abs(v), 1e-9)]
        if agree:
            out.append(("FILING-OK", field, shown, agree[0], note))
        elif (field == "intangibles"
              and _selected_context(sources.get("goodwill") or {})[0] == _selected_context(field_source)[0]
              and any(_agrees(float(row.get("goodwill") or 0), float(value), label)
                      for col in here for label, value in statements.all_matching(field_printed, "Goodwill", column=col))
              and any(
                abs((shown + (row.get("goodwill") or 0)) - v)
                <= FILING_TOLERANCE * max(abs(shown + (row.get("goodwill") or 0)), abs(v), 1e-9)
                for v in scaled)):
            # A caption reading "Intangible assets" is often the combined line.
            # Affinity Bancshares prints $17,984,000 against $17,200,000 of goodwill
            # and $765,000 of intangibles, and tangible book deducts the sum either
            # way — the split differs, the deduction does not.
            out.append(("FILING-OK", field, shown, shown, "printed combined with goodwill"))
        elif field == "intangibles" and _the_finite_part_only(facts, sources, scaled):
            # `IntangibleAssetsNetExcludingGoodwill` is finite-lived plus
            # indefinite-lived; a balance sheet often prints only the first under a
            # caption that reads like the whole. Apple's $25.4bn against a printed
            # $20.3bn is the $5.1bn of indefinite-lived assets, and tangible book
            # deducts both. Confirmed against the filer's own finite-lived tag
            # rather than assumed from the size of the gap.
            out.append(("FILING?", field, shown, scaled[0],
                        "the total of finite and indefinite intangibles; the printed "
                        "line states the finite part"))
        elif any(op in (field_source.get("tag") or "") for op in (" - ", " + ")):
            # An assembled figure is not the line the statement prints. Total
            # liabilities from assets-minus-equity necessarily contains the mezzanine
            # a balance sheet shows between the two — Crawford Capital's $176.5M of
            # shares subject to redemption sits there. And a summed one is wider than
            # any single caption: Nephros' intangibles are $302,000 of finite-lived
            # assets plus a $148,000 licence, and only the first is printed under
            # "Intangible assets, net" though tangible book must deduct both.
            out.append(("FILING?", field, shown, scaled[0],
                        "assembled from components; the printed line is one of them"))
        elif any(_only_the_scale_differs(shown, v) for v in scaled):
            out.append(("FILING?", field, shown, scaled[0],
                        "the same figure under the scale the statement's header "
                        "declares, which its own numbers contradict"))
        else:
            out.append(("FILING", field, shown, scaled[0], note))
    return out


def _by_subtraction(printed, column: int, shown: float, source: dict) -> bool:
    """Whether the panel's profit is the consolidated total less the minority's share.

    Explicit component provenance pins each printed input to its own qualified
    concept. Inputs without that companion retain the parent-qualified caption
    convention: some filers tag only the redeemable half of minority interest.
    """
    components = source.get("components") or []
    parent_printed, _, why = _qualified_views(printed, source, duration=True)
    if why:
        return False
    if components:
        if len(components) != 2:
            return False
        inputs = []
        for component in components:
            _, tagged, why = _qualified_views(printed, component, duration=True, document_source=source)
            if why:
                return False
            inputs.append(_by_element(tagged, component.get("tag"), [column]))
        totals = inputs[0]
        minority = [value for _, value in inputs[1]]
    else:
        totals = statements.all_matching(parent_printed, "net income", "net income (loss)",
                                         "net loss", "consolidated net income", column=column)
        minority = [values[column] for label, values in parent_printed
                    if re.search(r"noncontrolling|non-controlling|minority", label, re.I)
                    and column < len(values) and values[column] is not None]
    for _, total in totals:
        for share in minority:
            for candidate in (float(total) - float(share), float(total) + float(share)):
                if abs(shown - candidate) <= FILING_TOLERANCE * max(
                        abs(shown), abs(candidate), 1e-9):
                    return True
    return False


def _by_fresh_start_periods(printed, headings: list[date], column: int,
                            phrases: tuple[str, ...], shown: float, source: dict) -> bool:
    """Reconcile explicitly named, contiguous predecessor/successor intervals."""
    if any(op in (source.get("tag") or "") for op in (" - ", " + ")):
        return False  # Compound subinterval operands need their own selected source witnesses.
    context, why = _selected_context(source, duration=True)
    if why or not printed or not isinstance(printed[0], statements.PrintedLine):
        return False
    headers = printed[0].headers
    if not (0 <= column < len(headers)) or headers[column].end != context.end:
        return False
    start = statements.period_start(headers[column])
    if start is None or not re.search(r"\bsuccessor\b", headers[column].text, re.I):
        return False
    neighbours = [other for other in (column - 1, column + 1)
                  if 0 <= other < len(headers)
                  and re.search(r"\bpredecessor\b", headers[other].text, re.I)
                  and statements.period_start(headers[other]) == context.start
                  and headers[other].end is not None
                  and headers[other].end + timedelta(days=1) == start]
    if len(neighbours) != 1:
        return False
    other = neighbours[0]
    current_context = statements.SourceContext(context.end, start, context.measurement, context.currency)
    earlier_context = statements.SourceContext(headers[other].end, context.start, context.measurement, context.currency)
    current = statements.all_matching(statements.qualify(printed, current_context), *phrases,
                                     column=column, only_the_parent=True)
    earlier = statements.all_matching(statements.qualify(printed, earlier_context), *phrases,
                                     column=other, only_the_parent=True)
    for label, value in current:
        for earlier_label, earlier_value in earlier:
            if label != earlier_label:
                continue
            combined = float(value) + float(earlier_value)
            if _agrees(shown, combined, label):
                return True
    return False


def _same_concept_in_another_filed_unit(facts: dict, source: dict,
                                        shown: float,
                                        printed_values: list[float]) -> bool:
    """Whether a 20-F filed this exact fact in two currencies/units.

    SEC's rendered statement chooses one presentation currency while Company
    Facts can carry the issuer's convenience translation as a second unit. A USD
    dashboard value and the same tagged CNY line are corroborating evidence, not
    a scale error. Require both numbers in the exact accession/context and in
    distinct units; a coincidental ratio is never enough.
    """
    tag_text = source.get("tag") or ""
    if any(op in tag_text for op in (" - ", " + ")) or ":" not in tag_text:
        return False
    namespace, tag = _source_qname(tag_text)
    units = (facts.get("facts", {}).get(namespace, {}).get(tag) or {}).get(
        "units", {})

    def close(left: float, right: float) -> bool:
        return abs(left - right) <= FILING_TOLERANCE * max(
            abs(left), abs(right), 1e-9)

    matching: dict[str, list[float]] = {}
    for unit, entries in units.items():
        values = []
        for entry in entries:
            if ((not source.get("accn")
                 or entry.get("accn") == source.get("accn"))
                    and entry.get("end") == source.get("end")
                    and (source.get("start") is None
                         or entry.get("start") == source.get("start"))):
                values.append(float(entry["val"]))
        if values:
            matching[unit] = values
    shown_units = {unit for unit, values in matching.items()
                   if any(close(shown, value) for value in values)}
    printed_units = {unit for unit, values in matching.items()
                     if any(close(printed, value) for value in values
                            for printed in printed_values)}
    return bool(shown_units and printed_units and shown_units.isdisjoint(printed_units))


def _printed_receipt_units(printed, source: dict, columns: list[int], options) -> set[str]:
    """Expose printed candidate units without selecting one by its amount."""
    concepts = {part.strip().replace(":", "_") for part in re.split(
        r" [-+] ", source.get("tag") or "")}
    exact_rows = [line for line in printed if getattr(getattr(line, "row", None), "concept", None) in concepts]
    labels = {label for label, _ in options}
    candidates = exact_rows or [line for line in printed if line[0] in labels]
    return {cell.currency for line in candidates for column in columns
            if column < len(getattr(line, "cells", ()))
            for cell in (line.cells[column],) if cell.value is not None and cell.currency is not None}


def against_income(row: dict, edgar, facts: dict | None = None) -> list[tuple]:
    """Every year the annual report prints, against the series the panel shows.

    A statement of operations carries two or three fiscal years side by side and the
    panel holds a series for each of revenue, profit, operating profit and earnings
    per share. Each printed column is checked against its own year rather than only
    the newest: three times the evidence for one request, and it reaches the history
    the ratio table is built from.

    Earnings per share needed this most. It is a printed line, and the trailing
    figure the P/E divides by is stitched from quarters and matches no line
    anywhere — the annual series is the only part of that chain a statement can
    confirm at all.
    """
    facts = facts or {}
    series = {
        "revenue": row.get("annual_revenue") or {},
        "gross_profit": row.get("annual_gross_profit") or {},
        "net_income": row.get("annual_net_income") or {},
        "eps": row.get("annual_eps") or {},
        "operating_income": row.get("annual_operating_income") or {},
    }
    concepts = row.get("sources") or {}
    annual_sources = row.get("annual_sources")
    reports = {}
    out = []
    if annual_sources is None:
        source = concepts.get("eps") or {}
        if not _is_annual_form(source.get("form") or ""):
            return []
        try:
            response = _read_statement(row, edgar, "income")
        except Exception as exc:
            return [("FILING?", "income", None, None, f"could not read source: {exc!r}"[:110])]
        (printed, _), _, headings, why = response
        if printed is None:
            return [("FILING?", "income", None, None, why)]
        reports[_report_key(source)] = response
        if any(heading is None for heading in headings):
            out.append(("FILING?", "income", None, None, "one or more printed column periods are unknown"))
    for year in sorted({year for years in series.values() for year in years}):
        for field, phrases in PRINTED_INCOME:
            shown = series[field].get(year)
            if shown is None:
                continue
            field_source = ((annual_sources.get("annual_" + field) or {}).get(str(year)) or {}
                            if annual_sources is not None else concepts.get(field) or {})
            legacy_end = (_annual_ratio_series(row).get(str(year)) or {}).get("end")
            if (not field_source.get("end") or annual_sources is None
                    and field_source.get("end") != legacy_end):
                out.append(("FILING?", f"FY{year} {field}", shown, None,
                            "no exact selected-year source context in the serialized series"))
                continue
            try:
                heading = date.fromisoformat(field_source["end"])
            except (TypeError, ValueError):
                out.append(("FILING?", f"FY{year} {field}", shown, None, "no exact selected source period"))
                continue
            if annual_sources is not None and not _is_annual_form(field_source.get("form") or ""):
                out.append(("FILING?", f"FY{year} {field}", shown, None, "selected source is not an annual filing"))
                continue
            if reason := field_source.get("operation_unassessed_reason"):
                out.append(("FILING?", f"FY{year} {field}", shown, None, reason))
                continue
            key = _report_key(field_source)
            if key not in reports:
                try:
                    reports[key] = _read_statement(row, edgar, "income", field_source)
                except Exception as exc:
                    out.append(("FILING?", f"FY{year} {field}", shown, None,
                                f"could not read source: {exc!r}"[:110]))
                    continue
            (raw_printed, _), _, field_headings, field_why = reports[key]
            if raw_printed is None:
                out.append(("FILING?", f"FY{year} {field}", shown, None, field_why))
                continue
            columns = [i for i, candidate in enumerate(field_headings) if candidate == heading]
            if not columns:
                out.append(("FILING?", f"FY{year} {field}", shown, None, "selected period has no identifiable printed column"))
                continue
            if field == "eps" and _ratio(row) != 1:
                units = _printed_receipt_units(raw_printed, field_source, columns, [])
                if len(units) > 1 and not field_source.get("unit"):
                    out.append(("FILING?", f"FY{year} {field}", shown, None,
                                "printed receipt candidates have distinct units "
                                + ", ".join(sorted(units))
                                + "; no exact selected-unit/basis witness"))
                    continue
            printed, tagged, context_why = _qualified_views(raw_printed, field_source, duration=True)
            if context_why:
                out.append(("FILING?", f"FY{year} {field}", shown, None, context_why))
                continue
            derived_operating = (
                field == "operating_income"
                and "derived and reconciled" in
                (field_source.get("concept") or "").lower()
            )
            # the parent-attribution guard is about whose profit a line states and
            # says nothing useful about a per-share figure, which it would exclude
            # outright for containing the words "per share"
            exact = (
                [hit for candidate in columns
                 for hit in _reconciled_operating_income(
                     raw_printed, field_source, candidate)]
                if derived_operating else
                _by_element(tagged, field_source.get("tag"), columns)
            )
            if derived_operating and not exact:
                out.append(("FILING?", f"FY{year} {field}", shown, None,
                            "the tagged subtotal components do not reconcile on "
                            "the rendered statement"))
                continue
            if not exact and tagged and _absent_concept(tagged, field_source.get("tag")):
                # The statement does not state this concept. New Mountain Finance is
                # a business development company and the panel carries its net
                # investment income per share, while its statement prints earnings
                # per share, which includes realised and unrealised gains. Two
                # different questions about the same year.
                out.append(("FILING?", f"FY{year} {field}", shown, None,
                            "the statement tags no such concept"))
                continue
            options = exact or [
                hit for candidate in columns
                for hit in statements.all_matching(
                    printed, *phrases, column=candidate,
                    only_the_parent=(field != "eps"), money_only=(field == "eps"))
            ]
            if field == "eps" and _ratio(row) != 1:
                units = _printed_receipt_units(printed, field_source, columns, options)
                if len(units) > 1:
                    out.append(("FILING?", f"FY{year} {field}", shown, None,
                                "printed receipt candidates have distinct units "
                                + ", ".join(sorted(units))
                                + "; no exact selected-unit/basis witness"))
                    continue
            if any(_agrees(shown, float(v), label) for label, v in options):
                out.append(("FILING-OK", f"FY{year} {field}", shown, shown, None))
                continue
            if _same_concept_in_another_filed_unit(
                    facts, field_source, shown, [float(v) for _, v in options]):
                out.append(("FILING?", f"FY{year} {field}", shown,
                            float(options[0][1]) if options else None,
                            "the same concept is filed in another currency/unit; "
                            "the rendered statement chose that presentation"))
                continue
            if (field == "net_income"
                    and any(_by_subtraction(raw_printed, candidate, shown, field_source)
                            for candidate in columns)):
                out.append(("FILING-OK", f"FY{year} {field}", shown, shown, None))
                continue
            if (field == "net_income"
                    and any(_by_fresh_start_periods(
                        raw_printed, field_headings, candidate, phrases, shown, field_source)
                            for candidate in columns)):
                out.append(("FILING-OK", f"FY{year} {field}", shown, shown,
                            "sum of predecessor and successor statement periods"))
                continue
            concept = field_source.get("tag", "")
            if (field == "revenue" and "RevenueFromContractWithCustomer" in concept
                    and options and all("other" in label.lower() for label, _ in options)):
                out.append(("FILING?", f"FY{year} {field}", shown, None,
                            "tagged as revenue from contracts with customers; the "
                            "statement totals only to a line that adds other income"))
                continue
            if field == "net_income" and "AvailableToCommon" in concept:
                out.append(("FILING?", f"FY{year} {field}", shown, None,
                            "tagged as income available to the common, which the "
                            "statement does not print"))
                continue
            if not options:
                out.append(("FILING?", f"FY{year} {field}", shown, None, "no matching line"))
                continue
            label, value = options[0]
            if _only_the_scale_differs(shown, float(value)):
                out.append(("FILING?", f"FY{year} {field}", shown, float(value),
                            "the same figure under the scale the statement declares"))
                continue
            ratio = _ratio(row)
            if field == "eps" and ratio != 1 and abs(abs(shown / float(value)) - ratio) <= 0.02 * ratio:
                # A depositary receipt earns what its ordinary shares earn, times the
                # number it stands for. DBV Technologies' statement is per ordinary
                # share and the panel is per receipt, five to one.
                out.append(("FILING?", f"FY{year} {field}", shown, float(value),
                            f"per receipt, which is {ratio:g} ordinary shares"))
                continue
            split_value = next((float(candidate) for _, candidate in options
                                if field == "eps" and _split_since(
                                    facts, heading, shown, float(candidate))), None)
            if split_value is not None:
                out.append(("FILING?", f"FY{year} {field}", shown, split_value,
                            "split-adjusted; this filing predates the split"))
                continue
            if any(_only_the_scale_differs(shown, float(v)) for _, v in options):
                out.append(("FILING?", f"FY{year} {field}", shown, float(value),
                            "the same figure under the scale the statement declares"))
                continue
            if _restated_since(facts, field, heading, shown):
                out.append(("FILING?", f"FY{year} {field}", shown, float(value),
                            "restated after this filing, which prints the original"))
                continue
            out.append(("FILING", f"FY{year} {field}", shown, float(value),
                        f"no printed line matches; nearest is {label!r}"))
    return out


# What each series is tagged as, for asking whether a later filing revised it.
_CONCEPT_TAGS = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
                "Revenue", "RevenueFromContractsWithCustomers"),
    "net_income": ("NetIncomeLoss", "ProfitLoss",
                   "NetIncomeLossAvailableToCommonStockholdersBasic",
                   "ProfitLossAttributableToOwnersOfParent"),
    "eps": ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted",
            "IncomeLossFromContinuingOperationsPerDilutedShare",
            "DilutedEarningsLossPerShare", "BasicEarningsLossPerShare"),
    "operating_income": ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities"),
}


def _split_since(facts: dict, heading, shown: float, printed: float) -> bool:
    """Whether the gap is a share split the panel has applied and the filing has not.

    Mueller Industries' 10-K prints $6.86 of diluted earnings for 2025 and the panel
    shows $3.43, because the company split two for one afterwards and every
    per-share figure was restated onto the new count. The evidence is the share
    count itself: for one period end the February filing states 111,179,750 and the
    July filing 222,359,500.
    """
    if not shown or not printed:
        return False
    factor = printed / shown
    # Either direction. A forward split divides the per-share figures the panel
    # carries and a reverse split multiplies them, and small companies do the
    # second far more often — Palatin's earnings are fifty times the printed
    # figure, Yarrow's fifty, Sunlands twelve.
    if abs(factor) < 1:
        factor = 1 / factor
    if not 1.5 <= abs(factor) <= 10000:
        return False
    # A split restates the whole series, so the evidence is looked for at any period
    # end the filer reports twice — not only at this column's. Mueller's doubled
    # count stands against 2025-12-27 and the panel adjusts 2023 and 2024 with it.
    by_end: dict[str, set] = {}
    for namespace, tags in (
        ("us-gaap", ("CommonStockSharesOutstanding",
                     "WeightedAverageNumberOfDilutedSharesOutstanding")),
        ("ifrs-full", ("NumberOfSharesOutstanding", "AdjustedWeightedAverageShares",
                       "WeightedAverageShares")),
    ):
        for tag in tags:
            for units in (facts.get("facts", {}).get(namespace, {}).get(tag) or {}).get("units", {}).values():
                for e in units:
                    value = float(e.get("val") or 0)
                    if value > 0 and e.get("end"):
                        by_end.setdefault(e["end"], set()).add(value)
    return any(abs(big / small - factor) <= 0.02 * factor
               for counts in by_end.values()
               for small in counts for big in counts if small)


def _restated_since(facts: dict, field: str, heading, shown: float) -> bool:
    """Whether the panel's figure is a later filing's revision of this one.

    An annual report prints the year as it stood when it was written. Berkshire
    restated 2016 revenue from $223.6bn to $215.1bn in its 2019 report, and
    Trans-American revised one year three times; the panel carries the latest, which
    is the restatement rule working exactly as intended. Reading an older statement
    then shows a real difference that is not a disagreement.

    Confirmed rather than assumed: the figure on the panel has to actually appear in
    a LATER filing for the same period.
    """
    end = heading.isoformat()
    for namespace in ("us-gaap", "ifrs-full"):
        for tag in _CONCEPT_TAGS.get(field, ()):
            for units in (facts.get("facts", {}).get(namespace, {}).get(tag) or {}).get("units", {}).values():
                for e in units:
                    if e.get("end") != end or "start" not in e:
                        continue
                    value = float(e["val"])
                    if abs(shown - value) <= FILING_TOLERANCE * max(abs(shown), abs(value), 1e-9):
                        return True
    return False


def _agrees(shown: float, printed_value: float, label: str) -> bool:
    """Compare the literal printed sign, using the existing numeric tolerance."""
    return abs(shown - printed_value) <= FILING_TOLERANCE * max(
        abs(shown), abs(printed_value), 1e-9)


# Cash-flow-statement components behind the explicitly labelled all-capex floor.
# Reported earnings comes from the income statement, so only these are read here.
CASH_FLOW = {
    "operating cash flow": (
        "us-gaap_NetCashProvidedByUsedInOperatingActivities",
        "us-gaap_NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "ifrs-full_CashFlowsFromUsedInOperatingActivities"),
    "+ depreciation & amortisation": (
        "us-gaap_DepreciationDepletionAndAmortization",
        "us-gaap_DepreciationAmortizationAndAccretionNet",
        "us-gaap_DepreciationAndAmortization",
        "us-gaap_DepreciationDepletionAndAmortizationExcludingAmortizationOfDeferredFinancingFeesAndDebtDiscounts",
        "fusb_DepreciationAndAmortizationOfPropertyPlantAndEquipmentAndComputerPrograms",
        "ifrs-full_AdjustmentsForDepreciationAndAmortisationExpense"),
    "- total capital expenditure": (
        "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment",
        "us-gaap_PaymentsToAcquireProductiveAssets",
        "us-gaap_PaymentsToAcquireOtherPropertyPlantAndEquipment",
        "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        "ifrs-full_PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets"),
}


def against_cash_flow(row: dict, edgar) -> list[tuple]:
    """The all-capex-floor components against the cash-flow statement."""
    earnings = row.get("owner_earnings") or {}
    components = dict(earnings.get("components") or [])
    components.update(dict(earnings.get("free_cash_flow_components") or []))
    if not components:
        return []
    # the column for the year the figure was struck in, which is not always one the
    # filing prints: Berkshire's per-share provenance points at a 2017 report, and
    # its all-capex evidence is a recent year's
    year = str(earnings.get("fiscal_year"))
    # by the date the panel says that year ended, not by the calendar year the date
    # falls in: Walmart's fiscal 2025 ends 2026-01-31, and matching on the year
    # alone read its cash flow a year out
    ends = {str(y): v.get("end") for y, v in (row.get("annual_ratios") or {}).items()
            if isinstance(v, dict)}
    wanted = ends.get(year)
    out = []
    reports = {}
    source_keys = {"operating cash flow": "operating_cash_flow",
                   "+ depreciation & amortisation": "depreciation_and_amortisation",
                   "- total capital expenditure": "total_capex"}
    for name, concepts in CASH_FLOW.items():
        shown = components.get(name)
        if shown is None:
            continue
        source = (earnings.get("sources") or {}).get(source_keys[name]) or {}
        if source.get("end") != wanted or not wanted:
            out.append(("FILING?", f"all-capex evidence {name}", shown, None,
                        "no exact selected-year component source context"))
            continue
        key = _report_key(source)
        if key not in reports:
            try:
                reports[key] = _read_statement(row, edgar, "cash_flow", source)
            except Exception as exc:
                out.append(("FILING?", f"all-capex evidence {name}", shown, None,
                            f"could not read component source: {exc!r}"[:110]))
                continue
        (raw_printed, _), columns, _, why = reports[key]
        if raw_printed is None:
            out.append(("FILING?", f"all-capex evidence {name}", shown, None, why))
            continue
        _, tagged, why = _qualified_views(raw_printed, source, duration=True)
        if why:
            out.append(("FILING?", f"all-capex evidence {name}", shown, None, why))
            continue
        values = [float(value) for _, value in _by_element(tagged, source.get("tag"), columns)]
        if not values:
            out.append(("FILING?", f"all-capex evidence {name}", shown, None,
                        "no numeric matching concept in the selected source context"))
            continue
        # capital expenditure is a payment, so its sign is the filer's convention
        if any(abs(abs(shown) - abs(v)) <= FILING_TOLERANCE * max(abs(shown), abs(v), 1e-9)
               for v in values):
            out.append(("FILING-OK", f"all-capex evidence {name}", shown, shown, None))
        else:
            # A filer may tag several depreciation concepts of overlapping scope and
            # print one of them: Vistra states $1,986M of depreciation, depletion and
            # amortisation and $2,950M with accretion added, and accretion is not
            # depreciation. XPO's cash flow line includes $58M of intangible
            # amortisation the panel's element excludes. A choice of definition, and
            # the panel's is the narrower and more conservative one.
            out.append(("FILING?", f"all-capex evidence {name}", shown, values[0],
                        "the statement adds back a wider depreciation concept"))
    return out


def _one_moment(row: dict, facts: dict) -> list[tuple]:
    """Components struck at an older date THAN THE FILER HAS SINCE PUBLISHED.

    A balance sheet is one moment, but not every line is republished every quarter:
    Cohen & Steers tags its permanent minority interest quarterly and its redeemable
    half only in the 10-K, so a June total legitimately carries a December piece.
    Overstating a deduction from common equity is the conservative direction, and
    the provenance names both dates, so that case is disclosed rather than wrong.

    What is wrong is a component left behind when a newer value for the same
    concept exists — Energy Transfer's redeemable interest read $250M from December
    while $256M stood in the June filing under a renamed element. So the test is not
    "do the dates differ" but "did the filer already say something newer".
    """
    out = []
    for name, source in (row.get("sources") or {}).items():
        parent = (source or {}).get("end")
        if not parent:
            continue
        for part in (source or {}).get("components") or ():
            end, tag = part.get("end"), (part.get("tag") or "")
            if not end or end >= parent:
                continue
            ns, bare = _source_qname(tag)
            units = ((facts.get("facts", {}).get(ns or "us-gaap", {}).get(bare) or {})
                     .get("units", {}))
            preferred_unit = (
                "shares" if name in ("shares", "options", "rsus", "cover_shares")
                else "USD/shares" if name == "eps"
                else "USD"
            )
            entries = list(units.get(preferred_unit, ()))
            # only forms the engine itself reads. Cycurion's line of credit has a
            # newer figure, but it stands in an S-1 — a registration statement, not
            # a periodic report — and the engine deliberately reads neither, so its
            # 10-K figure is not superseded by anything it was ever going to see.
            newer = [e for e in entries if "start" not in e and e.get("end", "") > end
                     and _is_financial_form(e.get("form", ""))]
            if newer:
                out.append((name, parent, [f"{bare} stands at {max(e['end'] for e in newer)}"]))
    return out


def _rounding_slack(numerator: float, denominator: float) -> float:
    """How far a ratio can move because its denominator was stored rounded.

    The engine divides an exact book value; the payload keeps it to four decimals.
    That is nothing for a $25 share and everything for a sub-penny one: Transportation
    and Logistics Systems' book value stores as $0.0016, which is $0.0016 give or take
    3%, and the ratio built on it inherits that.
    """
    if not denominator:
        return 0.0
    # Both inputs are serialized to four decimals. Normally the numerator term
    # is microscopic; for MVCO's $0.0011 historical price it is almost one cent
    # on the displayed multiple and cannot be ignored.
    numerator_error = 0.00005 / abs(denominator)
    denominator_error = abs(numerator / denominator) * (0.00005 / abs(denominator))
    continuous = numerator_error + denominator_error
    # A ratio exactly on a half-cent boundary can round either way once the hidden
    # fifth decimal of either serialized input is restored. TATT stores 5.3000 /
    # 8.4800 = 0.625, while the unrounded close lies just above it: 0.63 is valid
    # even though recomputing from the payload alone uses banker's rounding to 0.62.
    centre = round(numerator / denominator, 2)
    low = round((numerator - 0.00005) / (denominator + 0.00005), 2)
    high = round((numerator + 0.00005) / (denominator - 0.00005), 2)
    return max(continuous, abs(low - centre), abs(high - centre))


def _derived_series(row: dict) -> list[tuple]:
    """The ratio table and the chapter-13 statistics, against their own inputs.

    Nothing here needs a filing: every one of these is arithmetic on figures the
    statement checks have already confirmed. They had never been checked at all, and
    they are most of what a reader compares one company to another with — the
    per-year multiples in the table, and the growth and stability figures beneath
    it.
    """
    out = []

    def check(name, shown, expected, formula, slack=0.0):
        if shown is not None and expected is not None:
            out.append((name, shown, expected, formula, slack))

    eps = {int(y): v for y, v in (row.get("annual_eps") or {}).items()}
    stats = row.get("ch13") or {}
    if eps and stats.get("latest_fy"):
        latest = stats["latest_fy"]
        recent = [eps[y] for y in range(latest - 2, latest + 1) if y in eps]
        if len(recent) == 3:
            # The engine averages Decimal filing values, then converts once for
            # display. Summing binary payload floats first can land on the other
            # side of a half-cent boundary (VTEX: exact 0.035 -> 0.04).
            exact_mean = sum(Decimal(str(v)) for v in recent) / Decimal(3)
            check("ch13.avg_recent", stats.get("avg_recent"),
                  round(float(exact_mean), 2), "mean of the last three years' EPS")
        window = [eps[y] for y in range(latest - 9, latest + 1) if y in eps]
        if window:
            check("ch13.ten_year_present", stats.get("ten_year_present"), len(window),
                  "how many of the last ten years have an EPS")
            # strictly positive: a year earning exactly nothing was not a positive
            # year, though it is also not the deficit criterion 4 tests for
            check("ch13.ten_year_positive", stats.get("ten_year_positive"),
                  sum(1 for v in window if v > 0), "how many of those earned something")

    earnings = row.get("owner_earnings") or {}
    component_map = dict(earnings.get("components") or [])
    parts = list(component_map.values())
    if parts and earnings.get("all_capex_floor") is not None:
        check("owner_earnings.all_capex_floor", earnings["all_capex_floor"], sum(parts),
              "reported earnings plus D&A less total capex")
    reported = (component_map.get("reported earnings available to common")
                if "reported earnings available to common" in component_map
                else component_map.get("reported earnings attributable to owners"))
    if reported is not None and earnings.get("maintenance_estimate") is not None:
        check("owner_earnings.maintenance_estimate", earnings["maintenance_estimate"],
              reported, "reported earnings when maintenance capex is assumed equal to D&A")
    cash_parts = [value for _, value in (earnings.get("free_cash_flow_components") or [])]
    if cash_parts and earnings.get("free_cash_flow") is not None:
        check("owner_earnings.free_cash_flow", earnings["free_cash_flow"],
              sum(cash_parts), "operating cash flow less total capex")
    reconciliation = earnings.get("fcf_reconciliation") or {}
    for method, detail in (reconciliation.get("methods") or {}).items():
        components = [value for _, value in (detail.get("components") or [])]
        if components and detail.get("value") is not None:
            check(f"owner_earnings.fcf_reconciliation.{method}", detail["value"],
                  sum(components), detail.get("formula") or "sum of displayed components")
    methods = list((reconciliation.get("methods") or {}).values())
    method_values = [method.get("value") for method in methods
                     if method.get("value") is not None]
    if len(method_values) == 3:
        check("owner_earnings.fcf_reconciliation.spread", reconciliation.get("spread"),
              max(method_values) - min(method_values), "maximum method less minimum method")
    capital = earnings.get("invested_capital")
    capital_evidence = earnings.get("invested_capital_evidence") or {}
    beginning = (capital_evidence.get("beginning") or {}).get("value")
    ending = (capital_evidence.get("ending") or {}).get("value")
    if beginning is not None and ending is not None:
        check("owner_earnings.invested_capital", capital,
              (beginning + ending) / 2,
              "average of exact beginning and ending invested capital")
    ntoa = earnings.get("average_net_tangible_operating_assets")
    ntoa_evidence = earnings.get("net_tangible_operating_assets_evidence") or {}
    ntoa_beginning = (ntoa_evidence.get("beginning") or {}).get("value")
    ntoa_ending = (ntoa_evidence.get("ending") or {}).get("value")
    if ntoa_beginning is not None and ntoa_ending is not None:
        check("owner_earnings.average_net_tangible_operating_assets", ntoa,
              (ntoa_beginning + ntoa_ending) / 2,
              "average of exact beginning and ending net tangible operating assets")
    lease_neutral_ntoa = earnings.get(
        "average_lease_neutral_net_tangible_operating_assets")
    lease_neutral_evidence = (
        earnings.get("lease_neutral_net_tangible_operating_assets_evidence") or {})
    lease_neutral_beginning = (
        lease_neutral_evidence.get("beginning") or {}).get("value")
    lease_neutral_ending = (
        lease_neutral_evidence.get("ending") or {}).get("value")
    if lease_neutral_beginning is not None and lease_neutral_ending is not None:
        check("owner_earnings.average_lease_neutral_net_tangible_operating_assets",
              lease_neutral_ntoa,
              (lease_neutral_beginning + lease_neutral_ending) / 2,
              "average of exact beginning and ending lease-neutral NTOA")
    if earnings.get("all_capex_return") is not None and capital:
        check("owner_earnings.all_capex_return", earnings["all_capex_return"],
              earnings["all_capex_floor"] / capital * 100,
              "all-capex floor over invested capital")
    if earnings.get("maintenance_estimate_return") is not None and capital:
        check("owner_earnings.maintenance_estimate_return",
              earnings["maintenance_estimate_return"],
              earnings["maintenance_estimate"] / capital * 100,
              "maintenance≈D&A estimate over invested capital")
    for field, deduction, formula in (
        ("free_cash_flow_after_stock_compensation", "stock_compensation",
         "free cash flow less stock compensation"),
        ("free_cash_flow_after_acquisitions", "cash_acquisitions",
         "free cash flow less cash acquisitions"),
        ("expanded_free_cash_flow", "capitalized_intangible_investment",
         "free cash flow less capitalized software and intangible investment"),
    ):
        if (earnings.get(field) is not None and earnings.get("free_cash_flow") is not None
                and earnings.get(deduction) is not None):
            check(f"owner_earnings.{field}", earnings[field],
                  earnings["free_cash_flow"] - abs(earnings[deduction]), formula)
    if (earnings.get("nopat_roic") is not None and earnings.get("nopat") is not None
            and capital):
        check("owner_earnings.nopat_roic", earnings["nopat_roic"],
              earnings["nopat"] / capital * 100,
              "NOPAT over average invested capital")
    if earnings.get("ronta") is not None and earnings.get("nopat") is not None and ntoa:
        check("owner_earnings.ronta", earnings["ronta"],
              earnings["nopat"] / ntoa * 100,
              "NOPAT over average net tangible operating assets")
    if (earnings.get("lease_neutral_ronta") is not None
            and earnings.get("nopat") is not None and lease_neutral_ntoa):
        check("owner_earnings.lease_neutral_ronta",
              earnings["lease_neutral_ronta"],
              earnings["nopat"] / lease_neutral_ntoa * 100,
              "NOPAT over average lease-neutral net tangible operating assets")
    for year, cell in (earnings.get("annual_per_share") or {}).items():
        if not isinstance(cell, dict):
            continue
        bridge = cell.get("cash_flow_bridge") or {}

        def bridge_value(name):
            point = bridge.get(name)
            if isinstance(point, dict):       # engine <=117 compatibility
                return point.get("value")
            if isinstance(point, list):
                return point[0] if point else None
            return point if isinstance(point, (int, float)) else None

        net_income = bridge_value("net_income")
        depreciation = bridge_value("depreciation_and_amortisation")
        stock_compensation = bridge_value("stock_compensation")
        other_adjustments = bridge_value("other_operating_cash_flow_adjustments")
        working_capital = bridge_value("working_capital_cash_effect")
        operating_cash = bridge_value("operating_cash_flow")
        if all(value is not None for value in (
                net_income, depreciation, other_adjustments, operating_cash)):
            check(f"owner_earnings.annual_per_share.{year}.cash_flow_bridge."
                  "operating_cash_flow", operating_cash,
                  net_income + depreciation + (stock_compensation or 0)
                  + other_adjustments + (working_capital or 0),
                  "net income + D&A + separately filed SBC + other residual "
                  "+ working-capital cash effect")
        capex = bridge_value("total_capital_expenditure")
        free_cash_flow = bridge_value("free_cash_flow")
        if operating_cash is not None and capex is not None and free_cash_flow is not None:
            check(f"owner_earnings.annual_per_share.{year}.cash_flow_bridge."
                  "free_cash_flow", free_cash_flow, operating_cash - capex,
                  "operating cash flow less cash capital expenditure")
        shares = cell.get("diluted_shares")
        for total_key, share_key, label in (
            ("all_capex_floor", "all_capex_floor_per_share", "all-capex floor"),
            ("maintenance_estimate", "maintenance_estimate_per_share",
             "maintenance≈D&A estimate"),
            ("free_cash_flow", "free_cash_flow_per_share", "free cash flow"),
            ("free_cash_flow_after_stock_compensation",
             "free_cash_flow_after_stock_compensation_per_share",
             "free cash flow after stock compensation"),
            ("free_cash_flow_after_acquisitions",
             "free_cash_flow_after_acquisitions_per_share",
             "free cash flow after acquisitions"),
            ("expanded_free_cash_flow", "expanded_free_cash_flow_per_share",
             "expanded free cash flow"),
        ):
            total = cell.get(total_key)
            if total is not None and shares:
                check(f"owner_earnings.annual_per_share.{year}.{share_key}",
                      cell.get(share_key), total / shares,
                      f"{label} over diluted weighted shares")

    for year, cell in _annual_ratio_series(row).items():
        if not isinstance(cell, dict):
            continue
        price, book = cell.get("price"), cell.get("bvps")
        if price and book:
            check(f"annual_ratios.{year}.pb", cell.get("pb"), round(price / book, 2),
                  "that year's price over that year's book value per share",
                  _rounding_slack(price, book))
        tangible = cell.get("tbvps")
        if price and tangible and tangible > 0:
            check(f"annual_ratios.{year}.ptbv", cell.get("ptbv"), round(price / tangible, 2),
                  "that year's price over its tangible book value per share",
                  _rounding_slack(price, tangible))
        revenue = (row.get("annual_revenue") or {}).get(year)
        gross_profit = (row.get("annual_gross_profit") or {}).get(year)
        income = (row.get("annual_net_income") or {}).get(year)
        if revenue is not None:
            check(f"annual_ratios.{year}.revenue", cell.get("revenue"), revenue,
                  "that year's reported revenue")
        if revenue and gross_profit is not None:
            check(f"annual_ratios.{year}.gross_margin", cell.get("gross_margin"),
                  gross_profit / revenue * 100,
                  "that year's gross profit over revenue")
        if revenue and income is not None:
            check(f"annual_ratios.{year}.net_margin", cell.get("net_margin"),
                  round(income / revenue * 100, 4), "that year's profit over its sales")
        net_tangible_assets = cell.get("net_tangible_assets")
        if net_tangible_assets is not None and net_tangible_assets > 0 and income is not None:
            check(f"annual_ratios.{year}.return_on_net_tangible_assets",
                  cell.get("return_on_net_tangible_assets"),
                  income / net_tangible_assets * 100,
                  "that year's profit over its net tangible assets")
        average_equity = cell.get("average_common_equity")
        if average_equity is not None and average_equity > 0 and income is not None:
            check(f"annual_ratios.{year}.return_on_equity",
                  cell.get("return_on_equity"), income / average_equity * 100,
                  "that year's profit over average common equity")
        common_equity, combined_debt = (cell.get("common_equity"),
                                        cell.get("combined_debt"))
        if common_equity is not None and common_equity > 0 and combined_debt is not None:
            check(f"annual_ratios.{year}.debt_to_equity", cell.get("debt_to_equity"),
                  combined_debt / common_equity,
                  "that year's combined interest-bearing debt over common equity")
        operating_income = cell.get("operating_income_for_nopat")
        tax_rate = cell.get("normalized_tax_rate")
        nopat = cell.get("nopat")
        if operating_income is not None and tax_rate is not None:
            check(f"annual_ratios.{year}.nopat", nopat,
                  operating_income * (1 - tax_rate / 100),
                  "operating income x (1 - normalized effective tax rate)")
        evidence = cell.get("operating_return_evidence") or {}
        for field, beginning_key, ending_key, label in (
            ("invested_capital", "invested_capital_beginning",
             "invested_capital_ending", "cash-excluded invested capital"),
            ("capital_including_cash", "capital_including_cash_beginning",
             "capital_including_cash_ending", "capital including cash"),
            ("average_net_tangible_operating_assets",
             "net_tangible_operating_assets_beginning",
             "net_tangible_operating_assets_ending",
             "net tangible operating assets"),
            ("average_lease_neutral_net_tangible_operating_assets",
             "lease_neutral_net_tangible_operating_assets_beginning",
             "lease_neutral_net_tangible_operating_assets_ending",
             "lease-neutral net tangible operating assets"),
        ):
            beginning = (evidence.get(beginning_key) or {}).get("value")
            ending = (evidence.get(ending_key) or {}).get("value")
            if (cell.get(field) is not None
                    and beginning is not None and ending is not None):
                check(f"annual_ratios.{year}.{field}", cell.get(field),
                      (beginning + ending) / 2,
                      f"average beginning and ending {label}")
        for field, denominator, label in (
            ("nopat_roic", cell.get("invested_capital"),
             "NOPAT over average cash-excluded invested capital"),
            ("nopat_return_including_cash", cell.get("capital_including_cash"),
             "NOPAT over average capital including cash"),
            ("ronta", cell.get("average_net_tangible_operating_assets"),
             "NOPAT over average net tangible operating assets"),
            ("lease_neutral_ronta",
             cell.get("average_lease_neutral_net_tangible_operating_assets"),
             "NOPAT over average lease-neutral net tangible operating assets"),
        ):
            if nopat is not None and denominator is not None and denominator > 0:
                check(f"annual_ratios.{year}.{field}", cell.get(field),
                      nopat / denominator * 100, label)

    estimate = row.get("operating_returns_estimate") or {}
    if estimate:
        assumptions = set(estimate.get("operating_return_assumptions") or ())
        allowed = {
            "short_term_investments", "noncurrent_investments",
            "goodwill", "intangibles",
        }
        strict = row.get("operating_returns") or {}
        check("operating_returns_estimate.status_guard",
              int(estimate.get("status") == "CONSERVATIVE_LOWER_BOUND"), 1,
              "the estimate is explicitly segregated from reported returns")
        check("operating_returns_estimate.assumption_guard",
              int(bool(assumptions) and assumptions <= allowed), 1,
              "only absent optional denominator deductions may be bounded at zero")
        check("operating_returns_estimate.strict_gap_guard",
              int(any(strict.get(field) is None and estimate.get(field) is not None
                      for field in ("nopat_roic", "ronta"))), 1,
              "an estimate is published only when it fills a strict ROIC/RONTA gap")
        check("operating_returns_estimate.positive_guard",
              int(all((estimate.get(field) or 0) > 0
                      for field in ("nopat", "nopat_roic", "ronta"))), 1,
              "a zero deduction is a lower bound only for a positive return")
        operating_income = estimate.get("operating_income_for_nopat")
        tax_rate = estimate.get("normalized_tax_rate")
        nopat = estimate.get("nopat")
        if operating_income is not None and tax_rate is not None:
            check("operating_returns_estimate.nopat", nopat,
                  operating_income * (1 - tax_rate / 100),
                  "operating income x (1 - normalized effective tax rate)")
        invested = estimate.get("invested_capital")
        capital_including_cash = estimate.get("capital_including_cash")
        if invested is not None and capital_including_cash is not None:
            check("operating_returns_estimate.denominator_floor_guard",
                  int(invested >= capital_including_cash * 0.02), 1,
                  "cash-excluded invested capital is at least 2% of capital including cash")
        for field, denominator, label in (
            ("nopat_roic", invested,
             "NOPAT over conservatively bounded average invested capital"),
            ("ronta", estimate.get("average_net_tangible_operating_assets"),
             "NOPAT over conservatively bounded average NTOA"),
            ("lease_neutral_ronta",
             estimate.get("average_lease_neutral_net_tangible_operating_assets"),
             "NOPAT over conservatively bounded average lease-neutral NTOA"),
        ):
            if nopat is not None and denominator is not None and denominator > 0:
                check(f"operating_returns_estimate.{field}", estimate.get(field),
                      nopat / denominator * 100, label)
        if strict.get("nopat") is not None:
            check("operating_returns_estimate.nopat_matches_strict", nopat,
                  strict["nopat"],
                  "the estimate changes denominator deductions, never NOPAT")

    quality = row.get("return_quality_assumption") or {}
    if quality:
        check("return_quality_assumption.status_guard",
              int(quality.get("status") in {"APPLIED", "UNAVAILABLE"}), 1,
              "the Return Quality overlay declares whether assumption mode ran")
        if quality.get("status") == "APPLIED":
            by_input = quality.get("input_assumptions") or {}
            disclosed = {
                assumption
                for assumptions in by_input.values()
                for assumption in (assumptions or ())
            }
            check("return_quality_assumption.disclosure_guard",
                  int(disclosed == set(quality.get("applied") or ())), 1,
                  "every zero substitution is assigned to an affected score input")
            check("return_quality_assumption.roe_strict_guard",
                  int(not (by_input.get("roe") or ())), 1,
                  "ROE remains filing-strict because the ratio table does not invent it")
            strict_returns = row.get("operating_returns") or {}
            comparisons = (
                ("roe", quality.get("roe"),
                 (row.get("profitability") or {}).get("on_equity")),
                ("nopat_roic", quality.get("nopat_roic"),
                 strict_returns.get("nopat_roic")),
                ("ronta", quality.get("ronta"), strict_returns.get("ronta")),
                ("debt_to_equity", quality.get("debt_to_equity"),
                 row.get("debt_to_equity")),
            )
            for field, shown, strict_value in comparisons:
                if strict_value is not None and not (by_input.get(field) or ()):
                    check(f"return_quality_assumption.{field}.strict_match",
                          shown, strict_value,
                          "an input with no substitution matches the strict calculation")
    return out


def _c(row: dict, n: int):
    """A criterion's displayed value, looked up by number — never by position."""
    return next((c.get("value") for c in (row.get("criteria") or ()) if c.get("n") == n), None)


# One of each shape the engine has to handle, so a pass means something: a
# December mega-cap, a June filer, a financial, a REIT, a depositary receipt, a
# dual-class, a loss-maker, a partnership, a small cap, a recent listing.
SPREAD = ("KO", "MSFT", "JPM", "O", "ZLAB", "GOOGL", "RIVN", "ET", "EML", "AAPL", "JNJ", "FUSB",
          "BRK-B", "PG", "XOM", "UNH", "T", "F", "PLTR", "MKL", "NKE", "ORCL")


def audit(rows: list[dict], cache: Path, quiet: bool = False, edgar=None) -> dict:
    totals = {"sourced_ok": 0, "sourced_bad": 0, "derived_ok": 0, "derived_bad": 0,
              "unchecked": 0, "mixed_dates": 0, "filing_ok": 0, "filing_bad": 0,
              "filing_unknown": 0}
    problems = []
    uncheckable = []
    native_sources = []
    for row in rows:
        facts = _facts(row["cik"], cache)
        sources = row.get("sources") or {}
        lines = []
        for field, key in SOURCED:
            shown, source = _payload_field(row, field), sources.get(key)
            if shown is None:
                continue
            # HCA's goodwill tag died in 2011, so the engine carries the whole
            # combined intangibles line as intangibles and sets goodwill to 0 — the
            # provenance says so in as many words. The pair still sums to the tagged
            # figure, which is what tangible book deducts, so it is checked as a pair
            # below rather than as two figures that each fail on their own.
            if "contained in" in (source or {}).get("concept", ""):
                continue
            if not source or not source.get("accn"):
                totals["unchecked"] += 1
                lines.append(("UNCHECKED", field, shown, None, "no provenance recorded"))
                continue
            ns, bare = _source_qname(source.get("tag") or "")
            ratio = _ratio(row) if field in ("shares", "cover_shares") else 1
            candidates = [v / ratio for v in _source_values(facts, source)]
            if not candidates:                      # a derived or summed provenance
                one = _fact_in_filing(facts, source)
                candidates = [] if one is None else [one / ratio]
            filed = next((v for v in candidates if _close(shown, v)),
                         candidates[0] if candidates else None)
            if filed is None:
                totals["unchecked"] += 1
                lines.append(("UNCHECKED", field, shown, None,
                              f"{source['tag']} @ {source['accn']} not in the cached filing"))
            elif _close(shown, filed):
                totals["sourced_ok"] += 1
            else:
                totals["sourced_bad"] += 1
                lines.append(("SOURCED", field, shown, filed,
                              f"{source['tag']} {source['end']} {source['accn']}"))
        # The recurring dividend is the filing's direct quarterly per-share fact
        # annualized fourfold (and, for an ADR, put onto the receipt basis). It is
        # derived, but its one source still has to name the exact filing quarter.
        recurring = row.get("recurring_dividend_per_share")
        recurring_source = sources.get("recurring_dividend_per_share")
        if recurring is not None:
            raw = (_fact_in_filing(facts, recurring_source)
                   if recurring_source and recurring_source.get("accn") else None)
            expected = None if raw is None else raw * 4 * _ratio(row)
            if expected is None:
                totals["unchecked"] += 1
                lines.append(("UNCHECKED", "recurring_dividend_per_share", recurring,
                              None, "quarterly dividend provenance is unavailable"))
            elif _close(recurring, expected):
                totals["sourced_ok"] += 1
            else:
                totals["sourced_bad"] += 1
                lines.append(("SOURCED", "recurring_dividend_per_share", recurring,
                              expected, "quarterly filing rate x 4"))
        for name, shown, expected, formula, *slack in _identities(row) + _derived_series(row):
            if _close(shown, expected, slack[0] if slack else 0.0):
                totals["derived_ok"] += 1
            else:
                totals["derived_bad"] += 1
                lines.append(("DERIVED", name, shown, expected, formula))
        extra_checks = list(_prior_history_checks(row, facts)) + list(_equity_checks(row, facts))
        if "_audit_edinet" in facts:
            extra_checks.extend(_edinet_cash_checks(row, facts))
        for line in extra_checks:
            bucket = {"SOURCED-OK": "sourced_ok", "SOURCED": "sourced_bad",
                      "DERIVED-OK": "derived_ok", "DERIVED": "derived_bad",
                      "UNCHECKED": "unchecked"}[line[0]]
            totals[bucket] += 1
            if line[0] not in {"SOURCED-OK", "DERIVED-OK"}:
                lines.append(line)
        for name, parent, stale in _one_moment(row, facts):
            totals["mixed_dates"] = totals.get("mixed_dates", 0) + 1
            lines.append(("ONE-DATE", name, parent, None,
                          "a newer value exists: " + ", ".join(stale)))
        if edgar is not None:
            filing_checks = (list(_edinet_filing_checks(row, facts)) if "_audit_edinet" in facts else
                             (against_filing(row, edgar, facts)
                         + against_income(row, edgar, facts)
                         + against_cash_flow(row, edgar)))
            for line in filing_checks:
                bucket = {"FILING": "filing_bad", "FILING-OK": "filing_ok"}.get(
                    line[0], "filing_unknown")
                totals[bucket] += 1
                if line[0] != "FILING-OK":       # matches are counted, not printed
                    lines.append(line)
        if "_audit_edinet" in facts:
            native_sources.extend({"cik": row["cik"], "ticker": row["ticker"], **item}
                                  for item in facts["_audit_edinet"]["sources"])
        bad = [ln for ln in lines if ln[0] not in ("UNCHECKED", "FILING?")]
        uncheckable.extend((row["ticker"], line) for line in lines
                           if line[0] in {"UNCHECKED", "FILING?"})
        if bad:
            problems.append((row["ticker"], bad))
        if not quiet:
            mark = "FAIL" if bad else "ok"
            print(f"  {row['ticker']:8s} {mark}")
            for kind, name, shown, expected, why in lines:
                print(f"      {kind:9s} {name:16s} shows {shown!r} "
                      f"{'vs ' + repr(round(expected, 4)) if expected is not None else ''}   {why}")
    result = {"totals": totals, "problems": problems, "uncheckable": uncheckable}
    if native_sources:
        result["edinet_native_sources"] = native_sources
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", help="comma-separated, instead of the standard spread")
    ap.add_argument("--all", action="store_true",
                    help="audit every company in the selected UI payload")
    ap.add_argument("--sample", type=int, help="this many companies at random")
    ap.add_argument("--seed", type=int, default=0,
                    help="which random sample: the same seed always draws the same "
                         "companies, so a run is reproducible and a new seed covers new ground")
    ap.add_argument("--quiet", action="store_true", help="only the failures")
    ap.add_argument("--show-unchecked", action="store_true",
                    help="list values that the payload cannot independently substantiate")
    ap.add_argument("--filings", action="store_true",
                    help="also read each company's published statements (network)")
    ap.add_argument("--dashboard", type=Path, default=Path(DASHBOARD_JSON),
                    help="UI payload to audit without replacing the shipped dashboard")
    args = ap.parse_args(argv)

    everything = json.loads(args.dashboard.read_text())["rows"]
    by_ticker = {r["ticker"]: r for r in everything if r.get("ticker")}
    if args.ticker:
        wanted = [by_ticker[t.strip().upper()] for t in args.ticker.split(",")
                  if t.strip().upper() in by_ticker]
    elif args.all:
        wanted = everything
    elif args.sample:
        import random
        random.seed(args.seed)
        wanted = random.sample(everything, min(args.sample, len(everything)))
    else:
        wanted = [by_ticker[t] for t in SPREAD if t in by_ticker]

    print(f"auditing {len(wanted)} companies\n")
    edgar = EdgarClient()
    result = audit(wanted, Path(edgar.cache_dir), args.quiet, edgar if args.filings else None)
    t = result["totals"]
    print(f"\n  values matching the filing they name : {t['sourced_ok']} ok, {t['sourced_bad']} wrong")
    print(f"  figures matching their own arithmetic: {t['derived_ok']} ok, {t['derived_bad']} wrong")
    print(f"  components superseded by a newer filing: {t['mixed_dates']}")
    print(f"  matching the published statement     : {t['filing_ok']} ok, {t['filing_bad']} wrong"
          f" ({t['filing_unknown']} lines not printed)")
    print(f"  not checkable from the payload       : {t['unchecked']}")
    if args.show_unchecked and result.get("uncheckable"):
        print("\n  uncheckable values:")
        for ticker, (_, field, shown, _, why) in result["uncheckable"]:
            print(f"    {ticker:8s} {field}: {shown!r} ({why})")
    if result["problems"]:
        print(f"\n  {len(result['problems'])} companies with at least one defect: "
              + ", ".join(t for t, _ in result["problems"]))
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
