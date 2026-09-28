"""Layer 2: EDGAR companyfacts JSON -> FinancialSnapshot.

All XBRL messiness lives here: tag fallback chains, annual-only selection,
restatement resolution (latest-filed wins), provenance. Missing stays missing in
the exported screen and Graham grades; the ``assume_absent_zero`` detail/Return
Quality calculation marks every substituted zero explicitly.
"""
from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal

from .models import (AnnualOperatingReturn, AnnualOwnerEarnings, Fact,
                     FinancialSnapshot, OwnerEarnings, Provenance)
from .sources import cover


class UnsupportedFilerError(Exception):
    """A filing basis not covered by the normalizer; never partially evaluate it."""


class PendingFilingFactsError(UnsupportedFilerError):
    """A newly indexed annual filing whose structured statements have not arrived."""

    def __init__(self, filing: tuple[str, str]):
        self.filing = filing
        filed, accession = filing
        super().__init__(
            f"SEC structured facts are pending for {accession or 'the annual filing'} "
            f"filed {filed}")


# Foreign issuers file annual 20-F/40-F reports and interim 6-K reports, mostly
# under IFRS. The normalizer admits one coherent ISO-currency statement under
# either standard taxonomy when the exact traded class is settled by the current
# filing cover. Accounting arithmetic stays in that reported currency; the export
# layer joins the US quote through an explicit dated FX rate. An unresolved
# depositary ratio or an ambiguous statement currency remains unsupported.
# The form tuples also preserve pre-transition history when a filer moves between
# foreign and domestic forms.
ANNUAL_FORMS = ("10-K", "20-F", "40-F", "IFRS-AR", "JP-AR", "JP-AR/A")
INTERIM_FORMS = ("10-Q", "6-K")
FINANCIAL_FORMS = ANNUAL_FORMS + INTERIM_FORMS
_FOREIGN_BALANCE_ANCHORS = (
    "Assets", "LiabilitiesAndStockholdersEquity", "Liabilities",
    "AssetsCurrent", "StockholdersEquity",
)
_IFRS_BALANCE_ANCHORS = (
    "Assets", "EquityAndLiabilities", "CurrentAssets", "Equity",
)

# Standard IFRS concepts whose accounting meaning is equivalent to a concept the
# existing US-GAAP chains already understand. This is a translation of taxonomy,
# not of values: units, periods, accessions and the original ``ifrs-full`` element
# all survive on the selected fact. Ambiguous near-matches stay absent. In
# particular, IFRS 16's generic lease liabilities are exposed as operating-lease
# context, not silently counted as Graham debt.
_IFRS_ALIASES: dict[str, tuple[str, ...]] = {
    # balance sheet and the listed-share denominator
    "Assets": ("Assets",),
    "LiabilitiesAndStockholdersEquity": ("EquityAndLiabilities",),
    "AssetsCurrent": ("CurrentAssets",),
    "LiabilitiesCurrent": ("CurrentLiabilities",),
    "LiabilitiesNoncurrent": ("NoncurrentLiabilities",),
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": ("Equity",),
    "StockholdersEquity": ("EquityAttributableToOwnersOfParent",),
    "MinorityInterest": ("NoncontrollingInterests",),
    "CommonStockSharesOutstanding": ("NumberOfSharesOutstanding",),
    "Goodwill": ("Goodwill",),
    "IntangibleAssetsNetExcludingGoodwill": ("IntangibleAssetsOtherThanGoodwill",),
    # borrowed money; generic IFRS lease liabilities deliberately are not finance debt
    "LongTermDebtNoncurrent": ("LongtermBorrowings",),
    "ShortTermBorrowings": ("ShorttermBorrowings",),
    "OperatingLeaseLiability": ("LeaseLiabilities",),
    # income statement and per-share history
    "Revenues": ("Revenue", "RevenueFromContractsWithCustomers"),
    "OperatingIncomeLoss": ("ProfitLossFromOperatingActivities",),
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": (
        "ProfitLossBeforeTax",
    ),
    "NetIncomeLoss": ("ProfitLossAttributableToOwnersOfParent",),
    "ProfitLoss": ("ProfitLoss",),
    "NetIncomeLossAttributableToNoncontrollingInterest": (
        "ProfitLossAttributableToNoncontrollingInterests",
    ),
    "EarningsPerShareDiluted": ("DilutedEarningsLossPerShare",),
    "EarningsPerShareBasic": ("BasicEarningsLossPerShare",),
    "WeightedAverageNumberOfDilutedSharesOutstanding": ("AdjustedWeightedAverageShares",),
    "WeightedAverageNumberOfSharesOutstandingBasic": ("WeightedAverageShares",),
    # common distributions. The cash-flow total is intentionally an aggregate;
    # it proves a payout but does not claim every dollar went to the listed class.
    "PaymentsOfDividendsCommonStock": (
        "DividendsPaidToOwnersOfParent",
        "DividendsRecognisedAsDistributionsToOwnersOfParent",
    ),
    "PaymentsOfOrdinaryDividends": (
        "DividendsPaidClassifiedAsFinancingActivities", "DividendsPaid",
    ),
    "CommonStockDividendsPerShareCashPaid": ("DividendsPaidOrdinarySharesPerShare",),
    "DividendsPayableCurrent": ("CurrentDividendPayables",),
    # owner-earnings and contextual evidence
    "DepreciationAndAmortization": (
        "AdjustmentsForDepreciationAndAmortisationExpense", "DepreciationExpense",
    ),
    "AmortizationOfIntangibleAssets": ("AmortisationIntangibleAssetsOtherThanGoodwill",),
    "IncomeTaxExpenseBenefit": ("IncomeTaxExpenseContinuingOperations",),
    "IncomeTaxesPaidNet": ("IncomeTaxesPaidRefundClassifiedAsOperatingActivities",),
    "DeferredIncomeTaxExpenseBenefit": ("DeferredTaxExpenseIncome",),
    "DeferredTaxAssetsGross": ("DeferredTaxAssets",),
    "PaymentsToAcquirePropertyPlantAndEquipment": (
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
    ),
    "CashAndCashEquivalentsAtCarryingValue": ("CashAndCashEquivalents",),
    "NetCashProvidedByUsedInOperatingActivities": (
        "CashFlowsFromUsedInOperatingActivities",
        "CashFlowsFromUsedInOperatingActivitiesContinuingOperations",
    ),
    "PaymentsForRepurchaseOfCommonStock": ("PaymentsToAcquireOrRedeemEntitysShares",),
    "InterestExpense": ("InterestExpense",),
    "AccountsReceivableNetCurrent": ("CurrentTradeReceivables",),
    "InventoryNet": ("Inventories", "InventoriesTotal"),
}
_IFRS_TO_CANONICAL = {
    source: canonical
    for canonical, sources in _IFRS_ALIASES.items()
    for source in sources
}
IFRS_SOURCE_TAGS = frozenset(_IFRS_TO_CANONICAL)
IFRS_PER_SHARE_TAGS = frozenset((
    "DilutedEarningsLossPerShare", "BasicEarningsLossPerShare",
    "DividendsPaidOrdinarySharesPerShare",
))


def _is_annual_form(form: str) -> bool:
    return form.startswith(ANNUAL_FORMS)


def _is_interim_form(form: str) -> bool:
    return form.startswith(INTERIM_FORMS)


def _is_financial_form(form: str) -> bool:
    return form.startswith(FINANCIAL_FORMS)


EPS_CONTINUING_TAG = "IncomeLossFromContinuingOperationsPerDilutedShare"
EPS_TAGS = (
    "EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted",
    # Partnerships report per unit, not per share, under their own elements. Energy
    # Transfer files 39 annual years of these; without them the whole midstream sector
    # returned "no annual EPS available" and failed criteria 1, 4 and 6 together.
    "NetIncomeLossNetOfTaxPerOutstandingLimitedPartnershipUnitDiluted",
    "NetIncomeLossPerOutstandingLimitedPartnershipUnitDiluted",
    "NetIncomeLossNetOfTaxPerOutstandingLimitedPartnershipUnitBasicNetOfTax",
    "NetIncomeLossPerOutstandingLimitedPartnershipUnitBasicNetOfTax",
    "NetIncomeLossPerOutstandingLimitedPartnershipUnit",
    "NetIncomeLossPerLimitedPartnershipUnitDiluted",
    "NetIncomeLossPerLimitedPartnershipUnitBasic",
    # Investment companies report per-share operating results under their own
    # element (equals diluted EPS at BXSL four years running); the NII-only
    # sibling fragments are deliberately excluded
    "InvestmentCompanyInvestmentIncomeLossFromOperationsPerShare",
)
# Last resort per missing year only. Basic >= diluted, so it flatters EPS slightly —
# but a year dropped entirely is worse: it staled the whole series and the TTM anchor.
EPS_BASIC_TAGS = ("EarningsPerShareBasic", "IncomeLossFromContinuingOperationsPerBasicShare")
_WEIGHTED_SHARE_TAGS = (
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
    "WeightedAverageNumberOfBasicAndDilutedSharesOutstanding",
    "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
    "WeightedAverageNumberOfSharesIssuedBasic",
    # LP unit counts are true totals (ET/EPD/BSM within 0.6% of the dei cover);
    # the BASIC LP variant is deliberately absent — depositary-receipt filers
    # tag it 27x off the real count (NEN)
    "WeightedAverageLimitedPartnershipUnitsOutstandingDiluted",
)
# EPS moves when the share count moves, so the numerator is carried separately:
# a company can grow EPS on buybacks alone while earnings are flat or falling.
NET_INCOME_TAGS = (
    "NetIncomeLoss",                                        # attributable to the parent
    "NetIncomeLossAvailableToCommonStockholdersBasic",
    "ProfitLoss",                                           # includes noncontrolling interests
)
# The subset that is the common's alone. Where minority holders own part of the
# group, only these may be divided by the parent's own share count: Westlake
# Chemical Partners' sponsor takes 82.7% of the consolidated profit, and dividing
# the group figure by the public units reported $9.65 a unit against a filed $1.77.
PARENT_INCOME_TAGS = NET_INCOME_TAGS[:2]
COMMON_INCOME_TAGS = (
    "NetIncomeLossAvailableToCommonStockholdersDiluted",
    "NetIncomeLossAvailableToCommonStockholdersBasic",
)
# The numerator paired with the standard continuing-operations EPS elements.
# Keep this separate from annual net income: discontinued operations make the
# two economically different even when they happen to be numerically close.
CONTINUING_INCOME_TAGS = ("IncomeLossFromContinuingOperations",)
# ...and where only the group figure carries a trailing window, the minority's own
# line is what has to come out of it.
NCI_INCOME_TAGS = (
    "NetIncomeLossAttributableToNoncontrollingInterest",
    "NetIncomeLossAttributableToRedeemableNoncontrollingInterest",
)
# ASC 606 (2018) moved most filers from the SalesRevenue* elements onto
# RevenueFromContractWithCustomer*, so nearly every mature series switches tags
# mid-history — selection must be recency-first with per-year fill, like EPS.
REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",                                             # umbrella total, incl. non-contract
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",                                      # pre-606
    "SalesRevenueGoodsNet",
    "SalesRevenueServicesNet",
    # sector top lines — for REITs, utilities, banks and insurers the generic
    # elements above are absent or carry sub-scope scraps (Camden's `Revenues`
    # is $13M against $1.5B of lease income)
    "RegulatedAndUnregulatedOperatingRevenue",
    "RegulatedOperatingRevenue",
    "OperatingLeaseLeaseIncome",
    "OperatingLeasesIncomeStatementLeaseRevenue",
    "RealEstateRevenueNet",
    "RevenuesNetOfInterestExpense",
    "InterestAndDividendIncomeOperating",
    # BDC total investment income (ARCC/BXSL have no revenue series without it)
    # ranks above the narrower interest-only line, so the fuller total wins the
    # tie-break (PSEC); SYF's $22.6B of interest income has no other home
    "GrossInvestmentIncomeOperating",
    "InterestIncomeOperating",
    "PremiumsEarnedNet",
)
# Banks and insurers generally have no operating-income subtotal. Industrial
# filers may omit the subtotal while still filing a complete reconciliation; the
# narrow fallback in `_annual_operating_income` handles that case.
OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
GROSS_PROFIT_TAGS = ("GrossProfit",)
# Only primary-statement totals may vote on the company-wide fiscal calendar.
# Company Facts also exposes hundreds of annual note contexts.  Caleres, for
# example, has December 31 note periods beside its January/February audited
# statements; allowing those note dates to vote made the shared calendar erase
# five genuine fiscal years.  These concepts are broad enough to cover the three
# statements and their per-share denominator while excluding note-only dates.
_FISCAL_CALENDAR_TAGS = frozenset((
    EPS_CONTINUING_TAG,
    *EPS_TAGS,
    *EPS_BASIC_TAGS,
    *_WEIGHTED_SHARE_TAGS,
    *NET_INCOME_TAGS,
    *CONTINUING_INCOME_TAGS,
    *REVENUE_TAGS,
    *OPERATING_INCOME_TAGS,
    *GROSS_PROFIT_TAGS,
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
))
COST_OF_REVENUE_TAGS = (
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "CostOfGoodsSold",
)
OPERATING_EXPENSE_TAGS = (
    "OperatingExpenses",
    "SellingGeneralAndAdministrativeExpense",
)
NONOPERATING_INCOME_TAGS = ("NonoperatingIncomeExpense",)
NET_INTEREST_INCOME_TAGS = ("InterestIncomeExpenseNonoperatingNet",)
OTHER_NONOPERATING_INCOME_TAGS = ("OtherNonoperatingIncomeExpense",)
# Some industrial statements (Johnson & Johnson since FY2015) stop at pretax
# income instead of tagging an operating-income subtotal.  Their interest income
# and expense remain separate rather than appearing in the net-interest element
# above.  These narrow chains are used only by the inverse, fully aligned
# reconciliation in `_annual_operating_income`; they are not interchangeable with
# operating interest reported by banks and insurers.
SEPARATE_NONOPERATING_INTEREST_INCOME_TAGS = (
    "InvestmentIncomeInterest",
    "InterestIncomeNonoperating",
)
SEPARATE_NONOPERATING_INTEREST_EXPENSE_TAGS = (
    "InterestExpenseNonoperating",
    "InterestExpense",
)
SEPARATELY_PRESENTED_RESEARCH_EXPENSE_TAGS = (
    # The generic ResearchAndDevelopmentExpense is often only a note disclosure
    # already included in SG&A (FSTR FY2014).  It cannot prove a separate
    # statement row.  J&J's narrower element identifies the separately presented
    # R&D line and leaves acquired IPR&D as the small constrained residual.
    "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
)
DIVIDEND_TAGS = (
    ("PaymentsOfDividendsCommonStock", ("USD",)),
    ("DividendsCommonStockCash", ("USD",)),
    ("DividendsCommonStock", ("USD",)),
    ("CommonStockDividendsPerShareDeclared", ("USD/shares",)),
    ("CommonStockDividendsPerShareCashPaid", ("USD/shares",)),
    # Partnerships distribute rather than pay dividends; the LP payout to
    # unitholders is the common payout for criterion 5 (EPD, MPLX, UAN). The
    # LLC-member variant is deliberately ABSENT: in Up-C structures it names
    # payouts to NCI holders only and would mark non-payers as paying.
    ("DistributionMadeToLimitedPartnerCashDistributionsPaid", ("USD",)),
    ("DistributionMadeToLimitedPartnerDistributionsPaidPerUnit", ("USD/shares",)),
    # BDCs/investment companies: the per-share element first — MAIN's dollar
    # amount tag carries supplemental-declaration fragments, the per-share one
    # reproduces the true total (BXSL: 3.08 x shares == the paid amount).
    ("InvestmentCompanyDistributionToShareholdersPerShare", ("USD/shares",)),
    ("InvestmentCompanyDividendDistribution", ("USD",)),
    # Aggregates below: they roll up preferred, GP/IDR and noncontrolling
    # distributions too, so they evidence "a payout" but not specifically a
    # common one. Tried last and labelled in provenance so criterion 5
    # discloses what it rested on.
    ("PaymentsOfOrdinaryDividends", ("USD",)),
    ("PaymentsOfDividends", ("USD",)),
    ("DividendsCash", ("USD",)),
    ("Dividends", ("USD",)),
    ("PartnersCapitalAccountDistributions", ("USD",)),
)
_AGGREGATE_DIVIDEND_TAGS = frozenset((
    "PaymentsOfOrdinaryDividends", "PaymentsOfDividends", "DividendsCash", "Dividends",
    "InvestmentCompanyDividendDistribution", "PartnersCapitalAccountDistributions",
))
# inbound dividends (received/income/proceeds/equity-method), subsidiary
# minority-interest payouts, preferred-only lines, EPS-allocation mechanics
# (undistributed/distributable), temporary-equity accretion, and cost lines that
# merely contain the word "distribution" are not common payouts
_DIVIDEND_EVIDENCE_EXCLUDE_RE = re.compile(
    r"received|income|proceeds|equitymethod|minorityinterest|noncontrolling|preferred"
    r"|receivable|paidtoparent|policyholder|temporaryequity|statutoryaccounting"
    r"|undistributed|distributable|affiliates|servicing|productionanddistribution"
    r"|propertyplant|deferredcompensation|fees"
    # ...and elements that cannot be a payment at all. Withholding a verdict is the
    # conservative direction, but it withheld one Graham's criterion could give for
    # 218 companies — BJ's, MicroStrategy, Celsius, CoreWeave among them — on the
    # strength of a capacity disclosure, a balance-sheet payable, an equity
    # reclassification or an option-pricing input.
    r"|amountavailablefor|capitaladequacy|payablecurrent|payablenoncurrent"
    r"|additionalpaidincapital|expecteddividend|dividendrate|dividendyield"
    r"|excludinginterestanddividends|arrearage|dividendspayable", re.I
)
# Preferred dividends come out of income before anything reaches the common:
# EPS already nets them, the NetIncomeLoss tag does not, so every NI-vs-EPS
# arithmetic (implied share counts, identity checks) needs this series.
PREFERRED_DIVIDEND_TAGS = (
    "DividendsPreferredStock",
    "PaymentsOfDividendsPreferredStockAndPreferenceStock",
    "PreferredStockDividendsIncomeStatementImpact",
    # Wheeler REIT's undeclared preferred dividends — 3,274,000 of a 6,242,000
    # quarter — are tagged here and nowhere else, and without them its derived
    # per-share figure counted the preferred's money as the common's.
    "OtherPreferredStockDividendsAndAdjustments",
    "PreferredStockDividendsAndOtherAdjustments",
    "DividendsPreferredStockCash",
)
_ANNUAL_DAYS = range(340, 401)  # full-fiscal-year duration incl. 52/53-week years
_DIVIDEND_RECENCY_DAYS = 400  # broad window for the unknown-evidence scan only
# "currently pays" window scales with the fact's own tagging cadence: a suspended
# quarterly payer fails within ~2 quarters, while annual-only taggers aren't false-failed
_DIVIDEND_LAG_DAYS = 60
_STALE_DAYS = 400  # instant facts older than this vs the latest balance sheet are treated as missing

# Non-recurring / non-cash lines big enough to decide criterion 1 on their own.
NONCASH_TAGS = (
    ("InventoryLIFOReserveEffectOnIncomeNet", "inventory valuation (LCM/LIFO) adjustment"),
    ("AssetImpairmentCharges", "asset impairment"),
    ("GoodwillImpairmentLoss", "goodwill impairment"),
    ("ImpairmentOfIntangibleAssetsExcludingGoodwill", "intangible-asset impairment"),
    ("RestructuringCharges", "restructuring charges"),
    ("InventoryWriteDown", "inventory write-down"),
    ("BusinessCombinationAcquisitionRelatedCosts", "acquisition costs"),
    ("OtherNonrecurringIncomeExpense", "other nonrecurring income/expense"),
    # The 2025 taxonomy added the expense-only presentation element. Bruker
    # files it beside the older income/expense element for the exact same
    # "Other charges, net" rollup; `seen` below prevents that dual tagging from
    # producing two warnings, while filers that use only the successor still
    # remain visible.
    ("OtherNonrecurringExpense", "other nonrecurring expense"),
    ("InducedConversionOfConvertibleDebtExpense", "induced debt-conversion expense"),
)
# GIS files the rollup AND both specific impairments at different amounts; the
# rollup speaks only when neither specific line produced a material note.
_IMPAIRMENT_ROLLUP = ("GoodwillAndIntangibleAssetImpairment", "goodwill and intangible impairment")
_IMPAIRMENT_SPECIFICS = frozenset(("GoodwillImpairmentLoss", "ImpairmentOfIntangibleAssetsExcludingGoodwill"))
# Gain-signed lines: a positive value ADDS to pre-tax income. One-time gains
# silently flatter the P/E test the same way one-time charges depress it.
GAIN_TAGS = (
    ("GainLossOnInvestments", "investment gain/loss"),
    ("GainOnSaleOfInvestments", "investment sale gain"),
    ("UnrealizedGainLossOnInvestments", "unrealized investment gain/loss"),
    ("GainLossOnSaleOfPropertyPlantEquipment", "property disposal gain/loss"),
    ("GainLossOnSaleOfProperty", "property disposal gain/loss"),
    ("GainOrLossOnSaleOfStockInSubsidiary", "subsidiary stock sale gain/loss"),
    ("GainLossOnDispositionOfAssets1", "asset disposition gain/loss"),
    ("SaleAndLeasebackTransactionGainLossNet", "sale-and-leaseback gain/loss"),
)
# Warrant remeasurement has no reliable sign convention in the wild (AMZN vs
# CVNA file opposite orientations), so its note reports magnitude only.
_WARRANT_TAG = ("FairValueAdjustmentOfWarrants", "warrant fair-value remeasurement")
_NEUTRAL_QUALITY_TAGS = (
    # The element reports an income-statement effect but does not define whether
    # a positive fact is a gain or a loss. OPK uses it around foreign-currency/
    # highly-inflationary accounting, so disclose magnitude without inventing a
    # direction.
    ("AmountRecognizedInIncomeDueToInflationaryAccounting",
     "highly inflationary accounting effect"),
)
PRETAX_TAGS = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeignAndDomestic",
)
_NONCASH_MATERIALITY = Decimal("0.10")  # share of pre-tax income
_SUSPENSION_SHARE = Decimal("0.10")     # ...of the typical year, or the record broke
_SHARE_DIVERGENCE = Decimal("0.05")     # worth disclosing
# Beyond this the composite stops being arithmetic: subtracting a per-share figure
# struck on a pre-IPO share count from one struck after the raise produces a number
# that describes no company. A 2025 IPO showed +9.58 built from a -12.80 that was
# only large because it was divided by a fifth of the shares.
_SHARE_INCOMPARABLE = Decimal("0.25")
# per share; a quarterly swing beyond this against a much smaller year is mistagging
_ABSURD_DELTA = Decimal("5")
# No share has ever earned this much in a year — Berkshire's A shares, the highest-priced
# stock in the market, earn tens of thousands. A figure above this is a dollar total the
# filer tagged into a per-share element, and dividing a price by it passes criterion 1 on
# a P/E of nearly zero: GRUSF published 243,446,152 a share.
_IMPLAUSIBLE_EPS = Decimal("100000")
_OWNER_EARNINGS_LAG = 800      # flows this far behind the balance sheet are a different company
_INVESTED_CAPITAL_FLOOR = Decimal("0.02")   # ...of total assets, or the ratio is noise
_MILLION = Decimal("1000000")


def _debt_figures(gaap: dict, fresh: date | None,
                  current_liabilities: Fact | None) -> tuple[Fact | None, Fact | None, Fact | None]:
    """The three debt figures and the four rules that keep them honest.

    Borrowed money, as Graham reads it: finance leases count, operating rent does
    not. Every rule here was written for a filer that broke the obvious reading —
    a short bucket larger than the current liabilities containing it, a long bucket
    that is only a lease while the company's own history shows borrowings, a rollup
    older than its parts, a rollup smaller than a part it must contain. Returns
    (total, long, short); criterion 3 reconciles the three in `settled_debt`.
    """
    total_debt = _latest_instant_across(
        gaap, "TotalDebt",
        ("DebtLongtermAndShorttermCombinedAmount", "DebtAndCapitalLeaseObligations",
         "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"),
        not_before=fresh,
    )
    long_term_debt, ltd_suppress = _long_term_debt(gaap, fresh)
    short_term_debt = _short_term_debt(gaap, fresh, suppress=ltd_suppress)
    # Short-term debt is a line INSIDE current liabilities, so a bucket larger than
    # the whole cannot be right: 60 rows shipped one, Lumentum at $6.477B against
    # $3.865B of current liabilities and IBM at $11.547B where its 10-Q says
    # $5.775B. The dedupe registry is scoped to one tag pair and an exact date
    # match, so a component pair it does not name is added twice. Falling back to
    # the largest single component keeps a figure that is at least a real line.
    if (short_term_debt is not None and current_liabilities is not None
            and short_term_debt.value > current_liabilities.value
            and short_term_debt.provenance.components):
        biggest = max(short_term_debt.provenance.components,
                      key=lambda c: _component_value(gaap, c, fresh) or Decimal(0))
        recovered = _latest_instant(gaap, biggest.concept,
                                    (biggest.tag.split(":", 1)[-1],), not_before=fresh)
        short_term_debt = (recovered if recovered is not None
                           and recovered.value <= current_liabilities.value else None)

    # A long bucket built only from a finance lease is not a company's borrowings.
    # Ford's fresh debt elements are a $754M lease liability while its own funding
    # arm carries orders of magnitude more; its noncurrent debt tag went stale in
    # 2020 and the staleness guard correctly dropped it, leaving the lease alone to
    # answer criterion 3 at 0.09x. Where the filer's history shows borrowings the
    # bucket cannot see, the figure is missing, not small.
    if (long_term_debt is not None
            and all("FinanceLease" in t for t in _tags_behind(long_term_debt))
            and _debt_elsewhere(gaap, long_term_debt)):
        long_term_debt = None
    # A combined rollup older than the parts is the stale-tag trap in another
    # place: SRI's rollup froze at $0.9M while LongTermDebt moved to $180.9M a
    # quarter later. The fresher basis wins; criterion 3 then uses the parts.
    parts_end = max((f.provenance.period_end for f in (long_term_debt, short_term_debt)
                     if f is not None and f.provenance.period_end is not None), default=None)
    if (total_debt is not None and parts_end is not None
            and total_debt.provenance.period_end is not None
            and total_debt.provenance.period_end < parts_end):
        total_debt = None
    # A rollup smaller than a part it must contain is not a total but a fragment
    # wearing a total's tag: Pangaea attaches
    # LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities to a single
    # $38.5M "current portion of financing obligations" line while $235.6M of
    # noncurrent debt sits on the same balance sheet, and criterion 3 then reads a
    # 3.75x debt load as 0.52x. Sixty-four filers ship a "total" below their own
    # long-term part, 58 of them at an identical period end, so staleness cannot
    # explain it. Only a same-date comparison can say the two describe one moment.
    if total_debt is not None:
        for part in (long_term_debt, short_term_debt):
            if (part is not None
                    and part.provenance.period_end == total_debt.provenance.period_end
                    and total_debt.value < part.value):
                total_debt = None
                break
    return total_debt, long_term_debt, short_term_debt


def _senior_claims(gaap: dict, fresh: date | None, total_assets: Fact | None,
                   total_liabilities: Fact | None, liabilities_derived: bool,
                   parent_only_derivation: bool
                   ) -> tuple[Fact | None, Fact | None, Fact | None]:
    """Everything standing between the assets and the common shareholder.

    Book value per share is what is left after these, so each one that goes
    unread overstates what the common owns. Three of them, and they overlap in
    ways the tags do not admit: preferred stock, the minority holders' share of a
    consolidated subsidiary, and mezzanine equity that is neither debt nor common.
    Returns (preferred, noncontrolling interest, temporary equity).
    """
    # liquidation preference first: it is the economically correct common-TBV deduction
    # and better maintained than the par-value tag (JPM par tag is stale since 2009)
    preferred = _latest_instant(
        gaap, "PreferredStock",
        ("PreferredStockLiquidationPreferenceValue", "PreferredStockValue",
         "PreferredStockValueOutstanding"),
        not_before=fresh,
    )
    # A - Liabilities is total equity INCLUDING noncontrolling interest, so NCI must
    # come out of common TBV — except when liabilities were derived via parent-only
    # StockholdersEquity, which already left NCI inside the liabilities figure.
    nci = None
    redeemable_nci = None
    if not parent_only_derivation:
        # permanent leg: alternatives for one concept — first the classic tag,
        # then the fuller variants some filers use instead (MS: 1,111M under
        # NonredeemableNoncontrollingInterest while MinorityInterest sits stale)
        permanent_nci = _latest_instant_across(
            gaap, "NoncontrollingInterest",
            ("MinorityInterest", "NonredeemableNoncontrollingInterest",
             "MinorityInterestInOperatingPartnerships",
             # WLKP carries its sponsor's two-thirds of OpCo here and nowhere else
             "MinorityInterestInLimitedPartnerships",
             "MinorityInterestInPreferredUnitHolders",   # Uber, $869M
             "PartnersCapitalAttributableToNoncontrollingInterest"),
            not_before=fresh,
        )
        redeemable_total = _latest_instant(
            gaap, "NoncontrollingInterest (redeemable)",
            ("RedeemableNoncontrollingInterestEquityCarryingAmount",), not_before=fresh,
        )
        # disjoint components, which stand in for the total where it is absent and
        # replace it where it has been abandoned mid-history (UDR tags a component
        # equal to the total; ET moved to the Other element in 2026)
        redeemable_parts = _sum_facts("NoncontrollingInterest (redeemable components)", [
                _latest_instant(
                    gaap, "NoncontrollingInterest (redeemable preferred)",
                    ("RedeemableNoncontrollingInterestEquityPreferredCarryingAmount",),
                    not_before=fresh),
                _latest_instant(
                    gaap, "NoncontrollingInterest (redeemable common)",
                    ("RedeemableNoncontrollingInterestEquityCommonCarryingAmount",),
                    not_before=fresh),
                _latest_instant(
                    gaap, "NoncontrollingInterest (redeemable other)",
                    ("RedeemableNoncontrollingInterestEquityOtherCarryingAmount",),
                    not_before=fresh),
            ])
        redeemable_nci = _fresher_rollup(redeemable_total, redeemable_parts)
        if redeemable_nci is None and _latest_instant(
            gaap, "TemporaryEquity (incl. NCI)",
            ("TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests",),
            not_before=fresh,
        ) is None:
            # fair value only when every carrying representation is absent —
            # HEICO tags fair value AND the incl-NCI temporary line for the
            # same holders; counting both would remove them twice
            redeemable_nci = _latest_instant(
                gaap, "NoncontrollingInterest (redeemable, fair value)",
                ("RedeemableNoncontrollingInterestEquityFairValue",), not_before=fresh,
            )
        nci = _sum_facts("NoncontrollingInterest", [permanent_nci, redeemable_nci])
    # Mezzanine (temporary) equity is senior to common but sits outside both the
    # preferred tag and liabilities (GTN: $600M of redeemable preferred = 28% of
    # equity, invisible to tangible book). Deducted only when the preferred slot
    # is empty or a zero placeholder (KDP tags PreferredStockValue=0 beside
    # $4.4B of mezzanine) and liabilities were NOT derived from the accounting
    # identity — a derived figure already contains the mezzanine.
    temporary_equity = None
    if not liabilities_derived and (preferred is None or preferred.value == 0):
        temporary_equity = _latest_instant(
            gaap, "TemporaryEquity", ("TemporaryEquityCarryingAmountAttributableToParent",),
            not_before=fresh,
        )
        if temporary_equity is None and redeemable_nci is None:
            # the incl-NCI variant only when no redeemable-NCI leg was counted,
            # or the same holders would come out twice (HEICO/ADM)
            temporary_equity = _latest_instant(
                gaap, "TemporaryEquity (incl. NCI)",
                ("TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests",),
                not_before=fresh,
            )
        if temporary_equity is None:
            # liquidation preference >= carrying value: the conservative stand-in
            temporary_equity = _latest_instant(
                gaap, "TemporaryEquity (liquidation preference)",
                ("TemporaryEquityLiquidationPreference",), not_before=fresh,
            )
    return preferred, nci, temporary_equity


def _ifrs_as_us_gaap(ifrs: dict) -> dict:
    """Expose exact IFRS equivalents through the established extraction chains.

    Entries are copied because Company Facts is shared with evidence/audit callers.
    Private markers retain both the filed namespace/tag and the normalized element
    used for rule selection; neither marker is serialized as filing data.
    """
    out: dict[str, dict] = {}
    for canonical, aliases in _IFRS_ALIASES.items():
        primary = next((source for source in aliases if source in ifrs), None)
        if primary is None:
            continue
        units: dict[str, list[dict]] = {}
        seen: dict[str, set[tuple[str | None, str | None]]] = {}
        source_data = None
        # The continuing-operations OCF total fills gaps only. For every other
        # concept, retain the established first-available-tag priority.
        sources = (
            aliases if canonical == "NetCashProvidedByUsedInOperatingActivities"
            else (primary,)
        )
        for source in sources:
            if source not in ifrs:
                continue
            source_data = source_data or ifrs[source]
            for unit, entries in (ifrs[source].get("units") or {}).items():
                selected = units.setdefault(unit, [])
                covered = seen.setdefault(unit, set())
                for entry in entries:
                    period = (entry.get("start"), entry.get("end"))
                    # Preserve every entry from the established primary tag.
                    # Only the fallback total is omitted when that period already
                    # has a primary OCF fact.
                    if source != primary and period in covered:
                        continue
                    covered.add(period)
                    selected.append({
                        **entry,
                        "_source_namespace": "ifrs-full",
                        "_source_tag": source,
                        "_normalized_tag": canonical,
                    })
        if source_data is not None:
            out[canonical] = {**source_data, "units": units}
    return out


def build_snapshot(
    ticker: str, cik: str, companyfacts: dict, assume_absent_zero: bool = False,
    dimensioned: dict | None = None, receipt: dict | None = None,
    foreign_identity_filing: tuple[str, str] | None = None,
) -> FinancialSnapshot:
    facts = companyfacts.get("facts", {})
    adapter = companyfacts.get("_adapter") or {}
    canonical_adapter = (
        adapter.get("statement_basis") == "canonical"
        and isinstance(facts.get("canonical"), dict)
    )
    statement_basis = (
        "canonical" if canonical_adapter
        else _reject_foreign(facts, receipt, identity_filing=foreign_identity_filing)
    )
    reporting_currency = (
        adapter.get("reporting_currency", "USD") if canonical_adapter
        else _current_statement_currency(facts, statement_basis) or "USD"
    )
    gaap = _with_fiscal_calendar(
        facts.get("canonical", {}) if statement_basis == "canonical"
        else facts.get("us-gaap", {}) if statement_basis == "us-gaap"
        else _ifrs_as_us_gaap(facts.get("ifrs-full", {})),
        cik=cik,
    )
    gaap.canonical_adapter = canonical_adapter
    gaap.reporting_currency = reporting_currency
    gaap.currency_adapter = canonical_adapter or reporting_currency != "USD"
    dei = facts.get("dei", {})
    # Company Facts cannot express a dimension, so a filer that reports only by
    # share class looks silent to it. The unambiguous half of those facts is read
    # through the same chains. A consolidated figure normally outranks one that
    # had to be attributed to a class; the narrow reconciliation gate below is
    # the exception when the filing's own income and weighted count prove the
    # consolidated EPS is mis-scaled.
    security_key = (str(cik).zfill(10), ticker.upper())
    verified_security_dimensions = _VERIFIED_SECURITY_DIMENSIONS.get(security_key)
    classed = (None if canonical_adapter else _unambiguous_dimensioned(
        dimensioned, _registered_class_title(ticker, receipt), statement_basis,
        verified_members=verified_security_dimensions))
    if classed:
        classed = _with_fiscal_calendar(classed, cik=cik)
        classed.reporting_currency = reporting_currency
        classed.currency_adapter = reporting_currency != "USD"
    sibling_classed = None
    sibling_dimensions = _VERIFIED_SECURITY_SIBLING_DIMENSIONS.get(security_key)
    if sibling_dimensions:
        sibling_classed = _unambiguous_dimensioned(
            dimensioned, statement_basis=statement_basis,
            verified_members=sibling_dimensions)
        if sibling_classed:
            sibling_classed = _with_fiscal_calendar(sibling_classed, cik=cik)
            sibling_classed.reporting_currency = reporting_currency
            sibling_classed.currency_adapter = reporting_currency != "USD"

    bare_eps = _annual_eps(gaap)
    annual_eps = bare_eps
    classed_eps: dict[int, Fact] = {}
    if classed:
        classed_eps = _annual_eps(classed)
        if verified_security_dimensions:
            # A dual-class issuer can allocate consolidated profit differently
            # between its listed classes.  A direct class EPS is consequently
            # stronger evidence for the priced security than the flattened value
            # Company Facts exposes, and is not expected to reconcile to total NI.
            # Before the second class existed, the undimensioned history remains
            # the only and correct common-stock series.  From the first classed
            # year onward, however, absence is safer than silently falling back to
            # the sibling class that Company Facts happened to flatten.
            first_classed_year = min(classed_eps, default=None)
            annual_eps = {
                **({y: f for y, f in annual_eps.items() if y < first_classed_year}
                   if first_classed_year is not None else {}),
                **classed_eps,
            }
        else:
            annual_eps = _prefer_reconciling_classed_eps(
                gaap, {**classed_eps, **annual_eps}, classed_eps)
    if verified_security_dimensions and classed:
        ttm_eps, ttm_inputs = _ttm_eps(classed, annual_eps)
    else:
        ttm_eps, ttm_inputs = _ttm_eps(gaap, annual_eps)
        if ttm_eps is None and classed:
            ttm_eps, ttm_inputs = _ttm_eps(classed, annual_eps)
    annual_net_income = _annual_net_income(gaap)
    ttm_net_income, ttm_ni_inputs = _ttm_eps(gaap, annual_net_income, unit=("USD",),
                                             per_share=False)
    # A negative "revenue" is a fund's net investment loss wearing a revenue
    # element (the gold trusts do this); it is not a top line, and the size test
    # would read it as a company that sold less than nothing.
    annual_revenue = {y: f for y, f in _annual_revenue(gaap).items() if f.value >= 0}
    annual_gross_profit = _annual_gross_profit(gaap)
    ttm_revenue, ttm_revenue_inputs = _ttm_eps(gaap, annual_revenue, unit=("USD",),
                                               per_share=False)
    if ttm_revenue is not None and ttm_revenue < 0:
        ttm_revenue, ttm_revenue_inputs = None, ()

    # LiabilitiesAndStockholdersEquity equals total assets by the accounting identity,
    # so it stands in for an untagged Assets total. Keep the normal priority chain:
    # independently advancing this side of the balance sheet can pair it with a
    # stale liability total from another date.
    total_assets = _latest_instant(
        gaap, "Assets", ("Assets", "LiabilitiesAndStockholdersEquity"))
    # §5.1: a concept absent from recent filings is missing, not resurrectable from
    # a years-old filing (e.g. a filer that stopped reporting Goodwill).
    fresh = (
        total_assets.provenance.period_end - timedelta(days=_STALE_DAYS)
        if total_assets and total_assets.provenance.period_end
        else None
    )
    # Assets cannot be negative. A filer that tags them so has made a sign error,
    # and carrying it through would produce a current ratio and a net current
    # asset value describing no company.
    if total_assets is not None and total_assets.value < 0:
        total_assets = None
    current_assets = _latest_instant(gaap, "AssetsCurrent", ("AssetsCurrent",), not_before=fresh)
    if current_assets is not None and current_assets.value < 0:
        current_assets = None
    current_liabilities = _latest_instant(gaap, "LiabilitiesCurrent", ("LiabilitiesCurrent",), not_before=fresh)
    if current_liabilities is None:
        # some classified filers tag only the noncurrent split; the identity
        # fills the current side. NEVER mirrored on the asset side —
        # AssetsNoncurrent is an ASC 280 disclosure, not a balance-sheet rollup.
        current_liabilities = _derived_instant(
            gaap, "LiabilitiesCurrent (derived: Liabilities - LiabilitiesNoncurrent)",
            "Liabilities", "LiabilitiesNoncurrent", fresh,
        )
    # Both legs must come from one balance sheet. Solidion shows a current ratio of
    # 1.46 built from assets at 2026-06-30 and liabilities at 2024-12-31; its own
    # June balance sheet gives 2.66. Where the pair cannot be struck at one date,
    # the ratio is not available — a figure spanning two years is not a ratio.
    if (current_assets is not None and current_liabilities is not None
            and current_assets.provenance.period_end != current_liabilities.provenance.period_end):
        slots = (("AssetsCurrent", ("AssetsCurrent",)),
                 ("LiabilitiesCurrent", ("LiabilitiesCurrent",)))
        # the newest balance sheet first — it is the one every other figure on the
        # row is struck at — then the older date, where the pair may still exist
        ends = sorted({current_assets.provenance.period_end,
                       current_liabilities.provenance.period_end}, reverse=True)
        current_assets = current_liabilities = None
        for end in ends:
            matched = [_at_period_end(gaap, concept, tags, end) for concept, tags in slots]
            if all(f is not None for f in matched):
                current_assets, current_liabilities = matched
                break
    total_debt, long_term_debt, short_term_debt = _debt_figures(
        gaap, fresh, current_liabilities)
    options_outstanding = _latest_instant(gaap, "OptionsOutstanding", OPTION_COUNT_TAGS,
                                          unit=("shares",), not_before=fresh)
    rsus_outstanding = _latest_instant(gaap, "RestrictedStockUnitsOutstanding", RSU_COUNT_TAGS,
                                       unit=("shares",), not_before=fresh)
    total_liabilities = _latest_instant(gaap, "Liabilities", ("Liabilities",), not_before=fresh)
    classified_liabilities = _classified_liabilities_from_statement(gaap, fresh)
    # Replace only an abandoned direct total with a genuinely newer complete
    # classified statement. If no direct total exists, the established equity-
    # identity derivation below remains authoritative; changing that choice for
    # hundreds of otherwise stable rows is a separate audit, not a side effect
    # of repairing stale tags such as DPZ's.
    if (total_liabilities is not None and classified_liabilities is not None
            and total_liabilities.provenance.period_end is not None
            and classified_liabilities.provenance.period_end is not None
            and classified_liabilities.provenance.period_end
            > total_liabilities.provenance.period_end):
        total_liabilities = classified_liabilities
    # A stopped total tag must not outrank the current statement merely because
    # it remains inside the 400-day freshness window. Bruker's June 2026 balance
    # sheet prints these three exhaustive liability rows while its nominal
    # Liabilities element stopped at December 2025.
    total_liabilities = _fresher_rollup(
        total_liabilities, _verified_liabilities_from_statement(gaap, fresh))
    parent_only_derivation = False
    liabilities_derived = False
    exact_identity_liabilities = _exact_total_equity_liabilities_successor(
        gaap, fresh, total_assets)
    if (total_liabilities is not None and exact_identity_liabilities is not None
            and total_liabilities.provenance.period_end is not None
            and exact_identity_liabilities.provenance.period_end is not None
            and exact_identity_liabilities.provenance.period_end
            > total_liabilities.provenance.period_end):
        total_liabilities = exact_identity_liabilities
        liabilities_derived = True
    elif total_liabilities is None:
        total_liabilities, parent_only_derivation = _derive_liabilities(gaap, fresh)
        liabilities_derived = total_liabilities is not None
    # Never present or subtract two sides from different balance sheets. Keep the
    # raw facts available to independent extractors: total assets is also the
    # owner-earnings scale guard and identifies the statement taxonomy/history.
    # Only the current, mutually comparable pair is withheld from the snapshot.
    # Otherwise repairing a current balance mismatch can accidentally unlock a
    # corrupt cash-flow series or erase valid annual IFRS history.
    aligned_total_assets = total_assets
    aligned_total_liabilities = total_liabilities
    unclassified_balance_sheet = (
        current_assets is None and current_liabilities is None
        and total_assets is not None
    )
    if (total_assets is not None and total_liabilities is not None
            and total_assets.provenance.period_end is not None
            and total_liabilities.provenance.period_end is not None
            and total_assets.provenance.period_end
            != total_liabilities.provenance.period_end):
        if (total_assets.provenance.period_end
                > total_liabilities.provenance.period_end):
            aligned_total_liabilities = None
        else:
            aligned_total_assets = None
    goodwill, intangibles = _goodwill_and_intangibles(gaap, fresh)
    preferred, nci, temporary_equity = _senior_claims(
        gaap, fresh, total_assets, total_liabilities, liabilities_derived,
        parent_only_derivation)
    shares = _latest_instant(
        gaap, "SharesOutstanding", ("CommonStockSharesOutstanding",), unit=("shares",), not_before=fresh
    )
    if shares is None:
        shares = _latest_instant(
            dei, "SharesOutstanding", ("EntityCommonStockSharesOutstanding",),
            ns="dei", unit=("shares",), not_before=fresh,
        )
    if shares is None:
        # the generic instant only AFTER the specific tags and the dei cover:
        # fragment and mis-scale cases are shielded by that ranking (SLB)
        shares = _latest_instant(
            gaap, "SharesOutstanding", ("SharesOutstanding",), unit=("shares",), not_before=fresh
        )
    if shares is None:
        shares = _weighted_shares(gaap, fresh)
    if shares is None and verified_security_dimensions and classed and sibling_classed:
        shares = _verified_total_current_shares(classed, sibling_classed, fresh)
    if shares is None and classed:
        shares = _latest_instant(
            classed, "SharesOutstanding", ("CommonStockSharesOutstanding",),
            unit=("shares",), not_before=fresh,
        ) or _weighted_shares(classed, fresh)
    if shares is None and _is_partnership(gaap, fresh):
        # LP unit instants are OP-unit fragments at REITs (MAA: 2.9M against a
        # 116M cover), so they count only for a filer that has partners capital
        # and no stockholders equity at all (SUN, KRP, DLNG)
        shares = _latest_instant(
            gaap, "SharesOutstanding (limited partner units)",
            ("LimitedPartnersCapitalAccountUnitsOutstanding",), unit=("shares",), not_before=fresh,
        )
    annual_preferred_dividends = _annual_union(gaap, PREFERRED_DIVIDEND_TAGS)
    # "Stale facts are missing facts" was never applied to this series: Wheeler's
    # last preferred fact ends 2019-12-31 with a value of zero, and the trailing
    # roll carried that zero forward as though the company had stopped paying its
    # preferred holders seven years ago.
    if fresh is not None:
        annual_preferred_dividends = {
            y: f for y, f in annual_preferred_dividends.items()
            if f.provenance.period_end is None or f.provenance.period_end >= fresh}
    ttm_preferred_dividends, _ = _ttm_eps(gaap, annual_preferred_dividends,
                                          unit=("USD",), per_share=False)
    # implied shares read only TAGGED eps — derived years below are computed FROM
    # the share count and would otherwise vote for themselves
    shares = _sane_shares(
        shares, gaap, dei, fresh,
        implied=(None if verified_security_dimensions else _implied_shares(
            gaap, annual_eps, annual_net_income, annual_preferred_dividends)),
    )
    # An option pool larger than the company it is granted out of is a tagging
    # error, not dilution: Greenlane's pool jumps from 235,000 to 235,000,000
    # between two quarters, Zerocarbon reports 3.2bn options against 10.4M shares,
    # and Astrotech 234.4M against 1.76M. Seventeen rows in all, against a median
    # overhang of 2.8% and a 99th percentile of 72%. §5.1: withhold rather than
    # publish a figure the filing cannot mean.
    if shares is not None and shares.value > 0:
        if options_outstanding is not None and options_outstanding.value > shares.value:
            options_outstanding = None
        if rsus_outstanding is not None and rsus_outstanding.value > shares.value:
            rsus_outstanding = None

    # Filers that tag earnings per share only on a share-class axis publish no
    # per-share element the Company Facts API can return (KKR since 2017, PAA
    # since 2016), and a co-op may simply stop tagging one. Their own income and
    # share count still divide, so the series continues as disclosed arithmetic
    # rather than stopping years before the balance sheet.
    derived_eps = ({} if verified_security_dimensions else _derived_annual_eps(
        gaap, dei, annual_eps, annual_net_income, annual_preferred_dividends,
        has_nci=_has_minority_interest(gaap, fresh, nci)))
    if derived_eps:
        # the same bound the tagged series carries: a per-share element holding a
        # dollar total is unusable, and a derived one can be built from a share
        # count small enough to say the same thing (-$20,176,748.50 a share shipped)
        annual_eps = {y: f for y, f in {**annual_eps, **derived_eps}.items()
                      if abs(f.value) <= _IMPLAUSIBLE_EPS}
        # A filer that stopped tagging per-share figures still has a per-share
        # TTM in its own income statement. The tagged trailing figure is not
        # merely absent for these filers — it is years stale (KKR's last tagged
        # EPS period ended 2018), and a stale figure priced against today's quote
        # is worse than none, so newer income overrides it.
        def _newest_end(facts) -> date:
            return max((f.provenance.period_end for f in facts if f.provenance.period_end),
                       default=date.min)

        income_is_newer = _newest_end(ttm_ni_inputs) > _newest_end(ttm_inputs)
        # ...and the same minority-interest guard the annual series applies: a
        # trailing figure built from ProfitLoss divides the whole group's profit,
        # the sponsor's two thirds included, by the units the public holds.
        group_profit = any(_tag_of(f) == "ProfitLoss" for f in ttm_ni_inputs)
        trailing_income, trailing_inputs = ttm_net_income, ttm_ni_inputs
        if group_profit and _has_minority_interest(gaap, fresh, nci):
            trailing_income, trailing_inputs = _ttm_eps(
                gaap, _annual_dollar_series(gaap, PARENT_INCOME_TAGS),
                unit=("USD",), per_share=False)
            if trailing_income is None:
                # Ares and KKR tag no annual parent series long enough to roll a
                # trailing window, but both report the minority's share directly.
                # Subtracting it reaches the same figure from the other side.
                minority = sum(
                    (v for tag in NCI_INCOME_TAGS
                     if (v := _ttm_eps(gaap, _annual_dollar_series(gaap, (tag,)),
                                       unit=("USD",), per_share=False)[0]) is not None),
                    Decimal(0))
                if minority:
                    trailing_income = ttm_net_income - minority
                    trailing_inputs = ttm_ni_inputs
        reported_scope = annual_eps[max(annual_eps)] if annual_eps else None
        if (not verified_security_dimensions
                and (ttm_eps is None or income_is_newer)
                and (reported_scope is None or _eps_uses_total_income(reported_scope))
                and trailing_income is not None and shares and shares.value > 0):
            ttm_eps = ((trailing_income - (ttm_preferred_dividends or Decimal(0)))
                       / shares.value)
            # the trailing figure still carries a date: the newest income period
            # behind it, which is what decides whether a price may be compared
            newest = max(trailing_inputs, key=lambda f: f.provenance.period_end or date.min,
                         default=None)
            if newest is not None:
                p = newest.provenance
                ttm_inputs = (Fact(value=ttm_eps, provenance=Provenance(
                    concept="TTM EPS (derived: earnings available to common / share count)",
                    tag=f"{p.tag} / {shares.provenance.tag}",
                    fiscal_year=None, form=p.form, accession=p.accession,
                    filed=p.filed, period_end=p.period_end, period_start=p.period_start,
                )),)

    balance_sheet_date = next(
        (f.provenance.period_end
         for f in (current_assets, total_assets)
         if f is not None),
        None,
    )
    # A trailing figure anchored to a decade-old annual filing is not trailing:
    # iShares Gold Trust last tagged revenue for 2013 and it was being presented
    # beside a 2026 balance sheet. Old is not the same as absent, but presenting
    # it as the current twelve months is the same as being wrong.
    if balance_sheet_date is not None:
        floor = balance_sheet_date - timedelta(days=_STALE_DAYS)
        if _newest_period_end(ttm_revenue_inputs) < floor:
            ttm_revenue = None

    reference = balance_sheet_date or _latest_annual_end(annual_eps)
    # UHAL and UHAL.B share one issuer but not one dividend policy.  The
    # verified class slice is the only evidence that answers whether the priced
    # voting security itself paid; consolidated cash dividends answer a
    # different question.
    security_gaap = classed if verified_security_dimensions and classed else gaap
    pays_dividend, dividend = _dividend(security_gaap, reference)
    dividend_per_share = _dividend_per_share(
        security_gaap, dividend, shares, fresh)
    recurring_dividend_per_share = _recurring_dividend_per_share(
        security_gaap, balance_sheet_date)

    assumed: set[str] = set()
    if assume_absent_zero:
        clean = _absent_zero_candidates(facts)
        # only concepts that are BOTH evidence-free in the current annual-report
        # window AND missing after extraction, and debt only where a classified
        # balance sheet exists (banks/REITs stay N/A)
        if ("debt" in clean and total_debt is None
                and (long_term_debt is None or short_term_debt is None)
                and current_assets is not None and current_liabilities is not None):
            assumed.add("debt")
        elif ("short_term_debt" in clean and short_term_debt is None
              and current_assets is not None and current_liabilities is not None):
            # A separately filed noncurrent balance does not imply that an
            # unreported current bucket is unknown forever. Under the explicit
            # mode, current-filing silence supplies only that missing zero.
            assumed.add("short_term_debt")
        if "goodwill" in clean and goodwill is None:
            assumed.add("goodwill")
        if "intangibles" in clean and intangibles is None:
            assumed.add("intangibles")

    # Whether the filing is internally consistent is asked of the filing's own
    # figures, before any restatement onto the traded security — the ratio moves
    # both sides of that comparison and would otherwise create the mismatch it is
    # there to detect.
    basis_conflict = (None if verified_security_dimensions else _basis_conflict(
        gaap, dei, annual_eps, annual_net_income, annual_preferred_dividends,
        _has_minority_interest(gaap, fresh, nci), shares))
    if basis_conflict is None and not verified_security_dimensions:
        basis_conflict = _depositary_dimension_conflict(
            bare_eps, classed_eps, receipt)

    # The annual-history table used to infer this count by dividing total net
    # income by EPS. That arithmetic is invalid when EPS is continuing-operations
    # income, an LP-unit allocation, or any other narrower numerator. Carry the
    # weighted count the filing actually reports instead. Dimensioned counts fill
    # only the years Company Facts cannot see, exactly as dimensioned EPS does.
    annual_share_counts = _annual_share_counts(
        gaap, dei,
        None if verified_security_dimensions else annual_eps,
        None if verified_security_dimensions else annual_net_income,
        None if verified_security_dimensions else annual_preferred_dividends)
    if verified_security_dimensions and classed and sibling_classed:
        # Entity-level equity and cash flow belong to both common classes.  The
        # filing states each class count separately, so their exact same-period
        # sum is the proper denominator even though EPS/dividends remain classed.
        annual_share_counts = {
            **_verified_total_annual_shares(classed, sibling_classed),
            **annual_share_counts,
        }
    elif classed:
        annual_share_counts = {
            **_annual_share_counts(
                classed, {},
                None if verified_security_dimensions else annual_eps,
                None if verified_security_dimensions else annual_net_income,
                None if verified_security_dimensions else annual_preferred_dividends),
            **annual_share_counts,
        }

    annual_eps, annual_share_counts = _restate_verified_equivalent_class_history(
        security_key, annual_eps, annual_share_counts)

    vintage = vintage_ttm_eps(gaap)
    if verified_security_dimensions and classed:
        # For a verified class, the class series is the traded security.  After
        # class-specific reporting begins, a missing class vintage must not fall
        # back to the flattened sibling series.
        class_ends = [f.provenance.period_end for f in classed_eps.values()
                      if f.provenance.period_end]
        if class_ends:
            class_start = min(class_ends).isoformat()
            vintage = {end: value for end, value in vintage.items()
                       if end < class_start}
        vintage.update(vintage_ttm_eps(classed))
    elif classed and any(_is_equivalent_class_basis(f) for f in classed_eps.values()):
        # Historical prices need earnings on the same class basis as the current
        # quote. Berkshire's "Equivalent Class B" facts explicitly put the whole
        # company onto that economic unit; Company Facts cannot carry the axis, so
        # let the selected equivalent basis fill vintages it cannot compute. Bare
        # values still win wherever they exist, matching the main EPS selection rule.
        vintage = {**vintage_ttm_eps(classed), **vintage}

    # A depositary receipt is priced per receipt while the statements count the
    # ordinary shares behind it; the cover names the ratio and nothing else can.
    if receipt and receipt.get("ratio"):
        ratio = Decimal(str(receipt["ratio"]))
        if ratio > 0 and ratio != 1:
            parts = {"shares": shares, "annual_eps": annual_eps,
                     "annual_share_counts": annual_share_counts, "ttm_eps": ttm_eps,
                     "dividend_per_share": dividend_per_share,
                     "recurring_dividend_per_share": recurring_dividend_per_share,
                     "ttm_eps_inputs": ttm_inputs, "ttm_eps_vintage": vintage}
            _restate_onto_receipt(
                parts, ratio, receipt.get("basis_accn") or receipt.get("accn", "")
            )
            shares, annual_eps = parts["shares"], parts["annual_eps"]
            annual_share_counts = parts["annual_share_counts"]
            ttm_eps, dividend_per_share = parts["ttm_eps"], parts["dividend_per_share"]
            recurring_dividend_per_share = parts["recurring_dividend_per_share"]
            ttm_inputs, vintage = parts["ttm_eps_inputs"], parts["ttm_eps_vintage"]

    # Owner-earnings evidence per share belongs on the same split/receipt basis as
    # every other per-share history. Build it only after the reported annual
    # denominator has gone through the security-class restatement above.
    annual_operating_income = _annual_operating_income(gaap)
    annual_operating_returns = _annual_operating_returns(
        gaap, annual_operating_income,
        cik=cik,
        assume_absent_zero=assume_absent_zero,
        all_facts=facts,
        fallback_end=balance_sheet_date)
    conservative_operating_returns = (
        _annual_operating_returns(
            gaap, annual_operating_income,
            cik=cik,
            years=1,
            conservative_missing_zero=True,
            all_facts=facts,
            fallback_end=balance_sheet_date)
        if not assume_absent_zero else {})
    owner_earnings = _owner_earnings(gaap, {
        "total_assets": total_assets,
        "current_liabilities": current_liabilities,
        "short_term_debt": short_term_debt,
    }, fresh, annual_share_counts, classed,
        cik=cik,
        dimensioned=dimensioned,
        assume_absent_debt_zero=bool(
            {"debt", "short_term_debt"} & assumed))

    operating_lease_liability = _latest_instant(
        gaap, "OperatingLeaseLiability", LEASE_OBLIGATION_TAGS,
        not_before=fresh)
    lease_cost_series = _annual_union(gaap, LEASE_COST_TAGS)
    lease_cost = None
    if lease_cost_series:
        candidate = lease_cost_series[max(lease_cost_series)]
        if fresh is None or (candidate.provenance.period_end is not None
                             and candidate.provenance.period_end >= fresh):
            lease_cost = candidate
    fixed_charge_coverage = None
    operating_series = annual_operating_income
    interest_series = _annual_union(gaap, INTEREST_EXPENSE_TAGS)
    if lease_cost is not None and lease_cost.provenance.fiscal_year is not None:
        lease_year = lease_cost.provenance.fiscal_year
        operating_source = operating_series.get(lease_year)
        interest_source = interest_series.get(lease_year)
        if (operating_source is not None and interest_source is not None
                and len({lease_cost.provenance.period_end,
                         operating_source.provenance.period_end,
                         interest_source.provenance.period_end}) == 1):
            fixed_charges = abs(interest_source.value) + abs(lease_cost.value)
            if fixed_charges > 0:
                fixed_charge_coverage = _derived_flow(
                    "Fixed-charge coverage proxy (operating income plus lease cost / "
                    "interest plus lease cost)",
                    f"({operating_source.provenance.tag} + {lease_cost.provenance.tag}) / "
                    f"({interest_source.provenance.tag} + {lease_cost.provenance.tag})",
                    (operating_source.value + abs(lease_cost.value)) / fixed_charges,
                    (operating_source, interest_source, lease_cost), lease_year)

    return FinancialSnapshot(
        cik=cik,
        ticker=ticker,
        annual_eps=annual_eps,
        annual_share_counts=annual_share_counts,
        annual_net_income=annual_net_income,
        ttm_net_income=ttm_net_income,
        ttm_eps=ttm_eps,
        ttm_eps_inputs=ttm_inputs,
        ttm_eps_vintage=vintage,
        annual_revenue=annual_revenue,
        ttm_revenue=ttm_revenue,
        annual_gross_profit=annual_gross_profit,
        annual_operating_income=annual_operating_income,
        dividend_record=_dividend_record(security_gaap),
        current_assets=current_assets,
        current_liabilities=current_liabilities,
        long_term_debt=long_term_debt,
        short_term_debt=short_term_debt,
        total_assets=aligned_total_assets,
        total_liabilities=aligned_total_liabilities,
        goodwill=goodwill,
        intangibles=intangibles,
        preferred_stock=preferred,
        temporary_equity=temporary_equity,
        ttm_preferred_dividends=ttm_preferred_dividends,
        annual_preferred_dividends={y: f.value for y, f
                                    in annual_preferred_dividends.items()},
        shares_outstanding=shares,
        cover_shares=_latest_instant(dei, "EntityCommonStockSharesOutstanding",
                                     ("EntityCommonStockSharesOutstanding",),
                                     ns="dei", unit=("shares",)),
        dividend=dividend,
        dividend_per_share=dividend_per_share,
        recurring_dividend_per_share=recurring_dividend_per_share,
        pays_dividend=pays_dividend,
        balance_sheet_date=balance_sheet_date,
        operating_lease_liability=operating_lease_liability,
        lease_cost=lease_cost,
        fixed_charge_coverage=fixed_charge_coverage,
        total_debt=total_debt,
        options_outstanding=options_outstanding,
        rsus_outstanding=rsus_outstanding,
        assumed_zero=frozenset(assumed),
        noncontrolling_interest=nci,
        earnings_quality=_earnings_quality(gaap, ttm_inputs, annual_eps),
        context_notes=_context_notes(gaap, annual_eps, annual_net_income,
                                     _annual_operating_income(gaap),
                                     annual_revenue, long_term_debt, shares,
                                     # Disclosure materiality is independent of
                                     # whether the current A/L pair may be shown.
                                     # Retain its established scale input here;
                                     # the mismatched equity itself is never
                                     # serialized or used by a criterion.
                                     _common_equity(total_assets,
                                                    total_liabilities,
                                                    preferred, nci, temporary_equity),
                                     fresh, dimensioned),
        tax_record=_tax_record(gaap, fresh),
        annual_operating_returns=annual_operating_returns,
        conservative_operating_returns=conservative_operating_returns,
        owner_earnings=owner_earnings,
        basis_conflict=basis_conflict,
        reporting_currency=reporting_currency,
        unclassified_balance_sheet=unclassified_balance_sheet,
    )


def _filing_keys(taxo: dict, forms: tuple[str, ...]) -> set[tuple[str, str]]:
    """Return the filing dates/accessions represented in one taxonomy."""
    out: set[tuple[str, str]] = set()
    for tagdata in taxo.values():
        for entries in tagdata.get("units", {}).values():
            for entry in entries:
                if entry.get("form", "").startswith(forms) and entry.get("filed"):
                    out.add((entry["filed"], entry.get("accn", "")))
    return out


_ISO_CURRENCY = re.compile(r"[A-Z]{3}")


def _balance_currency_in_filing(
    taxo: dict,
    filing: tuple[str, str],
    anchors: tuple[str, ...] = _FOREIGN_BALANCE_ANCHORS,
) -> str | None:
    """The coherent monetary unit used by a standard annual balance sheet.

    A filing may retain convenience-translation or note facts in another unit.
    Count distinct balance anchors rather than arbitrary facts, preserve the
    established USD path whenever a real USD anchor exists, and refuse a tie:
    choosing between two equally represented currencies would be guessing.
    """
    filed, accn = filing
    counts: dict[str, set[str]] = {}
    for tag in anchors:
        for unit, entries in taxo.get(tag, {}).get("units", {}).items():
            if not _ISO_CURRENCY.fullmatch(unit):
                continue
            if any((entry.get("filed"), entry.get("accn", "")) == (filed, accn)
                   for entry in entries):
                counts.setdefault(unit, set()).add(tag)
    if "USD" in counts:
        return "USD"
    if not counts:
        return None
    strongest = max(len(tags) for tags in counts.values())
    winners = [unit for unit, tags in counts.items() if len(tags) == strongest]
    return winners[0] if len(winners) == 1 else None


def _has_usd_balance_in_filing(
    taxo: dict,
    filing: tuple[str, str],
    anchors: tuple[str, ...] = _FOREIGN_BALANCE_ANCHORS,
) -> bool:
    """Compatibility predicate retained for audits and focused tests."""
    return _balance_currency_in_filing(taxo, filing, anchors) == "USD"


def _has_any_balance_in_filing(facts: dict, filing: tuple[str, str]) -> bool:
    """Whether the annual accession carries a standard balance in any currency."""
    filed, accn = filing
    for namespace, anchors in (("us-gaap", _FOREIGN_BALANCE_ANCHORS),
                               ("ifrs-full", _IFRS_BALANCE_ANCHORS)):
        taxo = facts.get(namespace, {})
        for tag in anchors:
            for entries in taxo.get(tag, {}).get("units", {}).values():
                if any((e.get("filed"), e.get("accn", "")) == (filed, accn)
                       for e in entries):
                    return True
    return False


def _has_prior_supported_foreign_annual(
    facts: dict, newest: tuple[str, str],
) -> bool:
    prior: set[tuple[str, str]] = set()
    for namespace, anchors in (("us-gaap", _FOREIGN_BALANCE_ANCHORS),
                               ("ifrs-full", _IFRS_BALANCE_ANCHORS)):
        taxo = facts.get(namespace, {})
        prior.update(filing for filing in _filing_keys(taxo, ("20-F", "40-F"))
                     if filing < newest)
    previous = max(prior, default=None)
    if previous is None:
        return False
    return any(
        _balance_currency_in_filing(facts.get(namespace, {}), previous, anchors)
        is not None
        for namespace, anchors in (("us-gaap", _FOREIGN_BALANCE_ANCHORS),
                                   ("ifrs-full", _IFRS_BALANCE_ANCHORS))
    )


def _current_supported_foreign_annual(
    facts: dict,
) -> tuple[tuple[str, str] | None, str | None]:
    """Newest 20-F/40-F and its coherent standard statement namespace, if any."""
    all_annual: set[tuple[str, str]] = set()
    for taxo in facts.values():
        all_annual.update(_filing_keys(taxo, ("20-F", "40-F")))
    newest = max(all_annual, default=None)
    if newest is None:
        return None, None
    for namespace, anchors in (
        ("us-gaap", _FOREIGN_BALANCE_ANCHORS),
        ("ifrs-full", _IFRS_BALANCE_ANCHORS),
    ):
        taxo = facts.get(namespace, {})
        if (newest in _filing_keys(taxo, ("20-F", "40-F"))
                and _balance_currency_in_filing(taxo, newest, anchors) is not None):
            return newest, namespace
    return newest, None


def _current_statement_currency(facts: dict, statement_basis: str) -> str | None:
    """Reporting currency of the newest annual filing on the selected basis."""
    if statement_basis not in {"us-gaap", "ifrs-full"}:
        return None
    if not _foreign_forms_are_current(facts):
        return "USD"
    newest, basis = _current_supported_foreign_annual(facts)
    if newest is None or basis != statement_basis:
        return None
    anchors = (_FOREIGN_BALANCE_ANCHORS if basis == "us-gaap"
               else _IFRS_BALANCE_ANCHORS)
    return _balance_currency_in_filing(facts.get(basis, {}), newest, anchors)


def _foreign_forms_are_current(facts: dict) -> bool:
    latest = {"foreign": "", "domestic": ""}
    for taxo in facts.values():
        for tagdata in taxo.values():
            for entries in tagdata.get("units", {}).values():
                for entry in entries:
                    form = entry.get("form", "")
                    if form.startswith(("20-F", "40-F", "6-K")):
                        side = "foreign"
                    elif form.startswith(("10-K", "10-Q")):
                        side = "domestic"
                    else:
                        continue
                    if entry.get("filed", "") > latest[side]:
                        latest[side] = entry["filed"]
    return latest["foreign"] > latest["domestic"]


def _reject_foreign(
    facts: dict, receipt: dict | None = None, *,
    identity_filing: tuple[str, str] | None = None,
) -> str:
    """Return the statement taxonomy to normalize, refusing unsafe foreign bases."""
    if not _foreign_forms_are_current(facts):
        return "us-gaap"

    # Old US-GAAP history can remain after an IFRS transition (and vice versa).
    # The basis is determined by the newest foreign annual itself, never by which
    # namespace happens to have the longer history.
    newest, statement_basis = _current_supported_foreign_annual(facts)
    recently_filed = bool(newest and 0 <= (date.today() - date.fromisoformat(newest[0])).days <= 7)
    if (newest is not None and statement_basis is None
            and not _has_any_balance_in_filing(facts, newest)
            and (recently_filed or _has_prior_supported_foreign_annual(facts, newest))):
        # SEC's filing index can lead Company Facts by hours or days. One cover
        # share fact from the new accession is enough to make it the newest 20-F,
        # but not enough to conclude that the filing is non-USD or unsupported.
        raise PendingFilingFactsError(newest)
    if newest is None or statement_basis is None:
        raise UnsupportedFilerError(
            "filer's current foreign annual report does not carry a coherent standard "
            "US-GAAP or IFRS balance sheet in one identifiable ISO currency"
        )

    # The exact SEC cover class is the bridge between statement figures and the
    # traded ticker. Ordinary/common shares need no conversion. A receipt must
    # carry a positive, filing-backed underlying-shares-per-receipt ratio.
    title = (receipt or {}).get("title")
    if not title:
        raise UnsupportedFilerError(
            "foreign filer has no exact filing-cover security title for this ticker"
        )
    cover_filing = identity_filing or newest
    acceptable_cover_accessions = {cover_filing[1]}
    if identity_filing is not None:
        # While Company Facts is incomplete, the last complete filing's cover is
        # still valid evidence for the fallback snapshot. If the new cover has
        # already been read, it is also valid and puts history onto today's
        # security ratio. The pending warning names which case the reader sees.
        acceptable_cover_accessions.add(newest[1])
    receipt_accn = (receipt or {}).get("accn")
    basis_accn = (receipt or {}).get("basis_accn")
    cover_accession = basis_accn if basis_accn is not None else receipt_accn
    if (not cover_accession or cover_accession not in acceptable_cover_accessions
            or (basis_accn is not None and not receipt_accn)):
        raise UnsupportedFilerError(
            "foreign filer's exact security title is not from its current annual cover"
        )
    if not cover.is_common_equity_security(title):
        raise UnsupportedFilerError(
            "foreign ticker's exact filing-cover class is not supported common equity"
        )
    if cover.is_depositary_security(title):
        raw_ratio = (receipt or {}).get("ratio")
        try:
            ratio = Decimal(str(raw_ratio)) if raw_ratio is not None else None
        except Exception:
            ratio = None
        if ratio is None or ratio <= 0:
            raise UnsupportedFilerError(
                "foreign depositary security has no resolved positive cover-page ratio"
            )
    return statement_basis


def _entries(taxo: dict, tag: str, unit_pref: tuple[str, ...]) -> list[dict]:
    """Facts for a tag, in the unit the caller asked for.

    Never in some other unit. Enbridge, Canadian Pacific and Imperial Oil report
    earnings per share only in CAD/shares, and taking whatever unit happened to be
    first divided a New York price by a Canadian figure — a P/E understated by the
    exchange rate, with nothing on the page saying so. A figure in the wrong
    currency is not a weaker figure, it is a different quantity.
    """
    units = taxo.get(tag, {}).get("units", {})
    for unit in unit_pref:
        if unit in units:
            # A period cannot close after the filing that reports it. PennyMac's
            # long-term debt was dated 2030-09-30, AAGH's a zero dated 2031-12-31 —
            # the stale-zero trap reached through the date instead of the tag order
            # — and Mannatech's dividend 2108-11-14. Eighteen such facts won a
            # "latest instant" pick outright, because latest is exactly what they
            # claim to be.
            return [e for e in units[unit]
                    if not (e.get("filed") and e.get("end") and e["end"] > e["filed"])]
    # Currency-aware foreign statements deliberately retain their filed monetary
    # unit. They are already on one internally consistent reporting basis, so an
    # algorithm asking for the engine's traditional USD *kind* may consume the
    # selected reporting currency. The taxonomy marker is the hard gate: a random
    # CAD/CNY note in an otherwise USD filing can never enter this fallback.
    if not getattr(taxo, "currency_adapter", False):
        return []
    reporting = getattr(taxo, "reporting_currency", None)
    if not reporting or not _ISO_CURRENCY.fullmatch(reporting):
        return []
    for requested in unit_pref:
        if requested not in {"USD", "USD/shares"}:
            continue
        suffix = "/shares" if requested.endswith("/shares") else ""
        actual = f"{reporting}{suffix}"
        adapted = entries = units.get(actual, [])
        if getattr(taxo, "canonical_adapter", False):
            adapted = [entry for entry in entries if entry.get("_canonical_adapter")]
        if adapted:
            return [
                {**entry, "_source_unit": entry.get("_source_unit") or actual}
                for entry in adapted
                if not (entry.get("filed") and entry.get("end")
                        and entry["end"] > entry["filed"])
            ]
    return []


def _dec(v) -> Decimal:
    return Decimal(str(v))


def _days(e: dict) -> int:
    return (date.fromisoformat(e["end"]) - date.fromisoformat(e["start"])).days


def _fy_label(end: date) -> int:
    # Date-only fallback. A January end is ambiguous: Veeva calls 2025-01-31
    # FY2025, while many retailers call it FY2024. `_fiscal_calendar` uses the
    # annual filing's own FY declaration first; this convention applies only when
    # the filing supplies no usable anchor.
    return end.year if end.month > 1 else end.year - 1


class _FiscalTaxonomy(dict):
    """A statement taxonomy carrying its one company-wide fiscal calendar."""


def _fiscal_year(value) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2200 else None


_FISCAL_YEAR_OFFSET_BY_CIK = {
    # Build-A-Bear's inline-XBRL DocumentFiscalYearFocus uses the calendar end
    # year, but the audited report itself calls the 52 weeks ended 2026-01-31
    # fiscal 2025 (and the 52 weeks ended 2025-02-01 fiscal 2024).  That issuer
    # convention is not encoded in Company Facts and cannot be inferred from a
    # Saturday-nearest-January calendar: Best Buy, TJX and Signet use the same
    # shape but call their years by the end year.
    "0001113809": -1,
}

# Exact issuer/calendar boundaries whose Company Facts ``fy`` metadata conflicts
# with the fiscal years printed in the audited reports. Keep this accession-shape
# allowlist narrow: a generic transition heuristic also relabelled predecessor
# statements retained under merger CIKs.
_VERIFIED_FISCAL_REGIME_STARTS = {
    # Dycom changed from a July close to January. Its report calls the six months
    # ended 2018-01-27 the 2018 transition period and the next full year, ended
    # 2019-01-26, fiscal 2019; Company Facts incorrectly labels that full year 2018.
    ("0000067215", "2017-07-29", "2019-01-26"): 2019,
}

_VERIFIED_FILING_FISCAL_ENDS = {
    # RBC Bearings' restated FY2022 10-K/A says both on its cover and audited
    # statements that the fiscal year ended 2022-04-02. Its XBRL also contains
    # three duplicate revenue/gross-profit contexts ending 2022-04-30.
    ("0001324948", "0001213900-22-045106"): "2022-04-02",
}


def _build_fiscal_calendar(gaap: dict, cik: str | None = None) -> dict[str, int]:
    """Map every annual period end to the fiscal year the filer calls it.

    Company Facts repeats the current filing's ``fy`` on its comparative columns,
    so that field is authoritative only for the newest full-year period in each
    annual accession. Once those accession-level anchors are found across the
    whole taxonomy, adjacent comparative ends are placed relative to them. This
    single map prevents EPS, income and balance-sheet tags from choosing different
    years merely because SEC supplied a calendar frame for one tag but not another.
    """
    by_end: dict[str, dict] = {}
    filings: dict[tuple[str, str], dict] = {}
    frames: dict[str, tuple[str, int]] = {}
    verified_rejected_ends: set[tuple[str, str]] = set()
    statement_facts = [
        (tag, tagdata) for tag, tagdata in gaap.items()
        if tag in _FISCAL_CALENDAR_TAGS
    ]
    # A deliberately narrow synthetic/canonical adapter may expose none of the
    # standard anchors above.  Retain the old all-facts fallback only there; real
    # US-GAAP statements nearly always supply at least one anchor concept.
    support: dict[str, set[str]] = {}
    for tag, tagdata in statement_facts or gaap.items():
        for entries in (tagdata.get("units") or {}).values():
            for e in entries:
                if ("start" not in e or not _is_annual_form(e.get("form", ""))
                        or _days(e) not in _ANNUAL_DAYS or not e.get("end")):
                    continue
                if e.get("filed") and e["end"] > e["filed"]:
                    continue
                end = e["end"]
                support.setdefault(end, set()).add(tag)
                previous = by_end.get(end)
                if previous is None or e.get("filed", "") > previous.get("filed", ""):
                    by_end[end] = e
                filing = filings.setdefault(
                    (e.get("filed", ""), e.get("accn", "")), {"ends": {}, "fy": []})
                filing["ends"].setdefault(end, set()).add(tag)
                if (fy := _fiscal_year(e.get("fy"))) is not None:
                    filing["fy"].append(fy)
                match = re.fullmatch(r"CY(\d{4})", e.get("frame") or "")
                if match:
                    candidate = int(match.group(1))
                    if candidate == _fy_label(date.fromisoformat(end)):
                        held = frames.get(end)
                        if held is None or e.get("filed", "") > held[0]:
                            frames[end] = (e.get("filed", ""), candidate)
    if not by_end:
        return {}

    # An accession's latest duration is its own fiscal year; earlier durations in
    # that accession are comparative columns carrying the same filing-level `fy`.
    anchors: dict[str, tuple[str, int]] = {}
    for (filed, accession), filing in filings.items():
        if not filing["ends"] or not filing["fy"]:
            continue
        verified_end = _VERIFIED_FILING_FISCAL_ENDS.get(
            (str(cik).zfill(10) if cik is not None else "", accession))
        end = verified_end if verified_end in filing["ends"] else max(filing["ends"])
        if verified_end is not None:
            verified_rejected_ends.update(
                (candidate, accession)
                for candidate in filing["ends"]
                if candidate != verified_end
                and abs((date.fromisoformat(candidate)
                         - date.fromisoformat(verified_end)).days) <= 60
            )
        end_date = date.fromisoformat(end)
        votes = filing["fy"]
        fy = max(set(votes), key=votes.count)
        if fy not in (end_date.year, end_date.year - 1):
            continue
        if fy == end_date.year - 1 and end_date.month > 2:
            continue
        held = anchors.get(end)
        if held is None or filed > held[0]:
            anchors[end] = (filed, fy)

    # Remove only the known contradictory sibling context, and only when that
    # accession still supplies the selected fact at the date. A later independent
    # filing using the same date would remain valid evidence.
    rejected_ends = {
        end for end, accession in verified_rejected_ends
        if by_end.get(end, {}).get("accn") == accession
    }
    for end in rejected_ends:
        by_end.pop(end, None)
        support.pop(end, None)
        frames.pop(end, None)

    # The current annual filing declares the company's convention. Older SEC
    # metadata is not stable for January filers: Veeva's 2022 accession says
    # FY2021 while its later filings and published statements consistently name
    # years by the January in which they end; several retailers consistently do
    # the opposite. Mixing accession-local offsets recreates the missing/duplicate
    # years this calendar exists to prevent, so the newest credible anchor governs
    # the adjacent history and `_fy_labels` propagates from that one point.
    # A Saturday-nearest-December calendar periodically lands in the first week
    # of January.  SEC's CY frame is then a reliable local fiscal-year anchor,
    # not merely a calendar-overlap label: V.F. Corp's 2015-01-03 period is
    # FY2014, between FY2013 ended 2013-12-28 and FY2015 ended 2016-01-02.
    # Keeping only Jan 1-7 avoids the genuinely ambiguous January 31 convention
    # (Veeva calls it the end year; many retailers call it the preceding year).
    labels = {
        end: value[1]
        for end, value in frames.items()
        if (end_date := date.fromisoformat(end)).month == 1 and end_date.day <= 7
    }
    # A real fiscal-calendar change leaves an abnormal gap between consecutive
    # full-year statement ends. V.F. Corp moved from 2017-12-30 to 2019-03-30
    # (a short transition period lies between them); Red Cat moved from a
    # calendar-year predecessor to an April year end. A single newest anchor
    # cannot count years correctly across either boundary. Use the filing's own FY
    # on both sides, but only when at least two primary-statement concepts
    # corroborate each date so a lone annual note context cannot split a regime.
    statement_ends = sorted(
        end for end, tags in support.items() if len(tags) >= 2
    )
    for left, right in zip(statement_ends, statement_ends[1:]):
        gap = (date.fromisoformat(right) - date.fromisoformat(left)).days
        if gap in _ANNUAL_DAYS or gap > 600 or right not in anchors:
            continue
        if left in anchors and anchors[left][1] != anchors[right][1]:
            labels[left] = anchors[left][1]
            verified = _VERIFIED_FISCAL_REGIME_STARTS.get(
                (str(cik).zfill(10) if cik is not None else "", left, right))
            labels[right] = verified if verified is not None else anchors[right][1]
        elif 400 < gap <= 600:
            # A pre-IPO comparative can predate the registrant's first annual
            # accession and therefore have no filing-level FY of its own.
            # ServiceNow's June-2011 full year precedes a six-month transition
            # and the anchored December-2012 year. It is FY2011, not FY2010.
            labels[left] = anchors[right][1] - 1
            labels[right] = anchors[right][1]
    if anchors:
        latest_end = max(anchors)
        latest_date = date.fromisoformat(latest_end)
        latest_year = anchors[latest_end][1]
        # A 52/53-week year can move from late December to early January.  When
        # those two ends are one ordinary fiscal year apart but their calendar
        # numbers differ by two (2024-12-28 -> 2026-01-03), calling the latter
        # FY2026 invents a missing FY2025. Advance Auto Parts and Eastern Company
        # both exhibit this SEC metadata pattern. Ordinary January filers such as
        # MAMA/Veeva end in January every year, so the prior end's calendar year is
        # only one lower and their declared end-year convention remains intact.
        earlier = [date.fromisoformat(end) for end in by_end if end < latest_end]
        prior = max(earlier, default=None)
        if (latest_date.month <= 2 and latest_year == latest_date.year
                and prior is not None
                and 340 <= (latest_date - prior).days <= 400
                and latest_date.year - prior.year == 2):
            latest_year -= 1
        if cik is not None:
            latest_year += _FISCAL_YEAR_OFFSET_BY_CIK.get(str(cik).zfill(10), 0)
        labels[latest_end] = latest_year
    # Calendar frames are useful for pre-IPO comparative-only years, but only
    # where no filing-level anchor settles the company's convention.
    if not labels:
        labels = {end: value[1] for end, value in frames.items()}
    return _fy_labels(
        sorted(by_end), labels, by_end,
        {end: len(tags) for end, tags in support.items()},
    )


def _with_fiscal_calendar(gaap: dict, cik: str | None = None) -> _FiscalTaxonomy:
    if isinstance(gaap, _FiscalTaxonomy):
        if cik is not None:
            gaap.cik = str(cik).zfill(10)
        return gaap
    wrapped = _FiscalTaxonomy(gaap)
    wrapped.fiscal_labels = _build_fiscal_calendar(gaap, cik=cik)
    if cik is not None:
        wrapped.cik = str(cik).zfill(10)
    _copy_taxonomy_context(wrapped, gaap)
    return wrapped


def _copy_taxonomy_context(target: _FiscalTaxonomy, source: dict) -> None:
    """Keep private unit-selection policy on narrowed taxonomy views."""
    for name in ("canonical_adapter", "currency_adapter", "reporting_currency", "cik"):
        if hasattr(source, name):
            setattr(target, name, getattr(source, name))


def _fiscal_labels(gaap: dict) -> dict[str, int]:
    labels = getattr(gaap, "fiscal_labels", None)
    return labels if labels is not None else _build_fiscal_calendar(gaap)


def _provenance_tag(tag: str, e: dict, ns: str = "us-gaap") -> str:
    """The element the filer used, even when a normalized chain selected it."""
    return f"{e.get('_source_namespace', ns)}:{e.get('_source_tag', tag)}"


def _canonical_tag(tag_text: str) -> str:
    """The normalized rule tag behind a source-provenance element."""
    bare = tag_text.split(":", 1)[-1]
    return _IFRS_TO_CANONICAL.get(bare, bare)


def _fact(concept: str, tag: str, e: dict, fiscal_year: int | None = None, ns: str = "us-gaap") -> Fact:
    if isinstance(override := e.get("_fact_override"), Fact):
        return override
    return Fact(
        value=_dec(e["val"]),
        provenance=Provenance(
            concept=e.get("_source_concept", concept),
            tag=_provenance_tag(tag, e, ns),
            fiscal_year=fiscal_year,
            form=e.get("form", ""),
            accession=e.get("accn", ""),
            filed=date.fromisoformat(e["filed"]),
            period_end=date.fromisoformat(e["end"]),
            period_start=date.fromisoformat(e["start"]) if "start" in e else None,
            segments=e.get("segments", ""),
            unit=e.get("_source_unit"),
            document=e.get("_source_document"),
            canonical_tag=e.get("_normalized_tag", tag),
        ),
    )


_SPLIT_RATIOS = (1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50, 100)
_FY_CLUSTER_DAYS = 14                   # a 52/53-week year re-dated is still that year
_SPLIT_VOTE_FLOOR = Decimal("0.05")     # per-share values below this vote on rounding
_SPLIT_RUN_GAP = 275                    # one restatement cycle: three quarters
_SPLIT_RUN_SPAN = 550                   # ...and a run may not chain past two years
_SHARE_CORROBORATION = Decimal("0.10")  # how far the share count may sit from the factor


def _as_split(ratio: Decimal) -> Decimal | None:
    """The ratio rounded to a real split, or None if it is not one.

    An ordinary restatement moves a figure by a few percent; a split moves it by a
    round multiple. Requiring the ratio to land on one keeps error corrections from
    being rescaled as though shares had been divided.
    """
    for k in _SPLIT_RATIOS:
        for cand in (Decimal(str(k)), 1 / Decimal(str(k))):
            if abs(ratio / cand - 1) < Decimal("0.02"):
                return cand
    return None


def _by_period(entries: list[dict], tag: str = "",
               into: dict | None = None) -> dict[tuple[str, str, str], list[tuple[str, Decimal]]]:
    """Every reported value for each exact period, oldest filing first.

    Keyed on tag and whole period, never the end date alone. A three-month quarter and
    a nine-month year-to-date can close on the same day, and comparing those two reads
    their threefold difference in length as a 3:1 split; basic and diluted EPS for one
    period differ slightly and would read as a restatement.
    """
    out = into if into is not None else {}
    for e in entries:
        try:
            out.setdefault((tag, e["start"], e["end"]), []).append((e["filed"], _dec(e["val"])))
        except Exception:
            continue
    for vals in out.values():
        vals.sort()
    return out


def _per_share_periods(gaap: dict) -> dict:
    """Split evidence pooled across every per-share tag the filer uses.

    A split is a corporate action, not a property of one element: it rebases every
    per-share figure at once. Broadcom restated EarningsPerShareDiluted 10:1 but left
    IncomeLossFromContinuingOperationsPerDilutedShare — the series the engine actually
    reads — carrying pre-split years with no restatement to reveal them. Evidence from
    any tag settles the factor for all of them.
    """
    out: dict = {}
    for tag in EPS_TAGS + EPS_BASIC_TAGS + (EPS_CONTINUING_TAG,):
        _by_period([e for e in _entries(gaap, tag, ("USD/shares",))
                    if "start" in e and _is_financial_form(e.get("form", ""))],
                   tag=tag, into=out)
    return out


def _share_periods(gaap: dict) -> dict:
    """The weighted-average share counts, pooled the same way, as a second witness.

    A split rebases the share count by exactly the factor it rebases per-share
    figures, so the two must move together. Essential Utilities files another
    registrant's audited statements under its own CIK: the earnings per share differ
    by 2.5x, which lands on a split candidate, while the share counts differ by 1.41x,
    which proves the two figures describe different companies rather than one company
    before and after a corporate action.
    """
    out: dict = {}
    for tag in _WEIGHTED_SHARE_TAGS:
        _by_period([e for e in _entries(gaap, tag, ("shares",))
                    if "start" in e and _is_financial_form(e.get("form", ""))],
                   tag=tag, into=out)
    return out


def _split_factor(periods: dict[tuple[str, str], list[tuple[str, Decimal]]],
                  filed: str, shares: dict | None = None,
                  cik: str | None = None) -> Decimal:
    """How much per-share figures have been rebased since `filed`.

    A split does not change what a company earned, only how the earnings are sliced,
    so afterwards every prior year is restated onto the new share count. The same
    fiscal period therefore appears at two values in filings of different vintage, and
    the ratio between them IS the split factor — observed, not inferred. Chipotle's
    FY2022 is 32.04 in the filings up to 2024 and 0.64 in the 2025 one: 50.06, its 50:1.

    Measured per period as "value as of that date, against the newest value", rather
    than by accumulating split events: one split gets restated across several filings,
    and counting each restatement separately squared Booking Holdings' 25:1 into 625.
    Periods that never moved vote 1, so a quiet history yields no adjustment, and a
    period the filer restated for some other reason fails _as_split and abstains.
    """
    factor = Decimal(1)
    for split_filed, k in _split_events(periods, shares):
        if (str(cik).zfill(10), split_filed) in _VERIFIED_NON_SPLIT_RESTATEMENTS:
            continue
        if filed < split_filed:
            factor *= k
    return factor


def _split_events(periods: dict[tuple[str, str], list[tuple[str, Decimal]]],
                  shares: dict | None = None) -> list[tuple[str, Decimal]]:
    """[(filing date, factor)] for each distinct split, oldest first.

    A single split is restated into every filing that still carries a period spanning
    it, so the same 25:1 shows up again a quarter later under a different period. Left
    alone that compounds — Booking Holdings' anchor came out divided by 625. Runs of
    the same factor within three quarters are therefore one corporate action, while
    Texas Pacific Land's two genuine 3:1 splits, a year apart, stay separate.
    """
    votes: dict[str, list[Decimal]] = {}
    for vals in periods.values():
        for (_, old), (filed_new, new) in zip(vals, vals[1:]):
            if not old or not new or old == new:
                continue
            # A cent moving on a two-cent figure lands on 1.5 or 0.5 as readily as a
            # real split does. Idaho Copper restated -0.02 to -0.01 and had its whole
            # pre-2021 record rescaled by a third; Apple's 2010 revenue-recognition
            # restatement read as a 2:3. Rounding is not a corporate action.
            if min(abs(old), abs(new)) < _SPLIT_VOTE_FLOOR:
                continue
            k = _as_split(old / new)
            if k is not None:
                votes.setdefault(filed_new, []).append(k)
    events: list[tuple[str, Decimal]] = []
    # the newest filing already folded into the current run, and where the run began:
    # one measures "the same restatement cycle", the other bounds how far a single
    # corporate action may go on being restated
    run_last = run_start = None
    for f in sorted(votes):
        ks = votes[f]
        k = max(set(ks), key=ks.count)  # periods disagreeing on one filing: take majority
        if not _shares_agree(shares or {}, f, k):
            continue
        if (events and events[-1][1] == k and run_last is not None
                and (date.fromisoformat(f) - date.fromisoformat(run_last)).days <= _SPLIT_RUN_GAP
                and (date.fromisoformat(f) - date.fromisoformat(run_start)).days <= _SPLIT_RUN_SPAN):
            # Lam Research's 10:1 reaches Company Facts one comparative at a time over
            # 287 days. Measuring the gap from the run's FIRST filing books the last
            # restatement as a second 10:1, and a year no filing ever restated comes out
            # divided by 100.
            run_last = f
            continue
        events.append((f, k))
        run_start = run_last = f
    return events


def _shares_agree(shares: dict, filed: str, k: Decimal) -> bool:
    """Whether the share counts restated in this filing moved by the same factor.

    Silence is not dissent: a filer that reports its share count in only one vintage
    offers no second witness, and a genuine split must not be discarded for that. Only
    an observed count that moved by a materially different factor refutes the event.
    """
    observed = []
    for vals in shares.values():
        for (_, old), (filed_new, new) in zip(vals, vals[1:]):
            if filed_new == filed and old and new:
                observed.append(old / new)
    if not observed:
        return True
    # One filing restates several periods, and only the ones spanning the split move
    # by the factor — the rest drift a percent or two on buybacks. So the question is
    # whether ANY restated count moved by k, not what the typical one did: a median
    # over a dozen quiet periods vetoes every genuine split.
    # A 25:1 split divides earnings per share by 25 and multiplies the share count by
    # 25, so the two ratios are reciprocals: the corroborating count is the one whose
    # old/new lands on 1/k.
    return any(abs(ratio * k - 1) <= _SHARE_CORROBORATION for ratio in observed)


def _annual_series(gaap: dict, tag: str, unit: tuple[str, ...] = ("USD/shares",)) -> dict[int, Fact]:
    """Full-fiscal-year facts from annual filings only. Never a sum of quarters (§5.2)."""
    by_end: dict[str, dict] = {}
    frames: dict[str, int] = {}
    annual_entries = []
    for e in _entries(gaap, tag, unit):
        if "start" not in e or not _is_annual_form(e.get("form", "")):
            continue
        if _days(e) not in _ANNUAL_DAYS:
            continue
        annual_entries.append(e)
        # SEC's frame is calendar-aligned: it names the calendar year the period mostly
        # falls in, which equals the fiscal year only for a December filer. Dorian LPG
        # closes in March, so its year ending 2024-03-31 is framed CY2023 while the
        # company — and every other tag in the same filing — calls it fiscal 2024.
        # Taking the frame as a label there shifts one series a year against the rest,
        # and since frames appear on some tags and not others, the two desynchronise.
        # So a frame is honoured only where it agrees with the period's own end date;
        # where it disagrees it is describing calendar overlap, not a fiscal label, and
        # the filer's own fy field anchors the series instead.
        m = re.fullmatch(r"CY(\d{4})", e.get("frame") or "")
        if m and int(m.group(1)) == _fy_label(date.fromisoformat(e["end"])):
            frames[e["end"]] = int(m.group(1))
        prev = by_end.get(e["end"])
        if prev is None or e["filed"] > prev["filed"]:  # restatement rule: latest-filed wins
            by_end[e["end"]] = e
    if not by_end:
        return {}
    # Prefer the taxonomy-wide filing calendar. A company can retain a few periods
    # from an old fiscal-calendar regime that collide with other statement dates and
    # therefore cannot all enter the shared map. Those old dates do not invalidate
    # the current company calendar for the whole tag. MAMA's NetIncomeLoss carries
    # old December periods beside its January fiscal years; Value Line carries two
    # calendar-year contexts beside its April statements. Falling back to the local
    # calendar in either case shifted every otherwise settled statement row by one
    # fiscal year. Merge only unmatched dates whose locally inferred year remains
    # unclaimed. A conflicting unmatched date is the ambiguous fact and is dropped;
    # the company-wide dates corroborated by the rest of the taxonomy remain.
    shared = _fiscal_labels(gaap)
    local = _fy_labels(sorted(by_end), frames, by_end)
    mapped = {end: shared[end] for end in by_end if end in shared}
    # A year is settled company-wide even when this particular element has no fact
    # at the winning date.  Magna's post-spin FY2023 closes in September, while one
    # stale GrossProfit context covers calendar 2023.  That distant context must be
    # absent.  A nearby alternate end is different: 52/53-week filers frequently
    # give one statement row a date a few days either side of the dominant calendar.
    # Keep those within the same narrow fortnight used by `_fy_labels`; otherwise a
    # harmless presentation difference would erase valid history wholesale.
    shared_ends_by_year: dict[int, list[date]] = {}
    for shared_end, shared_year in shared.items():
        shared_ends_by_year.setdefault(shared_year, []).append(
            date.fromisoformat(shared_end))

    def available_unmatched(end: str) -> bool:
        year = local[end]
        occupied = shared_ends_by_year.get(year, [])
        end_date = date.fromisoformat(end)
        return not occupied or any(
            abs((end_date - shared_end).days) <= _FY_CLUSTER_DAYS
            for shared_end in occupied
        )

    unmatched = {
        end: local[end]
        for end in by_end
        if end not in shared and end in local and available_unmatched(end)
    }
    labels = {**unmatched, **mapped} if shared else local
    series: dict[int, Fact] = {}
    kept: dict[int, str] = {}
    for end in sorted(by_end):
        if end not in labels:
            continue
        fy = labels[end]
        # one fiscal year re-dated across filings: the restatement is the answer
        prev = kept.get(fy)
        if prev is not None and by_end[end]["filed"] <= by_end[prev]["filed"]:
            continue
        kept[fy] = end
        series[fy] = _fact(tag, tag, by_end[end], fiscal_year=fy)
    return series


def _fy_labels(ends: list[str], frames: dict[str, int], by_end: dict[str, dict],
               support: dict[str, int] | None = None) -> dict[str, int]:
    """Label each annual period end with its fiscal year.

    SEC's calendar frame labels comparative calendar years. The newest period's
    own ``fy`` is a second anchor when it is structurally credible: normally it
    equals the end year, while Jan/Feb retail years may use the prior year. A
    comparative's filing-year value (GOOGL FY2015 on a 2014 period) and a March
    filer's stale prior-year value (CRUS) are not anchors. Ends without an anchor
    are placed relative to the nearest one. An inferred label never displaces
    another period — a colliding old year is dropped rather than invented.
    """
    labels = dict(frames)
    last = ends[-1]
    end_d = date.fromisoformat(last)
    fy = by_end[last].get("fy")
    if (last not in labels
            and isinstance(fy, int)
            and (fy == end_d.year or (end_d.month <= 2 and fy == end_d.year - 1))):
        # DECK retained an old calendar-frame anchor after moving to a March
        # year-end. Its current 10-K says FY2026 and must realign every tag to
        # that common fiscal calendar.
        labels[last] = fy
    if not labels:
        labels[last] = _fy_label(end_d)
    anchors = sorted(labels)
    taken = set(labels.values())
    for end in ends:
        if end in labels:
            continue
        end_d = date.fromisoformat(end)
        anchor = min(anchors, key=lambda k: abs((date.fromisoformat(k) - end_d).days))
        guess = labels[anchor] + round((end_d - date.fromisoformat(anchor)).days / 365)
        # a fiscal year cannot be labelled beyond the calendar year it ends in,
        # nor more than one year before it
        guess = max(end_d.year - 1, min(end_d.year, guess))
        if guess in taken:
            # A 52/53-week filer re-dates the same fiscal year by a few days:
            # ATI's fiscal 2021 ends 2021-12-31 in one filing and 2022-01-02 in
            # the restated one. Dropping the second discards the restatement and
            # keeps a loss the company has since restated to a profit, which
            # inverts the latest-filed-wins rule. Two genuine annual periods
            # cannot both run 340-400 days and end a fortnight apart, so a
            # collision that close is one year reported twice.
            held = next((k for k, v in labels.items() if v == guess), None)
            if held is None:
                continue
            if abs((end_d - date.fromisoformat(held)).days) > _FY_CLUSTER_DAYS:
                # A malformed/note context can precede the audited statement end
                # by weeks or months (RUSHA 2022-12-12 vs 2022-12-31; CODI
                # 2016-12-13 vs 2016-12-31).  Do not let traversal order make the
                # older filing permanent.  Explicit anchors still win; otherwise
                # retain the fact from the latest filing, as every annual selector
                # does for duplicate evidence at one date.
                support = support or {}
                end_support = support.get(end, 0)
                held_support = support.get(held, 0)
                end_filed = by_end[end].get("filed", "")
                held_filed = by_end[held].get("filed", "")
                end_is_statement = end_support >= 2
                held_is_statement = held_support >= 2
                if (held in anchors or
                        (held_is_statement and not end_is_statement) or
                        (end_is_statement == held_is_statement and
                         (end_filed < held_filed or
                          (end_filed == held_filed and
                           end_support <= held_support)))):
                    continue
                del labels[held]
        labels[end] = guess
        taken.add(guess)
    return labels


_EPS_RECONCILES = Decimal("0.02")     # a rounded per-share figure lands this close
_EPS_CONTRADICTS = Decimal("0.05")    # beyond this the filing disagrees with itself


def _reconciled_annual_eps(gaap: dict, series: dict[int, Fact],
                           preferred: dict[int, Fact] | None = None,
                           has_nci: bool = False) -> dict[int, Fact]:
    """Replace an EPS that its own filing's arithmetic contradicts.

    Earnings per share is not an independent fact: the filing states the income and
    the weighted count it was struck from, and the three must agree. Where a later
    filing reports the same year again, the restatement rule takes the newer figure
    — right for a genuine restatement, wrong when the newer figure is a mis-tag,
    because a mis-tag arrives dressed as a restatement.

    The Eastern Company's fiscal 2020 is the case. Its own 10-K states $5,405,522 of
    net income on 6,264,521 weighted shares — $0.86 a share, which is what the 2021
    filing tagged. The 2022 filing tagged $1.76 against the same period while
    leaving the income and the count untouched, so the newer figure contradicts the
    numbers beside it rather than superseding them. Eleven filers do this, several
    by sign alone: Truett-Hurst tags +$0.64 for a year its own income says was
    -$0.64.

    Two things keep this narrow. The swap only ever happens WITHIN one concept, so a
    diluted figure is never quietly replaced by a basic or a continuing-operations
    one — that would change what the series measures, not correct it. And a
    continuing-operations figure is never tested at all: it is meant to differ from
    total net income, which is exactly why `_basis_conflict` abstains on it too.
    Eastern's own fiscal 2024 is the reason — $2.13 from continuing operations
    beside a total loss of $1.37 a share is not an error, it is a discontinued
    business. A real restatement moves the income with the per-share figure, so
    nothing here disturbs one.
    """
    # The same two abstentions `_basis_conflict` makes, and for the same reason:
    # earnings per share is struck on income available to the common, so a preferred
    # dividend or a minority interest puts a legitimate wedge between it and net
    # income. Markel's fiscal 2020 is $55.63 against a net income that divides to
    # $59.03, and the $47M gap is its preferred dividend, not a mis-tag.  A directly
    # reported same-filing common numerator removes both ambiguities, however.
    # Neonode is the concrete case: the parent has NCI, but its FY2020 statement
    # expressly reports a $5.638M common loss beside an incorrectly positive $0.56
    # loss-per-share fact.  That direct numerator can settle the sign without
    # guessing anything about the minority allocation.
    if not series:
        return series
    preferred = preferred or {}
    parent_income = _annual_facts_by_accession(
        gaap, NET_INCOME_TAGS, ("USD",), "Net income")
    common_income = _annual_facts_by_accession(
        gaap, COMMON_INCOME_TAGS, ("USD",), "Income available to common")
    diluted = _by_accession(gaap, ("WeightedAverageNumberOfDilutedSharesOutstanding",), ("shares",))
    basic = _by_accession(gaap, ("WeightedAverageNumberOfSharesOutstandingBasic",), ("shares",))
    out = dict(series)
    for year, chosen in series.items():
        tag = _tag_of(chosen)
        end = chosen.provenance.period_end
        if (end is None or "ContinuingOperations" in tag
                or not _eps_uses_total_income(chosen)):
            continue
        iso = end.isoformat()
        # the count that belongs to this concept: a diluted figure divides the
        # diluted count, and comparing it against the basic one invents a mismatch
        counts = basic if "Basic" in tag and "Diluted" not in tag else diluted
        other_counts = diluted if counts is basic else basic

        def count_for(accn: str) -> Decimal | None:
            # A combined basic-and-diluted EPS row may be accompanied by only
            # the basic denominator even though both values are identical.
            return counts.get((accn, iso)) or other_counts.get((accn, iso))

        def numerator(accn: str) -> Fact | None:
            direct = common_income.get((accn, iso))
            if direct is not None:
                return direct
            if has_nci or year in preferred:
                return None
            return parent_income.get((accn, iso))

        def implied(accn: str) -> Decimal | None:
            ni, sh = numerator(accn), count_for(accn)
            return ni.value / sh if ni is not None and sh else None

        chosen_target = implied(chosen.provenance.accession)
        same_magnitude = bool(
            chosen_target is not None
            and any(
                abs(abs(chosen.value) - abs(chosen_target * factor))
                <= max(
                    _EPS_CONTRADICTS * abs(chosen_target * factor),
                    Decimal("0.0055"),
                )
                for factor in (
                    Decimal("0.000001"), Decimal("0.001"), Decimal("1"),
                    Decimal("1000"), Decimal("1000000"),
                )
            )
        )
        chosen_numerator = numerator(chosen.provenance.accession)
        sign_key = (
            str(getattr(gaap, "cik", "")).zfill(10),
            chosen.provenance.accession,
            year,
        )
        if (sign_key in _VERIFIED_ANNUAL_EPS_SIGN_FLIPS
                and chosen_numerator is not None and chosen.value and same_magnitude
                and chosen_numerator.value * chosen.value < 0):
            p = chosen.provenance
            chosen = Fact(
                value=-chosen.value,
                provenance=Provenance(
                    concept=(f"{p.concept} (sign corrected: primary statement "
                             "presentation verified)"),
                    tag=p.tag, fiscal_year=p.fiscal_year, form=p.form,
                    accession=p.accession, filed=p.filed,
                    period_end=p.period_end, period_start=p.period_start,
                    components=(p, chosen_numerator.provenance),
                    segments=p.segments, unit=p.unit, document=p.document,
                    canonical_tag=p.canonical_tag,
                ),
            )
            out[year] = chosen

        target = implied(chosen.provenance.accession)
        if target is None or abs(target) < Decimal("0.01"):
            continue
        if abs(chosen.value - target) <= _EPS_CONTRADICTS * abs(target):
            continue                                    # the filing agrees with itself
        for e in _entries(gaap, tag, ("USD/shares",)):
            if e.get("end") != iso or "start" not in e or not _is_annual_form(e.get("form", "")):
                continue
            # Only where the earlier filing states the SAME income on the SAME count:
            # that is what a mis-tag looks like, a per-share figure moving while the
            # two numbers it is made of stand still. A genuine restatement moves them
            # together — Gyre's fiscal 2022 went from -$8.2M on 31.5M shares to
            # +$4.3M on 75.7M after a reverse merger — and must be left alone.
            earlier_numerator = numerator(e["accn"])
            chosen_filing_numerator = numerator(chosen.provenance.accession)
            if ((earlier_numerator is None or chosen_filing_numerator is None
                 or earlier_numerator.value != chosen_filing_numerator.value)
                    or count_for(e["accn"]) != count_for(chosen.provenance.accession)):
                continue
            other = implied(e["accn"])
            if other is None:
                continue
            if abs(_dec(e["val"]) - other) <= _EPS_RECONCILES * abs(other):
                out[year] = _fact(chosen.provenance.concept, tag, e)
                break
    return out


def _by_accession(gaap: dict, tags: tuple[str, ...], unit: tuple[str, ...]) -> dict[tuple, Decimal]:
    """One full-year value per (filing, period end), so a figure can be checked
    against the others the same document states rather than against a series that
    has already collapsed several filings into one."""
    out: dict[tuple, Decimal] = {}
    for tag in tags:
        for e in _entries(gaap, tag, unit):
            if "start" not in e or not _is_annual_form(e.get("form", "")):
                continue
            if _days(e) not in _ANNUAL_DAYS:
                continue
            out.setdefault((e["accn"], e["end"]), _dec(e["val"]))
    return out


def _annual_facts_by_accession(
    gaap: dict, tags: tuple[str, ...], unit: tuple[str, ...], concept: str,
) -> dict[tuple[str, str], Fact]:
    """One filed annual fact per accession/end, without collapsing restatements.

    Annual series deliberately select the latest comparative and EPS is then put
    on today's split basis. Neither transformation belongs in an identity that
    asks what one particular filing meant by a displayed share count. Keeping
    the raw filing contexts lets ``_annual_share_counts`` compare the three cells
    that appeared together even when another accession later wins the history.
    """
    labels = _fiscal_labels(gaap)
    out: dict[tuple[str, str], Fact] = {}
    for tag in tags:
        for entry in _entries(gaap, tag, unit):
            if ("start" not in entry
                    or not _is_annual_form(entry.get("form", ""))
                    or _days(entry) not in _ANNUAL_DAYS):
                continue
            key = (entry["accn"], entry["end"])
            out.setdefault(
                key, _fact(concept, tag, entry, labels.get(entry["end"])))
    return out


def _annual_eps(gaap: dict) -> dict[int, Fact]:
    continuing = _annual_series(gaap, EPS_CONTINUING_TAG)
    candidates = [s for tag in EPS_TAGS if (s := _annual_series(gaap, tag))]
    if continuing:
        candidates.append(continuing)
    if not candidates:
        candidates = [s for tag in EPS_BASIC_TAGS if (s := _annual_series(gaap, tag))]
        if not candidates:
            return {}
    # Filers switch EPS tags mid-history (FCX moved to continuing-ops-only in 2022),
    # so recency dominates: a long-dead series must never beat a current one.
    # At equal currency, §5.3 prefers continuing operations; then deeper history.
    named = [(f"tag{i}", series) for i, series in enumerate(candidates)]
    continuing_key = next((k for k, series in named if series is continuing), None)
    _, best = _best_series(named, {k: 0 for k, _ in named},
                           prefer=lambda tag: tag == continuing_key)
    # reconciled after the gaps are filled — a year that enters the series from a
    # second tag needs checking too, and Eastern's fiscal 2020 is exactly that —
    # and before the split adjustment, since the check compares filed figures
    # against the filed income and count they were struck from.
    # Preferred dividends and minority interests are read here rather than passed in:
    # this runs before either is settled elsewhere, and both are only needed to know
    # when to abstain.
    series = _reconciled_annual_eps(
        gaap, _fill_missing_years(best, gaap),
        preferred=_annual_union(gaap, PREFERRED_DIVIDEND_TAGS),
        has_nci=bool(_latest_instant(gaap, "nci", ("MinorityInterest",))))
    series, _ = _reconcile_duration_scale(
        gaap, series,
        frozenset(EPS_TAGS + EPS_BASIC_TAGS + (EPS_CONTINUING_TAG,)),
        unit=("USD/shares",),
    )
    cik = str(getattr(gaap, "cik", "")).zfill(10)
    for year, fact in tuple(series.items()):
        factor = _VERIFIED_ANNUAL_EPS_PRESENTATION_SCALES.get(
            (cik, fact.provenance.accession, year))
        if factor is None:
            continue
        p = fact.provenance
        series[year] = Fact(
            value=fact.value * factor,
            provenance=Provenance(
                concept=(f"{p.concept} (scaled {factor:g}x: primary statement "
                         "per-share presentation verified)"),
                tag=p.tag, fiscal_year=p.fiscal_year, form=p.form,
                accession=p.accession, filed=p.filed, period_end=p.period_end,
                period_start=p.period_start, components=(p,), segments=p.segments,
                unit=p.unit, document=p.document, canonical_tag=p.canonical_tag,
            ),
        )
    series = {
        year: fact for year, fact in series.items()
        if (cik, fact.provenance.accession, year)
        not in _VERIFIED_ANNUAL_EPS_WITHHOLD
    }
    series = _split_adjust(series, gaap)
    # A per-share element holding a dollar total is not a small error to carry: it is
    # unusable, and leaving it in reads as a company earning millions per share.
    return {y: f for y, f in series.items() if abs(f.value) <= _IMPLAUSIBLE_EPS}


def _split_adjust(series: dict[int, Fact], gaap: dict) -> dict[int, Fact]:
    """Put every year on the share count the company has today.

    A 10-K reports its prior years as filed, and a split only restates the two or
    three comparatives the newest filing carries — so anything older keeps a per-share
    figure struck on a share count that no longer exists. Criterion 6 compares two
    endpoints years apart and lands squarely on that discontinuity: Chipotle's FY2020
    is 12.52 as filed and 0.2504 in today's shares, which is the difference between
    recording a large pass and recording a fail.
    """
    periods = _per_share_periods(gaap)
    shares = _share_periods(gaap)
    if not periods:
        return series
    cik = str(getattr(gaap, "cik", "")).zfill(10)
    out: dict[int, Fact] = {}
    for fy, fact in series.items():
        already_restated = (
            cik, fact.provenance.accession
        ) in _VERIFIED_ALREADY_SPLIT_ADJUSTED_ACCESSIONS
        factor = (Decimal(1) if already_restated else
                  _split_factor(periods, fact.provenance.filed.isoformat(), shares,
                                cik=cik))
        out[fy] = fact if factor == 1 else Fact(value=fact.value / factor,
                                                provenance=fact.provenance)
    return out


def _fill_missing_years(series: dict[int, Fact], gaap: dict) -> dict[int, Fact]:
    """Fill years the diluted chain lacks from other EPS tags — basic last.
    A filer tagging only basic in its newest 10-K (Lennar FY2025) would otherwise
    freeze the series a year back, staling criteria 4/6 and the TTM anchor."""
    for tag in EPS_TAGS + (EPS_CONTINUING_TAG,) + EPS_BASIC_TAGS:
        other = _annual_series(gaap, tag)
        for fy, fact in other.items():
            basic = tag in EPS_BASIC_TAGS
            held = series.get(fy)
            # A current annual filing sometimes stops tagging diluted EPS and
            # supplies only basic EPS, including newly restated comparative
            # columns. Flash Sports' post-reverse-split 10-K is the concrete
            # case: its FY2024 basic comparative is $-73.12, while the obsolete
            # diluted chain still held $-2.62 from the pre-split report. Prefer
            # the newer primary-report observation only when that accession has
            # no annual diluted/combined fact for the same period; otherwise the
            # normal diluted preference remains intact.
            cik = str(getattr(gaap, "cik", "")).zfill(10)
            newer_basic_only = bool(
                basic and held is not None
                and (cik, fact.provenance.accession, fy)
                    in _VERIFIED_NEWER_BASIC_EPS_COMPARATIVES
                and fact.provenance.filed > held.provenance.filed
                and "ContinuingOperations" not in _tag_of(held)
                and not any(
                    e.get("accn") == fact.provenance.accession
                    and e.get("end") == fact.provenance.period_end.isoformat()
                    and "start" in e
                    and _is_annual_form(e.get("form", ""))
                    and _days(e) in _ANNUAL_DAYS
                    for diluted_tag in EPS_TAGS
                    for e in _entries(gaap, diluted_tag, ("USD/shares",))
                )
            )
            if held is not None and not newer_basic_only:
                continue
            p = fact.provenance
            series[fy] = Fact(
                value=fact.value,
                provenance=Provenance(
                    concept=f"{p.concept} (basic — diluted not tagged)" if basic else p.concept,
                    tag=p.tag, fiscal_year=p.fiscal_year, form=p.form,
                    accession=p.accession, filed=p.filed, period_end=p.period_end,
                    period_start=p.period_start, segments=p.segments, unit=p.unit,
                    document=p.document, canonical_tag=p.canonical_tag,
                ),
            )
    return dict(sorted(series.items()))


def _best_series(candidates: list[tuple[str, dict]], order: dict[str, int],
                 prefer=lambda tag: False) -> tuple[str, dict]:
    """Which of several elements is THE series for a concept.

    One rule, in one place, because it was written three times with the three keys
    in three different orders and two of them were wrong. It ranks:

      1. recency — a series that stopped years ago cannot answer for today, whatever
         it is called. This is what lets a filer abandon an element mid-history.
      2. meaning — what the element IS. These chains are ordered by scope, and
         `ProfitLoss` is the whole group's profit where `NetIncomeLoss` is the
         parent's; `Revenues` is a total where a contract element is part of one.
      3. depth — only then. Starwood files sixteen years of the group's profit
         beside fourteen of its own, and ranking depth second handed two extra years
         of history the power to swap one concept for the other, so every margin and
         per-share figure divided profit the shareholders do not own.

    `prefer` marks a tag that outranks the tag order itself — §5.3's preference for
    continuing operations, which is a statement about scope rather than about which
    element a filer happens to use.
    """
    return max(candidates, key=lambda c: (max(c[1]), prefer(c[0]), -order[c[0]], len(c[1])))


def _annual_dollar_series(gaap: dict, tags: tuple[str, ...]) -> dict[int, Fact]:
    """Filers switch elements mid-history the same way they switch EPS elements —
    Advanced Energy's NetIncomeLoss series stops in 2024 while ProfitLoss runs on.
    Taking the first tag that returns anything would freeze the series a year back,
    so candidates are ranked by recency first.

    Then by tag preference, and only then by depth. The order matters: these tags
    differ in SCOPE, not merely in coverage. Starwood Property Trust files fourteen
    years of `NetIncomeLoss` — the parent's own $411.5M, which is what its printed
    income statement calls "Net income attributable to Starwood Property Trust" —
    beside sixteen years of `ProfitLoss`, the group's $443.1M including the minority
    holders. Ranking depth first handed two extra years of history the power to
    swap one for the other, and every margin and per-share figure then divided
    profit the shareholders do not own. Recency still settles the abandoned-series
    case, because a dead tag cannot win it."""
    candidates = [
        (tag, series)
        for tag in tags
        if (series := _annual_series(gaap, tag, unit=("USD",)))
    ]
    if not candidates:
        return {}
    order = {tag: i for i, tag in enumerate(tags)}
    _, best = _best_series(candidates, order)
    for _, other in candidates:                      # fill gaps the winner lacks
        for fy, fact in other.items():
            best.setdefault(fy, fact)
    return dict(sorted(best.items()))


def _annual_net_income(gaap: dict) -> dict[int, Fact]:
    """The parent's own profit each year.

    Not rebuilt from the group's figure minus the minority's, though the identity
    is tempting: Vivid Seats tags fiscal 2025 net income of +$806.1M beside a group
    loss of $721.5M, and its printed statement says -$292.2M. But the minority's
    own line is not reliably on file — TKO tags only the redeemable half of its
    noncontrolling interests without a dimension, so the same subtraction turns its
    correct $195.4M into $540.1M. One filer's bad tag is not worth another's good
    one, so the mis-tagged year stands and the audit reports it.
    """
    series = _apply_verified_annual_duration_fact_scales(
        gaap, _annual_dollar_series(gaap, NET_INCOME_TAGS))
    return _reconcile_duration_scale(gaap, series, frozenset(NET_INCOME_TAGS))[0]


def _apply_verified_annual_duration_fact_scales(
    gaap: dict, series: dict[int, Fact],
) -> dict[int, Fact]:
    """Apply a primary-statement correction to one exact monetary fact.

    Old annual tables commonly state dollars in thousands while an amended or
    comparative Inline-XBRL cell retains only the displayed digits.  The reverse
    also occurs, and a few amendments invert the sign as well.  Company size and
    neighbouring years are not authority to change a reported number, so this
    path is deliberately keyed to the reviewed CIK, accession, fiscal year and
    canonical tag.  The original provenance remains a component of the corrected
    fact so the UI can disclose exactly what happened.
    """
    cik = str(getattr(gaap, "cik", "")).zfill(10)
    out: dict[int, Fact] = {}
    for year, fact in series.items():
        factor = _VERIFIED_ANNUAL_DURATION_FACT_SCALES.get(
            (cik, fact.provenance.accession, year, _tag_of(fact)))
        if factor is None:
            out[year] = fact
            continue
        p = fact.provenance
        correction = (
            f"scaled {factor:g}x" if factor > 0 else
            f"scaled {abs(factor):g}x and sign-corrected"
        )
        out[year] = Fact(
            value=fact.value * factor,
            provenance=Provenance(
                concept=(f"{p.concept} ({correction}: primary statement "
                         "monetary presentation verified)"),
                tag=p.tag, fiscal_year=p.fiscal_year, form=p.form,
                accession=p.accession, filed=p.filed, period_end=p.period_end,
                period_start=p.period_start, components=(p,), segments=p.segments,
                unit=p.unit, document=p.document, canonical_tag=p.canonical_tag,
            ),
        )
    return dict(sorted(out.items()))


# staying on one element tolerates real business swings (Carnival's COVID
# collapse and 6x recovery are one tag's honest numbers); moving to a different
# element demands tight agreement, because elements differ in SCOPE and a large
# step at a boundary is a scope mismatch, not history
_SAME_TAG_RATIO = (Decimal(1) / 10, Decimal(10))
_SWITCH_RATIO = (Decimal(1) / 3, Decimal(3))


# `Revenues` is the umbrella total and `RevenueFromContractWithCustomer...` is the
# part of it that came from contracts with customers. Only these two stand in that
# relation — the assessed-tax pair does not, and taking the larger of THOSE would
# count sales taxes collected for the state as the company's revenue.
_TOTAL_AND_PART = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")


# Revenue net of interest expense IS the top line for a filer that lends or makes
# markets — the figure its own income statement totals to. Gross interest income is
# inside it and is larger, so no comparison by size can rank the two; the concepts
# do it instead.
_NET_OF_INTEREST = "RevenuesNetOfInterestExpense"
_INSIDE_NET_OF_INTEREST = ("InterestIncomeOperating", "InterestAndDividendIncomeOperating",
                           "InterestIncomeExpenseNet")

# DTE files the whole utility group under the first tag and its regulated
# operation under the second. The narrower series is longer, so the ordinary
# depth tie-break would otherwise put the component above its own total.
_REGULATED_TOTAL = "RegulatedAndUnregulatedOperatingRevenue"
_REGULATED_PART = "RegulatedOperatingRevenue"


# Sales tax collected for the state is at most a tenth or so of a sale, so the two
# assessed-tax elements describe one quantity twice and cannot be far apart.
_ASSESSED_TAX_MAX = Decimal("1.25")


def _without_incoherent_assessed_tax(candidates):
    """Drop the including-tax element where it cannot be the excluding one plus tax.

    Thirty filers tag a pair that no rate of sales tax explains — Precision Optics
    at 2.2x, Lifestance at 462x, SS Innovations at exactly 1000x, which is a units
    error wearing a revenue tag. The two elements are then measuring different
    things and the wider one is not revenue.

    Dropping it matters beyond the choice itself: the sub-scope guard below anchors
    on the LARGEST candidate, so one inflated element pushes every honest one under
    the threshold and out of contention. Precision Optics' own `Revenues` of $24.0M
    — the "Net sales" its income statement prints — was being discarded as a scrap
    beside a $53.5M figure the company never earned.
    """
    kept = dict(candidates)
    including = kept.get("RevenueFromContractWithCustomerIncludingAssessedTax")
    excluding = kept.get("RevenueFromContractWithCustomerExcludingAssessedTax")
    if including and excluding:
        shared = set(including) & set(excluding)
        if shared:
            year = max(shared)
            base = excluding[year].value
            if base > 0 and including[year].value / base > _ASSESSED_TAX_MAX:
                return [(t, s) for t, s in candidates
                        if t != "RevenueFromContractWithCustomerIncludingAssessedTax"]
    return candidates


def _wider_top_line(pool, tag: str, series: dict[int, Fact]):
    """Between a total and its own part, the total is the larger of the two.

    Ovintiv files $8,663M of contract revenue beside $8,908M of `Revenues`, and its
    income statement prints the second as "Total Revenues" — the difference is
    revenue that did not come from a contract with a customer. Ares files the
    reverse, $5,601M of contract revenue against a $4,756M `Revenues` that covers
    less than its own statement's "Total revenues". Ranking by tag order picked the
    smaller in both cases, each for a different reason.

    Whichever element holds the bigger figure for the shared year is the total; the
    other is a piece of it. Nothing else in the chain is compared this way, so a
    sector top line that is legitimately larger than a mis-scoped `Revenues` — the
    reason the order exists — is untouched.

    Not universal, and known not to be. Benchmark Electronics tags $2,748.7M of
    contract revenue against $2,659.1M of `Revenues`, and its printed statement
    closes on the smaller: sales of 2,659.1 less cost of sales of 2,389.0 is the
    gross profit of 270.1 it reports. Taking the larger is right for Ovintiv and for
    Ares and wrong for Benchmark, and no rule reading only these two tags can tell
    the three apart — the income statement's own arithmetic settles it and is not in
    the data. The rule follows the majority and `make audit-filings` names the
    exceptions.
    """
    if tag in _INSIDE_NET_OF_INTEREST:
        # Interactive Brokers' interest income is $7,782M against $6,205M of revenue
        # net of interest expense, and the panel showed the gross component as the
        # company's sales. The statement totals to the second.
        whole = next((s for t, s in pool if t == _NET_OF_INTEREST), None)
        if whole is not None and max(whole) >= max(series):
            return _NET_OF_INTEREST, whole
    if tag == _REGULATED_PART:
        whole = next((s for t, s in pool if t == _REGULATED_TOTAL), None)
        if whole is not None and max(whole) >= max(series):
            return _REGULATED_TOTAL, whole
    if tag not in _TOTAL_AND_PART:
        return tag, series
    other = next((s for t, s in pool if t in _TOTAL_AND_PART and t != tag), None)
    if other is None:
        return tag, series
    year = max(series)
    if max(other) != year or other[year].value <= series[year].value:
        return tag, series
    return next(t for t in _TOTAL_AND_PART if t != tag), other


def _annual_revenue(gaap: dict) -> dict[int, Fact]:
    """The revenue series is STITCHED, not merged: revenue elements carry wildly
    different scopes (ConAgra's umbrella `Revenues` holds a $1.6B sub-item beside
    $13B of true sales; Westlake's own `Revenues` changes meaning mid-history), so
    every year must prove continuity with the year it joins. The walk starts from
    the top line's latest year and extends one year at a time; a year no element
    can prove is where the series honestly ends."""
    candidates = []
    for tag in REVENUE_TAGS:
        series = _annual_series(gaap, tag, unit=("USD",))
        if series:
            series, _ = _reconcile_duration_scale(
                gaap, series, frozenset((tag,)))
            candidates.append((tag, series))
    candidates = _without_incoherent_assessed_tax(candidates)
    if not candidates:
        return {}
    order = {tag: i for i, tag in enumerate(REVENUE_TAGS)}
    # top-line pick: among elements current within a year of the freshest, a series
    # whose latest value is under half the biggest is a sub-scope scrap, not revenue
    latest_fy = max(max(s) for _, s in candidates)
    pool = [(t, s) for t, s in candidates if max(s) >= latest_fy - 1]
    peak = max((float(s[max(s)].value) for _, s in pool if s[max(s)].value > 0), default=0)
    strong = [(t, s) for t, s in pool if float(s[max(s)].value) >= peak / 2] or pool
    # NOT `_best_series`: revenue ranks depth above tag order, and alone among the
    # three chains it is right to. REVENUE_TAGS is not ordered by scope but by
    # generality — the sector top lines a bank, a REIT or a utility needs come after
    # the generic elements, so preferring an earlier tag prefers a generic scrap over
    # the line that is actually the company's revenue. Duke Energy's own statement
    # totals to the $32,237M this ordering finds; ranking by tag order gave $31,741M.
    # The scope guard here is `peak / 2` above, not the order.
    tag0, s0 = max(strong, key=lambda c: (max(c[1]), len(c[1]), -order[c[0]]))
    tag0, s0 = _wider_top_line(strong, tag0, s0)
    start = max(s0)
    chosen: dict[int, Fact] = {start: s0[start]}
    by_tag = dict(candidates)

    def _fits(v, ref, span) -> bool:
        return v > 0 and ref > 0 and span[0] <= v / ref <= span[1]

    for step in (-1, 1):                    # backward through history, then forward
        cur, ref_fy = tag0, start
        y = start + step
        floor, ceil = min(min(s) for _, s in candidates), max(max(s) for _, s in candidates)
        while floor <= y <= ceil:
            ref = chosen[ref_fy].value
            same = by_tag[cur].get(y)
            if same is not None and abs(ref_fy - y) <= 3 \
                    and _fits(same.value, ref, _SAME_TAG_RATIO):
                chosen[y] = same
            else:
                for tag, s in candidates:
                    f = s.get(y)
                    if tag != cur and f is not None and abs(ref_fy - y) <= 2 \
                            and _fits(f.value, ref, _SWITCH_RATIO):
                        chosen[y] = f
                        cur = tag
                        break
                else:
                    break                    # nothing can prove this year: stop
            ref_fy = y
            y += step
    cik = str(getattr(gaap, "cik", "")).zfill(10)
    return dict(sorted(
        (year, fact) for year, fact in chosen.items()
        if (cik, fact.provenance.accession, year)
        not in _VERIFIED_ANNUAL_REVENUE_WITHHOLD
    ))


_TINY_GROSS_PROFIT_DOLLARS = Decimal("1000")
_MATERIAL_GROSS_PROFIT_INPUT = Decimal("1000000")


def _annual_gross_profit(gaap: dict) -> dict[int, Fact]:
    """Reported gross profit, withholding a tiny fact the statement disproves.

    Dolphin Entertainment's FY2022 Company Facts row contains a standard
    ``GrossProfit`` fact of three dollars even though its published statement has
    no gross-profit subtotal.  The same filing reports $40.506m of revenue and
    $3.566m of cost of revenue, so the hidden three-dollar fact cannot satisfy the
    gross-profit identity.  It is neither safe to show nor safe to scale.

    The guard is intentionally limited to sub-$1,000 facts or a value repeated
    exactly across at least three fiscal years, beside million-dollar
    same-accession inputs.  The latter catches Dolphin's second malformed shape:
    a hard-coded $3m ``GrossProfit`` repeated while revenue changes every year.
    Genuine small gross results survive when the filed revenue-minus-cost
    identity supports them: Uranium Energy's $37,000 FY2024 profit and Rubicon's
    $40,000 FY2012 gross loss are examples.
    """
    gross = _apply_verified_annual_duration_fact_scales(
        gaap, _annual_union(gaap, GROSS_PROFIT_TAGS))
    revenue_by_source = _by_accession(gaap, REVENUE_TAGS, ("USD",))
    costs_by_source = _by_accession(gaap, COST_OF_REVENUE_TAGS, ("USD",))
    out = dict(gross)
    value_counts: dict[Decimal, int] = {}
    for fact in gross.values():
        value_counts[fact.value] = value_counts.get(fact.value, 0) + 1
    for year, subtotal in gross.items():
        end = subtotal.provenance.period_end
        if end is None:
            continue
        key = (subtotal.provenance.accession, end.isoformat())
        top_line = revenue_by_source.get(key)
        cost = costs_by_source.get(key)
        if top_line is None or cost is None:
            continue
        scale = max(abs(top_line), abs(cost))
        suspicious_shape = (
            abs(subtotal.value) < _TINY_GROSS_PROFIT_DOLLARS
            or value_counts[subtotal.value] >= 3
        )
        if (suspicious_shape
                and scale >= _MATERIAL_GROSS_PROFIT_INPUT
                and top_line - cost != subtotal.value):
            del out[year]
    return _reconcile_duration_scale(
        gaap, dict(sorted(out.items())), frozenset(GROSS_PROFIT_TAGS))[0]


def _annual_operating_income(gaap: dict) -> dict[int, Fact]:
    """Reported operating income, or an exactly reconciled statement subtotal.

    NIKE does not tag an operating-income subtotal, but it files gross profit,
    total selling and administrative expense, net interest income, other
    nonoperating income, and pretax income on the same statement. Gross profit
    less operating expense is accepted only when those independently filed
    nonoperating lines reconcile it exactly to pretax income. Merely finding the
    two subtraction inputs is not enough.
    """
    out = _apply_verified_annual_duration_fact_scales(
        gaap, _annual_dollar_series(gaap, OPERATING_INCOME_TAGS))
    gross = _annual_gross_profit(gaap)
    expenses = _annual_union(gaap, OPERATING_EXPENSE_TAGS)
    pretax = (_annual_union(gaap, PRETAX_TAGS)
              or _annual_union(gaap, PRETAX_INCOME_TAGS))
    aggregate_nonoperating = _annual_union(gaap, NONOPERATING_INCOME_TAGS)
    net_interest = _annual_union(gaap, NET_INTEREST_INCOME_TAGS)
    other_nonoperating = _annual_union(gaap, OTHER_NONOPERATING_INCOME_TAGS)
    separate_interest_income = _annual_union(
        gaap, SEPARATE_NONOPERATING_INTEREST_INCOME_TAGS)
    separate_interest_expense = _annual_union(
        gaap, SEPARATE_NONOPERATING_INTEREST_EXPENSE_TAGS)
    selling_general_admin = _annual_union(
        gaap, ("SellingGeneralAndAdministrativeExpense",))
    research_development = _annual_union(
        gaap, SEPARATELY_PRESENTED_RESEARCH_EXPENSE_TAGS)
    restructuring = _annual_union(gaap, ("RestructuringCharges",))

    for year in sorted(set(gross) & set(expenses) & set(pretax)):
        nonoperating = ([aggregate_nonoperating[year]]
                        if year in aggregate_nonoperating
                        else ([net_interest[year], other_nonoperating[year]]
                              if year in net_interest and year in other_nonoperating
                              else []))
        if not nonoperating:
            continue
        sources = (gross[year], expenses[year], pretax[year], *nonoperating)
        periods = {
            (fact.provenance.period_start, fact.provenance.period_end,
             fact.provenance.accession)
            for fact in sources
        }
        if len(periods) != 1:
            continue
        value = gross[year].value - expenses[year].value
        if value + sum((fact.value for fact in nonoperating), Decimal(0)) \
                != pretax[year].value:
            continue
        direct = out.get(year)
        scale = None
        if direct is not None:
            direct_period = (
                direct.provenance.period_start, direct.provenance.period_end,
                direct.provenance.accession,
            )
            if direct_period not in periods:
                continue
            if direct.value == value:
                continue
            if direct.value and value and direct.value * value > 0:
                ratio = max(abs(direct.value), abs(value)) / min(
                    abs(direct.value), abs(value))
                if ratio in (Decimal("1000"), Decimal("1000000")):
                    scale = ratio
            if scale is None:
                continue
        expression = f"{gross[year].provenance.tag} - {expenses[year].provenance.tag}"
        reconciliation = " + ".join(fact.provenance.tag for fact in nonoperating)
        if direct is not None:
            sources = (*sources, direct)
        out[year] = _derived_flow(
            ("Operating income (derived and reconciled to pretax income; "
             f"direct comparative had an exact {scale:g}x presentation-scale "
             "contradiction)" if scale is not None else
             "Operating income (derived and reconciled to pretax income)"),
            (f"{expression}; reconciled with {reconciliation} to "
             f"{pretax[year].provenance.tag}"),
            value, sources, year,
        )

    # A second industrial shape has no operating subtotal and no aggregate
    # OperatingExpenses line.  J&J, for example, prints SG&A, R&D and a small
    # issuer-extension IPR&D charge, then separate interest-income,
    # interest-expense and other-nonoperating rows.  Company Facts omits that
    # issuer-extension row, but the complete standard nonoperating bridge can
    # still recover the subtotal exactly:
    #
    #   operating = pretax - interest income + interest expense - other nonoperating
    #
    # This path is deliberately narrow.  Gross profit, SG&A and R&D must establish
    # an industrial statement; every arithmetic input must come from the identical
    # filing and period; and the implied total operating costs must cover all
    # identified operating costs.  A missing bridge row or a contradictory cost
    # stack leaves the subtotal absent.
    inverse_years = (
        set(gross) & set(pretax) & set(separate_interest_income)
        & set(separate_interest_expense) & set(other_nonoperating)
        & set(selling_general_admin) & set(research_development)
    )
    for year in sorted(inverse_years):
        if year in out:
            continue
        core = (
            pretax[year], separate_interest_income[year],
            separate_interest_expense[year], other_nonoperating[year],
            gross[year], selling_general_admin[year], research_development[year],
        )
        periods = {
            (fact.provenance.period_start, fact.provenance.period_end,
             fact.provenance.accession)
            for fact in core
        }
        if len(periods) != 1:
            continue
        if (gross[year].value <= 0
                or separate_interest_income[year].value < 0
                or separate_interest_expense[year].value < 0):
            continue
        value = (
            pretax[year].value
            - separate_interest_income[year].value
            + separate_interest_expense[year].value
            - other_nonoperating[year].value
        )
        implied_operating_costs = gross[year].value - value
        identified_costs = (
            selling_general_admin[year].value
            + research_development[year].value
        )
        aligned_restructuring = restructuring.get(year)
        if aligned_restructuring is not None:
            restructuring_period = (
                aligned_restructuring.provenance.period_start,
                aligned_restructuring.provenance.period_end,
                aligned_restructuring.provenance.accession,
            )
            if restructuring_period in periods:
                identified_costs += aligned_restructuring.value
            else:
                aligned_restructuring = None
        residual = implied_operating_costs - identified_costs
        if (implied_operating_costs < 0 or residual < 0
                or residual > gross[year].value * Decimal("0.05")):
            continue
        sources = core + ((aligned_restructuring,)
                          if aligned_restructuring is not None else ())
        expression = (
            f"{pretax[year].provenance.tag} - "
            f"{separate_interest_income[year].provenance.tag} + "
            f"{separate_interest_expense[year].provenance.tag} - "
            f"{other_nonoperating[year].provenance.tag}"
        )
        identified_cost_tags = " + ".join(
            fact.provenance.tag for fact in (
                selling_general_admin[year], research_development[year],
                *((aligned_restructuring,) if aligned_restructuring is not None else ()),
            )
        )
        out[year] = _derived_flow(
            "Operating income (derived and reconciled from pretax less complete "
            "nonoperating lines)",
            f"{expression}; checked against {gross[year].provenance.tag} less "
            f"identified operating costs ({identified_cost_tags}); remaining "
            f"operating-cost residual {residual}",
            value, sources, year,
        )
    return _reconcile_duration_scale(
        gaap, dict(sorted(out.items())), frozenset(OPERATING_INCOME_TAGS))[0]


def _latest_annual_end(annual_eps: dict[int, Fact]) -> date | None:
    if not annual_eps:
        return None
    return annual_eps[max(annual_eps)].provenance.period_end


def _ttm_eps(gaap: dict, annual_eps: dict[int, Fact],
             unit: tuple[str, ...] = ("USD/shares",),
             per_share: bool = True) -> tuple[Decimal | None, tuple[Fact, ...]]:
    """Current EPS for criterion 1: latest annual, rolled forward with interim
    year-to-date figures when a newer filing exists
    (TTM = FY + YTD_current - YTD_prior_year)."""
    if not annual_eps:
        return None, ()
    latest = annual_eps[max(annual_eps)]
    tag = _tag_of(latest)
    all_durations = [e for e in _entries(gaap, tag, unit) if "start" in e]
    quarters = [e for e in all_durations if _is_interim_form(e.get("form", ""))]
    if not quarters:
        return latest.value, (latest,)
    latest_end = max(date.fromisoformat(e["end"]) for e in quarters)
    if latest.provenance.period_end and latest_end <= latest.provenance.period_end:
        return latest.value, (latest,)
    ending_now = [e for e in quarters if date.fromisoformat(e["end"]) == latest_end]
    # The year-to-date leg must begin where the anchor's fiscal year ended, or the
    # composite spans the wrong window: a filer that tags no YTD total offers only a
    # three-month period, some tag a 364-day rolling year that would add a full year
    # to a full year, and after a fiscal-year change the newest quarter can belong to
    # a different year entirely. Preferring the period that actually starts the day
    # after the anchor closes rules out all three; only if none does is the longest
    # taken, and the window is then verified below.
    anchor_end = latest.provenance.period_end
    aligned = [e for e in ending_now
               if anchor_end and date.fromisoformat(e["start"]) - anchor_end == timedelta(days=1)]
    cur = max(aligned or ending_now, key=lambda e: (_days(e), e["filed"]))
    if anchor_end:
        gap = (date.fromisoformat(cur["start"]) - anchor_end).days
        # a 53-week year and an early-January close leave a few days of slack; a
        # quarter's worth of drift means the two legs do not meet
        if not (-14 <= gap <= 14):
            return latest.value, (latest,)
    target = latest_end - timedelta(days=365)
    prior_candidates = [
        e
        for e in all_durations
        if abs((date.fromisoformat(e["end"]) - target).days) <= 14
        and abs(_days(e) - _days(cur)) <= 14
    ]
    if not prior_candidates:
        # ponytail: comparative YTD missing -> latest annual stands in for TTM; provenance shows its age
        return latest.value, (latest,)
    prior = max(prior_candidates, key=lambda e: e["filed"])
    # Only per-share figures break when the share count moves. A dollar total adds
    # and subtracts across periods regardless of how many shares were outstanding.
    if per_share and _shares_incomparable(gaap, cur, prior):
        # the last audited year is a real per-share figure; the composite is not
        return latest.value, (latest,)
    # The anchor arrives already rebased onto today's share count by _split_adjust,
    # and the quarters come from the newest 10-Q, which is on that basis too. Rescaling
    # here as well divided Booking Holdings by 25 twice and produced 2.65 a share.
    delta = _dec(cur["val"]) - _dec(prior["val"])
    # Filers do mistag. Taboola's quarters carry 220.00 and -40.00 a share against a
    # full year of 0.13, giving a trailing 260.13 and a price/earnings of 0.02 that
    # passed criterion 1. No split explains a quarter dwarfing its own year by this
    # much, so the composite is refused and the audited year stands alone. Both bounds
    # are needed: the ratio alone would reject a genuine recovery off a tiny base.
    if per_share and abs(delta) > 20 * abs(latest.value) and abs(delta) > _ABSURD_DELTA:
        return latest.value, (latest,)
    value = latest.value + delta
    return value, (latest, _fact(tag, tag, cur), _fact(tag, tag, prior))


# the vintage runs only walk the EPS chains and the share-comparability guard,
# so only those tags need their history rewound
_VINTAGE_TAGS = frozenset(EPS_TAGS + EPS_BASIC_TAGS + (EPS_CONTINUING_TAG,) + _WEIGHTED_SHARE_TAGS)


def _filed_by(gaap: dict, cutoff: str) -> dict:
    """The filer's EPS-related facts as the record stood on `cutoff` — everything
    filed later removed, so restatements and splits are invisible until filed."""
    cut = {}
    for tag in _VINTAGE_TAGS:
        data = gaap.get(tag)
        if not data:
            continue
        units = {u: kept for u, entries in data.get("units", {}).items()
                 if (kept := [e for e in entries if e.get("filed", "") <= cutoff])}
        if units:
            cut[tag] = {**data, "units": units}
    wrapped = _with_fiscal_calendar(cut)
    _copy_taxonomy_context(wrapped, gaap)
    return wrapped


def fiscal_year_ends(gaap: dict) -> dict[int, str]:
    """The date each fiscal year actually closed on, by the label it carries.

    Microsoft's fiscal 2026 ends 2026-06-30, not 2026-12-31. Both halves of every
    multiple below are struck at this date — the price of that year and the
    trailing earnings knowable then — because a December close divided by a June
    balance sheet describes no moment that existed. Only annual filings are read,
    so the date has always already happened.
    """
    gaap = _with_fiscal_calendar(gaap)
    ends: dict[int, str] = {}
    # The earnings series first, because it labels fiscal years from SEC's own frame
    # and the balance-sheet reader labels them from the month the period ends in.
    # Those disagree under the retail convention: Lululemon's year ending 2025-02-02
    # is fiscal 2024 to the frame and 2025 to the month, so the ratio table paired
    # one year's balance sheet with another year's earnings and printed the same date
    # against two different years.
    sources = [_annual_eps(gaap)]
    sources += [_annual_balances(gaap, tags) for tags in
                (("Assets", "LiabilitiesAndStockholdersEquity"),
                 ("StockholdersEquity",), ("AssetsCurrent",))]
    claimed: set[str] = set()
    for series in sources:
        for year, f in series.items():
            end = f.provenance.period_end
            if end is None or year in ends or end.isoformat() in claimed:
                continue
            # one date belongs to one fiscal year. The balance-sheet reader labels
            # by the month a period ends in and the earnings reader by SEC's frame,
            # so under the retail convention the same date arrives twice under two
            # names: Lululemon's 2026-02-01 came in as fiscal 2025 and again as
            # 2026, and the ratio table grew a duplicate column of zeroes.
            ends[year] = end.isoformat()
            claimed.add(end.isoformat())
    return ends


def vintage_ttm_eps(gaap: dict) -> dict[str, Decimal]:
    """TTM EPS as it was knowable at each of the last fiscal year ends: the
    ordinary composite, run only on facts FILED by that date. No look-ahead — a
    10-K published in August was not knowledge the previous June, so a June filer's
    figure here is its trailing four quarters through Q3, exactly what a reader
    standing on that date could compute.

    Keyed by the fiscal year end itself rather than by a calendar December, which
    is the same date only for December filers. `fiscal_year_ends` is the one
    source both this series and the ratio table read, so their keys cannot drift.

    Each figure is then rebased onto today's share count (splits filed after the
    cutoff divided out), because the price series it will be divided into is
    split-adjusted to today as well."""
    periods = _per_share_periods(gaap)
    shares = _share_periods(gaap)
    out: dict[str, Decimal] = {}
    ends = fiscal_year_ends(gaap)
    for year in sorted(ends)[-7:]:
        iso = ends[year]
        cut = _filed_by(gaap, iso)
        ttm, _ = _ttm_eps(cut, _annual_eps(cut))
        if ttm is None:
            continue
        factor = (_split_factor(
            periods, iso, shares, cik=getattr(gaap, "cik", None))
            if periods else Decimal(1))
        out[iso] = ttm / factor
    return out


def _duration_fact(gaap: dict, tag: str, start: str, end: str, unit=("USD",)) -> Decimal | None:
    """Value for an exact reporting period, latest-filed wins."""
    matches = [e for e in _entries(gaap, tag, unit)
               if e.get("start") == start and e.get("end") == end
               and _is_financial_form(e.get("form", ""))]
    if not matches:
        return None
    return _dec(max(matches, key=lambda e: e["filed"])["val"])


def _year_earlier_fact(gaap: dict, tag: str, start: date, end: date) -> Decimal | None:
    """Same reporting period one year back. Fiscal calendars drift by days, so match
    on approximate dates and equal duration rather than an exact -364."""
    want_start, want_end = start - timedelta(days=365), end - timedelta(days=365)
    length = (end - start).days
    best = None
    for e in _entries(gaap, tag, ("USD",)):
        if "start" not in e or not _is_financial_form(e.get("form", "")):
            continue
        es, ee = date.fromisoformat(e["start"]), date.fromisoformat(e["end"])
        if abs((ee - want_end).days) <= 10 and abs((es - want_start).days) <= 10 \
                and abs((ee - es).days - length) <= 10:
            if best is None or e["filed"] > best["filed"]:
                best = e
    return _dec(best["val"]) if best else None


def _earnings_quality(gaap: dict, ttm_inputs: tuple[Fact, ...], annual_eps: dict[int, Fact]) -> tuple[str, ...]:
    """What the trailing earnings are made of. Criterion 1 is the one test a single
    accounting line can flip, so its composition is disclosed rather than trusted."""
    notes: list[str] = []
    if len(ttm_inputs) < 3:
        return ()
    cur = ttm_inputs[1].provenance
    if cur.period_start is None or cur.period_end is None:
        return ()
    start, end = cur.period_start.isoformat(), cur.period_end.isoformat()

    pretax = next(
        (v for tag in PRETAX_TAGS if (v := _duration_fact(gaap, tag, start, end)) is not None), None
    )
    # Same line is sometimes tagged twice (DINO tags one LCM adjustment under two
    # elements); report each distinct amount once.
    seen: set[Decimal] = set()
    emitted: set[str] = set()

    def swing_text(tag: str, amount: Decimal) -> str:
        # the year-earlier comparable period shows whether this line is steady or a swing
        prior = _year_earlier_fact(gaap, tag, cur.period_start, cur.period_end)
        if prior is None:
            return " No comparable figure is tagged for the year-earlier period."
        if abs(amount - prior) < abs(amount) * Decimal("0.25"):
            return (f" It was {prior / _MILLION:+,.0f}M in the same period a year earlier — "
                    "steady, so it is not what makes this period unusual.")
        return (f" The same line was {prior / _MILLION:+,.0f}M a year earlier, a swing of "
                f"{abs(amount - prior) / _MILLION:,.0f}M between the two periods.")

    def emit(tag: str, label: str, gain_signed: bool = False, neutral: bool = False) -> None:
        amount = _duration_fact(gaap, tag, start, end)
        if amount is None or amount == 0 or amount in seen:
            return
        # "23% of pre-tax income" against a pre-tax LOSS states a proportion of a
        # negative, which reads as though the charge were part of a profit. The
        # materiality test still needs a scale, and a loss is one, but the sentence
        # cannot be written as a share of income.
        if not (pretax and pretax > 0 and abs(amount) / abs(pretax) >= _NONCASH_MATERIALITY):
            return
        seen.add(amount)
        emitted.add(tag)
        share = abs(amount) / abs(pretax) * 100
        if neutral:
            lead = (f"{label.capitalize()} of {abs(amount) / _MILLION:,.0f}M moved pre-tax income "
                    "in a direction the filing's sign convention cannot settle")
        else:
            positive_adds = amount > 0 if gain_signed else amount < 0
            direction = "added to" if positive_adds else "reduced"
            lead = f"{label.capitalize()} of {abs(amount) / _MILLION:,.0f}M {direction} pre-tax income"
        notes.append(_note(
            label[0].upper() + label[1:],
            f"{lead} for {start} to {end}, a period inside the trailing window — {share:.0f}% of "
            f"that period's {pretax / _MILLION:,.0f}M pre-tax income.{swing_text(tag, amount)} "
            "Judge for yourself whether it belongs in a run-rate earnings figure."
        ))

    for tag, label in NONCASH_TAGS:
        emit(tag, label)
    if not (emitted & _IMPAIRMENT_SPECIFICS):
        emit(*_IMPAIRMENT_ROLLUP)
    for tag, label in GAIN_TAGS:
        emit(tag, label, gain_signed=True)
    emit(*_WARRANT_TAG, neutral=True)
    for tag, label in _NEUTRAL_QUALITY_TAGS:
        emit(tag, label, neutral=True)

    # a loss quarter dropping out of (or sitting inside) the window swings the TTM
    tag = _tag_of(ttm_inputs[0])
    quarters = [
        e for e in _entries(gaap, tag, ("USD/shares",))
        if "start" in e and 80 <= _days(e) <= 100 and _dec(e["val"]) < 0
        and e["end"] >= (cur.period_end - timedelta(days=730)).isoformat()
    ]
    if quarters:
        worst = min(quarters, key=lambda e: e["val"])
        notes.append(_note(
            "Loss quarter",
            f"A loss quarter ({worst['start']} to {worst['end']}, {_dec(worst['val'])} per share) "
            "falls in or near the trailing window; whether it is inside or outside moves the "
            "trailing figure without anything changing in the business."
        ))

    counts = []
    for f in ttm_inputs:
        p = f.provenance
        if p.period_start and p.period_end:
            c = _duration_fact(gaap, "WeightedAverageNumberOfDilutedSharesOutstanding",
                               p.period_start.isoformat(), p.period_end.isoformat(), unit=("shares",))
            if c:
                counts.append(c)
    if len(counts) >= 2 and min(counts) > 0:
        spread = max(counts) / min(counts) - 1
        if spread >= _SHARE_DIVERGENCE:
            notes.append(_note(
                "Share-count spread",
                f"The three periods combined into the trailing figure carry diluted share counts "
                f"differing by {spread * 100:.0f}% ({min(counts) / _MILLION:,.0f}M to "
                f"{max(counts) / _MILLION:,.0f}M), so the sum is not a like-for-like per-share number."
            ))
    return tuple(notes)


def _shares_incomparable(gaap: dict, cur: dict, prior: dict) -> bool:
    """True when the two year-to-date periods were struck on share counts so
    different that their per-share figures cannot be subtracted — an IPO, a large
    secondary, or a reverse split between them."""
    # A filer that reports only basic shares left this guard blind, because it read one
    # hard-coded diluted tag and gave up. Every other extractor here walks a chain; this
    # one now does too, and both legs must come from the same tag or the comparison is
    # between two different measures rather than two periods.
    # ...and the ratios from every tag are weighed together, not just the first.
    # A loss makes a convertible antidilutive, so the DILUTED count collapses to the
    # basic one while the company itself did nothing: AMC Networks reports 43,320
    # basic and 43,320 diluted in every loss column and 44,845 against 56,482 in every
    # profit one. Its basic count moved 3.4%. Taking the smallest disagreement asks
    # "did the share base really change", which is the question the guard exists for.
    ratios = []
    for tag in _WEIGHTED_SHARE_TAGS:
        counts = [_duration_fact(gaap, tag, e["start"], e["end"], unit=("shares",))
                  for e in (cur, prior)]
        if all(c is not None and c > 0 for c in counts):
            ratios.append(max(counts) / min(counts) - 1)
    if not ratios:
        return False        # unknown share counts are not evidence of a problem
    return min(ratios) >= _SHARE_INCOMPARABLE


def _latest_instant(
    taxo: dict,
    concept: str,
    tags: tuple[str, ...],
    ns: str = "us-gaap",
    unit: tuple[str, ...] = ("USD",),
    not_before: date | None = None,
) -> Fact | None:
    floor = not_before.isoformat() if not_before else ""
    for tag in tags:
        entries = [
            e
            for e in _entries(taxo, tag, unit)
            if "start" not in e
            and _is_financial_form(e.get("form", ""))
            and e["end"] >= floor
        ]
        if entries:
            e = max(entries, key=lambda e: (e["end"], e["filed"]))  # latest period, latest-filed
            return _fact(concept, tag, e, ns=ns)
    return None


def _at_period_end(taxo: dict, concept: str, tags: tuple[str, ...], end: date | None,
                   unit: tuple[str, ...] = ("USD",)) -> Fact | None:
    """The figure as of one exact balance-sheet date, or nothing."""
    if end is None:
        return None
    for tag in tags:
        entries = [e for e in _entries(taxo, tag, unit)
                   if "start" not in e and _is_financial_form(e.get("form", ""))
                   and e["end"] == end.isoformat()]
        if entries:
            return _fact(concept, tag, max(entries, key=lambda e: e["filed"]))
    return None


def _latest_instant_across(
    taxo: dict,
    concept: str,
    tags: tuple[str, ...],
    unit: tuple[str, ...] = ("USD",),
    not_before: date | None = None,
    largest_wins: bool = False,
) -> Fact | None:
    """Latest period end wins ACROSS the whole chain; chain order only breaks ties.

    Debt chains need this, not first-tag-wins: a filer that stops updating a
    high-priority tag leaves a stale figure — often a zero — that would outrank a
    newer fact on a lower-priority tag, and understating debt is the
    anti-conservative direction for criterion 3."""
    floor = not_before.isoformat() if not_before else ""
    best: tuple[dict, str] | None = None
    for tag in tags:
        entries = [
            e
            for e in _entries(taxo, tag, unit)
            if "start" not in e
            and _is_financial_form(e.get("form", ""))
            and e["end"] >= floor
        ]
        if not entries:
            continue
        e = max(entries, key=lambda e: (e["end"], e["filed"]))
        if best is None or e["end"] > best[0]["end"]:
            best = (e, tag)
        elif largest_wins and e["end"] == best[0]["end"] and e["val"] > best[0]["val"]:
            # Chain order breaks the tie everywhere else, and for debt that lets a
            # footnote fragment outrank the balance-sheet line it belongs to:
            # Carriage Services shipped 14.4M where its own LongTermDebt reads
            # 526.0M at the same date and LongTermDebtNoncurrent 5.4M is a note.
            # Between two figures for one date, the whole is the larger.
            best = (e, tag)
    if best is None:
        return None
    return _fact(concept, best[1], best[0])


def _tag_of(fact: Fact | None) -> str | None:
    return (fact.provenance.canonical_tag or _canonical_tag(fact.provenance.tag)) \
        if fact else None


# Short-bucket slots a combined (current + noncurrent) long-bucket tag makes
# redundant. Every combined tag counted in the long bucket must name its
# suppressions here, or the current portion is counted twice across the buckets.
# NotesPayable-family totals also cover their convertible members, so counting
# them suppresses the convertible-current family too.
_COMBINED_SUPPRESSIONS = {
    "LongTermDebt": frozenset({"ltd_current"}),  # the plain tag includes current maturities
    "NotesAndLoansPayable": frozenset({"ltd_current", "notes_current", "loans_current", "convertible_current"}),
    "NotesPayable": frozenset({"ltd_current", "notes_current", "convertible_current"}),
    "LoansPayable": frozenset({"ltd_current", "loans_current"}),
    "ConvertibleDebt": frozenset({"convertible_current"}),
    "ConvertibleNotesPayable": frozenset({"convertible_current"}),
    "LineOfCredit": frozenset({"loc_current"}),
    "SecuredDebt": frozenset({"secured_current"}),
    "FinanceLeaseLiability": frozenset({"finance_lease_current"}),
    "SeniorNotes": frozenset({"senior_current"}),
    "OtherLoansPayable": frozenset({"loans_current"}),
}


def _tags_behind(fact: Fact) -> list[str]:
    """Every element a figure rests on, a sum's components included."""
    p = fact.provenance
    return [c.tag for c in p.components] or [p.tag]


def _debt_elsewhere(gaap: dict, lease: Fact) -> bool:
    """Whether the interest bill is too large for the lease to be the whole debt.

    A finance lease costs its own interest rate — a few per cent of the balance a
    year. Ford pays $1,254M against a $754M lease book, so 166% of the principal:
    money is being paid for borrowings this bucket cannot see, and its own debt
    element went stale in 2020. The bar is deliberately at the whole principal
    rather than at a plausible interest rate, because most filers with a lease-only
    bucket are genuinely debt-free (Vertex, Incyte, MongoDB, Plexus all sit well
    under it) and a wrong INSUFFICIENT costs a reader a real answer.
    """
    interest = _annual_union(gaap, INTEREST_EXPENSE_TAGS)
    if not interest or lease.value <= 0:
        return False
    latest = max(interest)
    return abs(interest[latest].value) >= lease.value


def _component_value(gaap: dict, component, fresh: date | None) -> Decimal | None:
    f = _latest_instant(gaap, component.concept, (_canonical_tag(component.tag),),
                        not_before=fresh)
    return f.value if f else None


def _fresher_or_larger(a: Fact | None, b: Fact | None) -> Fact | None:
    """Between two readings of one quantity: the newer balance sheet, and at one
    date the larger figure — a fragment cannot exceed the line it is part of, and
    understating debt is the direction criterion 3 must never err in."""
    if a is None or b is None:
        return a or b
    ends = (a.provenance.period_end, b.provenance.period_end)
    if ends[0] != ends[1] and None not in ends:
        return a if ends[0] > ends[1] else b
    return a if a.value >= b.value else b


def _long_term_debt(gaap: dict, not_before: date | None) -> tuple[Fact | None, frozenset[str]]:
    primary = _latest_instant_across(
        gaap, "LongTermDebt",
        ("LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations",
         "LongTermNotesPayable", "LongTermDebt"),
        not_before=not_before, largest_wins=True,
    )
    # Some foreign US-GAAP statements present bank borrowings and funding notes
    # as two separate long-term balance-sheet lines. LX is explicit: $80.939M
    # LongTermDebt plus $121.633M LongTermNotesPayable. The ordinary domestic
    # meaning of LongTermDebt is a rollup, so this exception requires the same
    # foreign annual filing/date and the debt line to be smaller than the notes
    # line. That last guard keeps PMEC's $12.812M combined total from absorbing
    # its own $4.331M note component twice.
    borrowing = _latest_instant(gaap, "LongTermDebt (foreign borrowings)",
                                ("LongTermDebt",), not_before=not_before)
    funding = _latest_instant(gaap, "LongTermDebt (foreign funding notes)",
                              ("LongTermNotesPayable",), not_before=not_before)
    disjoint_foreign = None
    if (borrowing and funding
            and borrowing.provenance.form in ("20-F", "40-F")
            and funding.provenance.form == borrowing.provenance.form
            and funding.provenance.accession == borrowing.provenance.accession
            and funding.provenance.period_end == borrowing.provenance.period_end
            and borrowing.value < funding.value):
        disjoint_foreign = _sum_facts(
            "LongTermDebt (foreign borrowings + funding notes)",
            [borrowing, funding],
        )
        primary = _fresher_or_larger(primary, disjoint_foreign)
    # The instrument families are a SECOND representation of the same debt, not an
    # addition to the first, so they are built whatever the primary chain found and
    # the better of the two is taken. Gating them on "primary is None" dropped
    # WisdomTree's $1,057.6M of convertibles because a $13.6M LongTermNotesPayable
    # had satisfied the chain, and left Standard Motor's superseded revolver in
    # place while the fresher LongTermLineOfCredit sat in the same filing.
    instrument_parts: list[Fact | None] = []
    if True:
        parts_target = instrument_parts
        parts_target.append(_latest_instant(
            gaap, "LongTermDebt (other)",
            # the plain element beside the noncurrent one: Procter & Gamble carries
            # $5,265M there and nothing read it
            ("OtherLongTermDebtNoncurrent", "OtherLongTermDebt"), not_before=not_before
        ))
        parts_target.append(_latest_instant_across(
            gaap, "LongTermDebt (commercial paper, term)",
            # Disney funds $2,062M through commercial paper it classifies as
            # long-term; the element carries its own current portion
            ("LongtermCommercialPaperCurrentAndNoncurrent",), not_before=not_before,
        ))
        parts_target.append(_latest_instant_across(
            gaap, "LongTermDebt (transition bonds)",
            # utility securitisation bonds are borrowed money like any other
            ("LongTermTransitionBond",), not_before=not_before,
        ))
        parts_target.append(_latest_instant_across(
            gaap, "LongTermDebt (debtor-in-possession)",
            # borrowing arranged inside bankruptcy is still borrowing
            ("DebtorInPossessionFinancingBorrowingsOutstanding",), not_before=not_before,
        ))
        # notes/loans family: the parent rollups win over the pair (TEVA files
        # only the long-term combined variant, $16.8B)
        notes_group = _latest_instant_across(
            gaap, "LongTermDebt (notes and loans)",
            ("NotesAndLoansPayable", "LongTermNotesAndLoans"), not_before=not_before
        )
        if notes_group is None:
            notes_group = _sum_facts("LongTermDebt (notes and loans)", [
                _latest_instant_across(gaap, "LongTermDebt (notes payable)", ("NotesPayable",),
                                       not_before=not_before),
                _latest_instant_across(gaap, "LongTermDebt (loans payable)", ("LoansPayable",),
                                       not_before=not_before),
            ])
        instruments = [notes_group] if notes_group else []
        if notes_group is None:
            # convertibles hide inside the notes totals; a separate slot only
            # when no notes group fired (DDOG/SNOW: converts are the whole debt)
            instruments.append(_latest_instant_across(
                gaap, "LongTermDebt (convertible)",
                ("ConvertibleDebtNoncurrent", "ConvertibleLongTermNotesPayable",
                 "ConvertibleDebt", "ConvertibleNotesPayable"),
                not_before=not_before,
            ))
            instruments.append(_latest_instant_across(
                gaap, "LongTermDebt (loans)", ("LongTermLoansPayable", "OtherLoansPayable"),
                not_before=not_before
            ))
            # banks file this beside no instrument rollup at all; it sits INSIDE
            # unsecured borrowings, so it competes with the secured/unsecured
            # axis below rather than adding to it
            instruments.append(_latest_instant_across(
                gaap, "LongTermDebt (subordinated)", ("SubordinatedDebt",),
                not_before=not_before
            ))
            instruments.append(_latest_instant_across(
                gaap, "LongTermDebt (senior notes)", ("SeniorLongTermNotes", "SeniorNotes"),
                not_before=not_before
            ))
        instruments.append(_latest_instant_across(
            gaap, "LongTermDebt (credit line)", ("LongTermLineOfCredit", "LineOfCredit"),
            not_before=not_before,
        ))
        instruments = [f for f in instruments if f is not None]
        # Secured/unsecured is an AXIS over the same instruments, not another
        # instrument: the two sides are disjoint by definition, so their sum is
        # a second representation of the whole (GS reports $348B unsecured +
        # $11.6B secured and no instrument rollup at all). Take whichever
        # representation shows more, never both. A skip-if-present guard
        # misfires on fresh zeros (BRT) and fragments (AVA: 900x).
        axis = _sum_facts("LongTermDebt (secured + unsecured)", [
            _latest_instant_across(
                gaap, "LongTermDebt (secured)",
                # Welltower reports $2,814M under the "other" variant alone
                ("SecuredLongTermDebt", "SecuredDebt", "SecuredDebtOther"),
                not_before=not_before,
            ),
            _latest_instant_across(
                gaap, "LongTermDebt (unsecured)", ("UnsecuredLongTermDebt",),
                not_before=not_before,
            ),
        ])
        if axis is not None and axis.value > sum((f.value for f in instruments), Decimal(0)):
            parts_target.append(axis)
        else:
            parts_target.extend(instruments)

    # newer wins; at one date the larger of two representations of one quantity
    instruments_total = _sum_facts("LongTermDebt (instruments)",
                                   [f for f in instrument_parts if f is not None])
    chosen = _fresher_or_larger(primary, instruments_total)
    picked = _tag_of(chosen)
    parts = [chosen] if chosen else []
    parts.append(_latest_instant_across(
        gaap, "LongTermDebt (subordinated debentures)",
        ("JuniorSubordinatedDebentureOwedToUnconsolidatedSubsidiaryTrustNoncurrent",
         "JuniorSubordinatedNotes"),
        not_before=not_before,
    ))
    if picked != "LongTermDebtAndCapitalLeaseObligations":
        parts.append(_latest_instant_across(
            gaap, "LongTermDebt (finance leases)",
            ("FinanceLeaseLiabilityNoncurrent", "FinanceLeaseLiability"),
            not_before=not_before,
        ))
    suppress: set[str] = set()
    for f in parts:
        if f is None:
            continue
        for tag in f.provenance.tag.split(" + "):  # summed facts carry every component tag
            if f is disjoint_foreign and tag.split(":", 1)[-1] == "LongTermDebt":
                # Here the filing proves the plain tag is the noncurrent
                # borrowings line, not the usual current+noncurrent rollup.
                continue
            suppress |= _COMBINED_SUPPRESSIONS.get(_canonical_tag(tag), frozenset())
    return _sum_facts("LongTermDebt", parts), frozenset(suppress)


def _short_term_debt(
    gaap: dict, not_before: date | None, suppress: frozenset[str] = frozenset()
) -> Fact | None:
    whole = _latest_instant_across(gaap, "ShortTermDebt", ("DebtCurrent",), not_before=not_before)
    if whole is not None:
        return whole  # DebtCurrent already rolls up the whole short bucket

    # Borrowings slot. ShortTermBankLoansAndNotesPayable TERMINATES the slot and
    # the notes-current family: KEY files it equal to OtherShortTermBorrowings,
    # so summing anything beside it doubles the figure.
    borrowings = _latest_instant_across(
        gaap, "ShortTermDebt (borrowings)",
        ("ShortTermBorrowings", "ShortTermBankLoansAndNotesPayable"),
        not_before=not_before,
    )
    bank_loans_won = _tag_of(borrowings) == "ShortTermBankLoansAndNotesPayable"
    if borrowings is None:
        # KO: commercial paper and other short-term borrowings are disjoint lines;
        # mortgage-warehouse lines are their own facility
        borrowings = _sum_facts("ShortTermDebt (borrowings)", [
            _latest_instant_across(
                gaap, "ShortTermDebt (bank funding)",
                ("FederalFundsPurchasedAndSecuritiesSoldUnderAgreementsToRepurchase",),
                not_before=not_before),
            _latest_instant_across(gaap, "ShortTermDebt (commercial paper)",
                                   # S&P Global files only the carrying-amount variant
                                   # ($715M); Johnson & Johnson the long-term-CP current
                                   # portion ($2,000M)
                                   ("CommercialPaper", "CommercialPaperAtCarryingValue",
                                    "LongTermCommercialPaperCurrent"),
                                   not_before=not_before),
            _latest_instant_across(gaap, "ShortTermDebt (other borrowings)",
                                   ("OtherShortTermBorrowings",), not_before=not_before),
            _latest_instant_across(gaap, "ShortTermDebt (warehouse borrowings)",
                                   ("WarehouseAgreementBorrowings",), not_before=not_before),
            _latest_instant_across(gaap, "ShortTermDebt (bank and other loans)",
                                   ("LoansPayableToBankCurrent", "OtherLoansPayableCurrent"),
                                   not_before=not_before),
            _latest_instant_across(gaap, "ShortTermDebt (transition bonds)",
                                   ("LongtermTransitionBondCurrent",), not_before=not_before),
        ])

    ltd_current = None
    if "ltd_current" not in suppress:  # skipped when the long bucket already includes current maturities
        chain = ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent",
                 "UnsecuredDebtCurrent"]
        if "notes_current" not in suppress and not bank_loans_won:
            chain.append("NotesPayableCurrent")
        chain.append("OtherLongTermDebtCurrent")  # strict component: chained, never summed (SMP/NTAP)
        if "secured_current" not in suppress:
            chain.append("SecuredDebtCurrent")
        ltd_current = _latest_instant_across(
            gaap, "ShortTermDebt (current portion of long-term)", tuple(chain),
            not_before=not_before,
        )
        # ED: NotesPayableCurrent 869M == CommercialPaper 869M, same period end —
        # the tag is often the commercial paper under another name; count once.
        if (ltd_current is not None and _tag_of(ltd_current) == "NotesPayableCurrent"
                and borrowings is not None
                and borrowings.provenance.period_end == ltd_current.provenance.period_end
                and borrowings.value == ltd_current.value):
            ltd_current = None

    # Current-side instrument families exist only when NO current rollup fired:
    # MELI's LoansPayableCurrent and SMP's LinesOfCreditCurrent are components
    # of the rollup and would double count beside it.
    family: list[Fact | None] = []
    if ltd_current is None and "ltd_current" not in suppress:
        parent = None
        if {"notes_current", "loans_current"}.isdisjoint(suppress) and not bank_loans_won:
            parent = _latest_instant_across(
                gaap, "ShortTermDebt (notes and loans, current)", ("NotesAndLoansPayableCurrent",),
                not_before=not_before,
            )
        if parent is not None:
            family.append(parent)
        else:
            if "loc_current" not in suppress:
                family.append(_latest_instant_across(
                    gaap, "ShortTermDebt (credit line, current)", ("LinesOfCreditCurrent",),
                    not_before=not_before))
            if "loans_current" not in suppress:
                family.append(_latest_instant_across(
                    gaap, "ShortTermDebt (loans, current)", ("LoansPayableCurrent",),
                    not_before=not_before))
            if "notes_current" not in suppress and not bank_loans_won:
                family.append(_latest_instant_across(
                    gaap, "ShortTermDebt (other notes, current)", ("OtherNotesPayableCurrent",),
                    not_before=not_before))
                if "senior_current" not in suppress:  # combined SeniorNotes already holds it
                    family.append(_latest_instant_across(
                        gaap, "ShortTermDebt (senior notes, current)", ("SeniorNotesCurrent",),
                        not_before=not_before))
        if "convertible_current" not in suppress:
            family.append(_latest_instant_across(
                gaap, "ShortTermDebt (convertible, current)",
                ("ConvertibleNotesPayableCurrent", "ConvertibleDebtCurrent"),
                not_before=not_before))

    leases = None
    if ("finance_lease_current" not in suppress
            and (ltd_current is None or "CapitalLeaseObligations" not in ltd_current.provenance.tag)):
        leases = _latest_instant(
            gaap, "ShortTermDebt (finance leases)", ("FinanceLeaseLiabilityCurrent",),
            not_before=not_before,
        )
    return _sum_facts("ShortTermDebt", [ltd_current, *family, borrowings, leases])


def _fresher_rollup(total: Fact | None, parts: Fact | None) -> Fact | None:
    """A total tag and the components that make it, where the two disagree on date.

    Newer wins; at one date the total wins, because a rollup is the whole and the
    components on file may be a fragment of it. The same rule criterion 3 applies
    to the debt rollup, for the same reason: a filer that moves to a new element
    leaves the old one in place, and reading the abandoned one gives a figure that
    was true once.

    Energy Transfer moved its redeemable minority interest from
    `RedeemableNoncontrollingInterestEquityCarryingAmount` to `...Other...` after
    2025-12-31. Taking the total first read $250M into a June balance sheet whose
    printed page says $256M, and the difference was deducted from common equity.
    """
    if total is None or parts is None:
        return total or parts
    if parts.provenance.period_end is None or total.provenance.period_end is None:
        return total
    return parts if parts.provenance.period_end > total.provenance.period_end else total


def _sum_facts(concept: str, parts: list[Fact | None]) -> Fact | None:
    """Sum component facts. Components may carry different (already staleness-guarded)
    period ends and are ALL included: dropping any part understates the total, and
    for both debt (criterion 3) and intangibles (criterion 7) overstating is the
    conservative direction. The combined tag string discloses what was summed.

    The 2026-08-21 audit proposed requiring one period end across the components,
    because Coca-Cola's short-term debt picks up an OtherShortTermBorrowings from a
    superseded quarter and lands 26M under the filing's own 4,825M. That was not
    adopted: restricting the sum to the newest date drops a real component whenever
    a filer reports its pieces at different frequencies, which understates by more
    than the mismatch costs, and understating debt is the anti-conservative
    direction. The provenance names every component and its date, so the mismatch
    is visible rather than hidden."""
    parts = [p for p in parts if p is not None]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    latest = max(parts, key=lambda p: p.provenance.period_end).provenance
    starts = {part.provenance.period_start for part in parts}
    fiscal_years = {part.provenance.fiscal_year for part in parts}
    return Fact(
        value=sum(p.value for p in parts),
        provenance=Provenance(
            concept=f"{concept} (sum of components)",
            tag=" + ".join(p.provenance.tag for p in parts),
            fiscal_year=(next(iter(fiscal_years)) if len(fiscal_years) == 1 else None),
            form=latest.form, accession=latest.accession,
            filed=latest.filed, period_end=latest.period_end,
            period_start=(next(iter(starts)) if len(starts) == 1 else None),
            # components may carry different (individually fresh) period ends, so
            # the summary line's date belongs to one of them and not the rest
            components=tuple(p.provenance for p in parts),
        ),
    )


_DEBT_EVIDENCE_RE = re.compile(
    r"debt|borrowing|notespayable|loanspayable|debenture|commercialpaper"
    r"|financeleaseliability|lineofcredit|seniornotes|subordinatednotes"
    r"|mediumtermnotes|federalhomeloan|federalfundspurchased|bankoverdraft", re.I,
)
_DEBT_EVIDENCE_EXCLUDE_RE = re.compile(
    r"securit|maturit|capacity|issuancecost|weightedaverage|interestrate|remaining"
    r"|proceeds|payments|repayments|gainloss|extinguish|conversion|unamortized"
    r"|commitmentfee|increase|decrease", re.I,
)
_INTEREST_EVIDENCE_TAGS = frozenset(
    ("InterestExpenseDebt", "InterestExpenseDebtExcludingAmortization", "InterestExpenseBorrowings")
)
# Evidence that specifically says an interest-bearing balance is current. A
# noncurrent debt fact (EPAM's $25M LongTermDebtNoncurrent) must not block the
# explicit conclusion that its separately unreported current bucket is zero.
_SHORT_DEBT_EVIDENCE_RE = re.compile(
    r"shortterm(?:debt|borrow)|commercialpaper|debtcurrent|currentdebt|"
    r"borrowingscurrent|notespayablecurrent|seniornotescurrent|"
    r"convertible(?:debt|notespayable)current|linesofcreditcurrent|"
    r"financeleaseliabilitycurrent|currentportion.*(?:debt|note|borrow)",
    re.I,
)
_EVIDENCE_FLOOR = 1_000_000  # any real instrument counts; over-detection is the safe direction


def _historical_absent_zero_candidates(facts: dict, end: date) -> set[str]:
    """Optional concepts with no material annual XBRL evidence at one date.

    Unlike the standard-taxonomy extraction, this guard searches every namespace,
    including custom extensions. Broad investment matching intentionally errs on
    the side of withholding an assumption when an unfamiliar asset tag might be
    relevant.
    """
    evidence = {
        "short_term_debt": False,
        "short_term_investments": False,
        "goodwill": False,
        "intangibles": False,
        "noncurrent_investments": False,
    }
    wanted = end.isoformat()
    for taxonomy in facts.values():
        for tag, tagdata in taxonomy.items():
            low = tag.lower()
            material = any(
                isinstance(entry.get("val"), (int, float))
                and abs(entry["val"]) >= _EVIDENCE_FLOOR
                and "start" not in entry
                and entry.get("end") == wanted
                and entry.get("form", "").startswith(ANNUAL_FORMS)
                for entries in (tagdata.get("units") or {}).values()
                for entry in entries
            )
            if not material:
                continue
            if ("noncurrent" not in low
                    and _SHORT_DEBT_EVIDENCE_RE.search(tag)
                    and not _DEBT_EVIDENCE_EXCLUDE_RE.search(tag)):
                evidence["short_term_debt"] = True
            if (low.startswith("goodwill")
                    or "intangibleassetsnetincludinggoodwill" in low):
                evidence["goodwill"] = True
            intangible_balance = (
                low.startswith((
                    "intangibleassetsnet", "finitelivedintangibleassetsnet",
                    "indefinitelivedintangibleassets", "otherintangibleassetsnet",
                    "indefinitelivedtradename", "indefinitelivedtrademark",
                    "indefinitelivedlicense", "otherindefinitelivedintangible",
                ))
                or "intangibleassetsnetincludinggoodwill" in low)
            if intangible_balance:
                evidence["intangibles"] = True
            investment = any(word in low for word in (
                "investment", "marketablesecurit", "availableforsale",
                "heldtomaturity", "equitymethod"))
            footnote_only = any(word in low for word in (
                "maturit", "unrealized", "amortizedcost", "fairvaluedisclosure",
                "deferredtax", "collateral", "proceeds", "payments", "income",
                "expense", "gain", "loss", "reconciliation"))
            if investment and not footnote_only:
                if any(word in low for word in (
                        "noncurrent", "longterm", "equitymethod")):
                    evidence["noncurrent_investments"] = True
                elif any(word in low for word in ("current", "shortterm")):
                    evidence["short_term_investments"] = True
                else:
                    # An unclassified custom investment asset may include either
                    # bucket; do not silently assume either side is zero.
                    evidence["short_term_investments"] = True
                    evidence["noncurrent_investments"] = True
    return {concept for concept, found in evidence.items() if not found}


def _absent_zero_candidates(facts: dict) -> set[str]:
    """Concepts silent in the current annual-report filing window.

    The latest 10-K/20-F/40-F and every later structured quarterly filing are
    searched across every namespace, including custom extensions. Older debt does
    not prove a current balance: DECK, for example, last reported borrowings years
    before its current debt-free annual report. The caller must still opt in, and
    no assumption is allowed unless an annual filing anchors the window.
    """
    latest_annual_filed = max(
        (entry.get("filed") for taxo in facts.values() for tagdata in taxo.values()
         for entries in (tagdata.get("units") or {}).values() for entry in entries
         if (entry.get("filed")
             and entry.get("form", "").startswith(ANNUAL_FORMS))),
        default=None,
    )
    if latest_annual_filed is None:
        return set()

    def has_current_material_value(tagdata: dict) -> bool:
        return any(
            isinstance(entry.get("val"), (int, float))
            and abs(entry["val"]) >= _EVIDENCE_FLOOR
            and _is_financial_form(entry.get("form", ""))
            and (entry.get("filed") or "") >= latest_annual_filed
            for entries in (tagdata.get("units") or {}).values()
            for entry in entries
        )

    evidence = {"debt": False, "short_term_debt": False,
                "goodwill": False, "intangibles": False}
    for taxo in facts.values():
        for tag, tagdata in taxo.items():
            low = tag.lower()
            hits = []
            if not evidence["debt"] and (
                tag in _INTEREST_EVIDENCE_TAGS
                or (_DEBT_EVIDENCE_RE.search(tag) and not _DEBT_EVIDENCE_EXCLUDE_RE.search(tag))
            ):
                hits.append("debt")
            if (not evidence["short_term_debt"]
                    and "noncurrent" not in low
                    and _SHORT_DEBT_EVIDENCE_RE.search(tag)
                    and not _DEBT_EVIDENCE_EXCLUDE_RE.search(tag)):
                hits.append("short_term_debt")
            # startswith: "…ExcludingGoodwill" names goodwill without evidencing it
            if not evidence["goodwill"] and low.startswith("goodwill"):
                hits.append("goodwill")
            if not evidence["intangibles"] and "intangible" in low:
                hits.append("intangibles")
            if hits and has_current_material_value(tagdata):
                for h in hits:
                    evidence[h] = True
    return {concept for concept, found in evidence.items() if not found}


def _derive_liabilities(gaap: dict, not_before: date | None) -> tuple[Fact | None, bool]:
    """Many filers tag no Liabilities total. Derive it from the accounting identity
    L = LiabilitiesAndStockholdersEquity - total equity, same period end required.
    The boolean is True when the derivation used PARENT-ONLY equity, leaving
    noncontrolling interest inside the derived liabilities figure — the caller
    must then not subtract NCI a second time."""
    lse = _latest_instant(
        gaap, "LiabilitiesAndStockholdersEquity", ("LiabilitiesAndStockholdersEquity",),
        not_before=not_before,
    )
    if lse is None:
        return None, False
    for equity_tag, parent_only in (
        ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", False),
        ("StockholdersEquity", True),
        # partnerships have no stockholders equity at all (PAA's Liabilities tag
        # is stale since 2011); their capital accounts carry the same identity
        ("PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest", False),
        ("PartnersCapital", True),
    ):
        equity = _latest_instant(gaap, "Equity", (equity_tag,), not_before=not_before)
        if equity and equity.provenance.period_end == lse.provenance.period_end:
            p = lse.provenance
            return Fact(
                value=lse.value - equity.value,
                provenance=Provenance(
                    concept="Liabilities (derived: LiabilitiesAndStockholdersEquity - equity)",
                    tag=f"{lse.provenance.tag} - {equity.provenance.tag}",
                    fiscal_year=None, form=p.form, accession=p.accession,
                    filed=p.filed, period_end=p.period_end,
                    components=((lse.provenance, equity.provenance)
                                if p.document else ()),
                ),
            ), parent_only
    return None, False


def _exact_total_equity_liabilities_successor(
    gaap: dict, not_before: date | None, total_assets: Fact | None,
) -> Fact | None:
    """An exact newer liability total from one complete accounting identity.

    This is intentionally narrower than ``_derive_liabilities``. Replacing a
    filed but stale Liabilities fact is allowed only when Assets, the redundant
    assets-and-equity total, and equity *including NCI* all come from the same
    accession and period and the two asset totals agree exactly. Parent-only
    equity is excluded because the remainder could contain NCI; the caller must
    not label that amount simply as liabilities.
    """
    if total_assets is None or total_assets.provenance.period_end is None:
        return None
    lse = _latest_instant(
        gaap, "LiabilitiesAndStockholdersEquity",
        ("LiabilitiesAndStockholdersEquity",), not_before=not_before)
    if (lse is None
            or lse.value != total_assets.value
            or lse.provenance.period_end != total_assets.provenance.period_end
            or lse.provenance.accession != total_assets.provenance.accession):
        return None
    for equity_tag in (
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest",
    ):
        equity = _latest_instant(gaap, "Equity including NCI", (equity_tag,),
                                 not_before=not_before)
        if (equity is None
                or equity.provenance.period_end != lse.provenance.period_end
                or equity.provenance.accession != lse.provenance.accession):
            continue
        p = lse.provenance
        return Fact(
            value=lse.value - equity.value,
            provenance=Provenance(
                concept="Liabilities (derived: exact same-filing accounting identity)",
                tag=f"{lse.provenance.tag} - {equity.provenance.tag}",
                fiscal_year=None, form=p.form, accession=p.accession,
                filed=p.filed, period_end=p.period_end,
                components=((lse.provenance, equity.provenance)
                            if p.document else ()),
            ),
        )
    return None


# These are not a generic invitation to add arbitrary liability details. Each
# tuple is the complete set of rows printed on one issuer's current balance
# sheet, checked against Assets = Liabilities + Equity + mezzanine equity.
_VERIFIED_LIABILITY_STATEMENT_COMPONENTS = {
    "0001109354": (
        "LiabilitiesCurrent",
        "LongTermDebtNoncurrent",
        "OtherLiabilitiesNoncurrent",
    ),
    # J.B. Hunt's 2026 Q2 statement prints no Liabilities or
    # LiabilitiesNoncurrent rollup. These five rows are exhaustive and reconcile
    # exactly: $7,944.778m assets - $3,657.115m equity = $4,287.663m.
    "0000728535": (
        "LiabilitiesCurrent",
        "LongTermDebtNoncurrent",
        "SelfInsuranceReserveNoncurrent",
        "OtherLiabilitiesNoncurrent",
        "DeferredIncomeTaxLiabilitiesNet",
    ),
}


def _classified_liabilities_from_statement(
    gaap: dict, not_before: date | None,
) -> Fact | None:
    """Sum the two standard, disjoint liability classes at one filing date.

    Unlike arbitrary note detail, ``LiabilitiesCurrent`` and
    ``LiabilitiesNoncurrent`` are exhaustive balance-sheet subtotals. A same-
    accession requirement prevents a comparative column from being paired with
    a component that the current filing no longer presents.
    """
    parts = [
        _latest_instant(gaap, "Liabilities (current class)", ("LiabilitiesCurrent",),
                        not_before=not_before),
        _latest_instant(gaap, "Liabilities (noncurrent class)", ("LiabilitiesNoncurrent",),
                        not_before=not_before),
    ]
    if any(part is None for part in parts):
        return None
    ends = {part.provenance.period_end for part in parts}
    accessions = {part.provenance.accession for part in parts}
    if len(ends) != 1 or None in ends or len(accessions) != 1:
        return None
    return _sum_facts("Liabilities (current + noncurrent classes)", parts)


def _verified_liabilities_from_statement(
    gaap: dict, not_before: date | None,
) -> Fact | None:
    """Rebuild a verified issuer's liability total from printed statement rows.

    Same period and accession are mandatory. That keeps an apparently complete
    list from becoming a cross-filing sum if one component later disappears.
    """
    tags = _VERIFIED_LIABILITY_STATEMENT_COMPONENTS.get(
        getattr(gaap, "cik", None))
    if tags is None:
        return None
    parts = [
        _latest_instant(gaap, f"Liabilities ({tag})", (tag,), not_before=not_before)
        for tag in tags
    ]
    if any(part is None for part in parts):
        return None
    ends = {part.provenance.period_end for part in parts}
    accessions = {part.provenance.accession for part in parts}
    if len(ends) != 1 or None in ends or len(accessions) != 1:
        return None
    return _sum_facts("Liabilities (verified statement rows)", parts)


def _instants_by_end(gaap: dict, tag: str, not_before: date | None) -> dict[str, dict]:
    """Freshness-filtered instant entries keyed by period end, latest-filed wins."""
    floor = not_before.isoformat() if not_before else ""
    out: dict[str, dict] = {}
    for e in _entries(gaap, tag, ("USD",)):
        if "start" in e or not _is_financial_form(e.get("form", "")) or e["end"] < floor:
            continue
        cur = out.get(e["end"])
        if cur is None or e["filed"] > cur["filed"]:
            out[e["end"]] = e
    return out


def _derived_instant(
    gaap: dict, concept: str, minuend: str, subtrahend: str,
    not_before: date | None, subtrahend_optional: bool = False,
) -> Fact | None:
    """minuend − subtrahend at their latest COMMON period end. Mixed period ends
    would subtract different balance sheets (BBY: gross-only overstates 96x), and
    a negative result marks a mistagged pair (CALM), never a value. With
    subtrahend_optional the bare minuend stands when the subtrahend was never
    tagged — overstating a deduction is the conservative direction."""
    a = _instants_by_end(gaap, minuend, not_before)
    if not a:
        return None
    b = _instants_by_end(gaap, subtrahend, not_before)
    if not b and subtrahend_optional:
        end = max(a)
        return _fact(f"{concept} — {subtrahend} never tagged", minuend, a[end])
    common = set(a) & set(b)
    if not common:
        return None
    end = max(common)
    value = _dec(a[end]["val"]) - _dec(b[end]["val"])
    if value < 0:
        return None
    base = a[end] if a[end]["filed"] >= b[end]["filed"] else b[end]
    return Fact(
        value=value,
        provenance=Provenance(
            concept=concept,
            tag=(f"{_provenance_tag(minuend, a[end])} - "
                 f"{_provenance_tag(subtrahend, b[end])}"),
            fiscal_year=None, form=base.get("form", ""), accession=base.get("accn", ""),
            filed=date.fromisoformat(base["filed"]), period_end=date.fromisoformat(end),
        ),
    )


_INDEFINITE_CLASS_TAGS = (
    "IndefiniteLivedTradeNames", "IndefiniteLivedTrademarks",
    "IndefiniteLivedLicenseAgreements", "OtherIndefiniteLivedIntangibleAssets",
)
# Exact issuer exceptions must be backed by the rendered filing. EML's balance
# sheet separately prints trademarks and "Patents and other intangibles, net",
# while it misuses the nominal ex-goodwill rollup tag for only the latter.
_VERIFIED_FINITE_ONLY_INTANGIBLE_TOTAL_CIKS = frozenset({"0000031107"})
# OPK prints amortising intangibles and IPR&D on separate balance-sheet rows.
# Its nominal ex-goodwill total contains only the first row, while the filer uses
# the nominal indefinite-lived element for the (rounded) complete ex-goodwill
# total in the note. This is an exact issuer exception, not a semantic fallback:
# AVD uses the same element for gross cost, where choosing it would be wrong.
_VERIFIED_INTANGIBLE_TOTAL_OVERRIDE_TAGS = {
    "0000944809": "IndefiniteLivedIntangibleAssetsExcludingGoodwill",
    # BRKR stopped updating the nominal ex-goodwill total after FY2025. Its June
    # 2026 intangible note prints the newer finite-lived net balance, and no
    # indefinite-lived asset exists to add to it.
    "0001109354": "FiniteLivedIntangibleAssetsNet",
}


def _intangible_total_is_finite_only(gaap: dict, not_before: date | None) -> bool:
    """Detect a filer using the ex-goodwill rollup tag for its finite line.

    EML labels ``IntangibleAssetsNetExcludingGoodwill`` as "Patents and other
    intangibles" and files exactly the same amount under
    ``FiniteLivedIntangibleAssetsNet`` while trademarks remain a separate,
    positive balance-sheet line. Treating the nominal rollup as authoritative
    silently omitted the trademarks from tangible book. Exact agreement at a
    common balance-sheet date proves the nominal total is only the finite part;
    approximate agreement would be too weak for a promotion rule.
    """
    if getattr(gaap, "cik", None) not in _VERIFIED_FINITE_ONLY_INTANGIBLE_TOTAL_CIKS:
        return False
    total = _instants_by_end(gaap, "IntangibleAssetsNetExcludingGoodwill", not_before)
    finite = _instants_by_end(gaap, "FiniteLivedIntangibleAssetsNet", not_before)
    for end in set(total) & set(finite):
        if _dec(total[end]["val"]) == _dec(finite[end]["val"]):
            return True
    return False


def _intangibles(gaap: dict, not_before: date | None) -> Fact | None:
    """Total ex-goodwill tag when present; otherwise the larger of the
    finite+indefinite parts sum and the OtherIntangibleAssetsNet line (HBAN files
    both, and the Other line holds MSRs the parts miss — max never sums, so it
    cannot double count); then derivations; then sector last resorts."""
    total = _latest_instant(
        gaap, "Intangibles", ("IntangibleAssetsNetExcludingGoodwill",), not_before=not_before
    )
    finite = _latest_instant(
        gaap, "Intangibles (finite-lived)", ("FiniteLivedIntangibleAssetsNet",), not_before=not_before
    )
    indefinite = _latest_instant(
        gaap, "Intangibles (indefinite-lived)", ("IndefiniteLivedIntangibleAssetsExcludingGoodwill",),
        not_before=not_before,
    )
    if indefinite is None:
        # KO's trademarks and VZ's spectrum live only in class tags
        indefinite = _sum_facts("Intangibles (indefinite-lived classes)", [
            _latest_instant(gaap, f"Intangibles ({tag})", (tag,), not_before=not_before)
            for tag in _INDEFINITE_CLASS_TAGS
        ])
    if total is not None:
        override_tag = _VERIFIED_INTANGIBLE_TOTAL_OVERRIDE_TAGS.get(
            getattr(gaap, "cik", None))
        if (override_tag == "IndefiniteLivedIntangibleAssetsExcludingGoodwill"
                and indefinite is not None
                and total.provenance.period_end == indefinite.provenance.period_end
                and indefinite.value > total.value):
            return indefinite
        if (override_tag == "FiniteLivedIntangibleAssetsNet"
                and finite is not None
                and finite.provenance.period_end is not None
                and (total.provenance.period_end is None
                     or finite.provenance.period_end > total.provenance.period_end)):
            return finite
        # A nominal rollup normally wins. The only safe exception is a proved
        # finite-only use of that tag plus a separately filed indefinite balance
        # at the exact same balance-sheet date.
        if indefinite is not None \
                and total.provenance.period_end == indefinite.provenance.period_end \
                and _intangible_total_is_finite_only(gaap, not_before):
            return _sum_facts("Intangibles (finite rollup + indefinite-lived)", [
                total, indefinite,
            ])
        return total
    summed = _sum_facts("Intangibles", [finite, indefinite])
    other = _latest_instant(
        gaap, "Intangibles (other, net)", ("OtherIntangibleAssetsNet",), not_before=not_before
    )
    if summed is not None and other is not None:
        return other if other.value > summed.value else summed
    if summed is not None or other is not None:
        return summed or other
    derived = _derived_instant(
        gaap, "Intangibles (derived: combined line - goodwill)",
        "IntangibleAssetsNetIncludingGoodwill", "Goodwill", not_before,
    )
    if derived is not None:
        return derived
    for minuend in ("IntangibleAssetsGrossExcludingGoodwill", "FiniteLivedIntangibleAssetsGross"):
        derived = _derived_instant(
            gaap, "Intangibles (derived: gross - accumulated amortization)",
            minuend, "FiniteLivedIntangibleAssetsAccumulatedAmortization",
            not_before, subtrahend_optional=True,
        )
        if derived is not None:
            return derived
    servicing = _sum_facts("Intangibles (mortgage servicing rights)", [
        # the two measurement books are disjoint under ASC 860-50; WFC carries both
        _latest_instant(gaap, "Intangibles (servicing, fair value)",
                        ("ServicingAssetAtFairValueAmount",), not_before=not_before),
        _latest_instant(gaap, "Intangibles (servicing, amortized)",
                        ("ServicingAssetAtAmortizedValue",), not_before=not_before),
    ]) or _latest_instant(
        gaap, "Intangibles (mortgage servicing rights)", ("ServicingAsset",), not_before=not_before
    )
    if servicing is not None:
        return servicing
    return _latest_instant(
        gaap, "Intangibles (capitalized software standing in for an untagged intangibles line)",
        ("CapitalizedComputerSoftwareNet",), not_before=not_before,
    )


def _combined_goodwill_and_intangibles(gaap: dict, not_before: date | None) -> tuple[Fact | None, Fact | None]:
    """Some filers tag one combined goodwill+intangibles balance line and nothing
    else. Subtracting that line once equals subtracting both parts, so it fills
    the intangibles slot; goodwill becomes an explicit zero whose provenance
    names the combined line that already contains it."""
    combined = _latest_instant(
        gaap, "Intangibles (incl. goodwill)", ("IntangibleAssetsNetIncludingGoodwill",),
        not_before=not_before,
    )
    if combined is None:
        return None, None
    p = combined.provenance
    goodwill = Fact(
        value=Decimal(0),
        provenance=Provenance(
            concept="Goodwill (contained in the combined intangibles line)",
            tag=p.tag, fiscal_year=p.fiscal_year, form=p.form,
            accession=p.accession, filed=p.filed, period_end=p.period_end,
        ),
    )
    return goodwill, combined


def _goodwill_and_intangibles(
    gaap: dict, not_before: date | None,
) -> tuple[Fact | None, Fact | None]:
    """Extract the two tangible-book deductions under one shared policy.

    Current and historical tangible book must ask the same question. Previously
    the current path used the full fallback/derivation chain while the history
    read only two direct tags and silently changed either missing value to zero.
    That made 2,355 UI rows show historical P/TBV beside an uncomputable current
    P/TBV. This helper is reused for each annual balance sheet below so a missing
    deduction remains missing in every column.
    """
    goodwill = _latest_instant(gaap, "Goodwill", ("Goodwill",), not_before=not_before)
    if goodwill is None:
        # RNR files no Goodwill element at all; the combined line minus the
        # ex-goodwill line is the same figure by identity.
        goodwill = _derived_instant(
            gaap, "Goodwill (derived: combined line - intangibles excluding goodwill)",
            "IntangibleAssetsNetIncludingGoodwill", "IntangibleAssetsNetExcludingGoodwill",
            not_before,
        )
    if goodwill is None:
        goodwill = _derived_instant(
            gaap, "Goodwill (derived: gross - accumulated impairment)",
            "GoodwillGross", "GoodwillImpairedAccumulatedImpairmentLoss", not_before,
            subtrahend_optional=True,
        )
    intangibles = _intangibles(gaap, not_before)
    if goodwill is None and intangibles is None:
        goodwill, intangibles = _combined_goodwill_and_intangibles(gaap, not_before)
    return goodwill, intangibles


OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
# Energy and other filers whose income statement has no operating subtotal report a
# pre-tax figure instead. Context analysis may use it but owner earnings never does.
PRETAX_INCOME_TAGS = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeignAndDomestic",
)
# The domestic figure is one geography, not the consolidated company: standing
# alone it understates a multinational's profit. It serves only added to its foreign twin,
# and only where both cover the same year.
_PRETAX_GEOGRAPHY_TAGS = ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
                          "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign")
# Rollup tags first: a filer that reports the combined figure rarely also splits it,
# and summing the parts when the rollup exists would double-count.
DA_TAGS = (
    "DepreciationDepletionAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "DepreciationAndAmortization",
)
# Some cash-flow statement rows use a narrowly named issuer element while a
# standard D&A element elsewhere in the same filing is only a rounded narrative
# disclosure. DERA retains issuer extensions; this explicit semantic allowlist
# keeps the statement row available even when it is below the generic $100m
# extension materiality floor.
DA_EXTENSION_TAGS = (
    "DepreciationAndAmortizationOfPropertyPlantAndEquipmentAndComputerPrograms",
)
DA_PART_TAGS = ("Depreciation", "AmortizationOfIntangibleAssets")
TAX_TAGS = ("IncomeTaxExpenseBenefit",)
CASH_TAX_PAID_TAGS = ("IncomeTaxesPaidNet",)
# The deferred tax footnote answers two questions the income statement does not:
# how much of the tax charge was actually payable this year, and how much of the
# deductions the company has already earned it does not expect to use. Both are
# the company's own signed opinion of its future earning power.
DEFERRED_TAX_EXPENSE_TAGS = ("DeferredIncomeTaxExpenseBenefit",)
DEFERRED_TAX_ASSET_TAGS = ("DeferredTaxAssetsGross",)
# gross = net + allowance, so the net tag rebuilds the base when the gross one
# is stale or scoped to something narrower than the allowance covers
DEFERRED_TAX_ASSET_NET_TAGS = ("DeferredTaxAssetsNet",)
DEFERRED_TAX_ALLOWANCE_TAGS = ("DeferredTaxAssetsValuationAllowance",)
CASH_CAPEX_TAGS = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    # Some filers label the cash-flow row "Purchase of equipment and leasehold
    # improvements" with this older standard element (e.g. CLMB's 2025 10-K).
    "PaymentsForProceedsFromProductiveAssets",
    # Standard subtype rows used when the filing does not use the umbrella
    # property/productive-assets element.
    "PaymentsToAcquireOtherPropertyPlantAndEquipment",
    "PaymentsToAcquireMachineryAndEquipment",
    "PaymentsToAcquireOtherProductiveAssets",
    "PaymentsForCapitalImprovements",
    "PaymentsToAcquireOilAndGasProperty",
    "PaymentsToAcquireOilAndGasPropertyAndEquipment",
    "PaymentsToAcquireOilAndGasEquipment",
)
CAPEX_TAGS = (
    *CASH_CAPEX_TAGS,
    # last, so per-year fill only covers years the payments tags lack (SCHL's
    # payments tag died in FY2020); accrual-basis overstatement lowers the
    # all-capex floor and the provenance discloses the segment source
    "SegmentExpenditureAdditionToLongLivedAssets",
)
CASH_TAGS = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
)
# Context only — never an adjustment, never a criterion. Reported earnings that
# do not arrive as cash, a share count quietly growing, and interest that eats
# operating profit are the three things a Graham reader wants flagged beside a
# passing multiple.
OPERATING_CASH_FLOW_TAGS = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
# Supplemental cash-generation diagnostics.  These remain outside every Graham
# criterion and never replace standard FCF.  The chains are alternatives, not
# additive families: where both tags exist the first one supplies that year.
STOCK_COMPENSATION_TAGS = (
    "ShareBasedCompensation",
    "AllocatedShareBasedCompensationExpense",
)
ACQUISITION_PAYMENT_TAGS = (
    "PaymentsToAcquireBusinessesNetOfCashAcquired",
    "PaymentsToAcquireBusinessesGross",
)
# Financing cash outflows used to reacquire the issuer's equity. The common-only
# tag wins; broader standard rollups fill only years where it is absent, and the
# selected filing tag remains visible in provenance. Repurchases are displayed as
# a use of FCF and never enter the FCF calculation itself.
SHARE_REPURCHASE_TAGS = (
    "PaymentsForRepurchaseOfCommonStock",
    "PaymentsForRepurchaseOfEquity",
    "PaymentsForRepurchaseOfCommonAndPreferredStock",
)
CAPITALIZED_INTANGIBLE_INVESTMENT_TAGS = (
    "PaymentsToDevelopSoftware",
    "PaymentsToAcquireIntangibleAssets",
)
# ``IncreaseDecreaseInOperatingCapital`` reports the balance movement, so its
# sign is the inverse of the cash-flow-statement effect.  The workbook adapter's
# synthetic ``IncreaseDecreaseInOperatingAssetsAndLiabilities`` fact is already
# a signed cash effect.  Individual components are otherwise not summed: filers
# publish overlapping rollups and incomplete subsets. One deliberately narrow
# exception covers exact accession/year contexts whose complete statement rows
# were independently read; matching context and overlap checks remain mandatory.
WORKING_CAPITAL_CASH_EFFECT_TAGS = (
    "IncreaseDecreaseInOperatingCapital",
    "IncreaseDecreaseInOperatingAssetsAndLiabilities",
)
_WORKING_CAPITAL_COMPLETE_COMPONENT_WEIGHTS = (
    ("IncreaseDecreaseInAccountsReceivable", Decimal("-1")),
    ("IncreaseDecreaseInInventories", Decimal("-1")),
    ("IncreaseDecreaseInOtherOperatingAssets", Decimal("-1")),
    ("IncreaseDecreaseInAccountsPayableAndAccruedLiabilities", Decimal("1")),
    ("IncreaseDecreaseInOtherOperatingLiabilities", Decimal("1")),
)


def _working_capital_component_family(
    *, assets: tuple[str, ...] = (), liabilities: tuple[str, ...] = (),
) -> tuple[tuple[str, Decimal], ...]:
    """Extend the common rows with filing-verified asset/liability movements."""
    return (
        _WORKING_CAPITAL_COMPLETE_COMPONENT_WEIGHTS
        + tuple((tag, Decimal("-1")) for tag in assets)
        + tuple((tag, Decimal("1")) for tag in liabilities)
    )


# Company Facts omits issuer extensions, so even this apparently complete
# standard family can hide another statement row. Ennis FY2026 is the adverse
# control: its five standard rows omit the separately printed $72k prepaid/tax
# row. Component reconstruction is therefore filing-specific, not a general
# heuristic. These exact contexts were read against the rendered SEC statements.
_VERIFIED_WORKING_CAPITAL_COMPONENT_CONTEXTS = {
    "0000200406": {
        "0000200406-26-000016": (
            frozenset({2023, 2024, 2025}),
            _WORKING_CAPITAL_COMPLETE_COMPONENT_WEIGHTS,
            frozenset(),
        ),  # JNJ
    },
    "0000885725": {
        "0000885725-21-000008": (
            frozenset({2018}),
            _WORKING_CAPITAL_COMPLETE_COMPONENT_WEIGHTS,
            frozenset(),
        ),  # Boston Scientific
    },
    "0000006951": {
        "0001628280-25-056742": (
            frozenset({2023, 2024, 2025}),
            # Keep the statement row order stable in serialized provenance.
            _WORKING_CAPITAL_COMPLETE_COMPONENT_WEIGHTS[:-1] + (
                ("IncreaseDecreaseInContractWithCustomerLiability", Decimal("1")),
                ("IncreaseDecreaseInAccruedIncomeTaxesPayable", Decimal("1")),
                _WORKING_CAPITAL_COMPLETE_COMPONENT_WEIGHTS[-1],
            ),
            # This separately printed non-cash tax adjustment shares the same
            # IncreaseDecreaseIn prefix but is outside the statement's working-
            # capital section.
            frozenset({"IncreaseDecreaseInDeferredIncomeTaxes"}),
        ),  # Applied Materials
    },
    "0001874178": {
        "0001874178-26-000008": (
            frozenset({2023, 2024, 2025}),
            _working_capital_component_family(liabilities=(
                "IncreaseDecreaseInContractWithCustomerLiability",
            )),
            frozenset(),
        ),  # Rivian
    },
    "0000082020": {
        "0001104659-26-020480": (
            frozenset({2023, 2024, 2025}),
            _working_capital_component_family(assets=(
                "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets",
            )),
            frozenset(),
        ),  # United States Lime & Minerals
    },
    "0001314727": {
        "0001314727-25-000090": (
            frozenset({2023, 2024, 2025}),
            _working_capital_component_family(liabilities=(
                "IncreaseDecreaseInEmployeeRelatedLiabilities",
                "IncreaseDecreaseInContractWithCustomerLiability",
            )),
            frozenset(),
        ),  # Sonos
    },
    "0001944048": {
        "0001944048-26-000030": (
            frozenset({2023, 2024, 2025}),
            _working_capital_component_family(liabilities=(
                "IncreaseDecreaseInEmployeeRelatedLiabilities",
                "IncreaseDecreaseInAccruedTaxesPayable",
            )),
            frozenset(),
        ),  # Kenvue
    },
    "0000015615": {
        "0000015615-26-000020": (
            frozenset({2023, 2024, 2025}),
            _working_capital_component_family(
                assets=("IncreaseDecreaseInContractWithCustomerAsset",),
                liabilities=("IncreaseDecreaseInContractWithCustomerLiability",),
            ),
            frozenset(),
        ),  # MasTec
    },
    "0000906553": {
        "0001437749-26-004908": (
            frozenset({2023, 2024, 2025}),
            _working_capital_component_family(
                assets=(
                    "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets",
                    "IncreaseDecreaseInIncomeTaxesReceivable",
                ),
                liabilities=("IncreaseDecreaseInOperatingLeaseLiability",),
            ),
            frozenset({"IncreaseDecreaseInDeferredIncomeTaxes"}),
        ),  # Boyd Gaming
    },
    "0001035983": {
        "0001104659-26-017530": (
            frozenset({2023, 2024, 2025}),
            _working_capital_component_family(
                assets=(
                    "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets",
                    "IncreaseDecreaseInContractWithCustomerAsset",
                ),
                liabilities=("IncreaseDecreaseInContractWithCustomerLiability",),
            ),
            frozenset(),
        ),  # Comfort Systems USA
    },
    "0001534675": {
        "0001493152-26-008465": (
            frozenset({2023, 2024, 2025}),
            _working_capital_component_family(
                assets=(
                    "IncreaseDecreaseInPrepaidExpense",
                    "IncreaseDecreaseInCommodityContractAssetsAndLiabilities",
                ),
                liabilities=(
                    "IncreaseDecreaseInAccruedIncomeTaxesPayable",
                    "IncreaseDecreaseInEmployeeRelatedLiabilities",
                    "IncreaseDecreaseInDueToRelatedParties",
                ),
            ),
            frozenset(),
        ),  # Tecnoglass
    },
    "0001639825": {
        "0001639825-26-000038": (
            frozenset({2024, 2025, 2026}),
            _working_capital_component_family(
                assets=("IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets",),
                liabilities=(
                    "IncreaseDecreaseInContractWithCustomerLiability",
                    "IncreaseDecreaseInOperatingLeaseLiability",
                ),
            ),
            frozenset(),
        ),  # Peloton
    },
    "0001819574": {
        "0001628280-26-042242": (
            frozenset({2024, 2025, 2026}),
            _working_capital_component_family(
                assets=("IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets",),
                liabilities=(
                    "IncreaseDecreaseInContractWithCustomerLiability",
                    "IncreaseDecreaseInOperatingLeaseLiability",
                ),
            ),
            frozenset(),
        ),  # BARK
    },
    "0000278165": {
        "0001493152-26-016891": (
            frozenset({2024, 2025}),
            _working_capital_component_family(
                assets=(
                    "IncreaseDecreaseInPrepaidExpense",
                    "IncreaseDecreaseInDeferredIncomeTaxes",
                ),
                liabilities=(
                    "IncreaseDecreaseInAccruedTaxesPayable",
                    "IncreaseDecreaseInOperatingLeaseLiability",
                ),
            ),
            frozenset(),
        ),  # OMNIQ
    },
    "0001590364": {
        "0001628280-26-012940": (
            frozenset({2023, 2024, 2025}),
            _working_capital_component_family(liabilities=(
                "IncreaseDecreaseInDueToRelatedParties",
            ),
            ),
            frozenset(),
        ),  # FTAI Aviation
    },
}
INTEREST_EXPENSE_TAGS = (
    "InterestExpense", "InterestExpenseDebt", "InterestAndDebtExpense",
    "InterestExpenseNonoperating", "InterestIncomeExpenseNet",
)
# Where trouble accumulates quietly. None of these decides a criterion: a
# receivable growing faster than the sales it came from, inventory the customers
# did not take, and rent obligations that criterion 3 does not count as debt are
# all things a reader should see before trusting reported earnings.
RECEIVABLE_TAGS = ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent",
                   "AccountsReceivableNet")
# NVF bought a company seven times its size with debentures sold at 43 cents on
# the dollar, and Graham's objection was that neither the interest statement nor
# the share count told the truth afterwards: the coupon understated the cost of
# money, and warrants issued as currency diluted holders who could not see them.
# Both leave a trace a filing still has to make.
DEBT_DISCOUNT_TAGS = ("AmortizationOfDebtDiscountPremium",)
WARRANT_SHARE_TAGS = ("ClassOfWarrantOrRightNumberOfSecuritiesCalledByWarrantsOrRights",)
# Graham counts a convertible preferred as the common it becomes — table 18.6's
# share count for McGraw-Hill carries the footnote "including the conversion of
# preferred shares", and its book value per share is struck on that larger count.
# The reasoning is that the issue is small, the conversion arithmetic is easy, and
# at any favourable price those shares exist.
# Employee options still outstanding. Filers that grant only restricted stock —
# Apple, Microsoft and Nvidia among them — tag none of these, and that is a silence
# about options, not a zero: §5.1 applies here as everywhere.
OPTION_COUNT_TAGS = (
    "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber",
    "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsExercisableNumber",
)
# Restricted stock still to vest. A promise of shares rather than a right to buy
# them, so it dilutes whatever the price does — but the nonvested balance sits in a
# roll-forward table dimensioned by award type, which Company Facts drops and which
# Apple, Microsoft, Nvidia and Coca-Cola do not tag in the quarterly datasets either.
RSU_COUNT_TAGS = (
    "ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsNonvestedNumber",
    "ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsOutstandingNumber",
)
PREFERRED_COUNT_TAGS = ("PreferredStockSharesOutstanding", "PreferredStockSharesIssued")
_CONVERSION_OVERHANG = Decimal("0.05")  # same floor the warrant note uses
CONVERTIBLE_PREFERRED_SHARE_TAGS = (
    "ConvertiblePreferredStockSharesIssuedUponConversion",
    "PreferredStockConvertibleSharesIssuable",
    "ConvertiblePreferredStockSharesOutstanding",
)
INVENTORY_TAGS = ("InventoryNet",)
LEASE_OBLIGATION_TAGS = ("OperatingLeaseLiability",)
OPERATING_LEASE_CURRENT_TAGS = ("OperatingLeaseLiabilityCurrent",)
OPERATING_LEASE_NONCURRENT_TAGS = ("OperatingLeaseLiabilityNoncurrent",)
OPERATING_LEASE_ROU_ASSET_TAGS = ("OperatingLeaseRightOfUseAsset",)
LEASE_COST_TAGS = (
    "OperatingLeaseCost",
    "LeaseCost",
    "OperatingLeaseExpense",
    "RentExpense",
)
_DIVERGENCE = Decimal("1.30")   # this much faster than sales is worth saying
_DIVERGENCE_SPAN = 3            # years back to compare against
# The two post-ASU-2016-01 AFS successors and held-to-maturity go END of chain:
# PFE tags OtherShortTermInvestments as the total and the AFS tag as its
# footnote fragment. The dead pre-ASU AvailableForSaleSecuritiesCurrent is gone
# (zero fresh instants across the cache).
SHORT_TERM_INVESTMENT_TAGS = (
    "ShortTermInvestments",
    "MarketableSecuritiesCurrent",
    "OtherShortTermInvestments",
    "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    "DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent",
    "HeldToMaturitySecuritiesCurrent",
)
# These current-generation held-to-maturity elements do not have one safe
# economic meaning across filers. Vertiv prints the balance as a separate
# short-term-investment row, while Westlake uses the same family for securities
# with original maturities of at most three months that are already included in
# cash equivalents. Admit them only in exact statement contexts independently
# checked against the rendered annual report; otherwise subtracting them can
# double-count cash.
_MODERN_HTM_CURRENT_TAGS = (
    "DebtSecuritiesHeldToMaturityAmortizedCostAfterAllowanceForCreditLossCurrent",
    "DebtSecuritiesHeldToMaturityExcludingAccruedInterestAfterAllowanceForCreditLossCurrent",
)
_VERIFIED_MODERN_HTM_SHORT_TERM_INVESTMENT_CONTEXTS = {
    "0001674101": {
        "0001674101-26-000008": frozenset({
            date(2024, 12, 31), date(2025, 12, 31),
        }),  # Vertiv: separate Short-term investments balance-sheet row
    },
}
# One reported noncurrent-investment balance. Broader balance-sheet rollups
# outrank narrower fallbacks. These are alternatives, never additive: footnote
# categories frequently overlap the balance-sheet total. If none is filed,
# RONTA is withheld rather than treating an unknown non-operating asset as zero.
NONCURRENT_INVESTMENT_TAGS = (
    "OtherLongTermInvestments",
    "LongTermInvestments",
    "MarketableSecuritiesNoncurrent",
    "AvailableForSaleSecuritiesNoncurrent",
    "DebtSecuritiesAvailableForSaleNoncurrent",
    "HeldToMaturitySecuritiesNoncurrent",
    "EquityMethodInvestments",
)


def _annual_union(gaap: dict, tags: tuple[str, ...],
                  unit: tuple[str, ...] = ("USD",), *,
                  reconcile_scale: bool = True) -> dict[int, Fact]:
    """One annual series assembled from a chain of tags, earlier tags winning a year.

    Filers change tags mid-history and leave the abandoned one in place, so taking the
    first tag that has any data at all freezes the series at the year it was dropped.
    Filling year by year keeps the preferred tag where it exists and stays current.
    """
    out: dict[int, Fact] = {}
    for tag in tags:
        series = _annual_series(gaap, tag, unit=unit)
        if reconcile_scale and unit == ("USD",):
            series, _ = _reconcile_duration_scale(
                gaap, series, frozenset((tag,)))
        for year, fact in series.items():
            out.setdefault(year, fact)
    return out


def _annual_working_capital_cash_effect(
    gaap: dict, cik: str | None = None,
) -> dict[int, Fact]:
    """Return only a filing-proved, correctly signed working-capital effect.

    US-GAAP's operating-capital rollup is a change in the balance, not the
    displayed cash effect.  Coca-Cola's FY2025 filing, for example, tags a
    positive 7,208 million while its cash-flow statement prints (7,208).

    Some filers, including Johnson & Johnson, publish no rollup but do publish a
    complete component family. Company Facts omits issuer-extension rows, so a
    seemingly complete standard family is accepted only for exact SEC filing
    contexts already checked against the rendered statement. The same-period and
    overlap guards remain defence in depth within those contexts.
    """
    out: dict[int, Fact] = {}
    operating_capital = _annual_union(
        gaap, ("IncreaseDecreaseInOperatingCapital",))
    for year, source in operating_capital.items():
        out[year] = _derived_flow(
            "Working-capital cash effect (inverse of operating-capital change)",
            f"-({source.provenance.tag})", -source.value, (source,), year)

    # The non-SEC workbook adapter creates this canonical fact from already
    # signed source rows.  It therefore needs no sign inversion.
    for year, source in _annual_union(
            gaap, ("IncreaseDecreaseInOperatingAssetsAndLiabilities",)).items():
        out.setdefault(year, source)

    verified = _VERIFIED_WORKING_CAPITAL_COMPONENT_CONTEXTS.get(
        str(cik or "").zfill(10))
    if not verified:
        return out
    for accession, (verified_years, weights, permitted_other_tags) in verified.items():
        component_series = {
            tag: _annual_union(gaap, (tag,)) for tag, _weight in weights
        }
        candidate_years = set.intersection(*(
            set(series) for series in component_series.values())) & set(verified_years)
        component_tags = set(component_series)
        other_increase_decrease = {
            tag: _annual_union(gaap, (tag,))
            for tag in gaap
            if tag.startswith("IncreaseDecreaseIn") and tag not in component_tags
            and tag not in WORKING_CAPITAL_CASH_EFFECT_TAGS
            and tag not in permitted_other_tags
        }
        for year in sorted(candidate_years):
            if year in out:
                continue
            weighted = [
                (component_series[tag][year], weight) for tag, weight in weights
            ]
            sources = tuple(source for source, _weight in weighted)
            first = sources[0].provenance
            if first.accession != accession:
                continue
            if any(
                source.provenance.accession != first.accession
                or source.provenance.form != first.form
                or source.provenance.period_start != first.period_start
                or source.provenance.period_end != first.period_end
                for source in sources[1:]
            ):
                continue
            if any(
                (extra := series.get(year)) is not None
                and extra.provenance.accession == first.accession
                and extra.provenance.period_start == first.period_start
                and extra.provenance.period_end == first.period_end
                for series in other_increase_decrease.values()
            ):
                continue
            expression = " + ".join(
                (f"-({source.provenance.tag})" if weight < 0
                 else source.provenance.tag)
                for source, weight in weighted)
            out[year] = _derived_flow(
                "Working-capital cash effect (complete reported component family)",
                expression,
                sum((source.value * weight for source, weight in weighted), Decimal(0)),
                sources, year)
    return out


_DURATION_SCALE_FACTORS = (Decimal("1000"), Decimal("1000000"))
_DURATION_SCALE_NEIGHBOR_LIMIT = Decimal("10")
# These accessions were read against their rendered statements. The filed table
# heading applies a thousands/millions scale that contradicts the visible digits
# and earlier reports, so latest-filed-wins would corrupt every comparative row.
# Detection remains generic; mutation is limited to reviewed source documents.
_VERIFIED_PRESENTATION_SCALE_ACCESSIONS = frozenset({
    "0001213900-25-013985",  # Golden Sun FY2024 20-F; FY2022/FY2023 comparatives
    # Identiv's FY2023 10-K/A carries FY2021/FY2022 net income one million
    # times above both the original filing and the next two identical annual
    # comparatives.  The amendment contains no replacement income statement.
    "0001193125-24-122187",
})


def _scale_ratio(left: Decimal, right: Decimal) -> Decimal | None:
    """Unsigned ratio between two nonzero values, always at least one."""
    left, right = abs(left), abs(right)
    if not left or not right:
        return None
    return max(left, right) / min(left, right)


def _reconcile_duration_scale(
    gaap: dict, series: dict[int, Fact], allowed_tags: frozenset[str], *,
    allow_unverified_adjacent: bool = False,
    unit: tuple[str, ...] = ("USD",),
) -> tuple[dict[int, Fact], list[tuple[int, Decimal]]]:
    """Retire an isolated, exactly scaled later comparative only when proved.

    First US Bancshares' FY2023 D&A and Golden Sun's FY2022 income statement
    illustrate why latest-filed-wins needs one narrow exception. A later filing
    can repeat the exact visible comparative digits under a mistaken thousand-
    scale header, making SEC's machine value exactly 1,000x the original filing.

    Unusually small real cash flows must survive (IMMR FY2020 and ASLE FY2022).
    Consequently this never scales a lone fact and never infers a number from
    company size.  It retains an earlier *reported* value only when the same tag
    and same annual period disagree by exactly 1,000x or 1,000,000x, the earlier
    value is within one order of magnitude of both adjacent fiscal years while
    the later value is not, or two distinct earlier annual filings repeat that
    exact value. The general path is additionally gated to a rendered statement
    accession already reviewed in ``_VERIFIED_PRESENTATION_SCALE_ACCESSIONS``;
    the older D&A-only guard may use its established adjacent-year proof. Ordinary
    restatements remain latest-filed-wins.
    """
    out = dict(series)
    repairs: list[tuple[int, Decimal]] = []
    for year, current in sorted(series.items()):
        before, after = series.get(year - 1), series.get(year + 1)
        tag = _tag_of(current)
        if ("(scaled " in current.provenance.concept
                or tag not in allowed_tags
                or current.provenance.period_start is None
                or current.provenance.period_end is None):
            continue
        start = current.provenance.period_start.isoformat()
        end = current.provenance.period_end.isoformat()
        candidates = sorted(
            (
                entry for entry in _entries(gaap, tag, unit)
                if entry.get("start") == start
                and entry.get("end") == end
                and _is_annual_form(entry.get("form", ""))
                and _days(entry) in _ANNUAL_DAYS
                and entry.get("filed", "") < current.provenance.filed.isoformat()
            ),
            key=lambda entry: (entry.get("filed", ""), entry.get("accn", "")),
            reverse=True,
        )
        for entry in candidates:
            prior = _fact(tag, tag, entry, fiscal_year=year)
            if prior.value * current.value <= 0:
                continue
            conflict = _scale_ratio(prior.value, current.value)
            repeated_entry = next((
                other for other in candidates
                if other.get("accn") != entry.get("accn")
                and _fact(tag, tag, other, fiscal_year=year).value == prior.value
            ), None)
            adjacent_proof = False
            if before is not None and after is not None:
                prior_before = _scale_ratio(prior.value, before.value)
                prior_after = _scale_ratio(prior.value, after.value)
                current_before = _scale_ratio(current.value, before.value)
                current_after = _scale_ratio(current.value, after.value)
                adjacent_proof = (
                    prior_before is not None and prior_after is not None
                    and current_before is not None and current_after is not None
                    and prior_before <= _DURATION_SCALE_NEIGHBOR_LIMIT
                    and prior_after <= _DURATION_SCALE_NEIGHBOR_LIMIT
                    and current_before > _DURATION_SCALE_NEIGHBOR_LIMIT
                    and current_after > _DURATION_SCALE_NEIGHBOR_LIMIT
                )
            verified_source = (
                current.provenance.accession
                in _VERIFIED_PRESENTATION_SCALE_ACCESSIONS)
            if (conflict not in _DURATION_SCALE_FACTORS
                    or (not adjacent_proof and repeated_entry is None)
                    or (not verified_source
                        and not (allow_unverified_adjacent
                                 and adjacent_proof))):
                continue
            p = prior.provenance
            proof = (
                "corroborated by adjacent fiscal years" if adjacent_proof else
                "repeated identically in two earlier annual filings")
            components = [p, current.provenance]
            if adjacent_proof:
                components.extend((before.provenance, after.provenance))
            elif repeated_entry is not None:
                components.append(
                    _fact(tag, tag, repeated_entry, fiscal_year=year).provenance)
            out[year] = Fact(
                value=prior.value,
                provenance=Provenance(
                    concept=(
                        f"{p.concept} (earlier same-period fact retained; later "
                        f"comparative had an exact {conflict:.0f}x presentation-scale "
                        f"contradiction {proof})"
                    ),
                    tag=p.tag, fiscal_year=p.fiscal_year, form=p.form,
                    accession=p.accession, filed=p.filed,
                    period_end=p.period_end, period_start=p.period_start,
                    components=tuple(components),
                    segments=p.segments, unit=p.unit, document=p.document,
                    canonical_tag=p.canonical_tag,
                ),
            )
            repairs.append((year, conflict))
            break
    return dict(sorted(out.items())), repairs


def _reconcile_depreciation_scale(
    gaap: dict, series: dict[int, Fact],
) -> tuple[dict[int, Fact], list[tuple[int, Decimal]]]:
    """Compatibility wrapper for the owner-earnings D&A caveat."""
    return _reconcile_duration_scale(
        gaap, series, frozenset(DA_TAGS), allow_unverified_adjacent=True)


def _annual_instant_series(
    gaap: dict, tag: str, unit: tuple[str, ...] = ("USD",),
    labels: dict[str, int] | None = None,
) -> dict[int, Fact]:
    """Latest annual-report observation for each exact balance-sheet date."""
    labels = _fiscal_labels(gaap) if labels is None else labels
    out: dict[int, Fact] = {}
    ranks: dict[int, tuple[str, bool, str]] = {}
    for entry in _entries(gaap, tag, unit):
        if ("start" in entry
                or not entry.get("form", "").startswith(ANNUAL_FORMS)):
            continue
        anchored = entry["end"] in labels
        year = labels.get(entry["end"]) or _fy_label(
            date.fromisoformat(entry["end"]))
        # A fresh-start annual report can carry three balance sheets in one
        # accession: the predecessor close, the successor opening balance one
        # day later, and the actual fiscal-year end.  All have the same form,
        # ``fy`` and filed date.  Iteration order used to let the first March
        # reorganisation date permanently occupy FY2025 ahead of December 31,
        # mixing a full-year loss with a nine-month-old capital base.  A date
        # corroborated by the company-wide fiscal calendar is the statement
        # year-end; fallbacks remain available for sparse balance-only histories.
        rank = (entry.get("filed", ""), anchored, entry["end"])
        if year not in out or rank > ranks[year]:
            out[year] = _fact(tag, tag, entry, fiscal_year=year)
            ranks[year] = rank
    return dict(sorted(out.items()))


def _reconcile_instant_scale(
    gaap: dict, series: dict[int, Fact], allowed_tags: frozenset[str],
    unit: tuple[str, ...] = ("USD",),
) -> tuple[dict[int, Fact], list[tuple[int, Decimal]]]:
    """Retain an earlier balance for a reviewed later comparative scale defect."""
    out = dict(series)
    repairs: list[tuple[int, Decimal]] = []
    for year, current in sorted(series.items()):
        before, after = series.get(year - 1), series.get(year + 1)
        tag = _tag_of(current)
        if tag not in allowed_tags or current.provenance.period_end is None:
            continue
        end = current.provenance.period_end.isoformat()
        candidates = sorted(
            (
                entry for entry in _entries(gaap, tag, unit)
                if "start" not in entry
                and entry.get("end") == end
                and _is_annual_form(entry.get("form", ""))
                and entry.get("filed", "") < current.provenance.filed.isoformat()
            ),
            key=lambda entry: (entry.get("filed", ""), entry.get("accn", "")),
            reverse=True,
        )
        for entry in candidates:
            prior = _fact(tag, tag, entry, fiscal_year=year)
            if prior.value * current.value <= 0:
                continue
            conflict = _scale_ratio(prior.value, current.value)
            repeated_entry = next((
                other for other in candidates
                if other.get("accn") != entry.get("accn")
                and _fact(tag, tag, other, fiscal_year=year).value == prior.value
            ), None)
            adjacent_proof = False
            if before is not None and after is not None:
                prior_before = _scale_ratio(prior.value, before.value)
                prior_after = _scale_ratio(prior.value, after.value)
                current_before = _scale_ratio(current.value, before.value)
                current_after = _scale_ratio(current.value, after.value)
                adjacent_proof = (
                    prior_before is not None and prior_after is not None
                    and current_before is not None and current_after is not None
                    and prior_before <= _DURATION_SCALE_NEIGHBOR_LIMIT
                    and prior_after <= _DURATION_SCALE_NEIGHBOR_LIMIT
                    and current_before > _DURATION_SCALE_NEIGHBOR_LIMIT
                    and current_after > _DURATION_SCALE_NEIGHBOR_LIMIT
                )
            if (conflict not in _DURATION_SCALE_FACTORS
                    or (not adjacent_proof and repeated_entry is None)
                    or current.provenance.accession
                    not in _VERIFIED_PRESENTATION_SCALE_ACCESSIONS):
                continue
            p = prior.provenance
            proof = (
                "corroborated by adjacent annual balances" if adjacent_proof else
                "repeated identically in two earlier annual filings"
            )
            components = [p, current.provenance]
            if adjacent_proof:
                components.extend((before.provenance, after.provenance))
            else:
                components.append(
                    _fact(tag, tag, repeated_entry, fiscal_year=year).provenance)
            out[year] = Fact(
                value=prior.value,
                provenance=Provenance(
                    concept=(
                        f"{p.concept} (earlier same-date balance retained; later "
                        f"comparative had an exact {conflict:.0f}x presentation-scale "
                        f"contradiction {proof})"
                    ),
                    tag=p.tag, fiscal_year=year, form=p.form,
                    accession=p.accession, filed=p.filed,
                    period_end=p.period_end, period_start=None,
                    components=tuple(components), segments=p.segments,
                    unit=p.unit, document=p.document,
                    canonical_tag=p.canonical_tag,
                ),
            )
            repairs.append((year, conflict))
            break
    return dict(sorted(out.items())), repairs


def _annual_extension_depreciation(
    dimensioned: dict | None,
) -> dict[int, Fact]:
    """Unsegmented cash-flow D&A from explicitly admitted issuer extensions.

    Extension namespaces are accession-specific in the DERA datasets. Merge
    their allowlisted rows into one tag view, retain the real namespace in each
    entry for provenance, and let the ordinary annual-period rules choose the
    latest filing. Segment/geography rows remain excluded.
    """
    facts = (dimensioned or {}).get("facts", {}) or {}
    merged: dict[str, dict] = {}
    for namespace, taxo in facts.items():
        if not namespace.startswith("ext:") or namespace.startswith("ext:ifrs/"):
            continue
        for tag in DA_EXTENSION_TAGS:
            source = taxo.get(tag)
            if not source:
                continue
            target = merged.setdefault(tag, {"units": {}})["units"]
            for unit, entries in (source.get("units") or {}).items():
                target.setdefault(unit, []).extend(
                    {**entry, "_source_namespace": namespace}
                    for entry in entries
                    if not (entry.get("segments") or "")
                )
    if not merged:
        return {}
    return _annual_union(_with_fiscal_calendar(merged), DA_EXTENSION_TAGS)


def _common_owner_earnings(gaap: dict) -> dict[int, Fact]:
    """Annual earnings belonging to the common security being screened.

    A direct income-available-to-common line is the closest numerator. Parent
    net income is only the fallback, and a same-period preferred-dividend fact is
    then deducted because that claim never reaches the common. This is the same
    scope correction used by the EPS derivation, now applied to owner evidence.
    """
    common = _annual_union(gaap, COMMON_INCOME_TAGS)
    parent = _annual_union(gaap, ("NetIncomeLoss", "ProfitLoss"))
    preferred = _annual_union(gaap, PREFERRED_DIVIDEND_TAGS)
    cik = str(getattr(gaap, "cik", "")).zfill(10)
    for year, direct in tuple(common.items()):
        fallback = parent.get(year)
        if ((cik, direct.provenance.accession, year)
                not in _VERIFIED_COMMON_INCOME_SCALE_CONFLICTS
                or fallback is None
                or fallback.provenance.accession != direct.provenance.accession
                or fallback.provenance.period_end != direct.provenance.period_end
                or _scale_ratio(direct.value, fallback.value)
                not in _DURATION_SCALE_FACTORS):
            continue
        # Let the ordinary parent-income fallback below carry the real filing
        # provenance.  Keeping a rescaled malformed tag would make the source
        # look stronger than the statement actually is.
        del common[year]
    out = dict(common)
    for year, income in parent.items():
        if year in out:
            continue
        claim = preferred.get(year)
        if (claim is None or claim.value == 0
                or claim.provenance.period_end != income.provenance.period_end):
            out[year] = income
            continue
        latest = max((income, claim), key=lambda fact: fact.provenance.filed).provenance
        out[year] = Fact(
            value=income.value - abs(claim.value),
            provenance=Provenance(
                concept="Net income available to common (parent income less preferred claims)",
                tag=f"{income.provenance.tag} - {claim.provenance.tag}",
                fiscal_year=year, form=latest.form, accession=latest.accession,
                filed=latest.filed, period_end=income.provenance.period_end,
                period_start=income.provenance.period_start,
                components=(income.provenance, claim.provenance),
            ),
        )
    return dict(sorted(out.items()))


def _taxonomy_at_end(taxo: dict, end: date | None, *, annual_only: bool = False) -> dict:
    """A taxonomy view containing instants at exactly one balance-sheet date.

    Historical ratios use ``annual_only`` so that a later 10-Q carrying the prior
    year-end comparative balance does not replace the annual-report evidence.
    """
    if end is None:
        return {}
    wanted = end.isoformat()
    out = {}
    for tag, data in taxo.items():
        units = {
            unit: [entry for entry in entries
                   if ("start" not in entry and entry.get("end") == wanted
                       and (not annual_only
                            or entry.get("form", "").startswith(ANNUAL_FORMS)))]
            for unit, entries in (data.get("units", {}) or {}).items()
        }
        units = {unit: entries for unit, entries in units.items() if entries}
        if units:
            out[tag] = {**data, "units": units}
    if annual_only:
        # A later annual report can carry a prior-year balance under a mistaken
        # thousands/millions header. Replace only a proved conflict with the
        # earlier exact-date filing; the Fact override retains both versions and
        # the independent scale proof in provenance for downstream calculations.
        for tag, data in out.items():
            for unit, selected in list(data["units"].items()):
                if unit == "shares" or unit.endswith("/shares"):
                    continue
                latest_entry = max(
                    selected,
                    key=lambda entry: entry.get("filed", ""),
                )
                latest_value = _dec(latest_entry.get("val"))
                if not any(
                    (prior_value := _dec(entry.get("val"))) * latest_value > 0
                    and _scale_ratio(prior_value, latest_value)
                    in _DURATION_SCALE_FACTORS
                    for entry in selected
                    if entry is not latest_entry
                    and entry.get("filed", "") < latest_entry.get("filed", "")
                ):
                    continue
                series = _annual_instant_series(taxo, tag, unit=(unit,))
                reconciled, repairs = _reconcile_instant_scale(
                    taxo, series, frozenset((tag,)), unit=(unit,))
                repair_years = {year for year, _factor in repairs}
                matched = next((
                    (year, fact) for year, fact in reconciled.items()
                    if year in repair_years
                    and fact.provenance.period_end == end
                ), None)
                if matched is None:
                    continue
                _year, fact = matched
                source = next((
                    entry for entry in _entries(taxo, tag, (unit,))
                    if "start" not in entry
                    and entry.get("end") == wanted
                    and entry.get("accn", "") == fact.provenance.accession
                    and entry.get("filed", "") == fact.provenance.filed.isoformat()
                    and _dec(entry.get("val")) == fact.value
                ), None)
                if source is not None:
                    data["units"][unit] = [{**source, "_fact_override": fact}]
    wrapped = _FiscalTaxonomy(out)
    _copy_taxonomy_context(wrapped, taxo)
    return wrapped


_ASSUMED_ZERO_LABELS = {
    "operating_income": "operating income",
    "tax_rate": "effective income-tax rate",
    "total_assets": "total assets",
    "current_liabilities": "current liabilities",
    "short_term_debt": "short-term debt",
    "cash": "cash and cash equivalents",
    "restricted_cash": "restricted cash included in the cash rollup",
    "short_term_investments": "short-term investments",
    "goodwill": "goodwill",
    "intangibles": "other intangible assets",
    "noncurrent_investments": "noncurrent investments",
    "operating_lease_liability_current": "current operating lease liability",
    "operating_lease_rou_asset": "operating lease right-of-use asset",
}


def _assumed_zero_fact(
    field: str,
    end: date,
    caveats: list[str],
    assumed_zero: set[str] | None,
    *,
    fiscal_year: int | None = None,
    start: date | None = None,
) -> Fact:
    """A visibly synthetic zero for the explicit detail-only assumption mode."""
    label = _ASSUMED_ZERO_LABELS.get(field, field.replace("_", " "))
    when = f"FY{fiscal_year}" if fiscal_year is not None else end.isoformat()
    message = (
        f"{label} was not reported in recognized source evidence for {when}; it was assumed "
        "to be 0 by the explicit assume_absent_zero detail mode"
    )
    if message not in caveats:
        caveats.append(message)
    if assumed_zero is not None:
        assumed_zero.add(field)
    return Fact(
        value=Decimal(0),
        provenance=Provenance(
            concept=f"{label.title()} (assumed zero; not filing-reported)",
            tag=f"assumed-zero:{field}",
            fiscal_year=fiscal_year,
            form="ASSUMED_ZERO",
            accession="",
            filed=end,
            period_end=end,
            period_start=start,
        ),
    )


def _verified_modern_htm_short_term_investment(
    exact: dict, cik: str | None, end: date,
) -> Fact | None:
    """Return a modern HTM balance only where its statement role was verified."""
    verified = _VERIFIED_MODERN_HTM_SHORT_TERM_INVESTMENT_CONTEXTS.get(
        str(cik or "").zfill(10))
    if not verified:
        return None
    wanted = end.isoformat()
    for tag in _MODERN_HTM_CURRENT_TAGS:
        candidates = [
            entry for entry in _entries(exact, tag, ("USD",))
            if "start" not in entry and entry.get("end") == wanted
            and end in verified.get(entry.get("accn", ""), frozenset())
            and _is_financial_form(entry.get("form", ""))
        ]
        if candidates:
            # A later quarterly comparative can repeat this date, but the
            # allowlist is evidence about the audited statement accession. Keep
            # that exact source rather than letting the later filing replace it.
            entry = max(candidates, key=lambda item: item.get("filed", ""))
            return _fact("ShortTermInvestments", tag, entry)
    return None


def _capital_fact(gaap: dict, end: date | None, exclude_cash: bool,
                  caveats: list[str], *,
                  cik: str | None = None,
                  assume_absent_debt_zero: bool = False,
                  assume_absent_optional_zero: bool = False,
                  assume_absent_all_zero: bool = False,
                  assumed_zero: set[str] | None = None,
                  assumable_zero: set[str] | None = None,
                  exact: dict | None = None,
                  annual_only: bool = False,
                  conservative_estimate: bool = False) -> Fact | None:
    """Invested capital at one exact date, retaining every filed input."""
    exact = (_taxonomy_at_end(gaap, end, annual_only=annual_only)
             if exact is None else exact)
    absent_source = ("exact-date imported annual workbook row"
                     if getattr(exact, "canonical_adapter", False)
                     else "exact-date annual XBRL fact")
    if end is None or (not exact and not assume_absent_all_zero):
        return None
    assets = _latest_instant(
        exact, "Assets", ("Assets", "LiabilitiesAndStockholdersEquity"),
        not_before=end,
    )
    current_liabilities = _latest_instant(
        exact, "LiabilitiesCurrent", ("LiabilitiesCurrent",), not_before=end,
    )
    if current_liabilities is None:
        current_liabilities = _derived_instant(
            exact, "LiabilitiesCurrent (derived: Liabilities - LiabilitiesNoncurrent)",
            "Liabilities", "LiabilitiesNoncurrent", end,
        )
    if assets is None and assume_absent_all_zero:
        assets = _assumed_zero_fact(
            "total_assets", end, caveats, assumed_zero)
    if current_liabilities is None and assume_absent_all_zero:
        current_liabilities = _assumed_zero_fact(
            "current_liabilities", end, caveats, assumed_zero)
    if (assets is None or current_liabilities is None or assets.value < 0
            or (assets.value == 0 and not assume_absent_all_zero)):
        return None

    # NIBCL needs the current portion itself. The total-debt reconciler suppresses
    # that portion when a LongTermDebt rollup already includes it, which is right
    # for avoiding double-counted total debt but wrong for this classification.
    short_debt = _short_term_debt(exact, end)
    if short_debt is not None and short_debt.value > current_liabilities.value:
        short_debt = None
    if short_debt is None:
        can_assume = ((assume_absent_debt_zero or assume_absent_all_zero)
                      and (assumable_zero is None
                           or "short_term_debt" in assumable_zero
                           or assume_absent_all_zero))
        if not can_assume:
            caveats.append(
                f"no short-term-debt fact was available at {end.isoformat()}, so "
                "non-interest-bearing current liabilities and invested capital are withheld")
            return None
        caveats.append(
            f"short-term debt was assumed to be 0 at {end.isoformat()} because no "
            f"{absent_source} reports that bucket; this is the explicit "
            "assume_absent_zero opt-in")
        if assumed_zero is not None:
            assumed_zero.add("short_term_debt")
    nibcl = current_liabilities.value - (short_debt.value if short_debt else Decimal(0))
    value = assets.value - max(nibcl, Decimal(0))
    components = [assets, current_liabilities]
    expression = f"{assets.provenance.tag} - non-interest-bearing current liabilities"
    if short_debt is not None:
        components.append(short_debt)
        expression += f" ({current_liabilities.provenance.tag} - {short_debt.provenance.tag})"
    else:
        expression += f" ({current_liabilities.provenance.tag} - assumed absent debt 0)"

    if exclude_cash:
        cash = _latest_instant(exact, "Cash", CASH_TAGS, not_before=end)
        investments = _latest_instant(
            exact, "ShortTermInvestments",
            (*SHORT_TERM_INVESTMENT_TAGS, "AvailableForSaleSecuritiesCurrent")
            if annual_only else SHORT_TERM_INVESTMENT_TAGS,
            not_before=end)
        if investments is None:
            investments = _verified_modern_htm_short_term_investment(
                exact, cik, end)
        if annual_only and investments is None and cash is not None:
            cash_and_investments = _latest_instant(
                exact, "Cash and short-term investments",
                ("CashCashEquivalentsAndShortTermInvestments",), not_before=end)
            if (cash_and_investments is not None
                    and cash_and_investments.value >= cash.value):
                investments = Fact(
                    value=cash_and_investments.value - cash.value,
                    provenance=Provenance(
                        concept="Short-term investments (derived from cash rollup)",
                        tag=(f"{cash_and_investments.provenance.tag} - "
                             f"{cash.provenance.tag}"),
                        fiscal_year=None,
                        form=cash_and_investments.provenance.form,
                        accession=cash_and_investments.provenance.accession,
                        filed=max(cash_and_investments.provenance.filed,
                                  cash.provenance.filed),
                        period_end=end,
                        components=(cash_and_investments.provenance,
                                    cash.provenance),
                    ),
                )
        if cash is None and assume_absent_all_zero:
            cash = _assumed_zero_fact("cash", end, caveats, assumed_zero)
        can_assume_investments = (
            investments is None
            and (assume_absent_optional_zero or assume_absent_all_zero)
            and (assumable_zero is None
                 or "short_term_investments" in assumable_zero
                 or assume_absent_all_zero))
        if cash is None or (investments is None and not can_assume_investments):
            missing = []
            if cash is None:
                missing.append("cash")
            if investments is None:
                missing.append("short-term-investment")
            caveats.append(
                f"no exact-date {' or '.join(missing)} fact was available at "
                f"{end.isoformat()}, so cash-excluded invested capital is withheld")
            return None
        if investments is None:
            caveats.append(
                (f"short-term investments were conservatively bounded at 0 at "
                 f"{end.isoformat()} because no {absent_source} reports them; "
                 "the deduction's zero lower bound maximizes invested capital "
                 "and therefore produces a lower-bound return estimate")
                if conservative_estimate else
                (f"short-term investments were assumed to be 0 at {end.isoformat()} "
                 f"because no {absent_source} reports them; this is the "
                 "explicit assume_absent_zero opt-in"))
            if assumed_zero is not None:
                assumed_zero.add("short_term_investments")
        if (cash is not None
                and _tag_of(cash) == "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"):
            restricted = _restricted_cash(exact, end, end)
            if (restricted is None or restricted.value < 0
                    or restricted.value > cash.value):
                if not assume_absent_all_zero:
                    caveats.append(
                        f"the cash balance at {end.isoformat()} includes restricted cash "
                        "but no valid exact-date restricted portion was filed, so "
                        "cash-excluded invested capital is withheld")
                    return None
                _assumed_zero_fact(
                    "restricted_cash", end, caveats, assumed_zero)
                restricted = None
            if restricted is not None and restricted.value > 0:
                original = cash
                cash = Fact(value=cash.value - restricted.value, provenance=Provenance(
                    concept="Cash (restricted portion netted out)",
                    tag=f"{cash.provenance.tag} - {restricted.provenance.tag}",
                    fiscal_year=None, form=cash.provenance.form,
                    accession=cash.provenance.accession, filed=cash.provenance.filed,
                    period_end=end, components=(original.provenance, restricted.provenance),
                ))
                caveats.append(
                    f"the cash rollup at {end.isoformat()} includes restricted cash; "
                    "the filed restricted portion was excluded from deployable cash")
        value -= cash.value
        components.append(cash)
        expression += f" - {cash.provenance.tag}"
        if investments is not None:
            value -= investments.value
            components.append(investments)
            expression += f" - {investments.provenance.tag}"
        else:
            expression += " - assumed absent short-term investments 0"

    latest = max(components, key=lambda fact: fact.provenance.filed).provenance
    return Fact(
        value=value,
        provenance=Provenance(
            concept=("Invested capital excluding all cash and short-term investments"
                     if exclude_cash else "Invested capital including cash and investments"),
            tag=expression, fiscal_year=None, form=latest.form,
            accession=latest.accession, filed=latest.filed, period_end=end,
            components=tuple(fact.provenance for fact in components),
        ),
    )


def _first_operating_lease_recognition_end(gaap: dict) -> date | None:
    """First annual balance sheet with a positive operating-lease ROU asset."""
    ends = []
    for tag in OPERATING_LEASE_ROU_ASSET_TAGS:
        ends.extend(
            date.fromisoformat(entry["end"])
            for entry in _entries(gaap, tag, ("USD",))
            if ("start" not in entry
                and _is_annual_form(entry.get("form", ""))
                and _dec(entry.get("val", 0)) > 0)
        )
    return min(ends) if ends else None


def _current_operating_lease_liability(
    exact: dict, end: date,
) -> Fact | None:
    """Exact current lease liability, direct or total less noncurrent."""
    direct = _latest_instant(
        exact, "Current operating lease liability",
        OPERATING_LEASE_CURRENT_TAGS, not_before=end)
    if direct is not None:
        return direct if direct.value >= 0 else None

    total = _latest_instant(
        exact, "Operating lease liability", LEASE_OBLIGATION_TAGS,
        not_before=end)
    noncurrent = _latest_instant(
        exact, "Noncurrent operating lease liability",
        OPERATING_LEASE_NONCURRENT_TAGS, not_before=end)
    if (total is None or noncurrent is None
            or total.value < noncurrent.value or noncurrent.value < 0):
        return None
    latest = max((total, noncurrent),
                 key=lambda fact: fact.provenance.filed).provenance
    return Fact(
        value=total.value - noncurrent.value,
        provenance=Provenance(
            concept="Current operating lease liability (derived)",
            tag=f"{total.provenance.tag} - {noncurrent.provenance.tag}",
            fiscal_year=None, form=latest.form,
            accession=latest.accession, filed=latest.filed,
            period_end=end,
            components=(total.provenance, noncurrent.provenance),
        ),
    )


def _net_tangible_operating_assets_fact(
    gaap: dict, end: date | None, caveats: list[str], *,
    cik: str | None = None,
    assume_absent_debt_zero: bool = False,
    assume_absent_optional_zero: bool = False,
    assume_absent_all_zero: bool = False,
    assumed_zero: set[str] | None = None,
    assumable_zero: set[str] | None = None,
    exact: dict | None = None,
    annual_only: bool = False,
    lease_neutral: bool = False,
    conservative_estimate: bool = False,
) -> Fact | None:
    """NTOA at one exact balance-sheet date, with every deduction evidenced.

    NTOA = assets - goodwill - other intangibles - deployable cash - short-term
    investments - noncurrent investments - non-interest-bearing current
    liabilities. A separately reported current operating-lease liability is kept
    in capital, matching the financing treatment already applied to its noncurrent
    counterpart. The lease-neutral comparison then removes the recognized ROU
    asset to reproduce the pre-ASC-842 balance-sheet presentation; it does not
    reconstruct leases that were off balance sheet. Noncurrent operating
    liabilities cannot be identified comprehensively from primary XBRL and are
    therefore not silently guessed.
    """
    # This base already reconciles current liabilities with current debt and
    # removes deployable cash and short-term investments at the exact date.
    operating = _capital_fact(
        gaap, end, True, caveats,
        cik=cik,
        assume_absent_debt_zero=assume_absent_debt_zero,
        assume_absent_optional_zero=assume_absent_optional_zero,
        assume_absent_all_zero=assume_absent_all_zero,
        assumed_zero=assumed_zero, assumable_zero=assumable_zero,
        exact=exact, annual_only=annual_only,
        conservative_estimate=conservative_estimate)
    if operating is None or end is None:
        return None
    exact = (_taxonomy_at_end(gaap, end, annual_only=annual_only)
             if exact is None else exact)
    absent_source = ("exact-date imported annual workbook row"
                     if getattr(exact, "canonical_adapter", False)
                     else "exact-date annual XBRL fact")
    goodwill, intangibles = _goodwill_and_intangibles(exact, end)
    noncurrent_investments = _latest_instant(
        exact, "Noncurrent investments", NONCURRENT_INVESTMENT_TAGS,
        not_before=end,
    )
    lease_recognition_end = _first_operating_lease_recognition_end(gaap)
    lease_current = _current_operating_lease_liability(exact, end)
    rou_asset = _latest_instant(
        exact, "Operating lease right-of-use asset",
        OPERATING_LEASE_ROU_ASSET_TAGS, not_before=end)
    recognition_applies = (
        lease_recognition_end is not None and end >= lease_recognition_end)
    if recognition_applies and lease_current is None:
        if not assume_absent_all_zero:
            caveats.append(
                f"an operating-lease ROU asset is in the annual filing history, "
                f"but no exact-date current operating lease liability was available "
                f"at {end.isoformat()}; lease-consistent NTOA and RONTA are withheld")
            return None
        lease_current = _assumed_zero_fact(
            "operating_lease_liability_current", end, caveats, assumed_zero)
    if lease_neutral:
        # Do not add a second all-dash ratio to companies whose annual record has
        # never recognized an on-balance-sheet operating-lease asset.
        if lease_recognition_end is None:
            if lease_current is not None:
                caveats.append(
                    "a current operating lease liability is reported without a "
                    "recognized operating-lease ROU asset history, so lease-neutral "
                    "RONTA is withheld")
            return None
        if recognition_applies and rou_asset is None:
            if not assume_absent_all_zero:
                caveats.append(
                    f"no exact-date operating-lease ROU asset was available at "
                    f"{end.isoformat()}, so lease-neutral NTOA and RONTA are withheld")
                return None
            rou_asset = _assumed_zero_fact(
                "operating_lease_rou_asset", end, caveats, assumed_zero)
    missing = [
        label for label, fact in (
            ("goodwill", goodwill),
            ("other intangible assets", intangibles),
            ("noncurrent investments", noncurrent_investments),
        ) if fact is None
    ]
    names = {
        "goodwill": "goodwill",
        "other intangible assets": "intangibles",
        "noncurrent investments": "noncurrent_investments",
    }
    blocked = [label for label in missing
               if (not (assume_absent_optional_zero or assume_absent_all_zero)
                   or (assumable_zero is not None
                       and names[label] not in assumable_zero
                       and not assume_absent_all_zero))]
    if blocked:
        caveats.append(
            f"no exact-date {' or '.join(blocked)} fact was available at "
            f"{end.isoformat()}, so net tangible operating assets and RONTA are withheld"
        )
        return None
    if missing:
        for label in missing:
            caveats.append(
                (f"{label} was conservatively bounded at 0 at {end.isoformat()} "
                 f"because no {absent_source} reports it; the deduction's zero "
                 "lower bound maximizes net tangible operating assets and "
                 "therefore produces a lower-bound return estimate")
                if conservative_estimate else
                (f"{label} was assumed to be 0 at {end.isoformat()} because no "
                 f"{absent_source} reports it; this is the explicit "
                 "assume_absent_zero opt-in"))
            if assumed_zero is not None:
                assumed_zero.add(names[label])
    deductions = tuple(fact for fact in
                       (goodwill, intangibles, noncurrent_investments)
                       if fact is not None)
    if any(fact.value < 0 for fact in deductions):
        caveats.append(
            f"a tangible/non-operating deduction was negative at {end.isoformat()}, "
            "so net tangible operating assets and RONTA are withheld"
        )
        return None
    value = operating.value - sum((fact.value for fact in deductions), Decimal(0))
    adjustments: list[Fact] = []
    if (lease_current is not None
            and (lease_current.value != 0 or recognition_applies)):
        value += lease_current.value
        adjustments.append(lease_current)
        caveats.append(
            "RONTA classifies the reported current operating lease liability as "
            "financing, so neither its current nor noncurrent portion is deducted "
            "as a non-interest-bearing operating liability")
    if lease_neutral and recognition_applies and rou_asset is not None:
        if rou_asset.value < 0:
            caveats.append(
                f"the operating-lease ROU asset was negative at {end.isoformat()}, "
                "so lease-neutral NTOA and RONTA are withheld")
            return None
        value -= rou_asset.value
        adjustments.append(rou_asset)
        caveats.append(
            "lease-neutral RONTA removes reported operating-lease ROU assets after "
            "reversing the current lease-liability deduction; pre-recognition years "
            "are unchanged and off-balance-sheet leases are not reconstructed")
    if value <= 0:
        if not assume_absent_all_zero:
            caveats.append(
                f"net tangible operating assets were not positive at {end.isoformat()}, "
                "so RONTA is withheld"
            )
            return None
        caveats.append(
            f"net tangible operating assets were {value} at {end.isoformat()}; "
            "the detail-only assumption mode shows the arithmetic, but a non-positive "
            "operating-capital denominator is not economically comparable")
    latest = max((operating, *deductions, *adjustments),
                 key=lambda fact: fact.provenance.filed).provenance
    deduction_tags = [fact.provenance.tag for fact in deductions]
    deduction_tags.extend(f"assumed absent {label} 0" for label in missing)
    expression = " - ".join((f"({operating.provenance.tag})", *deduction_tags))
    if (lease_current is not None
            and (lease_current.value != 0 or recognition_applies)):
        expression += f" + {lease_current.provenance.tag}"
    if lease_neutral and recognition_applies and rou_asset is not None:
        expression += f" - {rou_asset.provenance.tag}"
    # Flatten the cash-excluded capital inputs so provenance names the original
    # filed figures rather than only an intermediate derived balance.
    operating_sources = operating.provenance.components or (operating.provenance,)
    return Fact(
        value=value,
        provenance=Provenance(
            concept=("Lease-neutral net tangible operating assets"
                     if lease_neutral else "Net tangible operating assets"),
            tag=expression, fiscal_year=None, form=latest.form,
            accession=latest.accession, filed=latest.filed, period_end=end,
            components=(*operating_sources,
                        *(fact.provenance for fact in deductions),
                        *(fact.provenance for fact in adjustments)),
        ),
    )


def _derived_flow(concept: str, tag: str, value: Decimal,
                  sources: tuple[Fact, ...], fiscal_year: int) -> Fact:
    """A duration fact derived from same-period filed cash-flow inputs."""
    latest = max(sources, key=lambda fact: fact.provenance.filed).provenance
    return Fact(
        value=value,
        provenance=Provenance(
            concept=concept, tag=tag, fiscal_year=fiscal_year, form=latest.form,
            accession=latest.accession, filed=latest.filed,
            period_end=latest.period_end, period_start=latest.period_start,
            components=tuple(fact.provenance for fact in sources),
        ),
    )


def _annual_operating_returns(
    gaap: dict,
    operating: dict[int, Fact],
    *,
    cik: str | None = None,
    years: int = 10,
    assume_absent_zero: bool = False,
    conservative_missing_zero: bool = False,
    all_facts: dict | None = None,
    fallback_end: date | None = None,
) -> dict[int, AnnualOperatingReturn]:
    """Calculate NOPAT, ROIC and RONTA for ten fiscal-year slots.

    NOPAT is deliberately independent of the owner-earnings cash bridge.  Its
    tax rate is the median of the usable effective rates in that fiscal year and
    the two immediately preceding years; one filing-backed rate is sufficient in
    the strict export. Return denominators are exact averages of beginning and
    ending annual balance sheets.

    The detail/Return Quality convention is intentionally broader: every absent
    input, including operating income, tax rate, assets, current liabilities and
    cash, becomes a visibly disclosed zero. It is exported only through a separate
    discovery overlay and is never used by reported fields or Graham grades.

    ``conservative_missing_zero`` is a separate discovery calculation. It may
    bound only missing short/noncurrent investments, goodwill and other
    intangibles at zero. Those fields are deductions from the denominator, so
    zero maximizes capital and gives the lowest positive return. Core inputs,
    current debt, lease liabilities, cash and tax remain filing-strict, and the
    normal denominator plausibility guard remains active.
    """
    gaap = _with_fiscal_calendar(gaap)
    ends = fiscal_year_ends(gaap)
    observed_years = sorted(set(ends) | set(operating))
    if assume_absent_zero:
        # The ratios table can have a later fiscal label from revenue/net income
        # even when the filing has no usable balance-sheet anchor for that label
        # (USCI FY2020). Align the ten synthetic operating-return slots with the
        # newest fiscal year the panel itself will display.
        observed_years = sorted(
            set(observed_years)
            | set(_annual_revenue(gaap))
            | set(_annual_net_income(gaap))
            | set(_annual_eps(gaap)))
    if not observed_years:
        if not assume_absent_zero:
            return {}
        fallback_end = fallback_end or date.today()
        latest_year = fallback_end.year
    else:
        latest_year = observed_years[-1]
    target_years = (list(range(latest_year - years + 1, latest_year + 1))
                    if assume_absent_zero else sorted(ends)[-years:])

    anchor_year = max(ends) if ends else latest_year
    anchor_source = operating.get(anchor_year)
    anchor_end = (date.fromisoformat(ends[anchor_year]) if anchor_year in ends
                  else anchor_source.provenance.period_end if anchor_source
                  else fallback_end)

    def end_for_year(year: int) -> date | None:
        if year in ends:
            return date.fromisoformat(ends[year])
        source = operating.get(year)
        if source is not None and source.provenance.period_end is not None:
            return source.provenance.period_end
        if anchor_end is None:
            return None
        calendar_year = anchor_end.year + (year - anchor_year)
        day = min(anchor_end.day, monthrange(calendar_year, anchor_end.month)[1])
        return date(calendar_year, anchor_end.month, day)

    tax = _annual_union(gaap, TAX_TAGS)
    direct_pretax = _annual_union(gaap, PRETAX_TAGS) or _annual_union(
        gaap, PRETAX_INCOME_TAGS)
    # DECK and similar filers move the consolidated subtotal from a direct tag
    # to a domestic/foreign split. Both same-period geographies together are the
    # consolidated pretax figure; either one by itself remains unusable.
    pretax = {**_geographic_pretax(gaap), **direct_pretax}
    usable_rates: dict[int, tuple[Decimal, Fact, Fact]] = {}
    for year in sorted(set(tax) & set(pretax)):
        tax_fact, pretax_fact = tax[year], pretax[year]
        if ((tax_fact.provenance.period_start, tax_fact.provenance.period_end)
                != (pretax_fact.provenance.period_start,
                    pretax_fact.provenance.period_end)
                or pretax_fact.value <= 0):
            continue
        rate = tax_fact.value / pretax_fact.value
        if Decimal(0) <= rate <= Decimal("0.50"):
            usable_rates[year] = (rate, tax_fact, pretax_fact)

    exact_by_end: dict[date, dict] = {}
    assumable_by_end: dict[date, set[str]] = {}

    def exact(end: date | None) -> dict:
        if end is None:
            return {}
        if end not in exact_by_end:
            exact_by_end[end] = _taxonomy_at_end(
                gaap, end, annual_only=True)
        return exact_by_end[end]

    def assumable(end: date) -> set[str] | None:
        if all_facts is None:
            return None
        if end not in assumable_by_end:
            assumable_by_end[end] = _historical_absent_zero_candidates(
                all_facts, end)
        return assumable_by_end[end]

    def median(values: list[Decimal]) -> Decimal:
        ordered = sorted(values)
        middle = len(ordered) // 2
        return (ordered[middle] if len(ordered) % 2
                else (ordered[middle - 1] + ordered[middle]) / 2)

    def average(beginning: Fact | None, ending: Fact | None) -> Decimal | None:
        if beginning is None or ending is None:
            return None
        value = (beginning.value + ending.value) / 2
        return value if assume_absent_zero or value > 0 else None

    def return_rate(numerator: Decimal | None, denominator: Decimal | None,
                    label: str, caveats: list[str]) -> Decimal | None:
        if numerator is None or denominator is None:
            return None
        if denominator != 0:
            return numerator / denominator * 100
        if assume_absent_zero and numerator == 0:
            caveats.append(
                f"{label} has an assumed-zero numerator and denominator; the "
                "detail-only zero-assumption convention displays the result as 0%")
            return Decimal(0)
        caveats.append(
            f"{label} is not mathematically measurable because its denominator is 0")
        return None

    out: dict[int, AnnualOperatingReturn] = {}
    optional_zero = assume_absent_zero or conservative_missing_zero
    for year in target_years:
        caveats: list[str] = []
        assumed: set[str] = set()
        operating_source = operating.get(year)
        expected_end = end_for_year(year)
        normalized_tax_rate = None
        tax_inputs: tuple[Fact, ...] = ()
        pretax_inputs: tuple[Fact, ...] = ()
        nopat_fact = None
        invested_beginning = invested_ending = None
        gross_beginning = gross_ending = None
        ntoa_beginning = ntoa_ending = None
        lease_neutral_ntoa_beginning = lease_neutral_ntoa_ending = None

        rate_years = [candidate for candidate in range(year - 2, year + 1)
                      if candidate in usable_rates]
        if rate_years:
            normalized_tax_rate = median(
                [usable_rates[candidate][0] for candidate in rate_years])
            tax_inputs = tuple(usable_rates[candidate][1]
                               for candidate in rate_years)
            pretax_inputs = tuple(usable_rates[candidate][2]
                                  for candidate in rate_years)
            caveats.append(
                "normalized tax rate is the median of aligned filing-reported "
                "effective rates from "
                + ", ".join(f"FY{candidate}" for candidate in rate_years))
        else:
            if assume_absent_zero:
                normalized_tax_rate = Decimal(0)
                assumed.add("tax_rate")
                caveats.append(
                    f"effective income-tax rate was not reported in usable aligned "
                    f"source evidence for FY{year} or its prior two fiscal years; it was assumed "
                    "to be 0 by the explicit assume_absent_zero detail mode")
            else:
                caveats.append(
                    f"FY{year} and its prior two fiscal years contain no aligned, "
                    "usable filing-reported tax expense / positive pretax-income rate; "
                    "NOPAT and its returns are withheld")

        if operating_source is None:
            if assume_absent_zero and expected_end is not None:
                prior_end = end_for_year(year - 1)
                flow_start = prior_end + timedelta(days=1) if prior_end else None
                operating_source = _assumed_zero_fact(
                    "operating_income", expected_end, caveats, assumed,
                    fiscal_year=year, start=flow_start)
            else:
                caveats.append(
                    f"no filing-reported or exactly reconciled FY{year} operating "
                    "income is available; NOPAT and its returns are withheld")
        if operating_source is not None:
            flow_start = operating_source.provenance.period_start
            flow_end = operating_source.provenance.period_end
            if flow_end != expected_end:
                if assume_absent_zero and flow_end is not None:
                    caveats.append(
                        f"FY{year} has no matching annual balance-sheet date; the "
                        f"operating-income end {flow_end} was used for the explicit "
                        "zero-assumption detail calculation")
                    expected_end = flow_end
                else:
                    caveats.append(
                        f"FY{year} operating income ends {flow_end}, not the annual "
                        f"balance-sheet date {expected_end}; capital returns are withheld")
            beginning_end = flow_start - timedelta(days=1) if flow_start else None
            if beginning_end is None and assume_absent_zero:
                beginning_end = end_for_year(year - 1)
                caveats.append(
                    f"FY{year} operating income does not report a usable period start; "
                    f"the inferred prior fiscal-year end {beginning_end} was used by "
                    "the explicit zero-assumption detail mode")
            if beginning_end is None:
                caveats.append(
                    f"FY{year} operating income has no period start, so an exact "
                    "beginning capital balance cannot be identified")

            if flow_end == expected_end and beginning_end is not None:
                beginning_view, ending_view = exact(beginning_end), exact(flow_end)
                beginning_assumable = (None if assume_absent_zero
                                       else assumable(beginning_end))
                ending_assumable = (None if assume_absent_zero
                                    else assumable(flow_end))
                invested_beginning = _capital_fact(
                    gaap, beginning_end, True, caveats,
                    cik=cik,
                    assume_absent_debt_zero=assume_absent_zero,
                    assume_absent_optional_zero=optional_zero,
                    assume_absent_all_zero=assume_absent_zero,
                    assumed_zero=assumed, assumable_zero=beginning_assumable,
                    exact=beginning_view, annual_only=True,
                    conservative_estimate=conservative_missing_zero)
                invested_ending = _capital_fact(
                    gaap, flow_end, True, caveats,
                    cik=cik,
                    assume_absent_debt_zero=assume_absent_zero,
                    assume_absent_optional_zero=optional_zero,
                    assume_absent_all_zero=assume_absent_zero,
                    assumed_zero=assumed, assumable_zero=ending_assumable,
                    exact=ending_view, annual_only=True,
                    conservative_estimate=conservative_missing_zero)
                gross_beginning = _capital_fact(
                    gaap, beginning_end, False, caveats,
                    cik=cik,
                    assume_absent_debt_zero=assume_absent_zero,
                    assume_absent_all_zero=assume_absent_zero,
                    assumed_zero=assumed, assumable_zero=beginning_assumable,
                    exact=beginning_view, annual_only=True)
                gross_ending = _capital_fact(
                    gaap, flow_end, False, caveats,
                    cik=cik,
                    assume_absent_debt_zero=assume_absent_zero,
                    assume_absent_all_zero=assume_absent_zero,
                    assumed_zero=assumed, assumable_zero=ending_assumable,
                    exact=ending_view, annual_only=True)
                ntoa_beginning = _net_tangible_operating_assets_fact(
                    gaap, beginning_end, caveats,
                    cik=cik,
                    assume_absent_debt_zero=assume_absent_zero,
                    assume_absent_optional_zero=optional_zero,
                    assume_absent_all_zero=assume_absent_zero,
                    assumed_zero=assumed, assumable_zero=beginning_assumable,
                    exact=beginning_view, annual_only=True,
                    conservative_estimate=conservative_missing_zero)
                ntoa_ending = _net_tangible_operating_assets_fact(
                    gaap, flow_end, caveats,
                    cik=cik,
                    assume_absent_debt_zero=assume_absent_zero,
                    assume_absent_optional_zero=optional_zero,
                    assume_absent_all_zero=assume_absent_zero,
                    assumed_zero=assumed, assumable_zero=ending_assumable,
                    exact=ending_view, annual_only=True,
                    conservative_estimate=conservative_missing_zero)
                lease_neutral_ntoa_beginning = _net_tangible_operating_assets_fact(
                    gaap, beginning_end, caveats,
                    cik=cik,
                    assume_absent_debt_zero=assume_absent_zero,
                    assume_absent_optional_zero=optional_zero,
                    assume_absent_all_zero=assume_absent_zero,
                    assumed_zero=assumed, assumable_zero=beginning_assumable,
                    exact=beginning_view, annual_only=True,
                    lease_neutral=True,
                    conservative_estimate=conservative_missing_zero)
                lease_neutral_ntoa_ending = _net_tangible_operating_assets_fact(
                    gaap, flow_end, caveats,
                    cik=cik,
                    assume_absent_debt_zero=assume_absent_zero,
                    assume_absent_optional_zero=optional_zero,
                    assume_absent_all_zero=assume_absent_zero,
                    assumed_zero=assumed, assumable_zero=ending_assumable,
                    exact=ending_view, annual_only=True,
                    lease_neutral=True,
                    conservative_estimate=conservative_missing_zero)

            if normalized_tax_rate is not None:
                rate_sources = tuple(
                    source for pair in zip(tax_inputs, pretax_inputs)
                    for source in pair)
                nopat_fact = _derived_flow(
                    "Net operating profit after normalized tax",
                    (f"{operating_source.provenance.tag} × (1 - "
                     + (f"median effective tax rate from {', '.join(f'FY{candidate}' for candidate in rate_years)}"
                        if rate_years else "assumed-zero effective tax rate")
                     + ")"),
                    operating_source.value * (Decimal(1) - normalized_tax_rate),
                    (operating_source, *rate_sources), year)

        invested = average(invested_beginning, invested_ending)
        capital_including_cash = average(gross_beginning, gross_ending)
        average_ntoa = average(ntoa_beginning, ntoa_ending)
        average_lease_neutral_ntoa = average(
            lease_neutral_ntoa_beginning, lease_neutral_ntoa_ending)
        if assume_absent_zero:
            for label, value in (
                ("average cash-excluded invested capital", invested),
                ("average capital including cash", capital_including_cash),
                ("average net tangible operating assets", average_ntoa),
                ("average lease-neutral net tangible operating assets",
                 average_lease_neutral_ntoa),
            ):
                if value is not None and value < 0:
                    caveats.append(
                        f"{label} is negative ({value}); the detail-only assumption "
                        "mode shows the arithmetic, but this return is not "
                        "economically comparable")
        if (invested is not None and invested > 0
                and capital_including_cash is not None
                and invested < capital_including_cash * _INVESTED_CAPITAL_FLOOR):
            caveats.append(
                "average cash-excluded invested capital is implausibly small "
                "relative to capital including cash; the detail-only assumption "
                "mode shows the arithmetic despite that warning"
                if assume_absent_zero else
                "average cash-excluded invested capital is implausibly small "
                "relative to capital including cash, so ROIC is withheld")
            if not assume_absent_zero:
                invested = None
        if invested is None:
            caveats.append(
                "an exact positive beginning-and-ending cash-excluded invested-"
                "capital pair is unavailable, so NOPAT ROIC is withheld")
        if capital_including_cash is None:
            caveats.append(
                "an exact positive beginning-and-ending capital-including-cash "
                "pair is unavailable, so that return is withheld")
        if average_ntoa is None:
            caveats.append(
                "an exact positive beginning-and-ending net tangible operating-"
                "asset pair is unavailable, so RONTA is withheld")

        nopat = nopat_fact.value if nopat_fact is not None else None
        nopat_roic = return_rate(nopat, invested, "NOPAT ROIC", caveats)
        nopat_return_including_cash = return_rate(
            nopat, capital_including_cash, "NOPAT return including cash", caveats)
        if average_ntoa is not None and average_ntoa <= 0:
            caveats.append(
                f"RONTA is not meaningful because average net tangible operating "
                f"assets are {average_ntoa}, not greater than 0; the detail panel "
                "displays N/M")
            ronta = None
        else:
            ronta = return_rate(nopat, average_ntoa, "RONTA", caveats)
        if (average_lease_neutral_ntoa is not None
                and average_lease_neutral_ntoa <= 0):
            caveats.append(
                "lease-neutral RONTA is not meaningful because average lease-neutral "
                f"net tangible operating assets are {average_lease_neutral_ntoa}, "
                "not greater than 0; the detail panel displays N/M")
            lease_neutral_ronta = None
        else:
            lease_neutral_ronta = return_rate(
                nopat, average_lease_neutral_ntoa, "Lease-neutral RONTA", caveats)
        out[year] = AnnualOperatingReturn(
            fiscal_year=year,
            operating_income=operating_source,
            normalized_tax_rate=normalized_tax_rate,
            tax_expense_inputs=tax_inputs,
            pretax_income_inputs=pretax_inputs,
            nopat=nopat_fact,
            invested_capital_beginning=invested_beginning,
            invested_capital_ending=invested_ending,
            invested_capital=invested,
            capital_including_cash_beginning=gross_beginning,
            capital_including_cash_ending=gross_ending,
            capital_including_cash=capital_including_cash,
            net_tangible_operating_assets_beginning=ntoa_beginning,
            net_tangible_operating_assets_ending=ntoa_ending,
            average_net_tangible_operating_assets=average_ntoa,
            lease_neutral_net_tangible_operating_assets_beginning=(
                lease_neutral_ntoa_beginning),
            lease_neutral_net_tangible_operating_assets_ending=(
                lease_neutral_ntoa_ending),
            average_lease_neutral_net_tangible_operating_assets=(
                average_lease_neutral_ntoa),
            nopat_roic=nopat_roic,
            nopat_return_including_cash=nopat_return_including_cash,
            ronta=ronta,
            lease_neutral_ronta=lease_neutral_ronta,
            assumption_mode=assume_absent_zero,
            conservative_estimate=conservative_missing_zero,
            assumed_zero=tuple(sorted(assumed)),
            caveats=tuple(dict.fromkeys(caveats)),
        )
    return out


def _annual_additive_flows(gaap: dict, tags: tuple[str, ...],
                           concept: str) -> dict[int, Fact]:
    """Sum distinct cash-flow concepts only when their statement periods align."""
    series = [_annual_union(gaap, (tag,)) for tag in tags]
    years = set().union(*(set(items) for items in series)) if series else set()
    out: dict[int, Fact] = {}
    for year in years:
        facts = tuple(items[year] for items in series if year in items)
        ends = {fact.provenance.period_end for fact in facts}
        starts = {fact.provenance.period_start for fact in facts}
        if not facts or len(ends) != 1 or len(starts) != 1:
            continue
        out[year] = (facts[0] if len(facts) == 1 else _derived_flow(
            concept,
            " + ".join(fact.provenance.tag for fact in facts),
            sum((abs(fact.value) for fact in facts), Decimal(0)),
            facts,
            year,
        ))
    return dict(sorted(out.items()))


def _owner_earnings(gaap: dict, snap_parts: dict, fresh: date | None,
                    annual_share_counts: dict[int, Fact] | None = None,
                    classed: dict | None = None, *,
                    cik: str | None = None,
                    dimensioned: dict | None = None,
                    assume_absent_debt_zero: bool = False) -> OwnerEarnings | None:
    """Build separately labelled owner-earnings evidence for audited years.

    Buffett's definition starts with the earnings attributable to owners, adds
    non-cash depreciation/amortisation, and deducts maintenance capital expenditure
    plus any additional working capital the business requires. Company Facts normally
    exposes total capex, not maintenance capex, and never identifies the *required*
    portion of a working-capital movement. Therefore no definitive owner-earnings
    number is manufactured here.

    Instead the payload carries three facts with different meanings:

    * reported earnings + D&A - total capex: an all-capex floor for organic growers;
    * reported earnings: the explicit maintenance≈D&A estimate; and
    * operating cash flow - total capex: standard free cash flow, separately named.

    The first two still omit required additional working capital. FCF includes the
    actual cash-flow-statement working-capital movement, but cannot say how much of it
    was required to preserve the company's competitive position.
    """
    caveats: list[str] = []
    flows: dict[str, dict[int, Fact]] = {}
    # The priced security is common equity. A filed common-income line therefore
    # wins; parent income is a fallback and is reduced by a same-period preferred
    # claim where the filing supplies one.
    if series := _common_owner_earnings(gaap):
        flows["reported earnings available to common"] = series
    if series := _annual_union(gaap, CAPEX_TAGS):
        flows["total capital expenditure"] = series
    operating_cash_flow = _annual_union(gaap, OPERATING_CASH_FLOW_TAGS)
    stock_compensation = _annual_union(gaap, STOCK_COMPENSATION_TAGS)
    cash_acquisitions = _annual_union(gaap, ACQUISITION_PAYMENT_TAGS)
    share_repurchases = _annual_union(gaap, SHARE_REPURCHASE_TAGS)
    capitalized_intangibles = _annual_additive_flows(
        gaap, CAPITALIZED_INTANGIBLE_INVESTMENT_TAGS,
        "Cash investment in capitalized software and acquired intangibles")
    working_capital_effect = _annual_working_capital_cash_effect(gaap, cik)

    # Keep the D&A repair here so its dedicated owner-earnings caveat can name
    # every retained earlier comparative. Other monetary duration chains are
    # reconciled centrally by _annual_union.
    da = _annual_union(gaap, DA_TAGS, reconcile_scale=False)
    parts = [s for tag in DA_PART_TAGS if (s := _annual_series(gaap, tag, unit=("USD",)))]
    if parts:
        # A filer that drops the combined tag mid-history keeps reporting the pieces;
        # taking the rollup wherever it exists and the sum elsewhere keeps the series
        # current instead of freezing it at the year the tag changed. Every year with
        # ANY part counts, and a summed year's provenance names every summed tag.
        summed = {}
        for y in {y for p in parts for y in p}:
            present = [p[y] for p in parts if y in p]
            summed[y] = present[0] if len(present) == 1 else _sum_facts(
                "DepreciationAndAmortization (sum of parts)", present)
        da = {**summed, **da}
    extension_da = _annual_extension_depreciation(dimensioned)
    for year, fact in extension_da.items():
        standard = da.get(year)
        if (standard is None
                or (fact.provenance.period_start == standard.provenance.period_start
                    and fact.provenance.period_end == standard.provenance.period_end
                    and fact.provenance.filed >= standard.provenance.filed)):
            da[year] = fact
    da, da_scale_repairs = _reconcile_depreciation_scale(gaap, da)
    if da_scale_repairs:
        caveats.append(
            "an earlier same-period D&A fact was retained after a later comparative "
            "had an independently proved exact presentation-scale contradiction: "
            + ", ".join(
                f"FY{year} ({factor:.0f}x)" for year, factor in da_scale_repairs)
            + "; both filing versions remain in provenance"
        )
    if da:
        flows["depreciation & amortisation"] = da

    required = ("reported earnings available to common",
                "depreciation & amortisation", "total capital expenditure")
    if any(k not in flows for k in required):
        return None
    shared = set.intersection(*(set(flows[k]) for k in required))
    if not shared:
        return None
    # A fiscal-year label is not enough: every numerator component must describe
    # the same statement period. Misaligned facts remain missing rather than
    # being combined into a number no filing reported.
    aligned = {
        year for year in shared
        if len({flows[label][year].provenance.period_end for label in required}) == 1
    }
    if not aligned:
        return None
    fy = max(aligned)
    # A return divides a year's flows by the capital that produced them. Stale flows
    # beside a current balance sheet are not a return on anything.
    newest_end = max((flows[k][fy].provenance.period_end for k in required
                      if flows[k][fy].provenance.period_end), default=None)
    if (fresh is not None and newest_end is not None
            and (fresh - newest_end).days > _OWNER_EARNINGS_LAG):
        return None

    earnings_source = flows["reported earnings available to common"][fy]
    depreciation_source = flows["depreciation & amortisation"][fy]
    capex_source = flows["total capital expenditure"][fy]
    earnings = earnings_source.value
    dep = depreciation_source.value
    capex = capex_source.value
    capex = abs(capex)
    # InspireMD tags $476M of depreciation against $44.6M of total assets. A single
    # year's flow cannot exceed everything the company owns; where it does, the
    # element is holding something other than the flow it names.
    scale = snap_parts.get("total_assets")
    if scale is not None and scale.value > 0:
        if any(abs(v) > scale.value for v in (earnings, dep, capex)):
            return None
    all_capex_floor = earnings + dep - capex
    maintenance_estimate = earnings  # D&A add-back and assumed maintenance capex cancel
    latest_flow = max((earnings_source, depreciation_source, capex_source),
                      key=lambda fact: fact.provenance.filed).provenance
    all_capex_floor_fact = Fact(
        value=all_capex_floor,
        provenance=Provenance(
            concept="Earnings after total capital expenditure",
            tag=(f"{earnings_source.provenance.tag} + "
                 f"{depreciation_source.provenance.tag} - {capex_source.provenance.tag}"),
            fiscal_year=fy, form=latest_flow.form, accession=latest_flow.accession,
            filed=latest_flow.filed, period_end=latest_flow.period_end,
            period_start=latest_flow.period_start,
            components=tuple(f.provenance for f in (
                earnings_source, depreciation_source, capex_source)),
        ),
    )
    maintenance_estimate_fact = Fact(
        value=maintenance_estimate,
        provenance=Provenance(
            concept="Reported earnings (maintenance capex assumed equal to D&A)",
            tag=(f"{earnings_source.provenance.tag} + {depreciation_source.provenance.tag} "
                 f"- assumed maintenance capex ({depreciation_source.provenance.tag})"),
            fiscal_year=fy, form=latest_flow.form, accession=latest_flow.accession,
            filed=latest_flow.filed, period_end=latest_flow.period_end,
            period_start=latest_flow.period_start,
            components=(earnings_source.provenance, depreciation_source.provenance),
        ),
    )
    cash_flow = None
    fcf_after_sbc = fcf_after_acquisitions = expanded_fcf = None
    stock_source = acquisition_source = intangible_source = None
    working_capital_source = cfo_before_working_capital = None
    cash_flow_components: tuple[tuple[str, Decimal], ...] = ()
    if (_tag_of(flows["total capital expenditure"][fy]) in CASH_CAPEX_TAGS
            and fy in operating_cash_flow
            and operating_cash_flow[fy].provenance.period_end
            == flows["total capital expenditure"][fy].provenance.period_end):
        operating_cash_source = operating_cash_flow[fy]
        cash_latest = max((operating_cash_source, capex_source),
                          key=lambda fact: fact.provenance.filed).provenance
        cash_flow = Fact(
            value=operating_cash_source.value - capex,
            provenance=Provenance(
                concept="Free cash flow (operating cash flow less total capex)",
                tag=f"{operating_cash_source.provenance.tag} - {capex_source.provenance.tag}",
                fiscal_year=fy, form=cash_latest.form, accession=cash_latest.accession,
                filed=cash_latest.filed, period_end=cash_latest.period_end,
                period_start=cash_latest.period_start,
                components=(operating_cash_source.provenance, capex_source.provenance),
            ),
        )
        cash_flow_components = (
            ("operating cash flow", operating_cash_source.value),
            ("- total capital expenditure", -capex),
        )

        def same_period(series: dict[int, Fact]) -> Fact | None:
            source = series.get(fy)
            return (source if source is not None
                    and source.provenance.period_end == cash_flow.provenance.period_end
                    else None)

        stock_source = same_period(stock_compensation)
        acquisition_source = same_period(cash_acquisitions)
        intangible_source = same_period(capitalized_intangibles)
        working_capital_source = same_period(working_capital_effect)
        if stock_source is not None:
            amount = abs(stock_source.value)
            fcf_after_sbc = _derived_flow(
                "Free cash flow after stock compensation",
                f"{cash_flow.provenance.tag} - {stock_source.provenance.tag}",
                cash_flow.value - amount, (cash_flow, stock_source), fy)
        if acquisition_source is not None:
            amount = abs(acquisition_source.value)
            fcf_after_acquisitions = _derived_flow(
                "Free cash flow after cash acquisitions",
                f"{cash_flow.provenance.tag} - {acquisition_source.provenance.tag}",
                cash_flow.value - amount, (cash_flow, acquisition_source), fy)
        if intangible_source is not None:
            amount = abs(intangible_source.value)
            expanded_fcf = _derived_flow(
                "Free cash flow after capitalized software and intangible investment",
                f"{cash_flow.provenance.tag} - {intangible_source.provenance.tag}",
                cash_flow.value - amount, (cash_flow, intangible_source), fy)
        if working_capital_source is not None:
            cfo_before_working_capital = _derived_flow(
                "Operating cash flow before reported working-capital cash effect",
                f"{operating_cash_source.provenance.tag} - "
                f"{working_capital_source.provenance.tag}",
                operating_cash_source.value - working_capital_source.value,
                (operating_cash_source, working_capital_source), fy)

    components = (
        ("reported earnings available to common", earnings),
        ("+ depreciation & amortisation", dep),
        ("- total capital expenditure", -capex),
    )

    caveats += [
        "definitive Buffett owner earnings are withheld because primary XBRL does not "
        "separate maintenance capital expenditure from growth capital expenditure",
        "earnings after total capex deducts growth and maintenance spending together and can "
        "materially understate a company investing for growth; because required working "
        "capital is omitted, it is not a guaranteed mathematical floor",
        "the reported-earnings assumption sets maintenance capital expenditure exactly equal "
        "to depreciation and amortisation, so the add-back and deduction cancel by construction",
        "required additional working capital is not separately reported and is not deducted "
        "from either estimate",
        "standard free cash flow is operating cash flow less total capital expenditure; it "
        "includes actual working-capital movements and is not labelled owner earnings",
        "past write-offs that reduced invested capital cannot be reconstructed from filings",
        "stock compensation is already expensed within reported earnings; the separate FCF "
        "after stock compensation measure deducts the CFO add-back as a conservative "
        "shareholder-cost diagnostic, not as a second earnings expense",
    ]

    # The return denominator belongs to the same fiscal period as its numerator.
    # Beginning capital is the instant immediately before the flow starts; ending
    # capital is the statement date on which it closes. Both must exist.
    flow_start, flow_end = earnings_source.provenance.period_start, earnings_source.provenance.period_end
    beginning_end = flow_start - timedelta(days=1) if flow_start else None
    invested_beginning = _capital_fact(
        gaap, beginning_end, True, caveats,
        cik=cik,
        assume_absent_debt_zero=assume_absent_debt_zero)
    invested_ending = _capital_fact(
        gaap, flow_end, True, caveats,
        cik=cik,
        assume_absent_debt_zero=assume_absent_debt_zero)
    gross_beginning = _capital_fact(
        gaap, beginning_end, False, caveats,
        cik=cik,
        assume_absent_debt_zero=assume_absent_debt_zero)
    gross_ending = _capital_fact(
        gaap, flow_end, False, caveats,
        cik=cik,
        assume_absent_debt_zero=assume_absent_debt_zero)
    ntoa_beginning = _net_tangible_operating_assets_fact(
        gaap, beginning_end, caveats,
        cik=cik,
        assume_absent_debt_zero=assume_absent_debt_zero)
    ntoa_ending = _net_tangible_operating_assets_fact(
        gaap, flow_end, caveats,
        cik=cik,
        assume_absent_debt_zero=assume_absent_debt_zero)
    lease_neutral_ntoa_beginning = _net_tangible_operating_assets_fact(
        gaap, beginning_end, caveats,
        cik=cik,
        assume_absent_debt_zero=assume_absent_debt_zero,
        lease_neutral=True)
    lease_neutral_ntoa_ending = _net_tangible_operating_assets_fact(
        gaap, flow_end, caveats,
        cik=cik,
        assume_absent_debt_zero=assume_absent_debt_zero,
        lease_neutral=True)
    invested = capital_including_cash = average_ntoa = None
    all_capex_return = maintenance_estimate_return = None
    all_capex_return_including_cash = maintenance_estimate_return_including_cash = None

    def average_capital(begin: Fact | None, end: Fact | None) -> Decimal | None:
        if begin is None or end is None:
            return None
        average = (begin.value + end.value) / 2
        return average if average > 0 else None

    capital_including_cash = average_capital(gross_beginning, gross_ending)
    invested = average_capital(invested_beginning, invested_ending)
    average_ntoa = average_capital(ntoa_beginning, ntoa_ending)
    average_lease_neutral_ntoa = average_capital(
        lease_neutral_ntoa_beginning, lease_neutral_ntoa_ending)
    if (invested is not None and capital_including_cash is not None
            and invested < capital_including_cash * _INVESTED_CAPITAL_FLOOR):
        invested = None
    if invested is not None:
        all_capex_return = all_capex_floor / invested * 100
        maintenance_estimate_return = maintenance_estimate / invested * 100
    else:
        caveats.append("an exact beginning-and-ending cash-excluded capital pair was unavailable "
                       "or too small, so its return is withheld")
    if capital_including_cash is not None:
        all_capex_return_including_cash = all_capex_floor / capital_including_cash * 100
        maintenance_estimate_return_including_cash = maintenance_estimate / capital_including_cash * 100

    # A conventional capital-provider numerator: operating income after a median
    # reported tax rate. This remains separate from the after-interest common-
    # earnings measures above and is withheld without at least two usable years.
    operating = _annual_operating_income(gaap)
    tax = _annual_union(gaap, TAX_TAGS)
    cash_taxes = _annual_union(gaap, CASH_TAX_PAID_TAGS)
    pretax = _annual_union(gaap, PRETAX_TAGS) or _annual_union(gaap, PRETAX_INCOME_TAGS)
    rate_years = sorted(set(tax) & set(pretax))[-3:]
    rates = sorted(
        tax[year].value / pretax[year].value
        for year in rate_years
        if (tax[year].provenance.period_end == pretax[year].provenance.period_end
            and pretax[year].value > 0
            and Decimal(0) <= tax[year].value / pretax[year].value <= Decimal("0.50"))
    )
    normalized_tax_rate = nopat = nopat_roic = nopat_return_including_cash = ronta = None
    lease_neutral_ronta = None
    operating_source = operating.get(fy)
    cash_taxes_paid = cash_taxes.get(fy)
    if (cash_taxes_paid is not None
            and (cash_taxes_paid.provenance.period_start
                 != earnings_source.provenance.period_start
                 or cash_taxes_paid.provenance.period_end != flow_end)):
        cash_taxes_paid = None
    if len(rates) >= 2:
        mid = len(rates) // 2
        normalized_tax_rate = (rates[mid] if len(rates) % 2
                               else (rates[mid - 1] + rates[mid]) / 2)
    else:
        caveats.append(
            "fewer than two aligned, usable effective tax rates were available "
            "from the latest three fiscal years, so normalized tax and NOPAT are withheld")
    if (normalized_tax_rate is not None and operating_source is not None
            and operating_source.provenance.period_end == flow_end):
        nopat = operating_source.value * (Decimal(1) - normalized_tax_rate)
        if invested is not None:
            nopat_roic = nopat / invested * 100
        if capital_including_cash is not None:
            nopat_return_including_cash = nopat / capital_including_cash * 100
        if average_ntoa is not None:
            ronta = nopat / average_ntoa * 100
        if average_lease_neutral_ntoa is not None:
            lease_neutral_ronta = nopat / average_lease_neutral_ntoa * 100
    elif normalized_tax_rate is not None:
        caveats.append(
            "no same-period reported or exactly reconciled operating income was available, "
            "so NOPAT and its returns are withheld")

    annual_revenue = _annual_revenue(gaap)
    stock_compensation_to_revenue = (
        abs(stock_source.value) / annual_revenue[fy].value * 100
        if (stock_source is not None and fy in annual_revenue
            and annual_revenue[fy].provenance.period_end == flow_end
            and annual_revenue[fy].value > 0)
        else None)
    stock_compensation_to_fcf = (
        abs(stock_source.value) / cash_flow.value * 100
        if stock_source is not None and cash_flow is not None and cash_flow.value > 0 else None)
    acquisitions_to_fcf = (
        abs(acquisition_source.value) / cash_flow.value * 100
        if acquisition_source is not None and cash_flow is not None and cash_flow.value > 0 else None)
    acquisition_window = [year for year in range(fy - 9, fy + 1)
                          if year in cash_acquisitions and cash_acquisitions[year].value != 0]
    acquisition_years_10 = len(acquisition_window) if cash_acquisitions else None
    cumulative_acquisitions = sum((abs(cash_acquisitions[y].value)
                                   for y in acquisition_window), Decimal(0))
    # Missing acquisition facts are not zero. Compare acquisitions with capex
    # only in the years that actually reported an acquisition payment.
    capex_window = [year for year in acquisition_window
                    if (year in flows["total capital expenditure"]
                        and cash_acquisitions[year].provenance.period_end
                        == flows["total capital expenditure"][year].provenance.period_end)]
    cumulative_capex = sum((abs(flows["total capital expenditure"][y].value)
                            for y in capex_window), Decimal(0))
    acquisitions_to_capex_10 = (cumulative_acquisitions / cumulative_capex * 100
                                if cumulative_capex > 0 and acquisition_window else None)
    wc_years = list(range(fy - 2, fy + 1))
    average_working_capital = (
        sum((working_capital_effect[y].value for y in wc_years), Decimal(0)) / 3
        if all(y in working_capital_effect and y in operating_cash_flow
               and working_capital_effect[y].provenance.period_end
               == operating_cash_flow[y].provenance.period_end
               for y in wc_years) else None)

    annual: dict[int, AnnualOwnerEarnings] = {}
    periods = _per_share_periods(gaap)
    share_periods = _share_periods(gaap)
    # A filer can put the traded class only on a dimension. Its split evidence
    # then lives in the DERA sidecar, not Company Facts, and must participate in
    # the same rebasing vote as the consolidated facts.
    if classed:
        for combined, additional in (
            (periods, _per_share_periods(classed)),
            (share_periods, _share_periods(classed)),
        ):
            for key, values in additional.items():
                combined.setdefault(key, []).extend(value for value in values
                                                        if value not in combined.get(key, ()))
                combined[key].sort()
    adjusted_counts: dict[int, Fact] = {}
    for year, count in (annual_share_counts or {}).items():
        # Comparative share counts in old filings predate later splits.  The EPS
        # path already rebases those years; multiply its denominator by the same
        # filing-observed factor so all owner-evidence per-share values are comparable.
        factor = (_split_factor(
            periods, count.provenance.filed.isoformat(), share_periods, cik=cik)
                  if periods else Decimal(1))
        adjusted = count
        if factor != 1:
            p = count.provenance
            adjusted = Fact(
                value=count.value * factor,
                provenance=Provenance(
                    concept=f"{p.concept} (restated {factor:g}x for later split)",
                    tag=p.tag, fiscal_year=p.fiscal_year, form=p.form,
                    accession=p.accession, filed=p.filed, period_end=p.period_end,
                    period_start=p.period_start, components=(p,), segments=p.segments,
                ),
            )
        adjusted_counts[year] = adjusted

    # The panel displays ten fiscal-year slots, but a ten-slot CAGR now uses the
    # average of slots 9, 10, and 11 as its starting level. Retain that one
    # additional evidence year; it is support for the calculation, not an
    # invitation to bridge a missing year or extend the displayed record.
    owner_years = {year for year in shared if fy - 10 <= year <= fy}
    scale_repairs: list[tuple[int, Decimal]] = []
    for year in sorted(adjusted_counts):
        if year not in owner_years:
            continue
        current = adjusted_counts[year]
        before, after = adjusted_counts.get(year - 1), adjusted_counts.get(year + 1)
        if (before is None or after is None or current.value <= 0
                or before.value <= 0 or after.value <= 0):
            continue
        reference = (before.value + after.value) / 2
        if abs(before.value - after.value) / reference > Decimal("0.10"):
            continue
        factor = next((candidate for candidate in _SHARE_SCALE_FACTORS
                       if abs(current.value * candidate - reference) / reference
                       <= _SHARE_SCALE_TOLERANCE), None)
        if factor is None:
            continue
        p = current.provenance
        adjusted_counts[year] = Fact(
            value=current.value * factor,
            provenance=Provenance(
                concept=f"{p.concept} (scaled {factor:g}x: adjacent fiscal years agree)",
                tag=p.tag, fiscal_year=p.fiscal_year, form=p.form,
                accession=p.accession, filed=p.filed, period_end=p.period_end,
                period_start=p.period_start,
                components=(p, before.provenance, after.provenance), segments=p.segments,
            ),
        )
        scale_repairs.append((year, factor))
    if scale_repairs:
        caveats.append(
            "the diluted share count carried an isolated table-scale value in "
            + ", ".join(f"FY{year} ({factor:g}x)" for year, factor in scale_repairs)
            + "; both adjacent split-adjusted fiscal years prove the exact unit correction")

    allow_shareless_totals = bool(getattr(gaap, "canonical_adapter", False))
    annual_years = (owner_years if allow_shareless_totals
                    else owner_years & set(adjusted_counts))
    for year in sorted(annual_years):
        facts = [flows[label][year] for label in required]
        adjusted_count = adjusted_counts.get(year)
        # A fiscal label alone is not evidence that differently dated statements
        # describe the same year.  Keep missing/misaligned evidence missing.
        ends = {fact.provenance.period_end for fact in facts
                if fact.provenance.period_end is not None}
        if len(ends) != 1:
            continue
        if (adjusted_count is not None
                and (adjusted_count.value <= 0
                     or adjusted_count.provenance.period_end not in ends)):
            adjusted_count = None
        earnings_y, dep_y, capex_y = (fact.value for fact in facts)
        capex_y = abs(capex_y)

        latest = max(facts, key=lambda fact: fact.provenance.filed).provenance
        floor_y = earnings_y + dep_y - capex_y
        floor_fact = Fact(
            value=floor_y,
            provenance=Provenance(
                concept="Earnings after total capital expenditure",
                tag=(f"{facts[0].provenance.tag} + {facts[1].provenance.tag} "
                     f"- {facts[2].provenance.tag}"),
                fiscal_year=year, form=latest.form, accession=latest.accession,
                filed=latest.filed, period_end=latest.period_end,
                period_start=latest.period_start,
                components=tuple(fact.provenance for fact in facts),
            ),
        )
        estimate_fact = Fact(
            value=earnings_y,
            provenance=Provenance(
                concept="Reported earnings (maintenance capex assumed equal to D&A)",
                tag=(f"{facts[0].provenance.tag} + {facts[1].provenance.tag} "
                     f"- assumed maintenance capex ({facts[1].provenance.tag})"),
                fiscal_year=year, form=latest.form, accession=latest.accession,
                filed=latest.filed,
                period_end=latest.period_end, period_start=latest.period_start,
                components=(facts[0].provenance, facts[1].provenance),
            ),
        )

        cash_flow_fact = None
        operating_cash_flow_y = other_operating_cash_flow_adjustments_y = None
        fcf_after_sbc_y = fcf_after_acquisitions_y = expanded_fcf_y = None
        stock_y = acquisition_y = intangible_y = working_capital_y = None
        repurchase_y = share_repurchases.get(year)
        if (repurchase_y is not None
                and repurchase_y.provenance.period_end != latest.period_end):
            repurchase_y = None
        cfo_before_working_capital_y = None
        cash_flow_source = operating_cash_flow.get(year)
        if (cash_flow_source is not None
                and cash_flow_source.provenance.period_end == latest.period_end):
            operating_cash_flow_y = cash_flow_source

            def annual_period_source(series: dict[int, Fact]) -> Fact | None:
                source = series.get(year)
                return (source if source is not None
                        and source.provenance.period_end == cash_flow_source.provenance.period_end
                        else None)

            stock_y = annual_period_source(stock_compensation)
            acquisition_y = annual_period_source(cash_acquisitions)
            intangible_y = annual_period_source(capitalized_intangibles)
            working_capital_y = annual_period_source(working_capital_effect)
            if working_capital_y is not None:
                cfo_before_working_capital_y = _derived_flow(
                    "Operating cash flow before reported working-capital cash effect",
                    f"{cash_flow_source.provenance.tag} - {working_capital_y.provenance.tag}",
                    cash_flow_source.value - working_capital_y.value,
                    (cash_flow_source, working_capital_y), year)
            stock_addback = abs(stock_y.value) if stock_y is not None else Decimal(0)
            working_capital_addback = (
                working_capital_y.value if working_capital_y is not None else Decimal(0))
            residual_sources = [cash_flow_source, facts[0], facts[1]]
            residual_tag = (
                f"{cash_flow_source.provenance.tag} - {facts[0].provenance.tag} - "
                f"{facts[1].provenance.tag}"
            )
            if stock_y is not None:
                residual_sources.append(stock_y)
                residual_tag += f" - {stock_y.provenance.tag}"
            if working_capital_y is not None:
                residual_sources.append(working_capital_y)
                residual_tag += f" - {working_capital_y.provenance.tag}"
            other_operating_cash_flow_adjustments_y = _derived_flow(
                "Other operating cash-flow adjustments (reconciliation residual)",
                residual_tag,
                (cash_flow_source.value - earnings_y - dep_y
                 - stock_addback - working_capital_addback),
                tuple(residual_sources), year)

            # Standard FCF requires a cash CapEx tag. An accrual-based segment
            # expenditure may still be displayed as CapEx evidence, but it must
            # never be mixed into a claimed cash-flow result.
            if _tag_of(facts[2]) in CASH_CAPEX_TAGS:
                cash_latest = max((cash_flow_source, facts[2]),
                                  key=lambda fact: fact.provenance.filed).provenance
                cash_flow_fact = Fact(
                    value=cash_flow_source.value - capex_y,
                    provenance=Provenance(
                        concept="Free cash flow (operating cash flow less total capex)",
                        tag=(f"{cash_flow_source.provenance.tag} - "
                             f"{facts[2].provenance.tag}"),
                        fiscal_year=year, form=cash_latest.form,
                        accession=cash_latest.accession, filed=cash_latest.filed,
                        period_end=cash_latest.period_end,
                        period_start=cash_latest.period_start,
                        components=(cash_flow_source.provenance, facts[2].provenance),
                    ),
                )
                if stock_y is not None:
                    fcf_after_sbc_y = _derived_flow(
                        "Free cash flow after stock compensation",
                        f"{cash_flow_fact.provenance.tag} - {stock_y.provenance.tag}",
                        cash_flow_fact.value - abs(stock_y.value),
                        (cash_flow_fact, stock_y), year)
                if acquisition_y is not None:
                    fcf_after_acquisitions_y = _derived_flow(
                        "Free cash flow after cash acquisitions",
                        f"{cash_flow_fact.provenance.tag} - {acquisition_y.provenance.tag}",
                        cash_flow_fact.value - abs(acquisition_y.value),
                        (cash_flow_fact, acquisition_y), year)
                if intangible_y is not None:
                    expanded_fcf_y = _derived_flow(
                        "Free cash flow after capitalized software and intangible investment",
                        f"{cash_flow_fact.provenance.tag} - {intangible_y.provenance.tag}",
                        cash_flow_fact.value - abs(intangible_y.value),
                        (cash_flow_fact, intangible_y), year)

        def per_share_fact(source: Fact | None, concept: str) -> Fact | None:
            if source is None or adjusted_count is None:
                return None
            return Fact(
                value=source.value / adjusted_count.value,
                provenance=Provenance(
                    concept=f"{concept} per diluted share (current security basis)",
                    tag=f"{source.provenance.tag} / {adjusted_count.provenance.tag}",
                    fiscal_year=year, form=source.provenance.form,
                    accession=source.provenance.accession,
                    filed=max(source.provenance.filed, adjusted_count.provenance.filed),
                    period_end=source.provenance.period_end,
                    period_start=source.provenance.period_start,
                    components=(source.provenance, adjusted_count.provenance),
                ),
            )

        annual[year] = AnnualOwnerEarnings(
            reported_earnings=facts[0],
            depreciation_and_amortisation=facts[1],
            total_capital_expenditure=facts[2],
            operating_cash_flow=operating_cash_flow_y,
            other_operating_cash_flow_adjustments=other_operating_cash_flow_adjustments_y,
            all_capex_floor=floor_fact,
            maintenance_estimate=estimate_fact,
            free_cash_flow=cash_flow_fact,
            free_cash_flow_after_stock_compensation=fcf_after_sbc_y,
            free_cash_flow_after_acquisitions=fcf_after_acquisitions_y,
            expanded_free_cash_flow=expanded_fcf_y,
            stock_compensation=stock_y,
            cash_acquisitions=acquisition_y,
            share_repurchases=repurchase_y,
            capitalized_intangible_investment=intangible_y,
            working_capital_cash_effect=working_capital_y,
            operating_cash_flow_before_working_capital=cfo_before_working_capital_y,
            diluted_shares=adjusted_count,
            all_capex_floor_per_share=per_share_fact(floor_fact, "Earnings after total capex"),
            maintenance_estimate_per_share=per_share_fact(
                estimate_fact, "Reported earnings assumption"),
            free_cash_flow_per_share=(
                per_share_fact(cash_flow_fact, "Free cash flow")
                if cash_flow_fact is not None else None),
            free_cash_flow_after_stock_compensation_per_share=(
                per_share_fact(fcf_after_sbc_y, "Free cash flow after stock compensation")
                if fcf_after_sbc_y is not None else None),
            free_cash_flow_after_acquisitions_per_share=(
                per_share_fact(fcf_after_acquisitions_y, "Free cash flow after acquisitions")
                if fcf_after_acquisitions_y is not None else None),
            expanded_free_cash_flow_per_share=(
                per_share_fact(expanded_fcf_y, "Expanded free cash flow")
                if expanded_fcf_y is not None else None),
        )

    return OwnerEarnings(
        fiscal_year=fy,
        all_capex_floor=all_capex_floor_fact,
        maintenance_estimate=maintenance_estimate_fact,
        free_cash_flow=cash_flow,
        cash_taxes_paid=cash_taxes_paid,
        free_cash_flow_after_stock_compensation=fcf_after_sbc,
        free_cash_flow_after_acquisitions=fcf_after_acquisitions,
        expanded_free_cash_flow=expanded_fcf,
        stock_compensation=stock_source,
        cash_acquisitions=acquisition_source,
        capitalized_intangible_investment=intangible_source,
        working_capital_cash_effect=working_capital_source,
        operating_cash_flow_before_working_capital=cfo_before_working_capital,
        average_working_capital_cash_effect_3y=average_working_capital,
        stock_compensation_to_revenue=stock_compensation_to_revenue,
        stock_compensation_to_free_cash_flow=stock_compensation_to_fcf,
        acquisitions_to_free_cash_flow=acquisitions_to_fcf,
        acquisition_years_10=acquisition_years_10,
        acquisitions_to_capex_10=acquisitions_to_capex_10,
        invested_capital_beginning=invested_beginning,
        invested_capital_ending=invested_ending,
        invested_capital=invested,
        capital_including_cash_beginning=gross_beginning,
        capital_including_cash_ending=gross_ending,
        capital_including_cash=capital_including_cash,
        all_capex_return=all_capex_return,
        maintenance_estimate_return=maintenance_estimate_return,
        all_capex_return_including_cash=all_capex_return_including_cash,
        maintenance_estimate_return_including_cash=maintenance_estimate_return_including_cash,
        normalized_tax_rate=normalized_tax_rate,
        nopat=nopat,
        nopat_roic=nopat_roic,
        nopat_return_including_cash=nopat_return_including_cash,
        net_tangible_operating_assets_beginning=ntoa_beginning,
        net_tangible_operating_assets_ending=ntoa_ending,
        average_net_tangible_operating_assets=average_ntoa,
        ronta=ronta,
        lease_neutral_net_tangible_operating_assets_beginning=(
            lease_neutral_ntoa_beginning),
        lease_neutral_net_tangible_operating_assets_ending=(
            lease_neutral_ntoa_ending),
        average_lease_neutral_net_tangible_operating_assets=(
            average_lease_neutral_ntoa),
        lease_neutral_ronta=lease_neutral_ronta,
        components=components,
        free_cash_flow_components=cash_flow_components,
        caveats=tuple(dict.fromkeys(caveats)),
        annual=annual,
    )


_DERIVED_EPS_SHARE_LAG = 460  # a cover count this close to the year end counts that year


_BASIS_TOLERANCE = Decimal("1.5")   # how far EPS x shares may sit from the filer's own income
_SHARE_SCALE_TOLERANCE = Decimal("0.05")
_SHARE_SCALE_FACTORS = (
    Decimal("0.000001"), Decimal("0.001"), Decimal("1000"), Decimal("1000000"),
)
_EPS_EXACT_SCALE_TOLERANCE = Decimal("0.01")
_EPS_SCALED_RECONCILES = Decimal("0.10")


def _share_scale_supported_by_history(
    counts: dict[int, Fact], year: int, factor: Decimal,
    current_shares: Decimal | None = None,
) -> bool:
    """Require the corrected count, rather than the raw count, to fit history.

    EPS, income and shares can form an exact identity even when the malformed
    cell is EPS or income. TCI's correct 8.1M-share count, for example, sits
    beside a mis-scaled loss/EPS pair that implies 8.1 thousand. Neighbouring
    filed share counts independently identify which of the three cells carries
    the scale error. A lone under-scaled count may still be recovered because
    statement tables commonly expose a displayed thousands/millions value under
    the plain ``shares`` unit; reducing a count without this second signal is
    deliberately refused.
    """
    raw = counts[year].value
    scaled = raw * factor
    # The established conservative rule already accepts only an exact upward
    # thousands/millions factor proved by filing-local arithmetic. Tiny literal
    # weighted counts are the characteristic lost-table-scale case. The extra
    # continuity requirement is for the new, opposite direction only: a large
    # raw count can just as plausibly be correct while EPS or income is mis-scaled.
    if factor > 1:
        return True

    if current_shares is not None and current_shares > 0:
        raw_near_current = Decimal("0.1") <= raw / current_shares <= Decimal("10")
        scaled_near_current = (
            Decimal("0.1") <= scaled / current_shares <= Decimal("10"))
        if raw_near_current != scaled_near_current:
            return scaled_near_current

    nearby = [
        fact.value for other_year, fact in counts.items()
        if other_year != year and abs(other_year - year) <= 5 and fact.value > 0
    ]
    if not nearby:
        nearby = [
            fact.value for other_year, fact in counts.items()
            if other_year != year and fact.value > 0
        ]

    def votes(value: Decimal) -> int:
        return sum(
            Decimal("0.1") <= value / reference <= Decimal("10")
            for reference in nearby
        )

    raw_votes, scaled_votes = votes(raw), votes(scaled)
    return bool(scaled_votes and scaled_votes > raw_votes)

_VERIFIED_ANNUAL_EPS_PRESENTATION_SCALES = {
    # Zion's primary FY2022 10-K prints FY2021 net loss per share as $(0.04).
    # Its Inline-XBRL fact instead reaches Company Facts as $(40), under a new
    # tag, so same-tag comparative reconciliation cannot recover the prior row.
    ("0001131312", "0001213900-23-023118", 2021): Decimal("0.001"),
    # Each exact row below was read against the primary annual statement or EPS
    # note.  The machine fact retained a table's thousands multiplier in the
    # per-share value even though EPS itself is stated in ordinary currency units.
    ("0000880984", "0000880984-12-000005", 2009): Decimal("0.001"),  # ACFN
    ("0001232582", "0001232582-18-000011", 2015): Decimal("0.001"),  # AHT
    ("0001860657", "0001213900-24-020864", 2023): Decimal("0.001"),  # ALLR
    ("0001130166", "0001493152-25-025179", 2023): Decimal("0.001"),  # BGMS
    ("0001309082", "0001580695-20-000260", 2019): Decimal("0.001"),  # CEIN
    ("0000896493", "0001437749-13-003708", 2011): Decimal("0.001"),  # GPUS
    ("0000896493", "0001437749-13-003708", 2012): Decimal("0.001"),
    ("0000896493", "0001437749-15-006253", 2013): Decimal("0.001"),
    ("0000896493", "0001437749-16-028793", 2014): Decimal("0.001"),
    ("0000896493", "0001437749-17-006372", 2015): Decimal("0.001"),
    ("0000896493", "0001214659-18-002877", 2016): Decimal("0.001"),
    ("0000896493", "0001214659-19-006507", 2017): Decimal("0.001"),
    ("0001583771", "0001583771-22-000003", 2020): Decimal("0.001"),  # HEPA
    ("0001583771", "0001583771-23-000003", 2021): Decimal("0.001"),
    ("0000897078", "0001493152-18-009029", 2016): Decimal("0.001"),  # KOAN
    ("0001590560", "0001558370-20-001883", 2017): Decimal("0.001"),  # QURE
    ("0001590560", "0001558370-21-002186", 2018): Decimal("0.001"),
    ("0001469443", "0001564590-16-014230", 2014): Decimal("0.001"),  # RKDA
    ("0001469443", "0001564590-17-003724", 2015): Decimal("0.001"),
    ("0001390478", "0001390478-14-000016", 2011): Decimal("0.001"),  # SLS
    ("0001390478", "0001390478-15-000012", 2012): Decimal("0.001"),
    ("0001390478", "0001390478-16-000095", 2013): Decimal("0.001"),
    ("0001905956", "0001575872-22-001246", 2021): Decimal("0.001"),  # TGL
    ("0001905956", "0001213900-23-080469", 2022): Decimal("0.001"),
    ("0001905956", "0001213900-24-083395", 2023): Decimal("0.001"),
    ("0001411685", "0001415889-14-001957", 2013): Decimal("0.001"),  # VTGN
    ("0001576789", "0001576789-23-000025", 2020): Decimal("0.001"),  # WIX
    # ADS-TEC's audited table reports FY2020 basic/diluted loss per share of
    # EUR 0.32 beside a EUR 10.280m loss and 32.039m weighted shares.
    ("0001879248", "0001213900-23-038592", 2020): Decimal("0.001"),  # ADSE
}

_VERIFIED_ANNUAL_REVENUE_WITHHOLD = frozenset({
    # Bion Environmental Technologies' FY2025 statement has no revenue row or
    # revenue activity.  Company Facts nevertheless exposes its $62 of
    # non-operating interest income under InterestIncomeOperating; that is not a
    # top line and cannot be shown as revenue.  Withhold rather than manufacture
    # a zero fact that carries no XBRL provenance.
    ("0000875729", "0001079973-25-001516", 2025),  # BNET
})

# Inline-XBRL sign metadata is not a trustworthy general rule for EPS. Some
# filings expose a positive machine value for a parenthesised loss (NEON/SOBR),
# while others expose a common-income fact whose own XBRL sign is wrong even
# though the EPS fact is right (CPWR/ECIA). Only the exact primary statements
# read below may override the selected numeric sign.
_VERIFIED_ANNUAL_EPS_SIGN_FLIPS = frozenset({
    ("0000087050", "0001213900-22-011462", 2020),  # NEON: $(0.56)
    ("0001425627", "0001477932-26-002117", 2024),  # SOBR: $(172.19)
    ("0001425627", "0001477932-26-002117", 2025),  # SOBR: $(6.12)
    ("0001643953", "0001213900-25-023815", 2022),  # PRPL: +$1.13
    ("0001643953", "0001213900-26-036974", 2023),  # PRPL: $(1.17)
    ("0001643953", "0001213900-26-036974", 2024),  # PRPL: $(0.91)
    ("0001643953", "0001213900-26-036974", 2025),  # PRPL: $(0.48)
    ("0001869601", "0001193125-23-062939", 2022),  # EMCGF: +$0.08
    # Pulsenmore labels the positive displayed 0.77 as a loss per share; the
    # normalized numeric convention carries losses as negative values.
    ("0002064764", "0001493152-26-013485", 2025),  # PLSM: loss NIS 0.77
})

_VERIFIED_ANNUAL_EPS_WITHHOLD = frozenset({
    # Guided Therapeutics' FY2017 statement reports a nonzero common loss,
    # +$997.64 basic loss-per-share and $0 diluted loss-per-share beside 11
    # basic and 0 diluted weighted shares.  The signs and diluted denominator
    # contradict the statement itself, so no one tagged EPS is publishable.
    ("0000924515", "0001654954-19-005469", 2017),
})

_VERIFIED_COMMON_INCOME_SCALE_CONFLICTS = frozenset({
    # KFFB's primary statements print the same ordinary net-income amount as
    # NetIncomeLoss.  The sibling income-available-to-common facts alone carry
    # a spurious thousands multiplier and must not drive Owner Earnings.
    ("0001297341", "0001213900-21-050183", 2020),
    ("0001297341", "0001213900-22-059709", 2021),
    ("0001297341", "0001213900-23-080514", 2022),
    ("0001297341", "0001213900-24-085121", 2023),
    ("0001297341", "0001213900-25-093967", 2024),
    ("0001297341", "0001213900-25-093967", 2025),
})

# Exact monetary cells whose primary statements explicitly supply a table scale
# that the selected Company Facts observation lost.  A negative factor is reserved
# for the separately verified case where an amendment also inverted the sign.
_VERIFIED_ANNUAL_DURATION_FACT_SCALES = {
    # Transcontinental Realty Investors' statements are in thousands; the two
    # selected old comparative facts retain only their displayed digits.
    ("0000733590", "0001010549-13-000233", 2010,
     "NetIncomeLoss"): Decimal("1000"),
    ("0000733590", "0001010549-13-000233", 2010,
     "OperatingIncomeLoss"): Decimal("1000"),
    ("0000733590", "0001010549-14-000144", 2011,
     "NetIncomeLoss"): Decimal("1000"),
    ("0000733590", "0001010549-14-000144", 2011,
     "OperatingIncomeLoss"): Decimal("1000"),
    # Income Opportunity Realty's 2009/2011 statements likewise present these
    # rows in thousands, while the plain USD observations contain 920/669 and
    # -1,325/-1,388 rather than the corresponding dollar totals.
    ("0000949961", "0001010549-12-000347", 2009,
     "NetIncomeLoss"): Decimal("1000"),
    ("0000949961", "0001010549-12-000347", 2009,
     "OperatingIncomeLoss"): Decimal("1000"),
    ("0000949961", "0001010549-14-000146", 2011,
     "NetIncomeLoss"): Decimal("1000"),
    ("0000949961", "0001010549-14-000146", 2011,
     "OperatingIncomeLoss"): Decimal("1000"),
    # Spirit's original audited EPS note labels every numerator "in thousands".
    # Later 10-K/A facts lost that multiplier; the FY2020 amendment also inverted
    # the loss sign.  Exact accession scoping prevents either amendment from
    # changing unrelated periods or tags.
    ("0001498710", "0001193125-25-107562", 2020,
     "NetIncomeLoss"): Decimal("-1000"),
    ("0001498710", "0001193125-26-197767", 2021,
     "NetIncomeLoss"): Decimal("1000"),
    ("0001498710", "0001193125-26-197767", 2022,
     "NetIncomeLoss"): Decimal("1000"),
    ("0001498710", "0001193125-26-197767", 2023,
     "NetIncomeLoss"): Decimal("1000"),
    ("0001498710", "0001193125-26-197767", 2024,
     "NetIncomeLoss"): Decimal("1000"),
    ("0001498710", "0001193125-26-197767", 2025,
     "NetIncomeLoss"): Decimal("1000"),
    # AppTech's FY2022 10-K comparative labels the whole statement in thousands;
    # gross profit is 204 thousand and loss from operations is 77.320 million.
    ("0001070050", "0001903596-23-000201", 2021,
     "GrossProfit"): Decimal("1000"),
    ("0001070050", "0001903596-23-000201", 2021,
     "OperatingIncomeLoss"): Decimal("-1000"),
}

# A split detector observes when a restatement first appears in Company Facts,
# not the corporate action's legal effective date.  Zoomcar's FY2025 10-K was
# filed after its 1-for-20 split and explicitly says every comparative share and
# per-share amount already reflects that split.  A later quarterly filing made
# the restatement observable to the detector, which otherwise applies it twice.
_VERIFIED_ALREADY_SPLIT_ADJUSTED_ACCESSIONS = frozenset({
    ("0001854275", "0001213900-25-059675"),
})

# Some exact-looking EPS changes are a recapitalisation or conversion, not a
# stock split.  Marathon Bancorp's April 2025 conversion used a 1.3728 exchange
# ratio; rounding changed a comparative quarter from $0.09 to $0.06, which alone
# resembles 1.5:1.  The primary 10-Q expressly identifies the conversion ratio.
_VERIFIED_NON_SPLIT_RESTATEMENTS = frozenset({
    ("0001835385", "2025-11-12"),  # MBBC
})

# Current annual reports that supply a newer basic-only comparative after the
# diluted chain stopped.  These are statement-verified exceptions to the usual
# semantic preference for diluted EPS over basic EPS.
_VERIFIED_NEWER_BASIC_EPS_COMPARATIVES = frozenset({
    ("0001706524", "0001213900-26-046211", 2024),  # FLZH: $(73.12)
})

# Some rendered statements explicitly present weighted shares in thousands while
# their Company Facts observation retains the plain ``shares`` unit; a few later
# comparatives apply that scale in the opposite direction.  EPS is often rounded
# to $0.00/$0.01, so income / EPS cannot always prove which side is wrong.  Those
# cases are admitted only after the primary 10-K/20-F table was read, and only for
# the exact selected CIK/accession/fiscal-year context.
_VERIFIED_ANNUAL_SHARE_PRESENTATION_SCALES = {
    # Non-Invasive Monitoring Systems: the statements print 72,052 through
    # 154,811 beside tens/hundreds of millions of issued common shares.
    ("0000720762", "0001144204-14-063035", 2013): Decimal("1000"),
    ("0000720762", "0001493152-17-012147", 2016): Decimal("1000"),
    ("0000720762", "0001493152-20-020128", 2019): Decimal("1000"),
    ("0000720762", "0001493152-21-026767", 2020): Decimal("1000"),
    # Sociedad Quimica y Minera: four later comparative facts apply a
    # thousands scale to an already absolute 263,196,524-share count.
    ("0000909037", "0001144204-19-020107", 2016): Decimal("0.001"),
    ("0000909037", "0001104659-20-049813", 2017): Decimal("0.001"),
    ("0000909037", "0001104659-20-049813", 2018): Decimal("0.001"),
    ("0000909037", "0001104659-20-049813", 2019): Decimal("0.001"),
    ("0000949961", "0001387131-22-003959", 2019): Decimal("0.001"),  # IOR
    ("0001102238", "0001010549-15-000092", 2012): Decimal("0.001"),  # ARL
    # Zion: each cited primary statement labels the displayed count "in
    # thousands"; its small rounded loss per share cannot prove the factor.
    ("0001131312", "0001213900-22-013250", 2020): Decimal("1000"),
    ("0001131312", "0001437749-26-009073", 2024): Decimal("1000"),
    ("0001131312", "0001437749-26-009073", 2025): Decimal("1000"),
    ("0001673358", "0001564590-17-003681", 2014): Decimal("0.001"),  # Yum China
    ("0001713930", "0001292814-22-001004", 2019): Decimal("1000"),  # Nexa
    ("0001713930", "0001292814-23-004334", 2020): Decimal("1000"),
    # Primary statements print these weighted counts in thousands.  Rounded or
    # zero-cent EPS prevents a strict income/EPS identity from recovering them.
    ("0000718937", "0001144204-13-014619", 2010): Decimal("1000"),  # STAA
    ("0000047111", "0001193125-12-067143", 2009): Decimal("1000"),  # Hershey
    ("0000839087", "0001654954-21-005117", 2019): Decimal("1000"),  # VASO
    ("0000839087", "0001654954-22-004333", 2020): Decimal("1000"),
    ("0000917225", "0000917225-13-000011", 2011): Decimal("1000"),  # XPL
    ("0000917225", "0000917225-19-000003", 2017): Decimal("1000"),
    ("0000917225", "0001654954-23-003530", 2021): Decimal("1000"),
    ("0000917225", "0001654954-23-003530", 2022): Decimal("1000"),
    ("0000924515", "0001654954-17-002258", 2015): Decimal("1000"),  # GTHP
    ("0000924515", "0001654954-19-005469", 2017): Decimal("1000"),
    ("0001013706", "0001171843-15-001702", 2013): Decimal("1000"),  # WHLM
    # Axos' FY2014 filing exposes the FY2012 weighted counts 1,000x above
    # both the printed statement basis and the neighbouring filed history.
    ("0001299709", "0001299709-14-000121", 2012): Decimal("0.001"),
    # Berkshire's old rendered statements show equivalent Class A counts, but
    # two comparative XBRL facts carry an extra million multiplier. The FY2011
    # report displays 1,551,174 for FY2009; the raw fact is 1,551,174,000,000.
    ("0001067983", "0001193125-11-048914", 2008): Decimal("0.000001"),
    ("0001067983", "0001193125-12-079022", 2009): Decimal("0.000001"),
    # Bicara's FY2024 10-K prints 580,109 FY2023 weighted shares, $(89.61)
    # EPS and a $51.985M loss. Company Facts alone adds a thousands multiplier.
    ("0002023658", "0002023658-25-000012", 2023): Decimal("0.001"),
    # CION's old investment-company EPS tables state the denominator as roughly
    # 56.6M shares. Three later comparative facts multiply those absolute counts
    # by 1,000 even though their own per-share arithmetic remains unscaled.
    ("0001534254", "0001534254-23-000004", 2020): Decimal("0.001"),
    ("0001534254", "0001534254-24-000008", 2021): Decimal("0.001"),
    ("0001534254", "0001534254-25-000004", 2022): Decimal("0.001"),
    # Clipper's statements label their money and weighted-share columns in
    # thousands. These selected Company Facts retained the displayed share cell
    # without the table multiplier; the surrounding absolute history confirms it.
    ("0001649096", "0001437749-20-004934", 2017): Decimal("1000"),
    ("0001649096", "0001437749-21-006192", 2018): Decimal("1000"),
    ("0001649096", "0001437749-22-006301", 2019): Decimal("1000"),
    ("0001649096", "0001437749-22-006301", 2020): Decimal("1000"),
    ("0001649096", "0001437749-23-006886", 2021): Decimal("1000"),
    # ConocoPhillips prints diluted weighted shares in millions. Three old
    # comparative facts retain the displayed 1,245,440-style thousands value.
    ("0001163165", "0001193125-19-043841", 2016): Decimal("1000"),
    ("0001163165", "0001193125-20-039954", 2017): Decimal("1000"),
    ("0001163165", "0001562762-21-000027", 2018): Decimal("1000"),
    # Everest's EPS note explicitly describes dollar amounts and denominators in
    # thousands (40,420, 40,586, etc.); these six observations need that factor.
    ("0001095073", "0001095073-17-000011", 2014): Decimal("1000"),
    ("0001095073", "0001095073-18-000008", 2015): Decimal("1000"),
    ("0001095073", "0001095073-19-000011", 2016): Decimal("1000"),
    ("0001095073", "0001095073-20-000006", 2017): Decimal("1000"),
    ("0001095073", "0001095073-21-000006", 2018): Decimal("1000"),
    ("0001095073", "0001095073-22-000005", 2019): Decimal("1000"),
    # EHang's IFRS tables present ordinary-share denominators in thousands. The
    # receipt conversion is applied later, after restoring the table multiplier.
    ("0001759783", "0001193125-22-125165", 2019): Decimal("1000"),
    ("0001759783", "0001193125-23-122090", 2020): Decimal("1000"),
    ("0001759783", "0001193125-24-095477", 2021): Decimal("1000"),
    ("0001759783", "0001193125-25-080649", 2022): Decimal("1000"),
    ("0001759783", "0001193125-26-226608", 2023): Decimal("1000"),
    ("0001759783", "0001193125-26-226608", 2024): Decimal("1000"),
    # Full House Resorts' FY2012 statement contains absolute 18.4M/18.7M share
    # counts; the two Company Facts comparatives alone carry an extra 1,000x.
    ("0000891482", "0001188112-13-000562", 2011): Decimal("0.001"),
    ("0000891482", "0001188112-13-000562", 2012): Decimal("0.001"),
    # Gogo's primary statement labels the weighted-share table in thousands and
    # prints 127,205 diluted shares for FY2021 beside $1.28 diluted EPS.
    ("0001537054", "0000950170-24-022040", 2021): Decimal("1000"),
    # Cereplast's statement-level EPS identities put both old denominators near
    # 11.8M/16.0M; their comparative XBRL cells add a thousands multiplier.
    ("0001324759", "0001193125-12-164541", 2010): Decimal("0.001"),
    ("0001324759", "0001193125-12-164541", 2011): Decimal("0.001"),
    # Nuveen Churchill's FY2021 denominator is 12.849M; a later comparative alone
    # exposes it as 12.849B under the ordinary shares unit.
    ("0001737924", "0001628280-24-007039", 2021): Decimal("0.001"),
    # OmniAb's FY2025 statement prints 4,969 in a thousands share column, matching
    # its $10.549M loss and $(2.23) diluted EPS.
    ("0001756064", "0001493152-26-014133", 2025): Decimal("1000"),
    # Osisko Gold Royalties prints 127,939 in a thousands share column between
    # absolute 104.824M and 156.617M denominators.
    ("0001627272", "0001062993-19-001469", 2017): Decimal("1000"),
    # PagSeguro's reports print absolute share quantities (for example,
    # 332,174,824 diluted shares in FY2021). These seven selected comparative
    # facts add a spurious thousands multiplier.
    ("0001712807", "0001193125-18-152723", 2015): Decimal("0.001"),
    ("0001712807", "0001193125-19-106992", 2016): Decimal("0.001"),
    ("0001712807", "0001193125-20-115076", 2017): Decimal("0.001"),
    ("0001712807", "0001628280-21-007622", 2018): Decimal("0.001"),
    ("0001712807", "0001628280-22-011586", 2019): Decimal("0.001"),
    ("0001712807", "0001628280-23-014131", 2020): Decimal("0.001"),
    ("0001712807", "0001628280-24-018744", 2021): Decimal("0.001"),
    # Repay's statements put the 2021-2025 diluted denominators near 83M-111M;
    # the selected comparative observations alone are 1,000x too large.
    ("0001720592", "0000950170-24-023102", 2021): Decimal("0.001"),
    ("0001720592", "0000950170-25-030855", 2022): Decimal("0.001"),
    ("0001720592", "0001193125-26-098518", 2023): Decimal("0.001"),
    ("0001720592", "0001193125-26-098518", 2024): Decimal("0.001"),
    ("0001720592", "0001193125-26-098518", 2025): Decimal("0.001"),
    # Yatra's report prints 44,286,393 and 46,477,249, not billions.
    ("0001516899", "0001493152-22-007713", 2019): Decimal("0.001"),
    ("0001516899", "0001493152-22-007713", 2020): Decimal("0.001"),
    # Yum!'s old income statement presents the FY2008 diluted denominator as
    # 491 million; Company Facts retains only the displayed 491.
    ("0001041061", "0001041061-11-000009", 2008): Decimal("1000000"),
    # Twist's FY2017 table reports 2,422,243 weighted shares, $(24.49) EPS and
    # a $59.31m loss; the selected comparative count alone is 1,000x too large.
    ("0001581280", "0001193125-19-313115", 2017): Decimal("0.001"),
}

# The cited Berkshire statement also says, in words, that its reported count is
# on an equivalent Class A basis and each Class B share is 1/1,500 of Class A.
# Applying the reciprocal to EPS and the factor to counts puts the old derived
# history on the BRK-B security that today's quote and recent classed facts use.
_VERIFIED_EQUIVALENT_CLASS_HISTORY = {
    ("0001067983", "BRK-B"): Decimal("1500"),
}


def _restate_verified_equivalent_class_history(
    security_key: tuple[str, str], annual_eps: dict[int, Fact],
    annual_counts: dict[int, Fact],
) -> tuple[dict[int, Fact], dict[int, Fact]]:
    factor = _VERIFIED_EQUIVALENT_CLASS_HISTORY.get(security_key)
    if factor is None:
        return annual_eps, annual_counts
    eps_out, count_out = dict(annual_eps), dict(annual_counts)
    for year in set(eps_out) & set(count_out):
        eps, count = eps_out[year], count_out[year]
        # Direct classed EPS already states the priced security. Only a value
        # derived from whole-company income and the old equivalent-class count
        # needs conversion.
        if not eps.provenance.concept.startswith("EarningsPerShare (derived:"):
            continue
        ep, cp = eps.provenance, count.provenance
        basis = f"verified equivalent Class A to Class B basis, 1:{factor:g}"
        eps_out[year] = Fact(
            value=eps.value / factor,
            provenance=Provenance(
                concept=f"{ep.concept} ({basis})", tag=ep.tag,
                fiscal_year=ep.fiscal_year, form=ep.form,
                accession=ep.accession, filed=ep.filed,
                period_end=ep.period_end, period_start=ep.period_start,
                components=(ep,), segments=ep.segments, unit=ep.unit,
                document=ep.document, canonical_tag=ep.canonical_tag,
            ),
        )
        count_out[year] = Fact(
            value=count.value * factor,
            provenance=Provenance(
                concept=f"{cp.concept} ({basis})", tag=cp.tag,
                fiscal_year=cp.fiscal_year, form=cp.form,
                accession=cp.accession, filed=cp.filed,
                period_end=cp.period_end, period_start=cp.period_start,
                components=(cp,), segments=cp.segments, unit=cp.unit,
                document=cp.document, canonical_tag=cp.canonical_tag,
            ),
        )
    return eps_out, count_out


def _eps_uses_total_income(fact: Fact) -> bool:
    """Whether total parent income can legitimately be divided by this EPS.

    Continuing-operations EPS omits discontinued operations, partnership-unit EPS
    carries the allocation to that unit class, and investment-company operating
    EPS excludes realised/unrealised results. None has total net income as its
    numerator, so NI/EPS is neither a share count nor an identity check for them.
    """
    tag = fact.provenance.tag
    return not any(scope in tag for scope in (
        "ContinuingOperations", "LimitedPartnership", "InvestmentCompany",
    ))


def _depositary_dimension_conflict(
    bare: dict[int, Fact], classed: dict[int, Fact], receipt: dict | None,
) -> str | None:
    """Detect a priced ADS whose ordinary-share ratio is still unknown.

    The same filing can state undimensioned EPS per ordinary share and a second
    value on ``ClassOfStock=AmericanDepositaryShare``. When they differ materially
    and no cover ratio was recovered, choosing either basis silently makes P/E,
    market cap and every per-share book value wrong by the ratio. AMRN states
    roughly -$0.09 per ordinary share and -$1.87 per ADS in FY2025: evidence of a
    20:1 basis, but not authority to guess the legal ratio. Withhold instead.
    """
    if (receipt or {}).get("ratio"):
        return None
    for year in sorted(set(bare) & set(classed), reverse=True):
        ordinary, ads = bare[year], classed[year]
        segments = (ads.provenance.segments or "").lower()
        if not any(word in segments for word in ("depositary", "=adr", "shares=adr")):
            continue
        if (ordinary.value == 0 or ads.value == 0
                or ordinary.provenance.tag != ads.provenance.tag
                or ordinary.provenance.accession != ads.provenance.accession
                or ordinary.provenance.period_end != ads.provenance.period_end):
            continue
        ratio = abs(ads.value / ordinary.value)
        if ratio > _BASIS_TOLERANCE or ratio < 1 / _BASIS_TOLERANCE:
            return (f"FY{year} reports {ordinary.value} per ordinary share and {ads.value} "
                    f"per depositary share ({ratio:.2f}x apart), but the cover evidence "
                    "carries no depositary ratio: the price and per-share figures cannot "
                    "be put on one security basis")
        return None
    return None


def _restate_onto_receipt(snapshot_parts: dict, ratio: Decimal, accn: str) -> None:
    """Put every per-share figure onto the security the price belongs to.

    A depositary receipt stands for a fixed number of ordinary shares, and a
    filer's statements count the ordinary ones. The price does not: it is quoted
    per receipt. So earnings per receipt are the ordinary figure times the ratio,
    and the receipt count is the ordinary count divided by it — one transformation,
    applied to both sides, leaving every multiple on the page comparing like with
    like. Onconova's thirteen ordinary shares per receipt made its market
    capitalisation $550bn instead of $42bn.

    The ratio is transcribed from the cover of a named filing, so it carries an
    accession like any other figure here.
    """
    def restated(fact: Fact, factor: Decimal, what: str) -> Fact:
        p = fact.provenance
        return Fact(value=fact.value * factor, provenance=Provenance(
            concept=f"{p.concept} (per depositary receipt: {what})", tag=p.tag,
            fiscal_year=p.fiscal_year, form=p.form, accession=p.accession, filed=p.filed,
            period_end=p.period_end, period_start=p.period_start, components=p.components))

    per_receipt = f"x{ratio} ordinary shares, cover of {accn}"
    shares = snapshot_parts.get("shares")
    if shares is not None:
        snapshot_parts["shares"] = restated(shares, 1 / ratio, per_receipt)
    snapshot_parts["annual_share_counts"] = {
        y: restated(f, 1 / ratio, per_receipt)
        for y, f in (snapshot_parts.get("annual_share_counts") or {}).items()
    }
    for key in ("annual_eps",):
        snapshot_parts[key] = {y: restated(f, ratio, per_receipt)
                               for y, f in (snapshot_parts.get(key) or {}).items()}
    snapshot_parts["ttm_eps_inputs"] = tuple(
        restated(f, ratio, per_receipt) for f in snapshot_parts.get("ttm_eps_inputs", ()))
    snapshot_parts["ttm_eps_vintage"] = {
        end: value * ratio
        for end, value in (snapshot_parts.get("ttm_eps_vintage") or {}).items()
    }
    for key in ("ttm_eps", "dividend_per_share"):
        value = snapshot_parts.get(key)
        if value is not None:
            snapshot_parts[key] = value * ratio
    recurring = snapshot_parts.get("recurring_dividend_per_share")
    if recurring is not None:
        snapshot_parts["recurring_dividend_per_share"] = restated(
            recurring, ratio, per_receipt)


def _basis_conflict(gaap: dict, dei: dict, annual_eps: dict[int, Fact],
                    annual_ni: dict[int, Fact], preferred: dict[int, Fact],
                    has_nci: bool, shares: Fact | None = None) -> str | None:
    """Whether the earnings series and the share count describe the same security.

    A filer's own earnings per share times its own weighted share count is its own
    net income. Where the two disagree by half again, one of them belongs to
    something else — a depositary share standing for thirteen ordinary ones, a
    class the ticker does not represent, a count struck for a different entity —
    and every per-share figure built on the pair is wrong by that factor. SM Energy
    ships a P/E of 4.03 where its own filing gives 13.25.

    The weighted count is used, not today's, so an issue after the year end cannot
    masquerade as a mismatch. Minority interests, preferred dividends and a
    continuing-operations basis are legitimate wedges, so the check abstains there.

    One family stays out of reach: a depositary receipt whose statements count
    ordinary shares reconciles with its own earnings perfectly, and only the ratio
    on the filing cover — which Company Facts does not carry — says that the price
    belongs to a bundle of thirteen of them. Comparing the cover count against the
    statements does not separate that case from a second share class (Heico's cover
    states one class of two), so it is not attempted here.
    """
    if has_nci:
        return None
    counts = _annual_share_counts(gaap, dei, annual_eps, annual_ni, preferred)
    shared = sorted(set(annual_eps) & set(annual_ni) & set(counts), reverse=True)
    if not shared:
        return None
    # This is a current-security guard, so only the newest comparable fiscal year
    # may answer it. Skipping an ineligible recent year and walking backward made
    # ZWS compare today's 167M shares with FY2009's 69M, and did the same more than
    # three years backward for 81 companies. Corporate actions make that a history
    # lesson, not evidence that today's TTM EPS and today's price use different
    # securities.
    year = shared[0]
    eps, ni, count = annual_eps[year], annual_ni[year], counts[year]
    if (eps.value == 0 or count.value <= 0 or year in preferred
            or not _eps_uses_total_income(eps)):
        return None
    stated = eps.value * count.value
    if stated == 0 or ni.value == 0:
        return None
    ratio = ni.value / stated
    if ratio > _BASIS_TOLERANCE or ratio < 1 / _BASIS_TOLERANCE:
        return (f"FY{year} earnings per share of {eps.value} on {count.value / _MILLION:,.1f}M "
                f"weighted shares comes to {stated / _MILLION:,.0f}M against the "
                f"{ni.value / _MILLION:,.0f}M of net income the same filing reports: the "
                "two do not describe one security")
    # The series can reconcile with its own year and still not describe the
    # security being priced: SM Energy's earnings are struck on 115.0M weighted
    # shares while the count every per-share figure divides by is 237.5M. A
    # price against that pair mixes two bases, whichever of them is right. The
    # two counts must also be near enough in time for ordinary issuance, buybacks
    # and splits not to explain the difference.
    share_end, count_end = shares and shares.provenance.period_end, count.provenance.period_end
    if (shares is not None and shares.value > 0 and share_end and count_end
            and abs((share_end - count_end).days) <= _DERIVED_EPS_SHARE_LAG):
        drift = shares.value / count.value
        if drift > _BASIS_TOLERANCE or drift < 1 / _BASIS_TOLERANCE:
            return (f"FY{year} earnings are struck on {count.value / _MILLION:,.1f}M "
                    f"weighted shares while the current count is "
                    f"{shares.value / _MILLION:,.1f}M, {drift:.2f}x apart: a per-share "
                    "figure cannot mix the two")
    return None


def _has_minority_interest(gaap: dict, fresh: date | None, nci: Fact | None) -> bool:
    """Whether minority holders own part of this balance sheet. The NCI tag is
    not the only evidence: Ares tags no MinorityInterest at all, yet its equity
    including noncontrolling interests is more than twice its parent equity."""
    if nci is not None and nci.value != 0:
        return True
    # A partnership files neither StockholdersEquity element. Westlake Chemical
    # Partners consolidates an OpCo two thirds owned by its sponsor and shows it
    # as PartnersCapital 253.7M inside 769.4M including the minority — a 3.03x
    # signal invisible to the corporate pair, which is why its whole group profit
    # was being divided by its own unit count.
    for incl, parent_tag in (
        ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
         "StockholdersEquity"),
        ("PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest",
         "PartnersCapital"),
    ):
        both = _latest_instant(gaap, "Equity (incl. NCI)", (incl,), not_before=fresh)
        parent = _latest_instant(gaap, "Equity (parent)", (parent_tag,), not_before=fresh)
        if both is None or parent is None or both.value == 0:
            continue
        if abs(both.value - parent.value) / abs(both.value) > Decimal("0.02"):
            return True
    return False


def _annual_share_counts(gaap: dict, dei: dict,
                         annual_eps: dict[int, Fact] | None = None,
                         annual_ni: dict[int, Fact] | None = None,
                         annual_preferred: dict[int, Fact] | None = None) -> dict[int, Fact]:
    """The share count a filer itself divided by for a given year.

    Only the weighted average serves. The count on a report cover looked like a
    reasonable stand-in and is not: measured against the figures companies
    actually report, it produced errors of exactly five, ten and a hundred and
    fifty times — a reverse split leaves today's small count against an old
    year's income, and a multi-class filer's cover names one class while the
    income belongs to all of them. Both failures are silent and both read as a
    bargain, so the stand-in is gone: a year without its own weighted average
    keeps no derived figure.

    A second, independently provable defect is repaired here. Some filings label
    their statement "shares in thousands" or "shares in millions" but expose the
    table value under the plain XBRL `shares` unit. McDonald's consequently reaches
    Company Facts as 716.4 shares even though the statement means 716.4 million.
    Only an exact thousand/million factor is accepted, only within one accession,
    and only when EPS x the rescaled count reconciles to that filing's income
    within five per cent. A share-class, split or ADR mismatch has no reason to
    land on that exact scale and remains blocked by `_basis_conflict`.
    """
    counts = _annual_union(gaap, _WEIGHTED_SHARE_TAGS, unit=("shares",))
    # A zero diluted denominator is not a share count.  Some loss filings tag
    # the anti-dilutive diluted row as zero while the same statement reports a
    # positive basic weighted count (Guided Therapeutics FY2017).  Fall through
    # the established tag preference for that year only; if no positive sibling
    # exists, preserve missing rather than serializing a false zero.
    if counts:
        alternatives = {
            tag: _annual_series(gaap, tag, unit=("shares",))
            for tag in _WEIGHTED_SHARE_TAGS
        }
        for year, count in tuple(counts.items()):
            if count.value > 0:
                continue
            replacement = next((
                series[year]
                for tag in _WEIGHTED_SHARE_TAGS
                if (series := alternatives[tag]).get(year) is not None
                and series[year].value > 0
            ), None)
            if replacement is None:
                del counts[year]
            else:
                counts[year] = replacement
    cik = str(getattr(gaap, "cik", "")).zfill(10)
    verified_counts: dict[int, Fact] = {}
    for year, count in counts.items():
        factor = _VERIFIED_ANNUAL_SHARE_PRESENTATION_SCALES.get(
            (cik, count.provenance.accession, year))
        if factor is None:
            verified_counts[year] = count
            continue
        p = count.provenance
        verified_counts[year] = Fact(
            value=count.value * factor,
            provenance=Provenance(
                concept=(f"{p.concept} (scaled {factor:g}x: primary statement "
                         "share presentation verified)"),
                tag=p.tag, fiscal_year=p.fiscal_year, form=p.form,
                accession=p.accession, filed=p.filed, period_end=p.period_end,
                period_start=p.period_start, components=(p,), segments=p.segments,
                unit=p.unit, document=p.document, canonical_tag=p.canonical_tag,
            ),
        )
    counts = verified_counts
    if not counts:
        return counts

    current_count = (
        _latest_instant(
            gaap, "SharesOutstanding", ("CommonStockSharesOutstanding",),
            unit=("shares",))
        or _latest_instant(
            dei, "SharesOutstanding", ("EntityCommonStockSharesOutstanding",),
            ns="dei", unit=("shares",))
    )
    if not annual_eps or not annual_ni:
        return counts

    preferred = annual_preferred or _annual_union(gaap, PREFERRED_DIVIDEND_TAGS)
    direct_common = _annual_dollar_series(gaap, COMMON_INCOME_TAGS)
    continuing_income = _annual_dollar_series(gaap, CONTINUING_INCOME_TAGS)
    # Use the cells from the count's own filing, before the selected EPS history
    # is restated for later stock splits. Comparing a 2016 count with today's
    # split-adjusted EPS made PACCAR's 351.8 million-share row miss the exact
    # million multiplier; selecting EPS from an older accession did the same to
    # other comparatives. The filing-local identity is stronger than either
    # transformed history.
    filed_eps = _annual_facts_by_accession(
        gaap, EPS_TAGS + (EPS_CONTINUING_TAG,) + EPS_BASIC_TAGS,
        ("USD/shares",), "EarningsPerShare")
    filed_common = _annual_facts_by_accession(
        gaap, COMMON_INCOME_TAGS, ("USD",), "Income available to common")
    filed_parent = _annual_facts_by_accession(
        gaap, PARENT_INCOME_TAGS, ("USD",), "Net income attributable to parent")
    filed_continuing = _annual_facts_by_accession(
        gaap, CONTINUING_INCOME_TAGS, ("USD",), "Income from continuing operations")
    filed_preferred = _annual_facts_by_accession(
        gaap, PREFERRED_DIVIDEND_TAGS, ("USD",), "Preferred dividends")
    out = dict(counts)

    def apply_scale(
        year: int, count: Fact, eps: Fact, income: Fact | None,
        adjustment: Fact | None, factors: tuple[Decimal, ...],
        *, allow_rounded_eps: bool = False,
    ) -> bool:
        if count.value <= 0 or eps.value == 0 or income is None:
            return False
        evidence = (count, eps, income) + ((adjustment,) if adjustment else ())
        if len({f.provenance.accession for f in evidence}) != 1:
            return False
        ends = {f.provenance.period_end for f in evidence if f.provenance.period_end}
        if len(ends) != 1:
            return False
        common = income.value - (adjustment.value if adjustment else Decimal(0))
        # A later comparative can itself carry the exact table-scale defect that
        # the duration-series reconciler has already retired.  Golden Sun's
        # FY2022 20-F, for example, repeats $2.139M of loss as $2.139B while its
        # 1.443M weighted count is sound.  Trusting those two raw cells together
        # would merely move the known 1,000x defect from income into shares.
        # The normalized annual line is independent evidence: when it differs
        # from this filing-local numerator by an exact presentation scale, the
        # local numerator is not allowed to prove a share-count rewrite.
        normalized = annual_ni.get(year)
        if normalized is not None and normalized.value and common:
            conflict = _scale_ratio(common, normalized.value)
            if conflict in _DURATION_SCALE_FACTORS:
                return False
        implied = common / eps.value
        if implied <= 0:
            return False
        def reconciles(
            candidate_count: Decimal, candidate_factor: Decimal | None = None,
        ) -> bool:
            if candidate_count <= 0:
                return False
            relative = abs(candidate_count - implied) / abs(implied)
            if relative <= _SHARE_SCALE_TOLERANCE:
                return True
            # Filing-local EPS is normally printed to cents.  Around zero, an
            # honest half-cent rounding interval is much wider than five per cent
            # of the implied count: -$0.04 on 564.188M shares can represent a
            # $20.812M loss.  Test the reported per-share value directly instead
            # of inventing precision the statement never supplied.  This is not
            # used with the selected split-adjusted EPS series above.
            return bool(
                allow_rounded_eps
                # A rounded cent may prove that a displayed thousands count is
                # missing its table multiplier. It must not turn an already
                # plausible multi-million count into billions merely to absorb
                # an unrelated numerator/EPS scope mismatch (GTBP/PIAC/DLTI).
                and (candidate_factor is None or candidate_factor < 1
                     or count.value < _MILLION)
                and common * eps.value > 0
                and abs(common / candidate_count - eps.value)
                <= Decimal("0.0055")
            )
        # A direct numerator that already reconciles the raw count settles the
        # question.  Do not continue to a weaker preferred/if-converted fallback:
        # NOTVQ's sound 7.96M count and -$1.07M common loss agree with -$0.13 EPS,
        # while a malformed $991M preferred-dividend tag would otherwise invent
        # a second, spurious 1,000x interpretation.
        if reconciles(count.value):
            return True
        factor = next((candidate for candidate in factors
                       if reconciles(count.value * candidate, candidate)), None)
        if factor is None or not _share_scale_supported_by_history(
                counts, year, factor,
                current_count.value if current_count is not None else None):
            return False
        p = count.provenance
        out[year] = Fact(
            value=count.value * factor,
            provenance=Provenance(
                concept=f"{p.concept} (scaled {factor:g}x: reconciled to EPS and income)",
                tag=p.tag, fiscal_year=p.fiscal_year, form=p.form,
                accession=p.accession, filed=p.filed, period_end=p.period_end,
                period_start=p.period_start,
                components=tuple(f.provenance for f in evidence), segments=p.segments,
            ),
        )
        return True

    # Preserve the established selected-series repair. It is intentionally
    # upward-only: without filing-local evidence, reducing a plausible absolute
    # share count could merely compensate for a malformed EPS or income fact.
    upward = tuple(f for f in _SHARE_SCALE_FACTORS if f > 1)
    for year in set(counts) & set(annual_eps) & set(annual_ni):
        count, eps = counts[year], annual_eps[year]
        direct = parent = parent_adjustment = None
        if "ContinuingOperations" in eps.provenance.tag:
            income = continuing_income.get(year)
            adjustment = preferred.get(year)
        elif _eps_uses_total_income(eps):
            direct = direct_common.get(year)
            income = direct or annual_ni[year]
            adjustment = None if direct is not None else preferred.get(year)
            parent = annual_ni.get(year)
            parent_adjustment = preferred.get(year)
            parent_evidence = (count, eps, parent) + (
                (parent_adjustment,) if parent_adjustment else ())
            if (direct is not None and parent is not None
                    and len({f.provenance.accession for f in parent_evidence}) == 1
                    and len({f.provenance.period_end for f in parent_evidence
                             if f.provenance.period_end}) == 1):
                parent_common = parent.value - (
                    parent_adjustment.value if parent_adjustment else Decimal(0))
                if (parent_common
                        and abs(count.value * eps.value - parent_common)
                        <= abs(parent_common) * _SHARE_SCALE_TOLERANCE):
                    income = parent
                    adjustment = parent_adjustment
        else:
            continue
        repaired = apply_scale(year, count, eps, income, adjustment, upward)
        # Diluted EPS can add a convertible preferred back to its numerator.
        # SB Financial's statement is explicit: $1.19 diluted EPS is struck on
        # 6.423M shares and the full $7.619M income, whereas its basic common
        # numerator is $6.663M.  Prefer the common line, but when it cannot prove
        # an exact table scale, let the same-filing parent line try both the
        # ordinary preferred deduction and the if-converted numerator.
        if not repaired and direct is not None and parent is not None:
            repaired = apply_scale(
                year, count, eps, parent, parent_adjustment, upward)
            if not repaired and parent_adjustment is not None:
                apply_scale(year, count, eps, parent, None, upward)

    # Then inspect the unmodified cells from the count's own accession. This
    # recovers comparisons hidden by a later split/restatement and permits a
    # reciprocal scale only when independent share-count history supports it.
    for year, count in counts.items():
        end = count.provenance.period_end
        key = (count.provenance.accession, end.isoformat() if end else "")
        eps = filed_eps.get(key)
        if eps is None:
            continue
        direct = parent = parent_adjustment = None
        if "ContinuingOperations" in eps.provenance.tag:
            income = filed_continuing.get(key)
            adjustment = filed_preferred.get(key)
        elif _eps_uses_total_income(eps):
            direct = filed_common.get(key)
            parent = filed_parent.get(key)
            adjustment = filed_preferred.get(key)
            income = direct or parent
            if direct is not None:
                adjustment = None
            elif (income is not None
                  and "AvailableToCommon" in income.provenance.tag):
                adjustment = None
            # The parent line independently protects a good count when a
            # sibling common-income fact is the malformed cell (KFFB).
            parent_adjustment = filed_preferred.get(key)
            if (parent is not None
                    and "AvailableToCommon" in parent.provenance.tag):
                parent_adjustment = None
            parent_common = (parent.value - (
                parent_adjustment.value if parent_adjustment else Decimal(0))
                if parent is not None else None)
            if (direct is not None and parent_common
                    and abs(count.value * eps.value - parent_common)
                    <= abs(parent_common) * _SHARE_SCALE_TOLERANCE):
                income = parent
                adjustment = parent_adjustment
        else:
            continue
        repaired = apply_scale(
            year, count, eps, income, adjustment, _SHARE_SCALE_FACTORS,
            allow_rounded_eps=True)
        if not repaired and direct is not None and parent is not None:
            repaired = apply_scale(
                year, count, eps, parent, parent_adjustment,
                _SHARE_SCALE_FACTORS, allow_rounded_eps=True)
            if not repaired and parent_adjustment is not None:
                apply_scale(
                    year, count, eps, parent, None, _SHARE_SCALE_FACTORS,
                    allow_rounded_eps=True)

    # A rounded one-cent EPS can be too imprecise to prove the table multiplier
    # arithmetically.  Capital City Bank's FY2012 $0.01 on 17.220M shares, for
    # example, cannot reproduce $108K of income within five per cent.  Two
    # independently filed adjacent counts do prove that an isolated 17,220 cell
    # is expressed in thousands.  Keep this deliberately narrower than the
    # filing-local path: both immediate neighbours must agree with one another
    # and with one exact thousand/million correction of the middle observation.
    for year, count in tuple(sorted(out.items())):
        if "(scaled " in count.provenance.concept:
            continue
        before, after = out.get(year - 1), out.get(year + 1)
        if (before is None or after is None or count.value <= 0
                or before.value <= 0 or after.value <= 0):
            continue
        reference = (before.value + after.value) / 2
        if abs(before.value - after.value) / reference > Decimal("0.10"):
            continue
        factor = next((candidate for candidate in _SHARE_SCALE_FACTORS
                       if abs(count.value * candidate - reference) / reference
                       <= _SHARE_SCALE_TOLERANCE), None)
        if factor is None:
            continue
        p = count.provenance
        out[year] = Fact(
            value=count.value * factor,
            provenance=Provenance(
                concept=(f"{p.concept} (scaled {factor:g}x: adjacent filed "
                         "share counts agree)"),
                tag=p.tag, fiscal_year=p.fiscal_year, form=p.form,
                accession=p.accession, filed=p.filed, period_end=p.period_end,
                period_start=p.period_start,
                components=(p, before.provenance, after.provenance),
                segments=p.segments, unit=p.unit, document=p.document,
                canonical_tag=p.canonical_tag,
            ),
        )

    return dict(sorted(out.items()))


def _derived_annual_eps(gaap: dict, dei: dict, annual_eps: dict[int, Fact],
                        annual_ni: dict[int, Fact], annual_preferred: dict[int, Fact],
                        has_nci: bool = False) -> dict[int, Fact]:
    """Per-share earnings for years the filer reported income but tagged no
    per-share element: (net income - preferred dividends) / that year's share
    count. Both figures are the company's own and belong to the same year; the
    provenance names both tags so no reader mistakes it for a reported EPS.

    ProfitLoss includes noncontrolling interests, which are not the common
    shareholder's earnings — dividing it by the common share count inflates EPS
    and makes the price look cheap. It serves only where the balance sheet shows
    no minority interest to inflate it (ARES would have read 2.60 against a
    genuine 3.00-odd; a co-op with no NCI is unaffected)."""
    counts = _annual_share_counts(gaap, dei, annual_eps, annual_ni, annual_preferred)
    # Where the group figure would be divided by the parent's own count, the
    # parent-attributable series takes its place — refusing the year outright
    # would leave a partnership with no per-unit figure at all when its own
    # income statement carries one.
    parent_ni = _annual_dollar_series(gaap, PARENT_INCOME_TAGS) if has_nci else {}
    out: dict[int, Fact] = {}
    for year, income in annual_ni.items():
        if year in annual_eps:
            continue
        if has_nci and _tag_of(income) == "ProfitLoss":
            income = parent_ni.get(year)
            if income is None:
                continue
        count = counts.get(year)
        if count is None or count.value <= 0:
            continue
        end = income.provenance.period_end
        share_end = count.provenance.period_end
        if end and share_end and (share_end - end).days > _DERIVED_EPS_SHARE_LAG:
            continue  # a count struck long after the year describes a different company
        preferred = annual_preferred.get(year)
        common = income.value - (preferred.value if preferred else Decimal(0))
        p, q = income.provenance, count.provenance
        out[year] = Fact(
            value=common / count.value,
            provenance=Provenance(
                concept="EarningsPerShare (derived: earnings available to common / share count)",
                tag=f"{p.tag} / {q.tag}",
                fiscal_year=year, form=p.form, accession=p.accession,
                filed=p.filed, period_end=p.period_end, period_start=p.period_start,
            ),
        )
    return out


_UNTAXED_SHARE = Decimal("0.01")  # tax this small against a profit is no tax at all


def _tax_record(gaap: dict, fresh: date | None) -> dict | None:
    """Profitable years, and how many of them carried effectively no income tax.

    Penn Central reported profits and paid no income tax for eleven years before
    it failed; Graham's reading was that the tax authorities did not believe the
    earnings and neither should the investor. The counting happens here, on the
    facts. Whether it is a warning or merely a pass-through structure doing what
    pass-through structures do is decided where the company's profile is known.
    """
    tax = _annual_union(gaap, TAX_TAGS)
    pretax = _annual_union(gaap, PRETAX_TAGS) or _annual_union(gaap, PRETAX_INCOME_TAGS)
    shared = sorted(set(tax) & set(pretax), reverse=True)[:10]
    if len(shared) < 5:
        return None
    profitable = [y for y in shared if pretax[y].value > 0]
    untaxed = [y for y in profitable if tax[y].value <= abs(pretax[y].value) * _UNTAXED_SHARE]
    return {
        "window_from": min(shared), "window_to": max(shared),
        "profitable_years": len(profitable), "untaxed_years": len(untaxed),
        # a partnership or an investment company owes no entity-level tax by
        # design, and neither does a REIT — but only the first two are legible
        # from the facts themselves
        "pass_through": _is_partnership(gaap, fresh) or bool(
            _annual_union(gaap, ("InvestmentCompanyInvestmentIncomeLossFromOperationsPerShare",
                                 "InvestmentIncomeOperatingAfterExpenseAndTax"))),
    }


# A dimension answers "which slice of the company", and most slices are not the
# company: a segment, a geography, a parent-only view. Only axes naming the class
# of traded security may supply its figure. Partnerships call that security a
# common unit and place it on LimitedPartnersCapitalAccountByClass; omitting that
# standard axis hid PAA's filed per-unit EPS after 2016 (and five current series in
# the cached universe) even though the cover identifies the listed unit class.
_CLASS_AXES = frozenset((
    "ClassOfStock", "StatementClassOfStock", "EquityClassOfStock",
    "LimitedPartnersCapitalAccountByClass", "ClassesOfShareCapital",
))

# A company-specific filing-backed bridge for an issuer whose standard facts do
# not identify the priced security.  U-Haul's FY2023-FY2026 reports allocate EPS
# and dividends between UHAL Voting Common and UHAL.B Series N Non-Voting Common.
# DERA preserves the voting class as these two issuer-defined members, while the
# Company Facts API flattens the non-voting sibling into UHAL.  ``EquityComponents``
# is also widely used for equity roll-forwards and is not a safe global class axis;
# keep this exception exact to the verified CIK+ticker instead.
_VERIFIED_SECURITY_DIMENSIONS = {
    ("0000004457", "UHAL"): frozenset((
        ("EquityComponents", "CommonStock"),
        ("ClassOfStock", "AmercoCommonStock"),
        # The FY2026 filing moved the same voting security to the standard
        # CommonClassA member; the report table still labels it Voting Stock.
        ("ClassOfStock", "CommonClassA"),
    )),
}

_VERIFIED_SECURITY_SIBLING_DIMENSIONS = {
    ("0000004457", "UHAL"): frozenset((
        ("ClassOfStock", "NonvotingCommonStock"),
    )),
}


def _sum_verified_share_facts(
    left: Fact | None, right: Fact | None, fiscal_year: int | None,
) -> Fact | None:
    """Sum two explicitly verified common classes from one filing context."""
    if left is None or right is None:
        return None
    lp, rp = left.provenance, right.provenance
    if (lp.period_end != rp.period_end or lp.period_start != rp.period_start
            or lp.accession != rp.accession or lp.form != rp.form):
        return None
    latest = max((lp, rp), key=lambda p: p.filed)
    return Fact(
        value=left.value + right.value,
        provenance=Provenance(
            concept="Shares outstanding (verified sum of listed common classes)",
            tag=f"{lp.tag} + {rp.tag}", fiscal_year=fiscal_year,
            form=latest.form, accession=latest.accession, filed=latest.filed,
            period_end=latest.period_end, period_start=latest.period_start,
            components=(lp, rp), unit=latest.unit,
        ),
    )


def _verified_total_current_shares(
    classed: dict, sibling: dict, fresh: date | None,
) -> Fact | None:
    left = _latest_instant(
        classed, "SharesOutstanding", ("CommonStockSharesOutstanding",),
        unit=("shares",), not_before=fresh)
    right = _latest_instant(
        sibling, "SharesOutstanding", ("CommonStockSharesOutstanding",),
        unit=("shares",), not_before=fresh)
    return _sum_verified_share_facts(left, right, None)


def _verified_total_annual_shares(
    classed: dict, sibling: dict,
) -> dict[int, Fact]:
    left = _annual_share_counts(classed, {})
    right = _annual_share_counts(sibling, {})
    return {
        year: total
        for year in sorted(set(left) & set(right))
        if (total := _sum_verified_share_facts(left[year], right[year], year))
        is not None
    }


def _dimensioned_statement_taxonomies(
    dimensioned: dict, statement_basis: str | None = None,
) -> list[dict]:
    facts = dimensioned.get("facts", {}) or {}
    if statement_basis == "us-gaap":
        return [facts.get("us-gaap", {})] if facts.get("us-gaap") else []
    if statement_basis == "ifrs-full":
        return [
            taxo for namespace, taxo in facts.items()
            if namespace == "ifrs-full" or namespace.startswith("ext:ifrs/")
        ]
    return [
        taxo for namespace, taxo in facts.items()
        if namespace in ("us-gaap", "ifrs-full") or namespace.startswith("ext:ifrs/")
    ]


def _registered_class_title(ticker: str, receipt: dict | None) -> str | None:
    """The cover's class title, or the class encoded in a ticker such as BH-A.

    SEC's ticker mapping itself distinguishes Berkshire/BH Class A from Class B
    even when no parsed cover record survived. A one-letter hyphen suffix is a
    common-class identifier; preferred conventions such as ``-PB`` deliberately
    do not match this fallback.
    """
    title = (receipt or {}).get("title")
    if title:
        # Older cover parsing stopped the trading-symbol cell at its first space.
        # A preferred symbol such as "GLP pr B" consequently became "GLP" and
        # overwrote the common row in SQLite; KKR suffered the same collision.
        # A plain exchange ticker cannot identify that preferred/redeemable row.
        # Ignore the contradictory cached title and let the sidecar's ordinary-
        # class ambiguity rules decide; never reinterpret the preferred itself.
        plain = bool(re.fullmatch(r"[A-Z0-9.]+", ticker or ""))
        # "redeemable" alone is not enough: a listed SPAC unit legitimately says
        # it contains a redeemable warrant, while its Class B founder count must
        # still be rejected (BCSS/CPTKW). GLP's overwritten row is specifically a
        # named redeemable Series B; KKR says Preferred outright.
        noncommon = bool(
            re.search(r"\bpreferred\b", title, re.I)
            or (re.search(r"\bseries\b", title, re.I)
                and re.search(r"\bredeemable\b", title, re.I))
        )
        if plain and noncommon:
            return None
        return title
    match = re.search(r"-([A-Z])$", ticker or "")
    return f"Class {match.group(1)} Common Stock" if match else None


# Where a rendered cover cell ends and the next one begins. The reader takes the
# cell's text and the renderer sometimes runs two together, so 126 of 5,791 stored
# titles trail into "Security Exchange Name NYSE" or "Document Information [Line
# Items]" — decoration that drowns the two or three words naming the class.
_CELL_RAN_ON = re.compile(
    r"\s*(?:Security Exchange Name|Document Information|Entity Incorporation"
    r"|Trading Symbol|No Trading Symbol|\[Line Items\]|\[Member\]|Title of \w+ class)",
    re.I)


def _class_member(title: str) -> frozenset[str]:
    """The words that identify a share class, from either side of the question.

    A cover page says "Common Stock, one dollar par value" and a dimension member
    says "CommonStock"; a cover says "Class A Common Stock" and the member says
    "CommonClassA". Reduced to word sets the two are comparable, and the par value
    and other decoration fall away.
    """
    title = _CELL_RAN_ON.split(title, 1)[0]
    # a lone capital is a word here: "Class A" and "Class B" differ by exactly one
    # letter, and a pattern needing a lowercase tail dropped it, leaving the two
    # classes identical and every dual-class cover unmatchable
    words = re.findall(r"[A-Z][a-z]*|[a-z]+", re.sub(r"[^A-Za-z ]", " ", title))
    kept = {w.lower() for w in words if w.lower() not in {
        "stock", "shares", "share", "par", "value", "per", "the", "of", "and",
        "one", "dollar", "no", "common", "voting", "vote",
        # LP covers spell out "Common Units Representing Limited Partner
        # Interests" while the taxonomy member compresses it to CommonUnits.
        # These are the legal wrapper, not the class discriminator. Class A,
        # subordinated, and Series B survive this reduction and remain distinct.
        "unit", "units", "representing", "limited", "partner", "partners",
        "partnership", "interest", "interests", "capital", "account", "equity",
        "fixed", "rate", "cumulative", "redeemable",
        # Berkshire states EPS and weighted shares on an economically equivalent
        # Class A/Class B basis. "Equivalent" describes the unit of measure, not
        # a third legal share class, so it must not defeat the cover/ticker match.
        "equivalent",
    }}
    # Some covers shorten "Series B Preferred Units" to "Series B Fixed Rate
    # Cumulative Redeemable". Once a series letter/number is present it is the
    # discriminator; the optional word "preferred" must not defeat the match.
    if "series" in kept:
        kept.discard("preferred")
    return frozenset(kept) or frozenset({"common"})


def _is_equivalent_class_basis(fact: Fact) -> bool:
    """Whether a classed fact states the whole security on an equivalent basis.

    A plain Class A weighted count is only one slice and cannot divide total common
    equity. Berkshire's ``EquivalentClassB`` is different: the filing has converted
    both legal classes into one economic Class B unit, so it is a valid denominator
    for whole-company book value as well as Class B EPS.
    """
    return "equivalentclass" in (fact.provenance.segments or "").lower()


def _dimensioned_class(
    dimensioned: dict, registered: str | None, statement_basis: str | None = None,
) -> str | None:
    """Which dimension member is the class the ticker registers, when it can be told.

    Hershey reports earnings per share for its Common Stock and its Class B
    together; Company Facts drops both because they are dimensioned, and the
    ambiguity rule below then refuses both because there are two. The cover of its
    own 10-Q names which one the symbol is — "Common Stock, one dollar par value" —
    and that is the missing half. Where the cover says nothing or the match is not
    unique, the refusal stands: a wrong class is worse than no class.
    """
    if not registered:
        return None
    want = _class_member(registered)
    members = {p.split("=", 1)[1]
               for taxo in _dimensioned_statement_taxonomies(dimensioned, statement_basis)
               for data in taxo.values()
               for entries in (data.get("units") or {}).values()
               for e in entries
               for p in (e.get("segments") or "").split(";")
               if p and p.split("=", 1)[0] in _CLASS_AXES}
    matched = [m for m in members if _class_member(m) == want]
    return matched[0] if len(matched) == 1 else None


def _a_different_class(segments: str, chosen: str | None) -> bool:
    """Whether this fact belongs to a share class the ticker is not.

    One class reported for a period reads as unambiguous, and usually is. But a
    blank-cheque company files a weighted share count for its founders' Class B and
    none at all for the Class A its ticker prices, and taking the only class on
    offer then counts the wrong shareholders: BCSS's cover registers "Units, each
    consisting of one Class A ordinary share", and the panel showed the 10,000,000
    founder shares rather than the public stock.

    Only ever refuses — where the cover names no class, the single-class reading
    stands as before.
    """
    if not chosen:
        return False
    member = next((p.split("=", 1)[1] for p in (segments or "").split(";")
                   if p and p.split("=", 1)[0] in _CLASS_AXES), None)
    return member is not None and member != chosen


def _clearly_noncommon_class(segments: str) -> bool:
    """A lone dimension member that cannot be the ordinary/common security.

    With no usable cover, a single common class remains the conservative useful
    fallback. A preferred, subordinated, founder or general-partner member does
    not: ELDN's only dimensioned EPS is for unlisted Series X convertible
    preferred stock, and treating "only one member" as "the ticker" would assign
    that preferred security's earnings to ELDN common.
    """
    member = next((p.split("=", 1)[1] for p in (segments or "").split(";")
                   if p and p.split("=", 1)[0] in _CLASS_AXES), "")
    return bool(re.search(r"preferred|subordinated|founder|generalpartner", member, re.I))


def _unambiguous_dimensioned(
    dimensioned: dict | None,
    registered: str | None = None,
    statement_basis: str | None = None,
    *,
    verified_members: frozenset[tuple[str, str]] | None = None,
) -> dict:
    """The dimensioned facts that can only mean one thing, shaped like the facts
    Company Facts returns so the ordinary chains can read them.

    Two conditions. The dimension must be a share class alone — a fact carrying a
    segment or a geography describes part of a business, and a fact carrying two
    axes at once describes a corner of it. And the class must be identifiable: one
    class reported for that concept and period, or several of which the filing's
    own cover page names the one this ticker registers.
    """
    if not dimensioned:
        return {}
    chosen = (None if verified_members else
              _dimensioned_class(dimensioned, registered, statement_basis))
    # A cover that names a class but matches no member is contradictory evidence,
    # not permission to take whichever class happens to be the only one reported.
    if registered and not chosen and not verified_members:
        return {}
    facts = dimensioned.get("facts", {}) or {}
    taxo = facts.get("us-gaap", {}) if statement_basis != "ifrs-full" else {}
    if not taxo:
        combined_ifrs: dict[str, dict] = {}
        for namespace, source in facts.items():
            if namespace == "ifrs-full" or namespace.startswith("ext:ifrs/"):
                combined_ifrs.update(source)
        taxo = _ifrs_as_us_gaap(combined_ifrs)
    out: dict[str, dict] = {}
    for tag, data in taxo.items():
        for unit, entries in (data.get("units") or {}).items():
            by_period: dict[tuple, list[dict]] = {}
            for e in entries:
                segments = e.get("segments") or ""
                pairs = [tuple(p.split("=", 1)) for p in segments.split(";") if "=" in p]
                if len(pairs) != 1:
                    continue
                if verified_members:
                    if pairs[0] not in verified_members:
                        continue
                elif pairs[0][0] not in _CLASS_AXES:
                    continue
                by_period.setdefault((e.get("start"), e.get("end")), []).append(e)
            if verified_members:
                keep = [e for group in by_period.values() for e in group]
                if keep:
                    out.setdefault(tag, {"units": {}})["units"][unit] = keep
                continue
            keep = [e for group in by_period.values()
                    if len({g["segments"] for g in group}) == 1
                    and not _a_different_class(group[0]["segments"], chosen)
                    and (chosen is not None
                         or not _clearly_noncommon_class(group[0]["segments"]))
                    for e in group]
            if not keep and chosen:
                # several classes, and the cover says which one the ticker is
                keep = [e for group in by_period.values() for e in group
                        if e["segments"].split("=", 1)[-1].rstrip(";") == chosen]
            if keep:
                out.setdefault(tag, {"units": {}})["units"][unit] = keep
    return out


def _preferred_outstanding(dimensioned: dict | None, fresh: date | None) -> Decimal | None:
    """How much preferred is still on the books, counting every class at once.

    A different question from `_unambiguous_dimensioned`, which picks the one class
    a ticker registers. Here the classes are summed rather than chosen: what
    settles whether a tagged conversion is still ahead of the company is simply
    whether ANY convertible preferred remains, so an ambiguous split across three
    series is not ambiguity at all. Zero is a real answer here and means the
    conversion has already happened — the shares it created are inside the common
    count, and adding them again would count them twice.
    """
    if not dimensioned:
        return None
    floor = fresh.isoformat() if fresh else ""
    facts = dimensioned.get("facts", {}).get("us-gaap", {}) or {}
    for tag in PREFERRED_COUNT_TAGS:
        entries = [e for e in ((facts.get(tag) or {}).get("units", {}) or {}).get("shares", [])
                   if "start" not in e and e.get("end", "") >= floor
                   and _is_financial_form(e.get("form", ""))]
        if not entries:
            continue
        newest = max(e["end"] for e in entries)
        latest = [e for e in entries if e["end"] == newest]
        # one filing's view of that date, the latest-filed, so a restatement does
        # not get added to the figure it replaced
        filed = max(e.get("filed", "") for e in latest)
        latest = [e for e in latest if e.get("filed", "") == filed]
        by_class: dict[str, Decimal] = {}
        for e in latest:
            by_class[e.get("segments") or ""] = _dec(e["val"])
        if "" in by_class and len(by_class) > 1:
            by_class.pop("")          # a total beside its own parts would double
        return sum(by_class.values(), Decimal(0))
    return None


def _as_converted_note(gaap: dict, shares: Fact | None, fresh: date | None,
                       dimensioned: dict | None = None) -> str | None:
    """What a convertible preferred would do to the share count, said rather than done.

    Graham's table 18.6 counts McGraw-Hill's shares "including the conversion of
    preferred" and strikes book value on that larger number, and the arithmetic is
    plainly right when you are reading the filing: the preferred converts, the
    common grows, the senior claim disappears. The elements that would let a
    machine do it cannot carry the same meaning, because three different facts are
    tagged with the same name:

      * a conversion that has ALREADY happened, its shares inside the common count
        (Structure Therapeutics tags 67.0M against 2023-02-07, the day its
        preferred became common at the IPO);
      * a ceiling on preferred that no longer exists (Aqua Power tags 500M,
        Ilustrato 31.98bn, both against nothing outstanding);
      * a live conversion right of preferred still on the books, which is the only
        one Graham means (XWELL, 66.7M issuable against 31,333 preferred shares).

    The preferred count separates them, and it is filed on a share-class axis that
    Company Facts drops — but the quarterly DERA datasets keep the axis, so for the
    filers those cover the first case can be recognised and dropped rather than
    shown as a warning about nothing. What is still not deducible is the second
    against the third: whether a tagged figure is the total common the preferred
    becomes or a ceiling nobody will reach. So the count is never touched; the
    note reports what is outstanding and names the document that settles the rest.
    """
    if shares is None or shares.value <= 0:
        return None
    converted = _latest_instant(gaap, "ConvertiblePreferredShares",
                                CONVERTIBLE_PREFERRED_SHARE_TAGS,
                                unit=("shares",), not_before=fresh)
    if converted is None or converted.value <= 0:
        return None
    if converted.value / shares.value < _CONVERSION_OVERHANG:
        return None
    outstanding = _preferred_outstanding(dimensioned, fresh)
    if outstanding is not None and outstanding == 0:
        # the conversion is history: those shares are in the common count already
        return None
    standing = (f"{outstanding:,.0f} preferred shares are still outstanding, so the "
                "conversion is ahead of the company rather than behind it, but the tag "
                "does not say whether that figure is the common they become or a "
                "ceiling nobody reaches. "
                if outstanding else
                "The tag is used for conversions already done as well as for conversions "
                "still to come, and no preferred count is on file to tell them apart. ")
    return (
        f"A convertible preferred is tagged at {converted.value / _MILLION:,.1f}M shares "
        f"against {shares.value / _MILLION:,.1f}M common, "
        f"{converted.value / shares.value * 100:,.0f}% more. Graham counts shares "
        "\"including the conversion of preferred\" and strikes book value per share on "
        f"that larger number. {standing}Every per-share figure here is therefore struck "
        "on the common as it stands. Read the capitalisation note in the "
        f"{converted.provenance.form} ({converted.provenance.accession})."
    )


def _equity_for_scale(gaap: dict, equity: Decimal | None) -> Decimal | None:
    """Common equity, only as a yardstick for whether an obligation is large."""
    return equity if equity and equity > 0 else None


def _note(kind: str, text: str) -> dict:
    """A disclosure note and what kind of thing it is. The kind is decided where
    the evidence is, not guessed from the prose by whatever displays it."""
    return {"kind": kind, "text": text}


def annual_ratios(gaap: dict, annual_ni: dict[int, Fact], annual_revenue: dict[int, Fact],
                  annual_operating: dict[int, Fact], years: int = 10,
                  annual_eps: dict[int, Fact] | None = None,
                  annual_share_counts: dict[int, Fact] | None = None,
                  annual_gross_profit: dict[int, Fact] | None = None) -> dict[int, dict]:
    """Graham's comparison ratios as they stood at each fiscal year end.

    Chapter 13 compares companies by putting the same handful of ratios side by
    side; a single current column says how a business looks today and nothing about
    how it got there. Every figure here comes from the annual report of its own
    year — the balance sheet that year closed on, divided into the earnings that
    year produced — so no ratio mixes a period with a balance sheet from another.

    The per-share book figures are carried rather than the price multiples: a
    multiple needs the price of that year, which lives in the price history at
    export time, not in the filings.
    """
    gaap = _with_fiscal_calendar(gaap)
    # one reading of which year each date belongs to, for the balance sheets and the
    # earnings alike; see `_annual_balances`
    ends = fiscal_year_ends(gaap)
    labels = {end: year for year, end in ends.items()}
    ca = _annual_balances(gaap, ("AssetsCurrent",), labels=labels)
    cl = _annual_balances(gaap, ("LiabilitiesCurrent",), labels=labels)
    assets = _annual_balances(gaap, ("Assets", "LiabilitiesAndStockholdersEquity"), labels=labels)
    liabilities = _annual_balances(gaap, ("Liabilities",), labels=labels)
    minority = _annual_balances(gaap, ("MinorityInterest",), labels=labels)
    parent_equity = _annual_balances(gaap, ("StockholdersEquity",), labels=labels)
    group_equity = _annual_balances(
        gaap, ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",),
        labels=labels)
    preferred = _annual_balances(gaap, ("PreferredStockLiquidationPreferenceValue",
                                        "PreferredStockValue", "PreferredStockValueOutstanding"),
                                 labels=labels)
    combined_debt: dict[int, Fact] = {}
    for year, iso_end in ends.items():
        end = date.fromisoformat(iso_end)
        exact = _taxonomy_at_end(gaap, end, annual_only=True)
        debt_total, debt_long, debt_short = _debt_figures(exact, end, cl.get(year))
        debt_parts = _sum_facts("CombinedDebt", [debt_long, debt_short])
        # This is the historical equivalent of `settled_debt`: a filing total
        # may coexist with incomplete components, so at one date use the larger
        # representation. Without a total, both long and short buckets must be
        # explicitly present; missing debt is never silently read as zero.
        if debt_total is not None:
            debt = _fresher_or_larger(debt_total, debt_parts)
        elif debt_long is not None and debt_short is not None:
            debt = debt_parts
        else:
            debt = None
        if debt is not None:
            combined_debt[year] = debt
    goodwill, intangibles = _annual_goodwill_and_intangibles(gaap, ends, years)
    if annual_eps is None:
        annual_eps = _annual_eps(gaap)
    if annual_gross_profit is None:
        annual_gross_profit = _annual_gross_profit(gaap)
    counts = _annual_share_counts(gaap, {}, annual_eps, annual_ni)
    if annual_share_counts is not None:
        # The snapshot has already selected dimensioned counts onto the traded
        # class. Company Facts cannot reproduce those, so let them fill its gaps.
        # Keep ordinary counts on the established path: receipt counts have already
        # been rebased by the snapshot and are restated later by sync, while using
        # them here would apply that legal ratio twice.
        classed_counts = {
            year: fact for year, fact in annual_share_counts.items()
            if _is_equivalent_class_basis(fact)
        }
        counts = {**classed_counts, **counts}
    options = _annual_balances(gaap, OPTION_COUNT_TAGS, unit=("shares",), labels=labels)
    rsus = _annual_balances(gaap, RSU_COUNT_TAGS, unit=("shares",), labels=labels)

    def at(series, year):
        f = series.get(year)
        return f.value if f is not None else None

    # Finkle's ROE averages the shareholders' capital at the beginning and end of
    # the fiscal year. Build the same common-equity series used by return-on-book
    # once, including the prior year even when it falls outside the displayed six.
    equity_years = (set(assets) | set(liabilities) | set(parent_equity)
                    | set(group_equity) | set(minority) | set(preferred))
    equities: dict[int, Decimal] = {}
    for year in equity_years:
        a_v, l_v = at(assets, year), at(liabilities, year)
        equity = at(parent_equity, year)
        if equity is None and (incl := at(group_equity, year)) is not None:
            equity = incl - (at(minority, year) or 0)
        if equity is None and a_v is not None and l_v is not None:
            equity = a_v - l_v - (at(minority, year) or 0)
        if equity is not None:
            equities[year] = equity - (at(preferred, year) or 0)

    out: dict[int, dict] = {}
    for year in sorted(set(annual_ni) | set(ca) | set(assets))[-years:]:
        row: dict[str, float] = {}
        ca_v, cl_v = at(ca, year), at(cl, year)
        if ca_v is not None and cl_v:
            row["current_ratio"] = float(ca_v / cl_v)
        ni, rev = at(annual_ni, year), at(annual_revenue, year)
        if rev is not None:
            row["revenue"] = float(rev)
        gross_fact = annual_gross_profit.get(year)
        revenue_fact = annual_revenue.get(year)
        if (gross_fact is not None and revenue_fact is not None and rev is not None
                and rev > 0
                and gross_fact.provenance.period_start == revenue_fact.provenance.period_start
                and gross_fact.provenance.period_end == revenue_fact.provenance.period_end):
            row["gross_margin"] = float(gross_fact.value / rev * 100)
        if ni is not None and rev and rev > 0:
            row["net_margin"] = float(ni / rev * 100)
        op = at(annual_operating, year)
        if op is not None and rev and rev > 0:
            row["operating_margin"] = float(op / rev * 100)
        # The common's own capital that year. The filer's own equity line is the
        # direct reading — Coca-Cola tags no Liabilities total at all, so an
        # assets-minus-liabilities subtraction cannot run for it.
        a_v, l_v = at(assets, year), at(liabilities, year)
        equity = equities.get(year)
        tangible = None
        if equity is not None:
            if equity > 0 and ni is not None:
                row["return_on_book"] = float(ni / equity * 100)
                prior_equity = equities.get(year - 1)
                if prior_equity is not None:
                    average_equity = (prior_equity + equity) / 2
                    if average_equity > 0:
                        row["common_equity"] = float(equity)
                        row["average_common_equity"] = float(average_equity)
                        row["return_on_equity"] = float(ni / average_equity * 100)
            if equity > 0 and l_v is not None and l_v >= 0:
                row["common_equity"] = float(equity)
                row["total_liabilities"] = float(l_v)
            debt = at(combined_debt, year)
            if (equity > 0 and debt is not None and debt >= 0
                    and (a_v is None or debt <= a_v)):
                row["common_equity"] = float(equity)
                row["combined_debt"] = float(debt)
                row["debt_to_equity"] = float(debt / equity)
            goodwill_v, intangibles_v = at(goodwill, year), at(intangibles, year)
            if goodwill_v is not None and intangibles_v is not None:
                tangible = equity - goodwill_v - intangibles_v
                if tangible > 0 and ni is not None:
                    row["common_equity"] = float(equity)
                    row["net_tangible_assets"] = float(tangible)
                    row["return_on_net_tangible_assets"] = float(ni / tangible * 100)
        shares = at(counts, year)
        if shares and shares > 0:
            # see the guard in build_snapshot: an award count above the whole share
            # count is a mis-tag, and each kind is judged on its own before summing
            awards = [v for v in (at(options, year), at(rsus, year))
                      if v is not None and 0 <= v <= shares]
            if awards:
                row["award_pct"] = float(sum(awards) / shares * 100)
            if equity is not None:
                row["bvps"] = float(equity / shares)
                if tangible is not None:
                    row["tbvps"] = float(tangible / shares)
            if ca_v is not None and l_v is not None:
                row["ncavps"] = float((ca_v - l_v - (at(preferred, year) or 0)
                                       - (at(minority, year) or 0)) / shares)
        if row:
            out[year] = {k: round(v, 4) for k, v in row.items()}
            # the date this year's figures were struck at, so export prices them
            # against the market of that day rather than of the following December
            if year in ends:
                out[year]["end"] = ends[year]
    return out


def _prefer_reconciling_classed_eps(
    gaap: dict, series: dict[int, Fact], classed: dict[int, Fact],
) -> dict[int, Fact]:
    """Use the listed class fact only when the filing proves the bare fact wrong.

    An undimensioned figure normally wins because it describes the whole issuer.
    A small group of filings, however, expose a mis-scaled bare EPS beside the
    correctly scaled common-class EPS in the same accession. MOBX's FY2025 bare
    value is -10.10, while its common-Class-A fact is -1.01 and the filing reports
    a $46M loss on 45.5M weighted shares. The three figures prove the classed value;
    neither tag priority nor a ticker-specific override is needed.

    The substitution is deliberately narrower than ordinary class selection: same
    tag, year, accession and period; total-income scope; bare value contradicts the
    filing by more than 5%; classed value reconciles within 2%. Basic vs diluted,
    continuing operations, LP allocations, ADR ratios, preferred dividends and
    minority interests cannot cross this gate.
    """
    if not series or not classed:
        return series
    if _latest_instant(gaap, "nci", ("MinorityInterest",)) is not None:
        return series
    preferred = _annual_union(gaap, PREFERRED_DIVIDEND_TAGS)
    income = _by_accession(gaap, NET_INCOME_TAGS, ("USD",))
    # Income explicitly available to common is the numerator closest to EPS.
    # MOBX FY2024 has a small preferred/attribution wedge against NetIncomeLoss;
    # using the common line proves its Class A value while still requiring every
    # input to come from the same filing below.
    income.update(_by_accession(gaap, COMMON_INCOME_TAGS, ("USD",)))
    diluted = _by_accession(
        gaap, ("WeightedAverageNumberOfDilutedSharesOutstanding",), ("shares",))
    basic = _by_accession(
        gaap, ("WeightedAverageNumberOfSharesOutstandingBasic",), ("shares",))
    out = dict(series)
    for year in set(series) & set(classed):
        chosen, candidate = series[year], classed[year]
        chosen_tag = _tag_of(chosen)
        candidate_tag = _tag_of(candidate)
        if (year in preferred or chosen_tag != candidate_tag
                or not _eps_uses_total_income(chosen)
                or chosen.provenance.accession != candidate.provenance.accession
                or chosen.provenance.period_end != candidate.provenance.period_end
                or chosen.provenance.period_end is None):
            continue
        iso = chosen.provenance.period_end.isoformat()
        counts = basic if "Basic" in chosen_tag and "Diluted" not in chosen_tag else diluted
        ni = income.get((chosen.provenance.accession, iso))
        count = counts.get((chosen.provenance.accession, iso))
        if ni is None or not count:
            continue
        target = ni / count
        if abs(target) < Decimal("0.01"):
            continue
        chosen_gap = abs(chosen.value - target) / abs(target)
        candidate_gap = abs(candidate.value - target) / abs(target)
        exact_scale = False
        if candidate.value:
            scale = abs(chosen.value / candidate.value)
            exact_scale = any(
                abs(scale - factor) / factor <= _EPS_EXACT_SCALE_TOLERANCE
                for factor in (Decimal("10"), Decimal("100"), Decimal("1000"))
            )
        if (chosen_gap > _EPS_CONTRADICTS
                and (candidate_gap <= _EPS_RECONCILES
                     or (exact_scale and candidate_gap <= _EPS_SCALED_RECONCILES))):
            out[year] = candidate
    return out


def _annual_goodwill_and_intangibles(
    gaap: dict, ends: dict[int, str], years: int,
) -> tuple[dict[int, Fact], dict[int, Fact]]:
    """The current tangible-book deduction chain at each recent 10-K end.

    Build one annual-only view per balance-sheet date in a single pass. Calling
    `_goodwill_and_intangibles` on those views preserves all of the current
    fallback identities and their provenance without allowing a later quarter to
    leak backward into a historical column.
    """
    recent = dict(sorted(ends.items())[-years:])
    labels = {end: year for year, end in recent.items()}
    views: dict[int, dict] = {year: {} for year in recent}
    for tag, data in gaap.items():
        for unit, entries in (data.get("units") or {}).items():
            for entry in entries:
                year = labels.get(entry.get("end"))
                if (year is None or "start" in entry
                        or not entry.get("form", "").startswith(ANNUAL_FORMS)):
                    continue
                views[year].setdefault(tag, {"units": {}})["units"].setdefault(unit, []).append(entry)

    goodwill: dict[int, Fact] = {}
    intangibles: dict[int, Fact] = {}
    for year, view in views.items():
        end = date.fromisoformat(recent[year])
        g, i = _goodwill_and_intangibles(view, end)
        if g is not None:
            goodwill[year] = g
        if i is not None:
            intangibles[year] = i
    return goodwill, intangibles


def _annual_balances(gaap: dict, tags: tuple[str, ...],
                     unit: tuple[str, ...] = ("USD",),
                     labels: dict[str, int] | None = None) -> dict[int, Fact]:
    """A balance-sheet line at each fiscal year end, from the annual reports.

    Balance figures appear in every quarterly filing too; only the ones a 10-K
    states are comparable year to year, which is what a trend needs.

    `labels` is the earnings series' own reading of which fiscal year each date
    belongs to, and it wins where it has an opinion. The two disagree under the
    retail convention — the year ending 2025-02-02 is fiscal 2024 to SEC's frame and
    2025 to the month it ends in — and a table that labels its balance sheets one
    way and its earnings the other pairs the wrong two together. GameStop and Kohl's
    had every retail year off by one, printing the same date against two years.
    """
    labels = _fiscal_labels(gaap) if labels is None else labels
    for tag in tags:
        out = _annual_instant_series(gaap, tag, unit=unit, labels=labels)
        if not any(requested == "shares" or requested.endswith("/shares")
                   for requested in unit):
            out, _ = _reconcile_instant_scale(
                gaap, out, frozenset((tag,)), unit=unit)
        if out:
            return out
    return {}


def _divergence_note(gaap: dict, tags: tuple[str, ...], annual_revenue: dict[int, Fact],
                     label: str, meaning: str) -> str | None:
    """Whether a balance has grown faster than the sales behind it.

    Receivables and inventory are where an income statement and reality part
    company: revenue booked but not collected, and goods made but not sold, both
    look like growth until the write-down arrives."""
    balances = _annual_balances(gaap, tags)
    years = sorted(set(balances) & set(annual_revenue))
    if len(years) < _DIVERGENCE_SPAN + 1:
        return None
    latest, base = years[-1], years[-1 - _DIVERGENCE_SPAN]
    if base not in years:
        return None
    now_sales, then_sales = annual_revenue[latest].value, annual_revenue[base].value
    now, then = balances[latest].value, balances[base].value
    if then <= 0 or then_sales <= 0 or now_sales <= 0:
        return None
    now_ratio, then_ratio = now / now_sales, then / then_sales
    if then_ratio <= 0 or now_ratio / then_ratio < _DIVERGENCE:
        return None
    return (f"{label} grew {(now / then - 1) * 100:.0f}% between FY{base} and FY{latest} "
            f"while sales grew {(now_sales / then_sales - 1) * 100:.0f}%, so it now stands at "
            f"{now_ratio * 100:.0f}% of sales against {then_ratio * 100:.0f}% then. {meaning}")


def _common_equity(assets, liabilities, preferred, nci, temporary) -> Decimal | None:
    """What the common shareholders own, for scale only."""
    if assets is None or liabilities is None:
        return None
    other = sum(f.value for f in (preferred, nci, temporary) if f is not None)
    return assets.value - liabilities.value - other


def _newest_period_end(facts) -> date:
    return max((f.provenance.period_end for f in facts if f.provenance.period_end),
               default=date.min)


def _geographic_pretax(gaap: dict) -> dict[int, Fact]:
    """Consolidated pre-tax income rebuilt from its two geographies, for the year
    both cover. Either half alone describes part of a company; only the pair
    describes the whole, so a year missing one of them stays missing."""
    domestic, foreign = (_annual_series(gaap, tag, unit=("USD",))
                         for tag in _PRETAX_GEOGRAPHY_TAGS)
    out: dict[int, Fact] = {}
    for year in set(domestic) & set(foreign):
        pair = [domestic[year], foreign[year]]
        if len({f.provenance.period_end for f in pair}) != 1:
            continue
        summed = _sum_facts("PretaxIncome (domestic + foreign)", pair)
        if summed is not None:
            out[year] = summed
    return out


_ALLOWANCE_SHARE = Decimal("0.50")     # this much of the deferred assets reserved is worth saying
_ALLOWANCE_MATERIAL = Decimal("0.10")  # ...and only when the assets matter to the company
_DEFERRED_SHARE = Decimal("0.80")      # a tax charge this deferred was not paid
_DEFERRED_MATERIAL = Decimal("0.10")   # deferred amount against the year's earnings


def _deferred_tax_assets(gaap: dict, fresh: date | None) -> tuple[Fact, Decimal] | None:
    """The valuation allowance and the deferred tax assets it stands against,
    both struck at one balance-sheet date.

    The gross tag is the direct reading, but a filer can leave it stale (Biogen's
    is four years older than its allowance) or scope it to something narrower
    than the allowance covers (Valaris reserves 3,292M against a "gross" 1,368M).
    One rule catches both: an allowance cannot exceed the assets it reserves
    against, so a pair that says otherwise is not a pair. The net tag then
    rebuilds the base from the same filing through gross = net + allowance.
    """
    allowance = _latest_instant(gaap, "DeferredTaxAssetsValuationAllowance",
                                DEFERRED_TAX_ALLOWANCE_TAGS, not_before=fresh)
    # a filer tagging the allowance as a negative contra amount is on a different
    # sign convention, and the identity below would silently invert
    if allowance is None or allowance.value <= 0:
        return None
    for concept, tags, derived in (
        ("DeferredTaxAssetsGross", DEFERRED_TAX_ASSET_TAGS, False),
        ("DeferredTaxAssetsGross (derived: net + allowance)", DEFERRED_TAX_ASSET_NET_TAGS, True),
    ):
        base = _latest_instant(gaap, concept, tags, not_before=fresh)
        if base is None or base.provenance.period_end != allowance.provenance.period_end:
            continue
        gross = base.value + allowance.value if derived else base.value
        if gross >= allowance.value:
            return allowance, gross
    return None


def _context_notes(gaap: dict, annual_eps: dict[int, Fact],
                   annual_ni: dict[int, Fact], annual_op: dict[int, Fact],
                   annual_revenue: dict[int, Fact] | None = None,
                   long_term_debt: Fact | None = None,
                   shares: Fact | None = None,
                   fresh_equity: Decimal | None = None,
                   fresh: date | None = None,
                   dimensioned: dict | None = None) -> tuple[str, ...]:
    """Three standing questions a passing multiple cannot answer by itself.

    Disclosure only: nothing here enters a criterion, a grade or an adjustment —
    a company can be cheap on every Graham test and still be worth a second look
    because its earnings never became cash, its share count keeps climbing, or
    its interest bill is most of its operating profit.
    """
    notes: list[str] = []
    ocf = _annual_union(gaap, OPERATING_CASH_FLOW_TAGS)
    shared = sorted(set(ocf) & set(annual_ni), reverse=True)[:3]
    if shared:
        cash = sum(ocf[y].value for y in shared)
        income = sum(annual_ni[y].value for y in shared)
        if income > 0:
            ratio = cash / income
            if ratio < Decimal("0.8"):
                notes.append(_note("Cash conversion",
                    f"Over FY{min(shared)}–FY{max(shared)} the business turned "
                    f"{ratio * 100:.0f}% of reported net income into operating cash "
                    f"({cash / _MILLION:,.0f}M against {income / _MILLION:,.0f}M). "
                    "Earnings that do not arrive as cash still count in every "
                    "multiple on this page."
                ))
            elif ratio > Decimal("1.5"):
                notes.append(_note("Cash conversion",
                    f"Operating cash flow over FY{min(shared)}–FY{max(shared)} is "
                    f"{ratio * 100:.0f}% of reported net income — depreciation-heavy "
                    "or working-capital-driven, so the earnings multiple understates "
                    "what the business collects."
                ))

    # Share counts must be compared within one filing. A split restates every
    # earlier year, so a count taken from a pre-split report against one from a
    # post-split report measures the split and calls it dilution: NVIDIA's
    # ten-for-one made it look like an 867% issuance when the count had fallen.
    # A report states three years on one basis, which is basis enough.
    weighted = _annual_share_counts(gaap, {}, annual_eps, annual_ni)
    newest = max((f.provenance.accession for f in weighted.values()), default=None,
                 key=lambda a: max(f.provenance.filed for f in weighted.values()
                                   if f.provenance.accession == a))
    same_filing = {y: f for y, f in weighted.items() if f.provenance.accession == newest}
    years = sorted(same_filing)
    if len(years) >= 3:
        latest, base = years[-1], years[0]
        old, new = same_filing[base].value, same_filing[latest].value
        if old > 0:
            change = (new / old - 1) * 100
            if change > 10:
                notes.append(_note("Dilution",
                    f"The share count grew {change:.0f}% between FY{base} and FY{latest} "
                    f"({old / _MILLION:,.1f}M to {new / _MILLION:,.1f}M shares). Per-share "
                    "figures are divided by a denominator that keeps rising."
                ))
            elif change < -10:
                notes.append(_note("Buybacks",
                    f"The share count fell {abs(change):.0f}% between FY{base} and FY{latest} "
                    f"({old / _MILLION:,.1f}M to {new / _MILLION:,.1f}M shares) — buybacks "
                    "are lifting per-share figures independently of the business."
                ))

    interest = _annual_union(gaap, INTEREST_EXPENSE_TAGS)
    shared_op = sorted(set(interest) & set(annual_op), reverse=True)[:1]
    for year in shared_op:
        cost, profit = abs(interest[year].value), annual_op[year].value
        if cost > 0 and profit > 0:
            cover = profit / cost
            if cover < 3:
                notes.append(_note("Interest cover",
                    f"FY{year} operating profit covers interest {cover:.1f}x "
                    f"({profit / _MILLION:,.0f}M against {cost / _MILLION:,.0f}M of interest). "
                    "Graham's debt test measures the balance sheet; this is what the "
                    "income statement pays for it."
                ))

    revenue = annual_revenue or {}
    if revenue:
        for tags, label, meaning in (
            (RECEIVABLE_TAGS, "Receivables",
             "Revenue booked and not yet collected is revenue the customer has not "
             "confirmed with cash."),
            (INVENTORY_TAGS, "Inventory",
             "Goods made and not sold sit at cost until they are written down."),
        ):
            note = _divergence_note(gaap, tags, revenue, label, meaning)
            if note:
                notes.append(_note(label, note))

    # NVF's debentures paid a 5% coupon and were sold at 43% of par, so the
    # interest statement described a cheaper company than the one that existed.
    # Where amortised discount is most of the interest bill, the coupon is not
    # the cost of the money.
    discount = _annual_union(gaap, DEBT_DISCOUNT_TAGS)
    shared_interest = sorted(set(discount) & set(interest), reverse=True)[:1]
    for year in shared_interest:
        amortised, total = abs(discount[year].value), abs(interest[year].value)
        if total > 0 and amortised / total >= Decimal("0.25"):
            notes.append(_note(
                "Debt discount",
                f"FY{year} interest of {total / _MILLION:,.0f}M includes "
                f"{amortised / _MILLION:,.0f}M of amortised debt discount, "
                f"{amortised / total * 100:.0f}% of the bill. Debt sold below face value "
                "costs more than its coupon says, and the difference arrives as a charge "
                "rather than a payment."
            ))

    conversion = _as_converted_note(gaap, shares, fresh, dimensioned)
    if conversion:
        notes.append(_note("Convertible preferred", conversion))

    # Graham devotes a whole section to warrants because they are dilution a
    # share count does not show: NVF paid for Sharon Steel partly in warrants on
    # its own stock, and every per-share figure afterwards was struck on a
    # denominator that ignored them.
    warrants = _latest_instant(gaap, "WarrantShares", WARRANT_SHARE_TAGS, unit=("shares",),
                               not_before=fresh)
    if warrants is not None and warrants.value > 0 and shares is not None and shares.value > 0:
        overhang = warrants.value / shares.value
        if overhang >= Decimal("0.05"):
            notes.append(_note(
                "Warrant overhang",
                f"Warrants call for {warrants.value / _MILLION:,.1f}M shares against "
                f"{shares.value / _MILLION:,.1f}M outstanding, {overhang * 100:.0f}% more. "
                "Graham's rule for these is not the fully-diluted one — assuming exercise and "
                "retiring debt with the proceeds he calls illogical, since it left National "
                "General's reported earnings per share unchanged at $1.51 either way. His rule "
                "is that warrants are part of the common-stock package, so their own market "
                "value belongs inside the market capitalisation: adding $221M of warrant value "
                "to $192M of common trebled the true price of that equity and took its "
                "price/earnings from 32 to 69. Warrants trade under their own symbols and this "
                "screen prices none of them, so the market capitalisation and every multiple "
                "built on it are understated by whatever the warrants are worth."
            ))

    # Graham read Penn Central's tax accounting as a second opinion on its
    # earnings, and took the tax authorities' side. The deferred tax footnote
    # holds two such opinions, and both are the company's own.
    latest_profit = annual_ni.get(max(annual_ni)) if annual_ni else None
    pair = _deferred_tax_assets(gaap, fresh)
    if pair is not None and latest_profit is not None and latest_profit.value > 0:
        allowance, gross = pair
        share = allowance.value / gross
        equity = _equity_for_scale(gaap, fresh_equity)
        # a full allowance on a tax asset worth a rounding error says nothing
        material = equity is None or gross >= equity * _ALLOWANCE_MATERIAL
        if share >= _ALLOWANCE_SHARE and material:
            notes.append(_note(
                "Valuation allowance",
                f"Deferred tax assets of {gross / _MILLION:,.0f}M carry a "
                f"{allowance.value / _MILLION:,.0f}M valuation allowance — "
                f"{share * 100:.0f}% of them — beside FY{max(annual_ni)} earnings of "
                f"{latest_profit.value / _MILLION:,.0f}M. The allowance is the company's own "
                "statement that it does not expect enough future profit to use deductions "
                "it has already earned."
            ))

    tax = _annual_union(gaap, TAX_TAGS)
    deferred = _annual_union(gaap, DEFERRED_TAX_EXPENSE_TAGS)
    for year in sorted(set(tax) & set(deferred) & set(annual_ni), reverse=True)[:1]:
        charge, postponed, profit = tax[year].value, deferred[year].value, annual_ni[year].value
        # a tax footnote older than the earnings record describes a different company
        if year < max(annual_ni) - 1 or charge <= 0 or profit <= 0:
            continue
        if (postponed / charge >= _DEFERRED_SHARE
                and abs(postponed) >= profit * _DEFERRED_MATERIAL):
            payable = charge - postponed
            paid = (f"only {payable / _MILLION:,.0f}M was currently payable" if payable > 0
                    else f"the current charge was a {abs(payable) / _MILLION:,.0f}M refund")
            notes.append(_note(
                "Deferred tax",
                f"FY{year}'s income tax charge of {charge / _MILLION:,.0f}M is "
                f"{postponed / charge * 100:.0f}% deferred: {paid}. Earnings taxed on paper "
                "and not in cash — usually timing on heavy capital spending, always a charge "
                "the tax return has not yet collected."
            ))

    # Rent is not borrowed money under criterion 3, which is Graham's reading and
    # the engine's policy — but a company can carry more of it than debt, and the
    # test that ignores it should say how much it is ignoring.
    leases = _latest_instant(gaap, "OperatingLeaseLiability", LEASE_OBLIGATION_TAGS)
    if leases is not None and leases.value > 0:
        debt = long_term_debt.value if long_term_debt else Decimal(0)
        # Large against the debt is only half of it: NVIDIA's lease book is more
        # than a quarter of its borrowings and 2% of its equity, which tells a
        # reader nothing. The obligation has to matter to the company too.
        equity = _equity_for_scale(gaap, fresh_equity)
        material = equity is None or leases.value >= equity * Decimal("0.10")
        if material and leases.value >= max(debt, Decimal(0)) * Decimal("0.25"):
            against = (f"against {debt / _MILLION:,.0f}M of long-term debt"
                       if debt else "with no long-term debt reported")
            notes.append(_note(
                "Lease obligations",
                f"Operating-lease obligations of {leases.value / _MILLION:,.0f}M {against}. "
                "The debt test counts borrowed money and not rent, which is Graham's "
                "reading, so this obligation sits outside it."
            ))
    return tuple(notes)


def _restricted_cash(gaap: dict, fresh: date | None, end: date | None) -> Fact | None:
    """One restricted-cash representation at exactly the given period end: the
    single total first, else current+noncurrent, else the cash-equivalents pair
    (GE's only fresh figure is the noncurrent variant of the latter)."""
    def at_end(concept: str, tag: str) -> Fact | None:
        f = _latest_instant(gaap, concept, (tag,), not_before=fresh)
        return f if f is not None and f.provenance.period_end == end else None

    total = at_end("RestrictedCash", "RestrictedCash")
    if total is not None:
        return total
    summed = _sum_facts("RestrictedCash", [
        at_end("RestrictedCash (current)", "RestrictedCashCurrent"),
        at_end("RestrictedCash (noncurrent)", "RestrictedCashNoncurrent"),
    ])
    if summed is not None:
        return summed
    return _sum_facts("RestrictedCash", [
        at_end("RestrictedCash (and equivalents)", "RestrictedCashAndCashEquivalents"),
        at_end("RestrictedCash (and equivalents, noncurrent)",
               "RestrictedCashAndCashEquivalentsNoncurrent"),
    ])


def _implied_shares(gaap: dict, annual_eps: dict[int, Fact], annual_ni: dict[int, Fact],
                    annual_preferred: dict[int, Fact] | None = None) -> Decimal | None:
    """Share count implied by the filer's own earnings: net income over EPS.

    Independent of every share tag, because it is the denominator the company must
    have divided by to publish the EPS it published. EPS nets preferred dividends
    from income and the NetIncomeLoss tag does not, so they come out first —
    GTN's implied count was 2.2x off until they did.
    """
    shared = sorted(set(annual_eps) & set(annual_ni), reverse=True)
    if not shared:
        return None
    # As with the basis check, an ineligible newest year is an abstention, not an
    # invitation to compare today's security with a much older capital structure.
    year = shared[0]
    eps, ni = annual_eps[year], annual_ni[year]
    if eps.value == 0 or not _eps_uses_total_income(eps):
        return None
    preferred = (annual_preferred or {}).get(year)
    common = ni.value - (preferred.value if preferred else Decimal(0))
    implied = common / eps.value
    if implied > 0:
        return implied
    return None


def _sane_shares(chosen: Fact | None, gaap: dict, dei: dict, fresh: date | None,
                 implied: Decimal | None = None) -> Fact | None:
    """A filer that tags its share count in thousands understates it a thousandfold,
    and that one number divides net current asset value, tangible book and market cap
    alike — so the error arrives disguised as a deep bargain rather than as a gap.

    Every filing carries three share counts drawn independently: the balance-sheet
    instant, the cover page, and the weighted average behind EPS. They never agree
    exactly — the cover page is dated later, the weighted average spans a period — but
    they agree on the order of magnitude, because they count the same shares. So when
    the chosen one sits a full order of magnitude from both others, it is the outlier
    rather than the truth, and the median of the three is taken instead. A median of
    three is unmoved by any single bad source, and since it returns one of the reported
    values rather than a blend, the ordinary drift between them cannot distort it. With
    fewer than three counts there is nothing to arbitrate, so the choice stands.
    """
    # A company cannot have no shares. A zero is a tagging artefact, and it is
    # the most dangerous one available: every per-share figure divides by this.
    if chosen is None or chosen.value <= 0:
        return None
    cover = _latest_instant(
        dei, "SharesOutstanding", ("EntityCommonStockSharesOutstanding",),
        ns="dei", unit=("shares",), not_before=fresh,
    )
    weighted = _weighted_shares(gaap, fresh)
    # The weighted average is only a third opinion when it is not already the choice;
    # counting it twice made the median a median of two, and the outlier test compared
    # the chosen value against its own duplicate — a ratio of one, so it never fired.
    others = [f for f in (cover, weighted)
              if f is not None and f.value > 0 and f is not chosen]
    candidates = [chosen] + others

    def ratio(a: Decimal, b: Decimal) -> Decimal:
        return max(a, b) / min(a, b)

    # Company Facts can flatten a classed balance-sheet row onto the wrong
    # comparative context. OMH's 2025 GAAP instant consequently carries 2.359M
    # (its 2024 count), while the same 20-F's DEI cover fact carries 22.260M and
    # its annual weighted count is 14.139M after substantial issuance. When the
    # GAAP and DEI facts share an accession and date, and the independent
    # weighted count agrees with DEI on the order of magnitude, the GAAP value
    # is the contradicted fragment/comparative and the DEI fact stands.
    if (cover is not None and weighted is not None
            and cover.provenance.accession == chosen.provenance.accession
            and cover.provenance.period_end == chosen.provenance.period_end
            and ratio(chosen.value, cover.value) > Decimal("5")
            and ratio(chosen.value, cover.value) < Decimal("10")
            and ratio(chosen.value, weighted.value) > Decimal("5")
            and ratio(cover.value, weighted.value) < Decimal("2")):
        return cover

    if len(candidates) >= 3:
        if all(ratio(chosen.value, f.value) > 10 for f in others):
            return sorted(candidates, key=lambda f: f.value)[1]
        # Dual-class fragment: an undimensioned instant can carry ONE class of a
        # multi-class filer — a 2-3x error, far under the magnitude test. When
        # the two independent witnesses agree with each other and both disagree
        # with the chosen count, the chosen count is the fragment.
        if (ratio(others[0].value, others[1].value) < Decimal("1.35")
                and all(ratio(chosen.value, f.value) > Decimal("1.5") for f in others)):
            return sorted(candidates, key=lambda f: f.value)[1]
        return chosen
    if len(candidates) == 2 and implied is not None and implied > 0:
        # earnings arithmetic is the third witness: HEI's stale 55M class
        # instant loses to the 141M weighted count that NI/EPS corroborates
        other = others[0]
        if (ratio(chosen.value, other.value) > Decimal("1.5")
                and ratio(other.value, implied) < Decimal("1.35")):
            return other
    # With one source there is nothing to outvote, and that is exactly where a filer
    # tagging shares in thousands slips through: Hub Group reported 60,333 against a
    # true 60.3 million, which passed criterion 7 at a price-to-book of 0.00. Earnings
    # give an independent reading, so an order-of-magnitude disagreement retires the
    # count rather than publishing a thousandfold-wrong book value.
    if implied is not None and implied > 0 and chosen.value > 0:
        if max(chosen.value, implied) / min(chosen.value, implied) > 10:
            return None
        # The last-resort tags are fragments exactly when nothing corroborates
        # them (SUN's LP-unit instant is 51.5M of ~136M real units); with no
        # witness and a 2x earnings disagreement, missing beats wrong.
        if not others and _tag_of(chosen) in (
            "LimitedPartnersCapitalAccountUnitsOutstanding", "SharesOutstanding",
        ) and max(chosen.value, implied) / min(chosen.value, implied) > 2:
            return None
    return chosen


def _is_partnership(gaap: dict, not_before: date | None) -> bool:
    """Partners capital present and stockholders equity absent — the shape of a
    filer whose 'shares' are limited-partner units."""
    partners = _latest_instant(
        gaap, "PartnersCapital",
        ("PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest", "PartnersCapital"),
        not_before=not_before,
    )
    stockholders = _latest_instant(
        gaap, "StockholdersEquity",
        ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
         "StockholdersEquity"),
        not_before=not_before,
    )
    return partners is not None and stockholders is None


def _weighted_shares(gaap: dict, not_before: date | None) -> Fact | None:
    """Multi-class filers often tag no point-in-time consolidated share count.
    Weighted-average shares from the income statement is the flagged proxy —
    diluted first, so basic-only filers still get a count instead of none."""
    floor = not_before.isoformat() if not_before else ""
    for tag in _WEIGHTED_SHARE_TAGS:
        entries = [
            e
            for e in _entries(gaap, tag, ("shares",))
            if "start" in e and _is_financial_form(e.get("form", "")) and e["end"] >= floor
        ]
        if not entries:
            continue
        latest_end = max(e["end"] for e in entries)
        best = None
        for e in entries:
            if e["end"] != latest_end:
                continue
            # shortest duration = closest to a point-in-time count; then latest filed
            if best is None or _days(e) < _days(best) or (_days(e) == _days(best) and e["filed"] > best["filed"]):
                best = e
        return _fact("SharesOutstanding (weighted-average proxy)", tag, best)
    return None


def _is_a_rate_not_a_total(gaap: dict, tag: str, unit: tuple[str, ...],
                           annual: dict[int, Fact]) -> bool:
    """Whether a per-share dividend element states the quarterly rate rather than
    the year's total.

    Visa tags 0.59 for a ninety-day quarter and 0.59 again for the whole fiscal
    year: the element carries the rate per payment, repeated against every
    context. Treating that as a year's dividends understates the yield fourfold,
    and the filer's own facts are what give it away — a total cannot equal one
    of its own quarters."""
    for year, fact in annual.items():
        end = fact.provenance.period_end
        if end is None:
            continue
        for e in _entries(gaap, tag, unit):
            if "start" not in e or not _is_financial_form(e.get("form", "")):
                continue
            if e["end"] != end.isoformat() or not 80 <= _days(e) <= 100:
                continue
            if _dec(e["val"]) == fact.value:
                return True
    return False


def _dividend_per_share(gaap: dict, dividend: Fact | None, shares: Fact | None,
                        fresh: date | None = None) -> Decimal | None:
    """A dividend fact covers whatever span the filer tagged — a quarter for one
    company, half a year for another — so the raw figure is not comparable. Roll it
    to twelve months the same way earnings are, then express it per share."""
    if dividend is None:
        return None
    # The element that proves a company still pays is not always the one it uses
    # for its annual totals: Coca-Cola's freshest dividend fact is quarterly
    # under DividendsCommonStockCash, and it tags no annual series there at all,
    # so reading the yield from that one tag alone left 167 payers — Coke, ADP,
    # Visa, Exxon — showing that they pay without saying how much. Every chained
    # element is tried, per-share ones first: those need no share count, so they
    # cannot inherit an error from it.
    chosen = _tag_of(dividend)
    ordered = sorted(
        DIVIDEND_TAGS,
        key=lambda pair: (pair[0] != chosen, "USD/shares" not in pair[1]),
    )
    for tag, unit in ordered:
        # The chain's own unit declaration decides per-share vs dollar aggregate;
        # a name test would silently mis-scale any newly added per-unit tag.
        per_share = "USD/shares" in unit
        annual = _annual_series(gaap, tag, unit=unit)
        if not annual:
            continue
        if per_share and _is_a_rate_not_a_total(gaap, tag, unit, annual):
            continue
        # A twelve-month roll built from a series that stopped years ago is not a
        # twelve-month figure. Wynn's 2012 special dividend was still being divided
        # by a 2026 price and published as a 9.49% yield.
        if fresh is not None:
            newest = max((f.provenance.period_end for f in annual.values()
                          if f.provenance.period_end), default=None)
            if newest is None or newest < fresh:
                continue
        ttm, _ = _ttm_eps(gaap, annual, unit=unit, per_share=False)
        if ttm is None or ttm <= 0:
            continue
        if per_share:
            return ttm
        if shares is not None and shares.value > 0:
            return ttm / shares.value
    return None


_RECURRING_DIVIDEND_TAGS = (
    "CommonStockDividendsPerShareDeclared",
    "CommonStockDividendsPerShareCashPaid",
)
_QUARTER_DAYS = range(80, 101)
_RECURRING_HISTORY_DAYS = 300
_RECURRING_FRESH_DAYS = 120
_SPECIAL_MULTIPLE = Decimal("1.5")
_RECURRING_STABILITY = Decimal("0.25")


def _recurring_dividend_per_share(gaap: dict, asof: date | None) -> Fact | None:
    """Latest ordinary quarterly common-dividend rate, annualized.

    The trailing cash total intentionally remains a separate figure: an annual or
    TTM total cannot say how much was regular and how much was special. A recurring
    rate is therefore published only when the filer supplies at least three recent,
    direct per-share quarterly facts. If the newest quarter jumps above the recent
    run, it is treated as special-inclusive and the latest ordinary-sized quarter
    is used instead. Partnerships and investment companies are excluded because
    their standard per-unit facts commonly include variable or supplemental
    distributions that cannot be separated into a recurring rate.
    """
    by_period: dict[str, tuple[str, dict]] = {}
    for tag in _RECURRING_DIVIDEND_TAGS:
        for e in _entries(gaap, tag, ("USD/shares",)):
            if ("start" not in e or not _is_financial_form(e.get("form", ""))
                    or _days(e) not in _QUARTER_DAYS or _dec(e["val"]) <= 0):
                continue
            # One economic quarter has one end date. Duplicate contexts with a
            # one-day start-date difference must not manufacture extra history.
            key = e["end"]
            prior = by_period.get(key)
            # A later filing supersedes an earlier one for the exact quarter.
            # On a tie, declared is the more direct statement of the rate.
            if (prior is None or e.get("filed", "") > prior[1].get("filed", "")
                    or (e.get("filed", "") == prior[1].get("filed", "")
                        and tag == _RECURRING_DIVIDEND_TAGS[0])):
                by_period[key] = (tag, e)
    if not by_period:
        return None

    quarters = sorted(by_period.values(), key=lambda item: item[1]["end"], reverse=True)
    latest_end = date.fromisoformat(quarters[0][1]["end"])
    if asof is not None and not 0 <= (asof - latest_end).days <= _RECURRING_FRESH_DAYS:
        return None

    recent = [item for item in quarters
              if (latest_end - date.fromisoformat(item[1]["end"])).days
              <= _RECURRING_HISTORY_DAYS]
    if len(recent) < 3:
        return None

    chosen_tag, chosen = recent[0]
    prior_values = sorted(_dec(e["val"]) for _, e in recent[1:5])
    if len(prior_values) >= 2:
        middle = len(prior_values) // 2
        prior_median = (prior_values[middle] if len(prior_values) % 2
                        else (prior_values[middle - 1] + prior_values[middle]) / 2)
        stable = [value for value in prior_values
                  if abs(value / prior_median - 1) <= _RECURRING_STABILITY]
        if len(stable) < 2:
            return None
        if _dec(chosen["val"]) > prior_median * _SPECIAL_MULTIPLE:
            ordinary = next(
                ((tag, e) for tag, e in recent[1:]
                 if abs(_dec(e["val"]) / prior_median - 1) <= _RECURRING_STABILITY),
                None,
            )
            if ordinary is None:
                return None
            chosen_tag, chosen = ordinary

    reported = _fact("RecurringDividendPerShare", chosen_tag, chosen)
    p = reported.provenance
    return Fact(value=reported.value * 4, provenance=Provenance(
        concept="RecurringDividendPerShare (latest quarterly rate annualized x4)",
        tag=p.tag, fiscal_year=p.fiscal_year, form=p.form,
        accession=p.accession, filed=p.filed, period_end=p.period_end,
        period_start=p.period_start, segments=p.segments,
    ))


def _dividend_record(gaap: dict) -> dict | None:
    """Which calendar years the filer actually paid a common dividend, from the
    chained tags' own facts. XBRL history only begins around 2009-2011, so the
    start of the record is part of the answer: "paid since 2011" can mean "paid
    for longer than the record can show" — display must say when the record begins."""
    totals: dict[int, Decimal] = {}
    aggregate_entries = [
        e for tag, unit in DIVIDEND_TAGS if tag in _AGGREGATE_DIVIDEND_TAGS
        for e in _entries(gaap, tag, unit)
        if "start" in e and _is_financial_form(e.get("form", ""))
    ]
    settlement_periods = {
        (e["start"], e["end"]) for e in aggregate_entries
        if _dec(e["val"]) > 0 and _settles_prior_dividend_payable(gaap, e)
    }
    payable_ends = {
        payable["end"]
        for payable_tag in ("DividendsPayableCurrent", "DividendsPayable")
        for payable in _entries(gaap, payable_tag, ("USD",))
        if "start" not in payable and _dec(payable["val"]) > 0
    }
    aggregate_annual_zeros = [
        e for e in aggregate_entries if _dec(e["val"]) == 0
        and _is_annual_form(e.get("form", "")) and _days(e) in _ANNUAL_DAYS
        and e["end"] in payable_ends
    ] if settlement_periods else []
    for tag, unit in DIVIDEND_TAGS:
        entries = [e for e in _entries(gaap, tag, unit)
                   if "start" in e and _is_financial_form(e.get("form", ""))]
        annual_zeros = aggregate_annual_zeros if tag in _AGGREGATE_DIVIDEND_TAGS else []
        for e in entries:
            if _dec(e["val"]) <= 0:
                continue
            # A later filed full-year zero contradicts an earlier positive YTD
            # value for the exact same cumulative period. This is deliberately
            # narrower than preferring annual facts by calendar year: doing that
            # moved real payment years for non-December filers.
            if any(z["start"] == e["start"] and z["end"] >= e["end"]
                   and z["filed"] >= e["filed"] for z in annual_zeros):
                continue
            if (tag in _AGGREGATE_DIVIDEND_TAGS
                    and (e["start"], e["end"]) in settlement_periods):
                continue
            year = int(e["end"][:4])
            totals[year] = max(totals.get(year, Decimal(0)), _dec(e["val"]))
    if not totals:
        return None
    # Graham's twenty years is a record of *paying*, and a company that cut its
    # dividend to a rounding error did interrupt it. A year whose largest payment
    # is a small fraction of the record's typical one is a suspension with a
    # residue, not a paid year.
    ordered = sorted(totals.values())
    typical = ordered[len(ordered) // 2]
    paid = {y for y, v in totals.items() if v >= typical * _SUSPENSION_SHARE}
    if not paid:
        return None
    years = sorted(paid)
    streak_from = years[-1]
    for y in reversed(years[:-1]):
        if y != streak_from - 1:
            break
        streak_from = y
    return {"first": years[0], "latest": years[-1],
            "streak_from": streak_from, "paid_years": len(years)}


def _is_all_preferred(gaap: dict, e: dict) -> bool:
    """Whether a payment for this exact period is entirely the preferred's."""
    for tag in PREFERRED_DIVIDEND_TAGS:
        for other in _entries(gaap, tag, ("USD",)):
            if (other.get("start") == e.get("start") and other.get("end") == e.get("end")
                    and _dec(other["val"]) >= _dec(e["val"]) * Decimal("0.99")):
                return True
    return False


def _settles_prior_dividend_payable(gaap: dict, e: dict) -> bool:
    """Whether an aggregate cash-flow payment merely clears an old payable.

    PFHO paid a 2015 dividend liability in 2025, mostly by escheating unclaimed
    funds to a state administrator. ``PaymentsOfDividends`` therefore says
    $37,000 even though no current dividend was declared. Require a current and
    prior payable, an exact-period aggregate matching their decline, and no
    common-specific payment for that period before treating the aggregate as a
    legacy settlement rather than criterion-5 evidence.
    """
    start, end = e.get("start"), e.get("end")
    if not start or not end:
        return False
    for tag, unit in DIVIDEND_TAGS:
        if tag in _AGGREGATE_DIVIDEND_TAGS:
            continue
        if any(other.get("start") == start and other.get("end") == end
               and _dec(other["val"]) > 0 for other in _entries(gaap, tag, unit)):
            return False
    aggregates = [other for tag, unit in DIVIDEND_TAGS
                  if tag in _AGGREGATE_DIVIDEND_TAGS
                  for other in _entries(gaap, tag, unit)
                  if other.get("start") == start and other.get("end") == end
                  and _dec(other["val"]) > 0]
    if not aggregates:
        return False
    payment = max(_dec(other["val"]) for other in aggregates)
    payable_entries = [other for tag in ("DividendsPayableCurrent", "DividendsPayable")
                       for other in _entries(gaap, tag, ("USD",))
                       if "start" not in other and _is_financial_form(other.get("form", ""))]
    current = [other for other in payable_entries if other.get("end") == end]
    # Compare the opening liability with the closing one. Interim instants inside
    # the payment period merely show the settlement happening in stages (PFHO's
    # $37,000 became $36,375 and then $36,250 before reaching zero).
    prior = [other for other in payable_entries if other.get("end", "") < start
             and (date.fromisoformat(end) - date.fromisoformat(other["end"])).days <= _STALE_DAYS]
    if not current or not prior:
        return False
    current_value = _dec(max(current, key=lambda other: other["filed"])["val"])
    prior_end = max(other["end"] for other in prior)
    prior_value = _dec(max((other for other in prior if other["end"] == prior_end),
                           key=lambda other: other["filed"])["val"])
    decline = prior_value - current_value
    tolerance = max(Decimal(1), payment * Decimal("0.01"))
    period_start = date.fromisoformat(start)
    # A normal dividend declared near year-end is payable at the opening date
    # and paid promptly; that is a real current payout even if the cash payment
    # happens to equal the payable's decline. PFHO's decade-old unclaimed amount
    # remained more than 90% outstanding through midyear, then was escheated.
    lingered = any(
        start < other.get("end", "") < end
        and (date.fromisoformat(other["end"]) - period_start).days >= 150
        and _dec(other["val"]) >= prior_value * Decimal("0.90")
        for other in payable_entries
    )
    return decline > 0 and lingered and abs(payment - decline) <= tolerance


def _dividend(gaap: dict, reference: date | None) -> tuple[bool | None, Fact | None]:
    if reference is None:
        return None, None
    for tag, unit in DIVIDEND_TAGS:
        positive = [e for e in _entries(gaap, tag, unit) if "start" in e and _dec(e["val"]) > 0]
        if not positive:
            continue
        e = max(positive, key=lambda e: (e["end"], e["filed"]))
        # An aggregate tag can hold a payment the common never receives: Boeing's
        # PaymentsOfDividends for the half year to 2026-06-30 is 172,000,000, and
        # its own DividendsPreferredStock for that identical period is the same
        # 172,000,000 — the cash-flow line reads "Dividends paid on mandatory
        # convertible preferred stock" and there is no common line at all. Where
        # the whole of a payment is the preferred's, it is not evidence that the
        # common is paid.
        if tag in _AGGREGATE_DIVIDEND_TAGS and _is_all_preferred(gaap, e):
            continue
        if tag in _AGGREGATE_DIVIDEND_TAGS and _settles_prior_dividend_payable(gaap, e):
            continue
        if (reference - date.fromisoformat(e["end"])).days <= _days(e) + _DIVIDEND_LAG_DAYS:
            concept = ("Dividends (aggregate — may include preferred and noncontrolling)"
                       if tag in _AGGREGATE_DIVIDEND_TAGS else "Dividends (common stock)")
            return True, _fact(concept, tag, e)
    # The chain missed, but a recent positive fact under ANY other dividend- or
    # distribution-named tag means the filer likely pays via a tag we don't
    # read: unknown, never FAIL. Up-C filers whose only payouts go to NCI
    # holders (SDHC/GLXY pattern) intentionally land here as unknown.
    floor = (reference - timedelta(days=_DIVIDEND_RECENCY_DAYS)).isoformat()
    chained = {tag for tag, _ in DIVIDEND_TAGS}
    for tag, tagdata in gaap.items():
        low = tag.lower()
        if tag in chained or ("dividend" not in low and "distribut" not in low):
            continue
        if _DIVIDEND_EVIDENCE_EXCLUDE_RE.search(tag):
            continue
        for unit_name, unit in tagdata.get("units", {}).items():
            if unit_name in ("shares", "pure"):  # counts and ratios are not payouts
                continue
            for e in unit:
                if (_is_financial_form(e.get("form", ""))
                        and e.get("end", "") >= floor
                        and isinstance(e.get("val"), (int, float)) and e["val"] > 0):
                    return None, None
    # Dividend payers must report payments in the cash-flow statement; no recent
    # positive fact under any dividend-named tag => not currently paying.
    return False, None
