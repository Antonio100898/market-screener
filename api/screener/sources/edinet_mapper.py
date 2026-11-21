"""Translate named EDINET XBRL concepts into the canonical facts contract."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from .edinet import reported_period_end, xbrl_facts


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
        "IntangibleAssetsNetIncludingGoodwill": ("GoodwillAndIntangibleAssetsIFRS",),
        "MinorityInterest": ("NonControllingInterestsIFRS",),
        # Bonds-and-borrowings rows are the whole interest-bearing bucket; the
        # narrower rows are its parts when a filer prints them separately.
        "DebtCurrent": ("ShortTermDebtIncludingCurrentPortionOfLongTermDebtCLIFRS",
                        "BondsAndBorrowingsCLIFRS", "InterestBearingLiabilitiesCLIFRS"),
        "OtherShortTermBorrowings": ("BorrowingsCLIFRS",),
        "LongTermDebtCurrent": ("CurrentPortionOfLongTermBorrowingsCLIFRS",),
        "LongTermDebtNoncurrent": ("BondsAndBorrowingsNCLIFRS", "LongTermDebtNCLIFRS",
                                   "InterestBearingLiabilitiesNCLIFRS"),
        "LongTermLoansPayable": ("BorrowingsNCLIFRS",),
        "SeniorLongTermNotes": ("BondsPayableNCLIFRS",),
        "PaymentsToAcquirePropertyPlantAndEquipment": (
            "PurchaseOfPropertyPlantAndEquipmentInvCFIFRS",),
        "PaymentsToAcquireProductiveAssets": (
            "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCFIFRS",),
        "PaymentsToAcquireIntangibleAssets": ("PurchaseOfIntangibleAssetsInvCFIFRS",),
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": (
            "ProfitLossBeforeTaxIFRS",),
        "IncomeTaxExpenseBenefit": ("IncomeTaxExpenseIFRS",),
        "OperatingIncomeLoss": ("OperatingProfitLossIFRS",),
        "NetIncomeLoss": ("ProfitLossAttributableToOwnersOfParentIFRS",),
        "EarningsPerShareBasic": ("BasicEarningsLossPerShareIFRS",),
        "EarningsPerShareDiluted": ("DilutedEarningsLossPerShareIFRS",),
        "NetCashProvidedByUsedInOperatingActivities": (
            "NetCashProvidedByUsedInOperatingActivitiesIFRS",),
        "DepreciationAndAmortization": ("DepreciationAndAmortizationOpeCFIFRS",),
        # As for Japanese GAAP, the cash paid in the year ranks after the
        # dividend per share the company declared for that year.
        "PaymentsOfOrdinaryDividends": ("DividendsPaidToOwnersOfParentFinCFIFRS",
                                        "DividendsPaidFinCFIFRS"),
        "Assets": ("AssetsIFRS",),
    },
    "jppfs_cor": {
        "Revenues": ("NetSales",),
        "AssetsCurrent": ("CurrentAssets",),
        "LiabilitiesCurrent": ("CurrentLiabilities",),
        "Liabilities": ("Liabilities",),
        "InventoryNet": ("Inventories",),
        "Goodwill": ("Goodwill",),
        # The Japanese GAAP intangible-assets subtotal includes goodwill.
        "IntangibleAssetsNetIncludingGoodwill": ("IntangibleAssets",),
        "MinorityInterest": ("NonControllingInterests", "MinorityInterests"),
        # Borrowings by instrument; normalization sums the separate rows.
        "OtherShortTermBorrowings": ("ShortTermLoansPayable",),
        "CommercialPaper": ("CommercialPapersLiabilities",),
        "LoansPayableCurrent": ("CurrentPortionOfLongTermLoansPayable",),
        "SeniorNotesCurrent": ("CurrentPortionOfBonds",),
        "LongTermLoansPayable": ("LongTermLoansPayable",),
        "OtherLoansPayable": ("BorrowedMoneyLiabilitiesBNK",),
        "SeniorLongTermNotes": ("BondsPayable",),
        "ConvertibleDebtNoncurrent": ("ConvertibleBondTypeBondsWithSubscriptionRightsToShares",),
        # Japanese GAAP lease obligations are finance leases; operating leases
        # stay off the balance sheet.
        "FinanceLeaseLiabilityCurrent": ("LeaseObligationsCL",),
        "FinanceLeaseLiabilityNoncurrent": ("LeaseObligationsNCL",),
        "OperatingIncomeLoss": ("OperatingIncome",),
        "NetIncomeLoss": ("ProfitLossAttributableToOwnersOfParent",),
        "ProfitLoss": ("ProfitLoss",),
        "IncomeTaxExpenseBenefit": ("IncomeTaxes",),
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": (
            "IncomeBeforeIncomeTaxes",),
        "NetCashProvidedByUsedInOperatingActivities": (
            "NetCashProvidedByUsedInOperatingActivities",),
        "DepreciationAndAmortization": ("DepreciationAndAmortizationOpeCF",),
        "PaymentsToAcquirePropertyPlantAndEquipment": (
            "PurchaseOfPropertyPlantAndEquipmentInvCF",),
        "PaymentsToAcquireProductiveAssets": (
            "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCF",
            "PurchaseOfNoncurrentAssetsInvCF"),
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
        # Declared for the fiscal year, interim included.
        "CommonStockDividendsPerShareDeclared": (
            "DividendPaidPerShareSummaryOfBusinessResults",),
    },
}
# The reporting company's own per-share rows: the summary files them on the
# standalone axis under either accounting standard, even for a group.
_COMPANY_ROWS = {"DividendPaidPerShareSummaryOfBusinessResults"}
_SOURCE_TO_CANONICAL = {
    (namespace, tag): (canonical, rank)
    for namespace, mappings in _CONCEPTS.items()
    for canonical, tags in mappings.items()
    for rank, tag in enumerate(tags)
}
_PER_SHARE = {"EarningsPerShareDiluted", "EarningsPerShareBasic",
              "CommonStockDividendsPerShareDeclared"}
_CASH_OUTFLOWS = {"PaymentsOfDividendsCommonStock", "PaymentsOfOrdinaryDividends",
                  "PaymentsToAcquirePropertyPlantAndEquipment",
                  "PaymentsToAcquireProductiveAssets", "PaymentsToAcquireIntangibleAssets"}


CONSOLIDATED = "consolidated"
STANDALONE = "standalone"


def _scope(fact: dict) -> str | None:
    """Consolidated facts carry no axis; standalone facts only the scope axis."""
    dimensions = fact["dimensions"]
    if not dimensions:
        return CONSOLIDATED
    if (len(dimensions) == 1
            and dimensions[0]["axis"].endswith("ConsolidatedOrNonConsolidatedAxis")
            and dimensions[0]["member"].endswith("NonConsolidatedMember")):
        return STANDALONE
    return None


_NET_ASSET_PARTS = ("ShareholdersEquity", "ValuationAndTranslationAdjustments",
                    "SubscriptionRightsToShares", "NonControllingInterests",
                    "MinorityInterests")


def _parent_equity(facts: list[dict], scope: str, currency: str) -> dict[str, list[dict]]:
    """Japanese GAAP parent equity rows at each balance-sheet date.

    Parent equity is shareholders' equity plus accumulated other comprehensive
    income. It is kept only where those rows, subscription rights and
    non-controlling interests add up to the reported net assets, which proves
    an unprinted row is nil rather than unknown. Each printed row is rounded to
    the statement's unit, so the sum may differ by half a unit per row.
    """
    by_end: dict[str, dict[str, dict]] = defaultdict(dict)
    for fact in facts:
        if (_taxonomy(fact["namespace"]) == "jppfs_cor" and _scope(fact) == scope
                and fact["unit"] == currency
                and fact["tag"] in ("NetAssets", *_NET_ASSET_PARTS)):
            by_end[fact["end"]].setdefault(fact["tag"], fact)
    out = {}
    for end, rows in by_end.items():
        if "ShareholdersEquity" not in rows or "NetAssets" not in rows:
            continue
        values = [fact["value"] for fact in rows.values()]
        unit = 1
        while unit < 1_000_000 and all(value % (unit * 10) == 0 for value in values):
            unit *= 10
        if (abs(sum(rows[tag]["value"] for tag in _NET_ASSET_PARTS if tag in rows)
                - rows["NetAssets"]["value"]) * 2 > unit * len(values)):
            continue
        out[end] = [rows[tag] for tag in _NET_ASSET_PARTS[:2] if tag in rows]
    return out


_ISSUED = "NumberOfSharesIssuedSharesVotingRights"
_TREASURY = "TreasurySharesSharesWithFullVotingRightsTreasurySharesEtcMember"
_TREASURY_ETC = "SharesWithFullVotingRightsTreasurySharesEtcMember"


def _member(fact: dict) -> str:
    return fact["dimensions"][0]["member"].rpartition(":")[2]


def _shares_outstanding(facts: list[dict], period_end: str) -> tuple[dict, dict | None] | None:
    """Issued shares and treasury shares at fiscal year end.

    Both come from the report's voting-rights table. Its rows add up to the
    issued total, so a table without a treasury row proves there is none. A
    combined treasury-and-cross-holding row counts as treasury only when no
    cross-holding row exists and the treasury table names a single holder.
    Another share class leaves the ordinary count unknown.
    """
    table = [fact for fact in facts
             if _taxonomy(fact["namespace"]) == "jpcrp_cor" and fact["tag"] == _ISSUED
             and fact["end"] == period_end and fact["unit"] == "shares"
             and len(fact["dimensions"]) <= 1]
    totals = [fact for fact in table if not fact["dimensions"]]
    rows = {_member(fact): fact for fact in table if fact["dimensions"]}
    if len({fact["value"] for fact in totals}) != 1 or any(
            not member.startswith(("OrdinaryShares", "SharesWith", "SharesLessThanOneUnit"))
            for member in rows):
        return None
    issued = totals[0]
    treasury = [fact for member, fact in rows.items() if member.endswith(_TREASURY)]
    combined = [fact for member, fact in rows.items()
                if member.endswith(_TREASURY_ETC) and not member.endswith(_TREASURY)
                and "Reciprocal" not in member]
    if not treasury and combined:
        holders = {_member(fact) for fact in facts
                   if fact["tag"] == "TotalNumberOfSharesHeldTreasurySharesEtc"
                   and fact["end"] == period_end and len(fact["dimensions"]) == 1}
        if len(holders) > 1 or any("Reciprocal" in member for member in rows):
            return None
        treasury = combined
    if len(treasury) > 1:
        return None
    if not treasury and sum(fact["value"] for fact in rows.values()) != issued["value"]:
        return None
    return issued, (treasury[0] if treasury else None)


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
    period_end = record.get("periodEnd") or reported_period_end(archive, document_id)
    if not isinstance(period_end, str) or not period_end:
        raise ValueError("EDINET annual filing has no period end")
    date.fromisoformat(period_end)
    filed = _filing_date(record)
    raw = xbrl_facts(archive, document_id)

    def anchors_in(scope):
        return {
            (namespace, fact["unit"])
            for fact in raw
            if (namespace := _taxonomy(fact["namespace"])) in {"jpigp_cor", "jppfs_cor"}
            and fact["tag"] == ("AssetsIFRS" if namespace == "jpigp_cor" else "Assets")
            and (fact.get("entity") or "").partition("-")[0] == entity_id
            and fact["start"] is None and fact["end"] == period_end
            and _scope(fact) == scope and fact["end"] <= filed
            and isinstance(fact["unit"], str)
            and len(fact["unit"]) == 3 and fact["unit"].isupper()
            and fact["unit"].isalpha()
        }

    # A company without subsidiaries files only its standalone statements.
    scope = CONSOLIDATED
    anchors = anchors_in(CONSOLIDATED)
    if not anchors:
        scope = STANDALONE
        anchors = anchors_in(STANDALONE)
    if len(anchors) != 1:
        raise ValueError("EDINET annual filing lacks one reported balance currency")
    statement_basis, reporting_currency = anchors.pop()

    candidates: dict[tuple, tuple[int, dict, str, str]] = {}
    for fact in raw:
        namespace = _taxonomy(fact["namespace"])
        company_row = namespace == "jpcrp_cor" and fact["tag"] in _COMPANY_ROWS
        if (namespace is None or not fact["end"]
                or (fact.get("entity") or "").partition("-")[0] != entity_id):
            continue
        if company_row:
            if _scope(fact) is None:
                continue
        elif _scope(fact) != scope:
            continue
        if namespace == "jpcrp_cor":
            if not company_row and ("IFRS" in fact["tag"]) != (statement_basis == "jpigp_cor"):
                continue
        elif namespace != statement_basis:
            continue
        mapped = _SOURCE_TO_CANONICAL.get((namespace, fact["tag"]))
        if mapped is None:
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
        if previous is None or rank < previous[0]:
            candidates[key] = (rank, fact, namespace, currency)

    canonical_facts: dict = defaultdict(lambda: {"units": defaultdict(list)})

    def add(canonical, value, unit, start, end, namespace, tag, source_file, concept=None):
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
            "_source_tag": tag,
            "_source_unit": unit,
            "_source_document": document_id,
            "_normalized_tag": canonical,
            "_source_file": source_file,
        }
        if scope == STANDALONE:
            entry["_source_scope"] = "standalone statements; the company reports no consolidated group"
        if start is not None:
            entry["start"] = start
        if concept:
            entry["_source_concept"] = concept
        canonical_facts[canonical]["units"][unit].append(entry)

    for (canonical, start, end, unit), (_, fact, namespace, currency) in candidates.items():
        if end > period_end:
            continue
        value = abs(fact["value"]) if canonical in _CASH_OUTFLOWS else fact["value"]
        add(canonical, value, unit, start, end, namespace, fact["tag"], fact["source_file"],
            f"{canonical} (cash outflow magnitude; source signed negative)"
            if value != fact["value"] else None)
    own = [fact for fact in raw
           if (fact.get("entity") or "").partition("-")[0] == entity_id
           and fact["start"] is None and fact["end"] and fact["end"] <= period_end]
    if statement_basis == "jppfs_cor":
        for end, parts in _parent_equity(own, scope, reporting_currency).items():
            add("StockholdersEquity", sum(f["value"] for f in parts), reporting_currency,
                None, end, "jppfs_cor", " + jppfs_cor:".join(f["tag"] for f in parts),
                parts[0]["source_file"],
                "StockholdersEquity (parent equity: net assets less subscription "
                "rights and non-controlling interests, which the filing's rows add up to)")
    shares = _shares_outstanding(own, period_end)
    if shares is not None:
        issued, treasury = shares
        held = treasury["value"] if treasury else 0
        add("CommonStockSharesOutstanding", issued["value"] - held, "shares", None,
            period_end, "jpcrp_cor",
            f"{_ISSUED} - jpcrp_cor:{_ISSUED}[{_member(treasury)}]" if treasury else _ISSUED,
            issued["source_file"],
            f"Shares outstanding (issued {issued['value']} less treasury {held} "
            "at fiscal year end"
            + ("" if treasury else "; the voting-rights table has no treasury row and "
               "its rows add up to the issued total") + ")")

    return {
        "cik": entity_id,
        "entityName": (record.get("filerNameEnglish") or record.get("filerNameEn")
                       or record.get("filerName") or entity_id),
        "facts": {"canonical": {
            tag: {"units": dict(data["units"])}
            for tag, data in canonical_facts.items()
        }},
        "_adapter": {
            "kind": ADAPTER_KIND,
            "statement_basis": "canonical",
            "reporting_currency": reporting_currency,
            "quote_currency": reporting_currency,
            "ticker": ticker,
            "security_code": security_code,
            "security_basis": "PRIMARY_ORDINARY_SHARE",
            "statement_scope": scope,
            "reports": [{
                "fiscal_year": int(period_end[:4]),
                "published": filed,
                "period_start": record.get("periodStart"),
                "period_end": period_end,
                "document": document_id,
                "scope": scope,
                "corrects": (record.get("parentDocID")
                             if record["docTypeCode"] == "130" else None),
                "mapped_facts": sum(
                    len(entries)
                    for data in canonical_facts.values()
                    for entries in data["units"].values()
                ),
            }],
        },
    }
