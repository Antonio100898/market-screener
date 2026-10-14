"""Translate named EDINET XBRL concepts into the canonical facts contract."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import date
import hashlib
import re
from .edinet import xbrl_facts, issuer_class_evidence


ADAPTER_KIND = "edinet_xbrl"
FORM = "JP-AR"

# Each source concept has a checked accounting meaning. The order of a source
# group's entries is the preference when statement and summary values coexist.
_CONCEPTS = {
    "jpigp_cor": {
        "Revenues": ("RevenueIFRS",),
        "AssetsCurrent": ("CurrentAssetsIFRS",),
        "LiabilitiesCurrent": ("TotalCurrentLiabilitiesIFRS",),
        "Liabilities": ("LiabilitiesIFRS",),
        "StockholdersEquity": ("EquityAttributableToOwnersOfParentIFRS",),
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": (
            "EquityIFRS",),
        "CashAndCashEquivalentsAtCarryingValue": ("CashAndCashEquivalentsIFRS",),
        "InventoryNet": ("InventoriesCAIFRS",),
        "Goodwill": ("GoodwillIFRS",),
        "IntangibleAssetsNetExcludingGoodwill": ("IntangibleAssetsIFRS",),
        "DebtCurrent": ("ShortTermDebtIncludingCurrentPortionOfLongTermDebtCLIFRS",),
        "OperatingIncomeLoss": ("OperatingProfitLossIFRS",),
        "NetIncomeLoss": ("ProfitLossAttributableToOwnersOfParentIFRS",),
        "EarningsPerShareBasic": ("BasicEarningsLossPerShareIFRS",),
        "EarningsPerShareDiluted": ("DilutedEarningsLossPerShareIFRS",),
        "NetCashProvidedByUsedInOperatingActivities": (
            "NetCashProvidedByUsedInOperatingActivitiesIFRS",),
        "DepreciationAndAmortization": ("DepreciationAndAmortizationOpeCFIFRS",),
        "PaymentsToAcquirePropertyPlantAndEquipment": (
            "PurchaseOfPropertyPlantAndEquipmentInvCFIFRS",),
        "PaymentsToAcquireIntangibleAssets": ("PurchaseOfIntangibleAssetsInvCFIFRS",),
        "PaymentsOfDividendsCommonStock": ("DividendsPaidToOwnersOfParentFinCFIFRS",),
        "Assets": ("AssetsIFRS",),
    },
    "jppfs_cor": {
        "ShareholdersCapitalExcludingAccumulatedOtherComprehensiveIncome": ("ShareholdersEquity",),
        "ParentAccumulatedOtherComprehensiveIncome": ("ValuationAndTranslationAdjustments",),
        "Revenues": ("NetSales",),
        "AssetsCurrent": ("CurrentAssets",),
        "LiabilitiesCurrent": ("CurrentLiabilities",),
        "Liabilities": ("Liabilities",),
        "CashAndCashEquivalentsAtCarryingValue": ("CashAndCashEquivalents",),
        "InventoryNet": ("Inventories",),
        "OperatingIncomeLoss": ("OperatingIncome",),
        "NetIncomeLoss": ("ProfitLossAttributableToOwnersOfParent",),
        "ProfitLoss": ("ProfitLoss",),
        "IncomeTaxExpenseBenefit": ("IncomeTaxes",),
        "NetCashProvidedByUsedInOperatingActivities": (
            "NetCashProvidedByUsedInOperatingActivities",),
        "DepreciationAndAmortization": ("DepreciationAndAmortizationOpeCF",),
        "PaymentsToAcquirePropertyPlantAndEquipment": (
            "PurchaseOfPropertyPlantAndEquipmentInvCF",),
        "PaymentsToAcquireProductiveAssets": (
            "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCF",),
        "PaymentsToAcquireIntangibleAssets": ("PurchaseOfIntangibleAssetsInvCF",),
        "PaymentsOfOrdinaryDividends": ("CashDividendsPaidFinCF",),
        "Assets": ("Assets",),
    },
    "jpcrp_cor": {
        "Revenues": ("RevenueIFRSSummaryOfBusinessResults",
                     "NetSalesSummaryOfBusinessResults"),
        "NetIncomeLoss": (
            "ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults",
            "ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults",
        ),
        "StockholdersEquity": (
            "EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults",),
        "Assets": ("TotalAssetsIFRSSummaryOfBusinessResults",
                   "TotalAssetsSummaryOfBusinessResults"),
        "CashAndCashEquivalentsAtCarryingValue": (
            "CashAndCashEquivalentsIFRSSummaryOfBusinessResults",
            "CashAndCashEquivalentsSummaryOfBusinessResults",
        ),
        "NetCashProvidedByUsedInOperatingActivities": (
            "CashFlowsFromUsedInOperatingActivitiesIFRSSummaryOfBusinessResults",
            "NetCashProvidedByUsedInOperatingActivitiesSummaryOfBusinessResults",
        ),
        "EarningsPerShareBasic": (
            "BasicEarningsLossPerShareIFRSSummaryOfBusinessResults",
            "BasicEarningsLossPerShareSummaryOfBusinessResults",
        ),
        "EarningsPerShareDiluted": (
            "DilutedEarningsLossPerShareIFRSSummaryOfBusinessResults",),
    },
}
_SOURCE_TO_CANONICAL = {
    (namespace, tag): (canonical, rank)
    for namespace, mappings in _CONCEPTS.items()
    for canonical, tags in mappings.items()
    for rank, tag in enumerate(tags)
}
_PER_SHARE = {"EarningsPerShareDiluted", "EarningsPerShareBasic"}
_CASH_OUTFLOWS = {"PaymentsOfDividendsCommonStock", "PaymentsOfOrdinaryDividends",
                  "PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireIntangibleAssets",
                  "PaymentsToAcquireProductiveAssets"}


def _taxonomy(namespace: str) -> str | None:
    for name in _CONCEPTS:
        if namespace.rstrip("/").endswith("/" + name):
            return name
    return None


def _filing_date(record: dict) -> str:
    value = record.get("submitDateTime") or record.get("filed")
    if not value:
        raise ValueError("EDINET filing has no submission date")
    result = str(value)[:10]
    date.fromisoformat(result)
    return result


def build_edinet_companyfacts(
    record: dict, archive: bytes, *, ticker: str | None = None,
) -> dict:
    """Build a canonical bundle for one filed annual EDINET XBRL archive.

    Only undimensioned, named concepts in the filing's own monetary unit enter.
    Missing concepts remain absent; no value is inferred from another row.
    """
    if record.get("docTypeCode") not in {"120", "130"} or record.get("xbrlFlag") != "1":
        raise ValueError("EDINET record is not an XBRL annual securities report")
    document_id = record["docID"]
    entity_id = record["edinetCode"]
    security_code = record["secCode"]
    period_end = record.get("periodEnd")
    if not isinstance(period_end, str) or not period_end:
        raise ValueError("EDINET annual filing has no period end")
    date.fromisoformat(period_end)
    filed = _filing_date(record)
    raw = xbrl_facts(archive, document_id)
    anchors = {
        (namespace, fact["unit"])
        for fact in raw
        if (namespace := _taxonomy(fact["namespace"])) in {"jpigp_cor", "jppfs_cor"}
        and fact["tag"] == ("AssetsIFRS" if namespace == "jpigp_cor" else "Assets")
        and (fact.get("entity") or "").partition("-")[0] == entity_id
        and fact["start"] is None and fact["end"] == period_end
        and not fact["dimensions"] and fact["end"] <= filed
        and isinstance(fact["unit"], str)
        and len(fact["unit"]) == 3 and fact["unit"].isupper()
        and fact["unit"].isalpha()
    }
    if len(anchors) != 1:
        raise ValueError("EDINET annual filing lacks one reported balance currency")
    statement_basis, reporting_currency = anchors.pop()
    equity_tags = {"ShareholdersCapitalExcludingAccumulatedOtherComprehensiveIncome",
                   "ParentAccumulatedOtherComprehensiveIncome"}
    class_evidence = (issuer_class_evidence(archive, document_id, entity_id)
                      if statement_basis == "jppfs_cor"
                      and any(fact.get("element_ordinal") is not None for fact in raw) else [])
    artifact_digest = hashlib.sha256(archive).hexdigest()
    from ..normalize import debt_evidence_roles
    native_presence: dict = defaultdict(lambda: {"units": defaultdict(list)})
    for fact in raw:
        if (_taxonomy(fact["namespace"]) != statement_basis or fact["dimensions"]
                or fact.get("entity") != f"{entity_id}-000"
                or fact.get("entity_scheme") != "http://disclosure.edinet-fsa.go.jp"
                or fact["start"] is not None or not fact["end"]
                or fact["end"] > period_end or fact["end"] > filed
                or fact["unit"] != reporting_currency or not debt_evidence_roles(fact["tag"])
                or fact.get("native_context") is None
                or fact["native_context"].instant is None
                or fact.get("unit_measures") != ("{http://www.xbrl.org/2003/iso4217}" + reporting_currency,)):
            continue
        entry = {
            "val": str(fact["value"]) if fact["value"] is not None else None,
            "accn": document_id, "fy": int(period_end[:4]), "fp": "FY",
            "form": f"{FORM}/A" if record["docTypeCode"] == "130" else FORM,
            "filed": filed, "end": fact["end"],
            "_source_namespace": statement_basis, "_source_tag": fact["tag"],
            "_source_unit": fact["unit"], "_source_document": document_id,
            "_source_file": fact["source_file"], "_source_entity": fact["entity"],
            "_source_entity_scheme": fact["entity_scheme"],
            "_source_artifact_sha256": artifact_digest,
            "_source_lexical": fact.get("source_lexical", str(fact["value"])),
            "_source_nil": fact.get("nil", False),
            "_source_native_xml": {key: fact.get(key) for key in
                                   ("element_ordinal", "fact_qname", "context_ref", "unit_ref")},
        }
        native_presence[fact["tag"]]["units"][fact["unit"]].append(entry)

    candidates: dict[tuple, tuple[int, dict, str, str]] = {}
    conflicting_equity = set()
    for fact in raw:
        namespace = _taxonomy(fact["namespace"])
        if (namespace is None or fact["dimensions"] or not fact["end"]
                or (fact.get("entity") or "").partition("-")[0] != entity_id):
            continue
        if namespace == "jpcrp_cor":
            if ("IFRS" in fact["tag"]) != (statement_basis == "jpigp_cor"):
                continue
        elif namespace != statement_basis:
            continue
        mapped = _SOURCE_TO_CANONICAL.get((namespace, fact["tag"]))
        if mapped is None or fact["value"] is None:
            continue
        canonical, rank = mapped
        if fact["tag"].endswith("SummaryOfBusinessResults"):
            rank += 1
        unit = fact["unit"]
        if (not isinstance(unit, str) or
                (canonical in _PER_SHARE and not unit.endswith("/shares")) or
                (canonical not in _PER_SHARE and "/" in unit)):
            continue
        currency = unit.removesuffix("/shares")
        if len(currency) != 3 or not currency.isalpha() or not currency.isupper():
            continue
        if currency != reporting_currency:
            continue
        if fact["end"] > filed:
            continue
        key = (canonical, fact["start"], fact["end"], unit)
        previous = candidates.get(key)
        if (canonical in equity_tags and previous is not None and rank == previous[0]
                and (fact["value"] != previous[1]["value"]
                     or fact["source_file"] != previous[1]["source_file"])):
            conflicting_equity.add(key)
        if previous is None or rank < previous[0]:
            candidates[key] = (rank, fact, namespace, currency)

    canonical_facts: dict = defaultdict(lambda: {"units": defaultdict(list)})
    for (canonical, start, end, unit), (_, fact, namespace, currency) in candidates.items():
        if end > period_end or (canonical, start, end, unit) in conflicting_equity:
            continue
        value = abs(fact["value"]) if canonical in _CASH_OUTFLOWS else fact["value"]
        entry = {
            "val": str(value),
            "accn": document_id,
            "fy": int(period_end[:4]),
            "fp": "FY",
            "form": f"{FORM}/A" if record["docTypeCode"] == "130" else FORM,
            "filed": filed,
            "end": end,
            "_canonical_adapter": ADAPTER_KIND,
            "_source_namespace": namespace,
            "_source_tag": fact["tag"],
            "_source_unit": unit,
            "_source_document": document_id,
            "_normalized_tag": canonical,
            "_source_file": fact["source_file"],
        }
        if start is not None:
            entry["start"] = start
        if canonical in equity_tags:
            entry.update(
                _source_entity=fact["entity"],
                _source_entity_scheme=fact.get("entity_scheme"),
                _source_artifact_sha256=artifact_digest,
                _source_native_xml={key: fact.get(key) for key in
                                    ("element_ordinal", "fact_qname", "context_ref", "unit_ref")},
                _equity_class_evidence=[asdict(evidence) for evidence in class_evidence
                                       if evidence.as_of == end
                                       and evidence.location.source_file == fact["source_file"]])
        if value != fact["value"]:
            entry["_source_concept"] = (
                f"{canonical} (cash outflow magnitude; source signed negative)"
            )
        canonical_facts[canonical]["units"][unit].append(entry)

    return {
        "cik": entity_id,
        "entityName": (record.get("filerNameEnglish") or record.get("filerNameEn")
                       or record.get("filerName") or entity_id),
        "facts": {**({statement_basis: {tag: {"units": dict(data["units"])}
                                      for tag, data in native_presence.items()}} if native_presence else {}),
                  "canonical": {
            tag: {"units": dict(data["units"])}
            for tag, data in canonical_facts.items()
        }},
        "_adapter": {
            "kind": ADAPTER_KIND,
            "statement_basis": "canonical",
            "reporting_currency": reporting_currency,
            "quote_currency": "JPY",
            "ticker": ticker,
            "security_code": security_code,
            "security_basis": "PRIMARY_ORDINARY_SHARE",
            "accounting_standard": "JGAAP" if statement_basis == "jppfs_cor" else "IFRS",
            "reports": [{
                "fiscal_year": int(period_end[:4]),
                "published": filed,
                "period_start": record.get("periodStart"),
                "period_end": period_end,
                "document": document_id,
                "mapping_record": dict(record),
                "mapped_facts": sum(
                    len(entries)
                    for data in canonical_facts.values()
                    for entries in data["units"].values()
                ),
            }],
        },
    }


def merge_companyfacts(previous: dict | None, incoming: dict) -> dict:
    """Keep earlier reports while replacing a remapped report's old concepts."""
    if not previous or (previous.get("_adapter") or {}).get("kind") != ADAPTER_KIND:
        return incoming
    if previous.get("cik") != incoming.get("cik"):
        raise ValueError("EDINET company identity changed during import")
    out = previous
    prior_reports = (out.get("_adapter") or {}).get("reports") or []
    new_report = incoming["_adapter"]["reports"][0]
    prior_reports = [report for report in prior_reports
                     if report["document"] != new_report["document"]]
    for taxonomy in out.get("facts", {}).values():
        for tagdata in taxonomy.values():
            for unit, entries in tagdata.get("units", {}).items():
                tagdata["units"][unit] = [entry for entry in entries
                                          if entry.get("accn") != new_report["document"]]
    for namespace, taxonomy in incoming["facts"].items():
        for tag, tagdata in taxonomy.items():
            target = out.setdefault("facts", {}).setdefault(namespace, {}) \
                .setdefault(tag, {"units": {}})["units"]
            for unit, entries in tagdata["units"].items():
                target.setdefault(unit, []).extend(entries)
    out["_adapter"]["reports"] = sorted(
        [*prior_reports, new_report], key=lambda report: report["published"])
    if new_report["published"] >= out["_adapter"]["reports"][-1]["published"]:
        out["_adapter"]["reporting_currency"] = incoming["_adapter"]["reporting_currency"]
        out["_adapter"]["quote_currency"] = incoming["_adapter"]["quote_currency"]
        out["_adapter"]["ticker"] = incoming["_adapter"]["ticker"]
        out["_adapter"]["accounting_standard"] = incoming["_adapter"]["accounting_standard"]
        out["entityName"] = incoming["entityName"]
    return out


def rebuild_companyfacts(cached: dict, load_archive) -> dict:
    """Remap retained annual archives without fetching or changing their bytes."""
    adapter = cached.get("_adapter") or {}
    if adapter.get("kind") != ADAPTER_KIND:
        return cached
    security = adapter.get("security_code")
    quote_currency = adapter.get("quote_currency")
    if (not isinstance(cached.get("cik"), str) or not re.fullmatch(r"E\d{5}", cached["cik"])
            or not isinstance(security, str) or not re.fullmatch(r"\d{3}[0-9A-Z]0", security)
            or adapter.get("ticker") != f"{security[:-1]}.T"
            or adapter.get("security_basis") != "PRIMARY_ORDINARY_SHARE"
            or not isinstance(quote_currency, str) or not re.fullmatch(r"[A-Z]{3}", quote_currency)):
        raise ValueError("retained EDINET issuer or primary security is invalid")
    reports = adapter.get("reports") or []
    if not reports:
        raise ValueError("retained EDINET report manifest is empty")
    documents = [report.get("document") for report in reports]
    if (any(not isinstance(document, str) or not re.fullmatch(r"[A-Z0-9]+", document)
            for document in documents) or len(set(documents)) != len(documents)):
        raise ValueError("invalid or duplicate retained EDINET document")
    rebuilt = None
    for report in sorted(reports, key=lambda item: (item.get("published", ""), item.get("document", ""))):
        document = report.get("document")
        record = report.get("mapping_record")
        if record is None:
            forms = {
                entry.get("form")
                for data in cached.get("facts", {}).get("canonical", {}).values()
                for entries in data.get("units", {}).values()
                for entry in entries if entry.get("accn") == document
            }
            if len(forms) != 1 or not forms <= {FORM, f"{FORM}/A"}:
                raise ValueError(f"{document}: retained annual-report form is missing or conflicting")
            record = {
                "docID": document, "edinetCode": cached.get("cik"),
                "secCode": adapter.get("security_code"),
                "docTypeCode": "130" if f"{FORM}/A" in forms else "120",
                "xbrlFlag": "1", "submitDateTime": report.get("published"),
                "periodStart": report.get("period_start"), "periodEnd": report.get("period_end"),
                "filerName": cached.get("entityName"),
            }
        if (not isinstance(record, dict) or record.get("docID") != document
                or record.get("edinetCode") != cached.get("cik")
                or record.get("secCode") != adapter.get("security_code")
                or record.get("periodStart") != report.get("period_start")
                or record.get("periodEnd") != report.get("period_end")
                or str(record.get("submitDateTime", ""))[:10] != report.get("published")):
            raise ValueError(f"{document}: retained filing record disagrees with its manifest")
        mapped = build_edinet_companyfacts(record, load_archive(document), ticker=adapter.get("ticker"))
        rebuilt = merge_companyfacts(rebuilt, mapped)
    rebuilt["_adapter"]["quote_currency"] = quote_currency
    return rebuilt
