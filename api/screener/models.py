"""Shared data model. Decimal for all financial arithmetic; float never."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .figure_history import ActionEvidence, DocumentEvidence, ShareBasis


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True)
class Provenance:
    concept: str
    tag: str
    fiscal_year: int | None
    form: str
    accession: str
    filed: date
    period_end: date | None = None
    period_start: date | None = None  # duration facts only; None for balance-sheet instants
    # a figure summed or derived from several facts keeps each of them here: the
    # combined tag string alone cannot say which filing or which balance-sheet
    # date a given component came from
    components: tuple["Provenance", ...] = ()
    # the dimension a fact was reported under, when it has one — "which slice of
    # the company is this" is part of what the number means
    segments: str = ""
    # Reporting unit and source document are explicit for non-SEC adapters. SEC
    # Company Facts already identifies units in its outer JSON key and documents
    # through accession; retaining both here makes the canonical boundary honest.
    unit: str | None = None
    document: str | None = None
    # Rule-selection identity can differ from the exact reported element/row
    # shown in ``tag`` when an IFRS adapter maps into a canonical concept.
    canonical_tag: str | None = None
    # An incorporated exhibit is the exact source, while the annual wrapper is
    # the filing that makes the fact current for annual-selection rules.
    annual_accession: str | None = None
    annual_form: str | None = None
    annual_filed: date | None = None
    source_file: str | None = None
    native_xml_location: NativeXmlOrdinalLocation | None = None
    source_entity: str | None = None
    source_entity_scheme: str | None = None
    source_artifact_sha256: str | None = None
    equity_class_evidence: tuple[IssuerClassEvidence, ...] = ()


@dataclass(frozen=True)
class CapturedSource:
    role: str
    artifact_sha256: str
    source_document: str
    source_item_id: int
    source_observation_id: int
    occurrence_id: int | None = None
    fidelity: str = "UNKNOWN"


@dataclass(frozen=True)
class RawFactOrigin:
    source: CapturedSource
    pointer: str
    lexical_text: str
    namespace: str
    concept: str
    unit: str
    start: str | None
    end: str | None
    accession: str | None
    form: str | None
    filed: str | None


@dataclass(frozen=True)
class NativeInlineOrdinalLocation:
    """Zero-based original namespace-resolved nonFraction start ordinal."""
    element_ordinal: int
    fact_qname: str
    context_ref: str
    unit_ref: str
    fact_id: str | None = None


@dataclass(frozen=True)
class NativeXmlOrdinalLocation:
    """Zero-based direct child ordinal in the original XBRL instance."""
    element_ordinal: int
    fact_qname: str
    context_ref: str
    unit_ref: str
    fact_id: str | None = None


@dataclass(frozen=True)
class NativeSource:
    artifact_sha256: str
    byte_size: int
    cik: str
    accession: str
    form: str
    filed: date
    source_document: str
    annual_accession: str
    annual_form: str
    annual_filed: date
    # Local verified bytes do not invent registered source/check identities.
    captured_source: CapturedSource | None = None


@dataclass(frozen=True)
class NativeDimension:
    axis: str
    kind: str
    member: str

    def __post_init__(self):
        if any(type(v) is not str for v in (self.axis, self.kind, self.member)):
            raise ValueError("native dimension requires immutable strings")


@dataclass(frozen=True)
class NativeContext:
    entity_identifier: str
    entity_scheme: str
    instant: date | None
    start: date | None
    end: date | None
    dimensions: tuple[NativeDimension, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "dimensions", tuple(self.dimensions))
        if any(not isinstance(v, NativeDimension) for v in self.dimensions):
            raise ValueError("native context requires typed dimensions")


@dataclass(frozen=True)
class NativeNumericRequest:
    source: NativeSource
    location: NativeInlineOrdinalLocation | NativeXmlOrdinalLocation
    context: NativeContext
    unit_numerator: tuple[str, ...]
    unit_denominator: tuple[str, ...] = ()
    decimals: str | None = None
    transformation: str | None = None
    sign: str | None = None
    scale: int = 0

    def __post_init__(self):
        object.__setattr__(self, "unit_numerator", tuple(self.unit_numerator))
        object.__setattr__(self, "unit_denominator", tuple(self.unit_denominator))
        if any(type(v) is not str for v in (*self.unit_numerator, *self.unit_denominator)):
            raise ValueError("native unit measures require immutable strings")


@dataclass(frozen=True)
class NativeNumericInput:
    request: NativeNumericRequest
    lexical_text: str
    decoded_text: str
    value: Decimal


@dataclass(frozen=True)
class XmlTextBlockTableLocation:
    """Physical class table inside an original XML text block; not a numeric fact."""
    source_file: str
    fact_qname: str
    element_ordinal: int
    context_ref: str
    table: int
    header_row: int
    class_rows: tuple[int, ...]
    total_row: int
    column: int

    def __post_init__(self):
        object.__setattr__(self, "class_rows", tuple(self.class_rows))
        if any(type(n) is not int or n < 0 for n in
               (self.element_ordinal, self.table, self.header_row, self.total_row,
                self.column, *self.class_rows)) or not self.class_rows:
            raise ValueError("class table requires physical source positions")
        if (tuple(sorted(set(self.class_rows))) != self.class_rows
                or not self.header_row < min(self.class_rows)
                or not max(self.class_rows) < self.total_row or self.column == 0):
            raise ValueError("class table requires distinct ordered header/class/total roles")
        if (not self.source_file.startswith("XBRL/PublicDoc/")
                or ".." in self.source_file.split("/")
                or not self.fact_qname.startswith("{http://disclosure.edinet-fsa.go.jp/")
                or not self.context_ref):
            raise ValueError("class table requires an original EDINET text block")


@dataclass(frozen=True)
class IssuerClassEvidence:
    artifact_sha256: str
    member_sha256: str
    document: str
    entity: str
    entity_scheme: str
    context_start: str | None
    context_end: str
    as_of: str
    outcome: str
    classes: tuple[tuple[str, str], ...]
    total_lexical: str
    header_lexical: str
    location: XmlTextBlockTableLocation
    registration_status: str = "UNREGISTERED_NATIVE_MEMBER"

    def __post_init__(self):
        object.__setattr__(self, "classes", tuple(tuple(row) for row in self.classes))
        if (self.outcome not in {"ORDINARY_ONLY", "PREFERRED_MONETARY_CLAIM_REQUIRED", "UNRESOLVED"}
                or self.registration_status != "UNREGISTERED_NATIVE_MEMBER"
                or any(len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
                       for value in (self.artifact_sha256, self.member_sha256))
                or not self.entity_scheme or not self.entity or not self.document
                or not self.classes or any(len(row) != 2 for row in self.classes)):
            raise ValueError("issuer class evidence is incomplete")
        date.fromisoformat(self.as_of)
        date.fromisoformat(self.context_end)
        if date.fromisoformat(self.as_of) > date.fromisoformat(self.context_end):
            raise ValueError("issuer class endpoint is after its source context")
        if self.context_start is not None:
            date.fromisoformat(self.context_start)

    @classmethod
    def from_record(cls, record):
        location = dict(record["location"])
        location["class_rows"] = tuple(location["class_rows"])
        return cls(**{**record, "classes": tuple(tuple(row) for row in record["classes"]),
                      "location": XmlTextBlockTableLocation(**location)})


@dataclass(frozen=True)
class PrimaryInputMapping:
    key: str
    canonical_namespace: str
    canonical_tag: str
    original: NativeNumericRequest
    replacement: NativeNumericRequest | None = None
    unary_factor_text: str | None = None
    documents: tuple["DocumentEvidence", ...] = ()
    actions: tuple["ActionEvidence", ...] = ()
    share_basis: "ShareBasis | None" = None
    target_origin: RawFactOrigin | None = None

    def __post_init__(self):
        object.__setattr__(self, "documents", tuple(self.documents))
        object.__setattr__(self, "actions", tuple(self.actions))


@dataclass(frozen=True)
class AdmittedPrimaryInput:
    mapping: PrimaryInputMapping
    original: NativeNumericInput
    replacement: NativeNumericInput | None


@dataclass(frozen=True)
class UnavailablePrimaryInput:
    key: str
    canonical_namespace: str
    canonical_tag: str
    reason: str
    original: NativeNumericRequest
    target_origin: RawFactOrigin | None = None


@dataclass(frozen=True)
class Fact:
    value: Decimal
    provenance: Provenance
    exact_origin: RawFactOrigin | None = field(default=None, compare=False, repr=False)
    exact_unassessed_reason: str | None = field(default=None, compare=False, repr=False)
    primary_input: AdmittedPrimaryInput | None = field(default=None, compare=False, repr=False)
    derived_eps_inputs: DerivedEpsInputs | None = field(default=None, compare=False, repr=False)
    operation_inputs: tuple[Fact, ...] = field(default=(), compare=False, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "operation_inputs", tuple(self.operation_inputs))


@dataclass(frozen=True)
class DerivedEpsInputs:
    income: Fact
    count: Fact
    preferred: Fact | None
    absent_preferred_reason: str | None
    roles: tuple[str, ...] = ("income", "count", "preferred")

    def __post_init__(self):
        object.__setattr__(self, "roles", tuple(self.roles))


@dataclass(frozen=True)
class Quote:
    price: Decimal
    asof: datetime
    source: str
    # Session which produced `price`, and the exchange state when it was fetched.
    # They differ after the bell: the last price can be AFTER_HOURS while the
    # market itself is now CLOSED.
    session: str = "REGULAR"
    market_state: str = "UNKNOWN"
    market_timezone: str | None = None
    market_state_asof: datetime | None = None


@dataclass(frozen=True)
class PriceHistory:
    """Weekly closes behind the quote, oldest first — the input to pricestats."""
    quote: Quote
    closes: tuple[tuple[date, Decimal], ...]
    # (effective date, factor applied to pre-event prices). A 2:1 forward split
    # carries 0.5; a 1:10 reverse split carries 10.
    splits: tuple[tuple[date, Decimal], ...] = ()


@dataclass(frozen=True)
class AnnualOwnerEarnings:
    """Owner-earnings evidence for one audited year on today's share basis.

    SEC XBRL normally reports total capital expenditure, not the maintenance
    portion Buffett's definition requires.  The floor and the explicitly named
    maintenance≈D&A estimate therefore remain separate; neither is serialized as
    a definitive owner-earnings figure.
    """
    reported_earnings: Fact | None
    depreciation_and_amortisation: Fact | None
    total_capital_expenditure: Fact | None
    operating_cash_flow: Fact | None
    other_operating_cash_flow_adjustments: Fact | None
    all_capex_floor: Fact | None
    maintenance_estimate: Fact | None
    free_cash_flow: Fact | None
    free_cash_flow_after_stock_compensation: Fact | None
    free_cash_flow_after_acquisitions: Fact | None
    expanded_free_cash_flow: Fact | None
    stock_compensation: Fact | None
    cash_acquisitions: Fact | None
    share_repurchases: Fact | None
    capitalized_intangible_investment: Fact | None
    working_capital_cash_effect: Fact | None
    operating_cash_flow_before_working_capital: Fact | None
    diluted_shares: Fact | None
    all_capex_floor_per_share: Fact | None
    maintenance_estimate_per_share: Fact | None
    free_cash_flow_per_share: Fact | None
    free_cash_flow_after_stock_compensation_per_share: Fact | None
    free_cash_flow_after_acquisitions_per_share: Fact | None
    expanded_free_cash_flow_per_share: Fact | None


@dataclass(frozen=True)
class AnnualOperatingReturn:
    """One fiscal year's filing-backed NOPAT and capital-return evidence.

    These measures are independent of owner earnings: NOPAT needs operating
    income and tax evidence, not D&A, capital expenditure, or a diluted-share
    denominator.  Keeping the exact endpoint facts here lets every historical
    return retain the balance sheets that produced its average denominator.
    """
    fiscal_year: int
    operating_income: Fact | None
    normalized_tax_rate: Decimal | None
    tax_expense_inputs: tuple[Fact, ...]
    pretax_income_inputs: tuple[Fact, ...]
    nopat: Fact | None
    invested_capital_beginning: Fact | None
    invested_capital_ending: Fact | None
    invested_capital: Decimal | None
    capital_including_cash_beginning: Fact | None
    capital_including_cash_ending: Fact | None
    capital_including_cash: Decimal | None
    net_tangible_operating_assets_beginning: Fact | None
    net_tangible_operating_assets_ending: Fact | None
    average_net_tangible_operating_assets: Decimal | None
    lease_neutral_net_tangible_operating_assets_beginning: Fact | None
    lease_neutral_net_tangible_operating_assets_ending: Fact | None
    average_lease_neutral_net_tangible_operating_assets: Decimal | None
    nopat_roic: Decimal | None
    nopat_return_including_cash: Decimal | None
    ronta: Decimal | None
    lease_neutral_ronta: Decimal | None
    # True only for a broad assumption calculation. The primary static dashboard
    # row remains filing-strict, but a compact subset of these inputs is exported
    # separately for Return Quality and the full row powers company detail.
    assumption_mode: bool = False
    # A separate, non-Graham discovery value. Only denominator deductions whose
    # absence makes zero the conservative direction may be bounded at zero;
    # numerator, debt, lease, tax and core balance-sheet inputs remain strict.
    conservative_estimate: bool = False
    assumed_zero: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()


@dataclass(frozen=True)
class OwnerEarnings:
    """Evidence around owner earnings over the capital that produced it.

    Never a criterion — Graham's requirements do not measure return on capital at all.
    A definitive Buffett figure requires maintenance capital expenditure and required
    additional working capital. Primary XBRL normally supplies neither, so the model
    carries a total-capex floor, a maintenance≈D&A estimate, and standard free cash
    flow as three separately labelled observations instead of pretending one is exact.
    """
    fiscal_year: int
    all_capex_floor: Fact | None
    maintenance_estimate: Fact | None
    free_cash_flow: Fact | None
    cash_taxes_paid: Fact | None
    free_cash_flow_after_stock_compensation: Fact | None
    free_cash_flow_after_acquisitions: Fact | None
    expanded_free_cash_flow: Fact | None
    stock_compensation: Fact | None
    cash_acquisitions: Fact | None
    capitalized_intangible_investment: Fact | None
    working_capital_cash_effect: Fact | None
    operating_cash_flow_before_working_capital: Fact | None
    average_working_capital_cash_effect_3y: Decimal | None
    stock_compensation_to_revenue: Decimal | None
    stock_compensation_to_free_cash_flow: Decimal | None
    acquisitions_to_free_cash_flow: Decimal | None
    acquisition_years_10: int | None
    acquisitions_to_capex_10: Decimal | None
    # The return denominator is the exact average of the fiscal year's beginning
    # and ending balance sheets.  The endpoint facts retain every filed input;
    # `invested_capital` is their arithmetic mean for API compatibility.
    invested_capital_beginning: Fact | None
    invested_capital_ending: Fact | None
    invested_capital: Decimal | None
    capital_including_cash_beginning: Fact | None
    capital_including_cash_ending: Fact | None
    capital_including_cash: Decimal | None
    all_capex_return: Decimal | None
    maintenance_estimate_return: Decimal | None
    all_capex_return_including_cash: Decimal | None
    maintenance_estimate_return_including_cash: Decimal | None
    normalized_tax_rate: Decimal | None
    nopat: Decimal | None
    nopat_roic: Decimal | None
    nopat_return_including_cash: Decimal | None
    # RONTA uses a narrower denominator than ROIC: tangible operating assets
    # net of non-interest-bearing operating liabilities. Both exact endpoints
    # retain the filing inputs behind the average.
    net_tangible_operating_assets_beginning: Fact | None
    net_tangible_operating_assets_ending: Fact | None
    average_net_tangible_operating_assets: Decimal | None
    ronta: Decimal | None
    # A presentation-comparability view. Post-ASC-842 operating-lease ROU assets
    # are removed after the current lease liability has been kept with financing;
    # pre-recognition years remain on their filed off-balance-sheet presentation.
    lease_neutral_net_tangible_operating_assets_beginning: Fact | None
    lease_neutral_net_tangible_operating_assets_ending: Fact | None
    average_lease_neutral_net_tangible_operating_assets: Decimal | None
    lease_neutral_ronta: Decimal | None
    # signed contributions in statement order, so the UI can show the derivation
    components: tuple[tuple[str, Decimal], ...]
    free_cash_flow_components: tuple[tuple[str, Decimal], ...]
    caveats: tuple[str, ...] = ()
    # Each value retains the flow facts and reported diluted denominator behind it.
    # The count is restated for later splits (and, where applicable, the traded
    # depositary receipt) so one year's per-share figures are comparable.
    annual: dict[int, AnnualOwnerEarnings] = field(default_factory=dict)


@dataclass(frozen=True)
class FinancialSnapshot:
    cik: str
    ticker: str
    annual_eps: dict[int, Fact]  # fiscal year -> diluted EPS, 10-K facts only
    # Profit attributable to the parent, used for margins and return on book.
    # It is not necessarily the EPS numerator: preferred dividends and other
    # common-specific adjustments may sit between the two.
    annual_net_income: dict[int, Fact]
    ttm_net_income: Decimal | None
    ttm_eps: Decimal | None
    ttm_eps_inputs: tuple[Fact, ...]
    current_assets: Fact | None
    current_liabilities: Fact | None
    long_term_debt: Fact | None
    short_term_debt: Fact | None
    total_assets: Fact | None
    total_liabilities: Fact | None
    goodwill: Fact | None
    intangibles: Fact | None
    preferred_stock: Fact | None
    shares_outstanding: Fact | None
    dividend: Fact | None  # evidence for pays_dividend, when True
    dividend_per_share: Decimal | None  # rolled to twelve months, for the yield
    pays_dividend: bool | None
    balance_sheet_date: date | None
    # Contractual rent sits outside Graham's borrowed-money test. These fields
    # expose it separately; fixed-charge coverage is a labelled proxy based on
    # reported operating income, interest, and lease cost from one annual period.
    operating_lease_liability: Fact | None = None
    lease_cost: Fact | None = None
    fixed_charge_coverage: Fact | None = None
    # A direct filing-reported quarterly common dividend rate, annualized. Unlike
    # dividend_per_share, this deliberately excludes identifiable special cash.
    recurring_dividend_per_share: Fact | None = None
    # The weighted denominator each annual EPS was struck on. This is a reported
    # filing fact, unlike total net income / EPS, which is invalid when the two
    # figures have different scopes (continuing operations, LP allocations, BDCs).
    annual_share_counts: dict[int, Fact] = field(default_factory=dict)
    total_debt: Fact | None = None  # total-style rollup tag; preferred over long+short when present
    # employee options still outstanding — dilution a share count does not show
    options_outstanding: Fact | None = None
    # restricted stock still to vest: dilution that does not need a rising price
    rsus_outstanding: Fact | None = None
    # concepts with zero facts anywhere in the filing history, opt-in treated as 0 (flagged)
    assumed_zero: frozenset = frozenset()
    # deducted in TBV: Assets - Liabilities includes minority holders' equity;
    # None when the liabilities derivation already excluded it
    noncontrolling_interest: Fact | None = None
    # disclosures about what the TTM earnings are made of — never criteria, but
    # criterion 1 can turn on a single non-recurring line, so they are surfaced
    earnings_quality: tuple[dict, ...] = ()   # {"kind", "text"}, as context_notes carries
    owner_earnings: OwnerEarnings | None = None
    # "Dec 31" -> TTM EPS computable from facts FILED by that date, rebased onto
    # today's share count; the hindsight-free denominator for historical P/E
    ttm_eps_vintage: dict[str, Decimal] = field(default_factory=dict)
    # chapter-13 comparison inputs: sales, operating income, dividend continuity
    annual_revenue: dict[int, Fact] = field(default_factory=dict)
    ttm_revenue: Decimal | None = None
    annual_gross_profit: dict[int, Fact] = field(default_factory=dict)
    annual_operating_income: dict[int, Fact] = field(default_factory=dict)
    # {"first", "latest", "streak_from", "paid_years"} — calendar years with a
    # positive common dividend; None when the record holds none
    dividend_record: dict | None = None
    # mezzanine (temporary) equity senior to common — set only when it must be
    # deducted from common book: absent/zero preferred, liabilities not derived
    temporary_equity: Fact | None = None
    # trailing preferred dividends: EPS nets them, NetIncomeLoss does not
    ttm_preferred_dividends: Decimal | None = None
    # ...and the same series by year, for the implied-share cross-check
    annual_preferred_dividends: dict[int, Decimal] = field(default_factory=dict)
    # the dei cover-page count, whatever the chain finally chose. Kept only so that a
    # depositary ratio can be seen: for a receipt-listed foreign issuer the cover
    # counts receipts while the statements count ordinary shares.
    cover_shares: Fact | None = None
    # cash conversion, dilution and interest coverage — context a passing
    # multiple cannot answer; never a criterion, never an adjustment
    context_notes: tuple[str, ...] = ()
    # profitable years against years that carried effectively no income tax
    # {"window_from", "window_to", "profitable_years", "untaxed_years", "pass_through"}
    tax_record: dict | None = None
    # Ten fiscal-year slots of operating returns, calculated independently of
    # owner-earnings/FCF availability and retaining every endpoint fact.
    annual_operating_returns: dict[int, AnnualOperatingReturn] = field(default_factory=dict)
    # Latest-year lower-bound candidates used only for discovery/sorting. Exact
    # returns above remain authoritative and every bounded field is disclosed.
    conservative_operating_returns: dict[int, AnnualOperatingReturn] = field(default_factory=dict)
    # set when the earnings series and the share count cannot be the same security:
    # every per-share figure would be wrong by the factor between them
    basis_conflict: str | None = None
    # Currency of every monetary statement figure. USD remains the default for
    # SEC Company Facts; foreign canonical adapters must state this explicitly.
    reporting_currency: str = "USD"
    # Supplementary reported history, excluded from current calculation inputs.
    prior_accounting_history: tuple[dict, ...] = field(default=(), compare=False, repr=False)
    # The filing intentionally has no current/noncurrent balance-sheet split
    # (banks and similar filers). Keep this structural fact even when one stale
    # current total is withheld from display for a cross-period mismatch.
    unclassified_balance_sheet: bool = False
    debt_unavailable: tuple[UnavailablePrimaryInput, ...] = field(default=(), compare=False, repr=False)

    senior_claims_unavailable: tuple[UnavailablePrimaryInput, ...] = field(default=(), compare=False, repr=False)
    parent_equity: Fact | None = None
    common_equity: Fact | None = None
    common_equity_required: bool = False
    common_equity_unavailable: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "debt_unavailable", tuple(self.debt_unavailable))
        object.__setattr__(self, "senior_claims_unavailable", tuple(self.senior_claims_unavailable))

    def current_senior_claims_available(self) -> bool:
        return not getattr(self, "senior_claims_unavailable", ())

    def common_equity_value(self) -> Decimal | None:
        if getattr(self, "common_equity_required", False):
            fact = getattr(self, "common_equity", None)
            return fact.value if fact is not None else None
        if not FinancialSnapshot.current_senior_claims_available(self):
            return None
        if self.total_assets is None or self.total_liabilities is None:
            return None
        other = sum(f.value for f in (self.preferred_stock, self.noncontrolling_interest,
                                      self.temporary_equity) if f is not None)
        return self.total_assets.value - self.total_liabilities.value - other


@dataclass(frozen=True)
class EpsGrowth:
    """Latest annual EPS against the best year in the 4-7-year window behind it.

    Never a criterion. Graham's fifth requirement compared the latest year against
    a *fixed* calendar year, 1966, and the book gives no rule for choosing that base
    in any other year — a rolling pointer, a prior peak, and a prior average all
    change what passing proves, and none of them is his. Rather than attribute a
    modern base year to Graham, the comparison is reported and left to the reader.
    """
    base_fiscal_year: int
    base_eps: Decimal
    latest_fiscal_year: int
    latest_eps: Decimal


@dataclass(frozen=True)
class CriterionResult:
    criterion: int
    name: str
    status: Status
    value: Decimal | None
    threshold: str
    inputs: tuple[Fact, ...]
    note: str | None = None


@dataclass(frozen=True)
class ScreenResult:
    ticker: str
    cik: str
    verdict: Verdict
    criteria: tuple[CriterionResult, ...]
    quote: Quote | None
    balance_sheet_date: date | None
    annual_eps_series: dict[int, Decimal]
    # relaxations applied to the screen: assumed-zero concepts (opt-in)
    assumptions: tuple[str, ...] = ()
    # disclosure, not a test — see EpsGrowth
    eps_growth: EpsGrowth | None = None
