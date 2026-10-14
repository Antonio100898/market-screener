"""Graham applicability and alignment summaries for the dashboard.

These are deliberately separate from a strict screen verdict.  Graham stated
industrial and public-utility financial tests explicitly, but did not supply a
universal modern sector formula.  The dashboard therefore exposes both (a) the
profile to which a company belongs and (b) the evidence currently available.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .ch13 import _avg3
from .sources import cover

# Annual XBRL filing became mandatory for every filer size by fiscal 2011; a
# record that begins later marks a genuinely short public history, not a gap in
# the dataset. Windowed defensive tests need at least this many years to say
# anything about a company's record.
_XBRL_FULL_COVERAGE = 2011
_MIN_WINDOW_YEARS = 5
# By this year every dividend payer tagged its payments, so a filing year with
# earnings and no dividend fact is a year the company did not pay.
_DIVIDEND_TAGGING_RELIABLE = 2013
# a margin that has given up this much of itself is the business getting harder
_MARGIN_DECLINE = 0.33

PROFILE_OPERATING = "OPERATING"
PROFILE_UTILITY = "UTILITY"
PROFILE_FINANCIAL = "FINANCIAL"
PROFILE_SPECIAL = "SPECIAL"
PROFILE_REVIEW = "REVIEW"

PROFILE_META = {
    PROFILE_OPERATING: {
        "label": "Industrial-style operating business",
        "short": "Operating",
        "detail": "Eligible for the Chapter 15 industrial financial-condition tests.",
    },
    PROFILE_UTILITY: {
        "label": "Public utility",
        "short": "Utility",
        "detail": "Use the defensive utility financial-position rule; the industrial current-ratio rule is not applicable.",
    },
    PROFILE_FINANCIAL: {
        "label": "Financial / real-estate structure",
        "short": "Financial",
        "detail": "Show valuation evidence, but do not present an industrial Graham verdict.",
    },
    PROFILE_SPECIAL: {
        "label": "Special structure",
        "short": "Special",
        "detail": "Shells and blank-check companies are outside the ordinary-company screens.",
    },
    PROFILE_REVIEW: {
        "label": "Manual applicability review",
        "short": "Review",
        "detail": "Sector or business-model classification is not reliable enough for a strict profile.",
    },
}

# Business-model routing is advisory, never a screen or a grade. The SEC SIC
# description and filing-derived diagnostics are enough to identify where a
# generic industrial ratio is structurally non-comparable, but not enough to
# invent AFFO, reserve quality, NAV, or organic growth that the filing did not tag.
ROUTE_META = {
    "BANK": {
        "label": "Bank",
        "preferred": "Tangible book, ROTCE, regulatory capital, credit losses and deposit quality",
        "deemphasize": "CFO minus capex and the generic current ratio",
    },
    "INSURER": {
        "label": "Insurer",
        "preferred": "Adjusted book value, combined ratio, reserve adequacy and investment quality",
        "deemphasize": "Generic free cash flow and the current ratio",
    },
    "REIT": {
        "label": "REIT",
        "preferred": "Company-reported FFO/AFFO, NAV, occupancy and debt to EBITDA",
        "deemphasize": "GAAP EPS, ordinary price/book and generic CFO minus capex",
    },
    "MLP_PIPELINE": {
        "label": "MLP / pipeline",
        "preferred": "Company-reported distributable cash flow, sustaining capex, coverage and leverage",
        "deemphasize": "CFO minus total capex as owner earnings and taxed-company ROIC",
    },
    "SHIPPING": {
        "label": "Shipping",
        "preferred": "Vessel NAV, charter coverage, fleet age and debt",
        "deemphasize": "Historical-cost price/book on its own",
    },
    "RETAIL_LEASE": {
        "label": "Lease-heavy retail / hospitality",
        "preferred": "Lease-adjusted debt, fixed-charge coverage, inventory turns and cash flow",
        "deemphasize": "Conventional debt without operating leases",
    },
    "SOFTWARE": {
        "label": "Software",
        "preferred": "FCF after stock compensation, diluted-share trend and capitalized software",
        "deemphasize": "Price/book and unadjusted CFO minus capex",
    },
    "SERIAL_ACQUIRER": {
        "label": "Acquisition-led",
        "preferred": "FCF after acquisitions, acquisition cadence, goodwill and leverage",
        "deemphasize": "Free cash flow that omits acquisition dependence",
    },
    "COMMODITY": {
        "label": "Commodity producer",
        "preferred": "Mid-cycle earnings, reserves or replacement cost, and balance-sheet resilience",
        "deemphasize": "Latest-year P/E on its own",
    },
    "UTILITY": {
        "label": "Regulated utility",
        "preferred": "Regulatory asset base, allowed returns, capex funding and leverage",
        "deemphasize": "All-capex earnings after treating expansion capex as a current-period cost",
    },
}


def analysis_routes(row: dict) -> list[dict]:
    """Explicitly route structurally different businesses by filed/SEC evidence."""
    industry = (row.get("industry") or "").lower()
    name = (row.get("name") or "").lower()
    sector = row.get("sector") or ""
    route_ids: list[str] = []

    def add(route: str) -> None:
        if route not in route_ids:
            route_ids.append(route)

    if any(term in industry for term in (
        "commercial bank", "savings institution", "credit union", "bank holding")):
        add("BANK")
    if "insurance" in industry:
        add("INSURER")
    if "real estate investment trust" in industry:
        add("REIT")
    pass_through = bool((row.get("tax_record") or {}).get("pass_through"))
    pipeline = any(term in industry for term in (
        "natural gas transmission", "crude petroleum pipelines", "pipeline transportation"))
    partnership_name = any(term in name for term in (
        " limited partnership", " l.p.", " lp", " partners"))
    if pipeline or (pass_through and sector == "Energy" and partnership_name):
        add("MLP_PIPELINE")
    if any(term in industry for term in (
        "deep sea", "water transportation", "marine transportation", "shipping")):
        add("SHIPPING")
    if any(term in industry for term in (
        "retail", "eating places", "restaurants", "hotels", "motels")):
        add("RETAIL_LEASE")
    if any(term in industry for term in (
        "prepackaged software", "computer programming services", "services-computer programming")):
        add("SOFTWARE")
    owner = row.get("owner_earnings") or {}
    if (owner.get("acquisition_years_10") or 0) >= 3 and (
        (owner.get("acquisitions_to_capex_10") or 0) >= 50
        or (owner.get("acquisitions_to_free_cash_flow") or 0) >= 50
    ):
        add("SERIAL_ACQUIRER")
    if any(term in industry for term in (
        "oil and gas extraction", "crude petroleum", "metal mining", "coal mining",
        "mining and quarrying", "agricultural production")):
        add("COMMODITY")
    if sector == "Utilities":
        add("UTILITY")
    return [{"id": route, **ROUTE_META[route]} for route in route_ids]


def _number(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _valuation_price(row: dict) -> Decimal | None:
    """Price expressed in the same currency as filed per-share figures."""
    reporting = row.get("reporting_currency") or row.get("currency") or "USD"
    quote = row.get("quote_currency") or row.get("currency") or "USD"
    if reporting != quote:
        return _number(row.get("price_reporting_currency"))
    return _number(row.get("price"))


def _amount_in_usd(row: dict, value: Decimal | None) -> Decimal | None:
    """Convert a reported total only for Graham's explicitly dollar size floor."""
    if value is None:
        return None
    reporting = row.get("reporting_currency") or row.get("currency") or "USD"
    if reporting == "USD":
        return value
    fx = row.get("fx") or {}
    rate = _number(fx.get("rate"))
    if (fx.get("base") != "USD" or fx.get("counter") != reporting
            or rate is None or rate <= 0):
        return None
    return value / rate


def _status(pass_: bool | None) -> str:
    if pass_ is None:
        return "INSUFFICIENT_DATA"
    return "PASS" if pass_ else "FAIL"


def profile_for(sector: str | None) -> str:
    """Return a cautious Graham applicability profile from the existing SIC sector."""
    if sector == "Utilities":
        return PROFILE_UTILITY
    if sector in {"Financials", "Real estate"}:
        return PROFILE_FINANCIAL
    if sector == "Shell & blank-check":
        return PROFILE_SPECIAL
    if sector in (None, "", "Other"):
        return PROFILE_REVIEW
    return PROFILE_OPERATING


def _eps_series(row: dict) -> dict[int, Decimal]:
    out: dict[int, Decimal] = {}
    for year, value in (row.get("annual_eps") or {}).items():
        try:
            out[int(year)] = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            continue
    return out


def _criterion_status(row: dict, number: int) -> str:
    for criterion in row.get("criteria") or ():
        if criterion.get("n") == number:
            return criterion.get("status") or "INSUFFICIENT_DATA"
    return "INSUFFICIENT_DATA"


def _latest_eps_label(row: dict, eps: dict[int, Decimal]) -> int | None:
    """Keep the row's selected year even when its numeric EPS is unavailable."""
    selected = (row.get("ch13") or {}).get("latest_fy")
    labels = list(eps)
    if type(selected) is int:
        labels.append(selected)
    return max(labels, default=None)


def _pe3(row: dict) -> Decimal | None:
    """Current price over the three latest consecutive fiscal-year EPS values."""
    price = _valuation_price(row)
    eps = _eps_series(row)
    if price is None or not eps:
        return None
    latest = _latest_eps_label(row, eps)
    years = (latest - 2, latest - 1, latest)
    if any(year not in eps for year in years):
        return None
    average = sum((eps[year] for year in years), Decimal(0)) / Decimal(3)
    return price / average if average > 0 else None


def _summary(tests: dict[str, str], eligible: bool = True) -> dict:
    n_pass = sum(status == "PASS" for status in tests.values())
    n_fail = sum(status == "FAIL" for status in tests.values())
    n_unknown = len(tests) - n_pass - n_fail
    if not eligible:
        verdict = "OUT_OF_SCOPE"
    elif n_fail:
        verdict = "BLOCKED"
    elif n_unknown:
        verdict = "EVIDENCE_INCOMPLETE"
    else:
        verdict = "ALIGNED"
    return {"verdict": verdict, "passed": n_pass, "total": len(tests), "unknown": n_unknown, "tests": tests}


def _enterprising(row: dict, profile: str) -> dict:
    # The six existing test fields are kept intact.  The additional growth
    # comparison is a clearly marked modern 4-fiscal-year analogue, not
    # attributed as an original rolling Graham requirement.
    tests = {
        "valuation": _criterion_status(row, 1),
        "liquidity": _criterion_status(row, 2),
        "debt": _criterion_status(row, 3),
        "stability": _criterion_status(row, 4),
        "dividend": _criterion_status(row, 5),
        "tangible_assets": _criterion_status(row, 7),
    }
    eps = _eps_series(row)
    latest = _latest_eps_label(row, eps)
    base = latest - 4 if latest is not None else None
    if latest not in eps or base not in eps:
        growth = {"status": "INSUFFICIENT_DATA", "label": "Modern 4-FY EPS comparison", "base_fy": base, "latest_fy": latest}
    else:
        growth = {
            "status": "PASS" if eps[latest] > eps[base] else "FAIL",
            "label": "Modern 4-FY EPS comparison",
            "base_fy": base,
            "base_eps": float(eps[base]),
            "latest_fy": latest,
            "latest_eps": float(eps[latest]),
        }
    # Strictly, this is the book's industrial low-multiplier method.  We still
    # show the six direct tests outside the profile so a reader can inspect
    # valuation evidence, but never call it an enterprising alignment.
    result = _summary(tests, eligible=profile == PROFILE_OPERATING)
    result["growth_modern_4fy"] = growth
    result["profile_note"] = (
        "The four-year EPS comparison is a disclosed modern analogue; Graham named FY1966 and gave no rolling formula."
    )
    return result


def margin_note(row: dict) -> str | None:
    """A margin that has given up a third of itself over the record."""
    m = row.get("profitability") or {}
    series = {int(y): v for y, v in (m.get("by_year") or {}).items()}
    if len(series) < 6:
        return None
    latest = max(series)
    earlier = max(y for y in series if y <= latest - 5)
    then, now = series[earlier], series[latest]
    if then < 2 or now >= then * (1 - _MARGIN_DECLINE):
        return None
    return (f"Net margin has fallen from {then:.1f}% of sales in FY{earlier} to {now:.1f}% in "
            f"FY{latest}. Graham counts profitability as net profit against sales, and a margin "
            "eroding while the earnings still look adequate is the business getting harder, not "
            "the accounting changing.")


def depositary_note(row: dict) -> str | None:
    """What the cover said, once it has been read.

    The sentence is quoted rather than summarised: a ratio parsed out of prose is
    only as good as the parse, and showing the filer's own words lets a reader
    check it at a glance instead of trusting it.
    """
    receipt = row.get("receipt") or {}
    if receipt.get("title") is None:
        return None
    where = receipt.get("accn") or "the latest annual filing"
    if not receipt["title"]:
        return (f"The cover of {where} names this symbol but tags no security title. It therefore "
                "does not establish whether the price belongs to an ordinary share or a "
                "depositary receipt, or what ratio would join that price to the statement "
                "share count.")
    if not receipt.get("ratio"):
        if cover.is_depositary_security(receipt["title"]):
            return (f"The cover of {where} registers this symbol as \u201c{receipt['title']}\u201d, but "
                    "does not state a ratio the parser can resolve. The price and statement "
                    "share count therefore cannot safely be put on one security basis.")
        return (f"The cover of {where} registers this symbol as \u201c{receipt['title']}\u201d — "
                "one class, no depositary ratio, so the share count in the statements is the "
                "one the price belongs to.")
    return (f"The cover of {where} registers this symbol as \u201c{receipt['title']}\u201d. Every "
            f"per-share figure here is therefore stated per receipt: the share count is the "
            f"ordinary count divided by {receipt['ratio']}, and earnings, book value and "
            "dividends are multiplied by it, so the price and the figures it is compared "
            "against describe one security.")


def foreign_listing_note(row: dict) -> str | None:
    """A foreign issuer's US listing may be a depositary receipt, and the ratio
    between the receipt and the ordinary shares is not in any data this screener
    reads.

    One ADS stands for a fixed number of ordinary shares — thirteen for Onconova,
    two thousand for Akari. The price on this page is per receipt, because that is
    what trades; the share count comes from the financial statements, which count
    ordinary shares. Market capitalisation, P/E, P/B and P/NCAV all divide the two
    together, so all four are wrong by the ratio wherever a receipt is what you buy.

    The ratio is stated in prose on the filing cover — "each American Depositary
    Share represents thirteen ordinary shares" — and prose is exactly what an XBRL
    feed does not carry. So this says what to go and read, rather than guessing.
    """
    if (row.get("receipt") or {}).get("title"):
        return None          # the cover has been read; depositary_note says what it said
    code, _, name = (row.get("incorporation") or "").partition("|")
    # SEC codes US states with two letters and every foreign jurisdiction with a
    # digit: E9 Cayman Islands, X0 United Kingdom, F4 Canada.
    if not code or not any(ch.isdigit() for ch in code):
        return None
    where = name or code
    shares, cover = row.get("shares"), row.get("cover_shares")
    if shares and cover and max(shares, cover) / min(shares, cover) > 1.5:
        return (f"Incorporated in {where}. Its cover page states {cover / 1e6:,.1f}M shares "
                f"while its statements count {shares / 1e6:,.1f}M — "
                f"{max(shares, cover) / min(shares, cover):.1f}x apart, which is what a "
                "depositary ratio looks like. Every per-share figure here divides by the "
                "larger count while the price belongs to the smaller security. Read the ratio "
                "off the cover of the latest 10-K before trusting the market cap, P/E, P/B or "
                "P/NCAV.")
    return (f"Incorporated in {where}. If its US listing is a depositary receipt, one ADS "
            "stands for several ordinary shares, and the market cap, P/E, P/B and P/NCAV on "
            "this page divide by the ordinary count while the price is per receipt. The ratio "
            "is prose on the filing cover and no XBRL feed carries it — check it there before "
            "using any of the four.")


def acquisition_book_note(row: dict) -> str | None:
    """Whether the company's book value is anything more than what it paid for
    other companies.

    NVF ended with a balance sheet whose assets were the accounting of its own
    takeover — Graham's point was that a book value made of goodwill is a book
    value that has never been tested by a buyer. When goodwill and intangibles
    reach the whole of common equity, tangible book is gone."""
    assets, liabilities = row.get("total_assets"), row.get("total_liabilities")
    goodwill = row.get("goodwill") or 0
    intangibles = row.get("intangibles") or 0
    if assets is None or liabilities is None or goodwill + intangibles <= 0:
        return None
    qualified = row.get("asset_quality") or {}
    equity = (qualified["common_equity"] if "common_equity" in qualified else
              assets - liabilities - (row.get("preferred_stock") or 0)
              - (row.get("noncontrolling_interest") or 0) - (row.get("temporary_equity") or 0))
    if equity is None:
        return None
    if equity <= 0 or goodwill + intangibles < equity:
        return None
    return (f"Goodwill and intangibles of {(goodwill + intangibles) / 1e6:,.0f}M stand against "
            f"{equity / 1e6:,.0f}M of common equity, so tangible book value is gone: what the "
            "company paid for other businesses is worth more than everything its shareholders "
            "own. Criterion 7 measures what is left after removing it.")


def peer_efficiency_note(row: dict) -> str | None:
    """Penn Central's operating ratio ran at 47.5% against a comparable
    railroad's 35.2%, and Graham's point was that the gap had been visible for
    years. A margin far under the industry's median says the same thing."""
    peer = row.get("peer_efficiency") or {}
    if not peer.get("behind"):
        return None
    return (f"Operating margin of {peer['margin']}% against a median of "
            f"{peer['industry_median']}% across {peer['peers']} companies in the same industry. "
            "Graham's sixth Penn Central signal was exactly this: an operating gap against "
            "comparable businesses that had been visible for years before the failure.")


_REIT_INDUSTRIES = ("real estate investment trust",)


def tax_note(row: dict) -> str | None:
    """Graham's Penn Central reading: a taxable company that reports profits for
    years while paying no income tax is telling two different stories, and the
    tax authorities' version is the one to believe.

    A partnership, an investment company or a REIT owes no entity-level tax by
    design, so for them the same facts are a structure, not a warning — and each
    is worth stating plainly, because a reader comparing their earnings with an
    ordinary corporation's should know the earnings were never taxed."""
    record = row.get("tax_record") or {}
    profitable = record.get("profitable_years") or 0
    untaxed = record.get("untaxed_years") or 0
    if profitable < 5 or untaxed < 3:
        return None
    window = f"FY{record['window_from']}–FY{record['window_to']}"
    industry = (row.get("industry") or "").lower()
    pass_through = record.get("pass_through") or any(k in industry for k in _REIT_INDUSTRIES)
    if pass_through:
        return (f"{untaxed} of {profitable} profitable years in {window} carried no income tax — "
                "expected for a pass-through structure, which owes no tax at the entity level. "
                "Its earnings are not comparable with a taxed corporation's without adjustment.")
    return (f"{untaxed} of {profitable} profitable years in {window} carried effectively no income "
            "tax. Penn Central reported profits and paid no tax for eleven years before it failed: "
            "earnings the tax authorities do not recognise deserve the same scepticism here.")


def earnings_shape_note(row: dict) -> dict | None:
    """Which half of a ten-year record produced its growth.

    Graham compares smoothed three-year levels about five and ten years apart
    precisely so that one good year cannot carry the comparison. The ten-year
    figure still cannot say *when* the growth happened, and the difference
    matters: a business that grew through both halves has been tested twice,
    while one whose decade went nowhere until its last three years is priced on
    the part of its history nothing has tested yet.
    """
    ch13 = row.get("ch13") or {}
    shape, last = ch13.get("shape"), ch13.get("latest_fy")
    if shape is None or last is None:
        return None
    early, late = ch13["growth_early"], ch13["growth_5y"]
    levels = (f"${ch13['avg_old']:,.2f} in FY{last - 12}–FY{last - 10}, "
              f"${ch13['avg_middle']:,.2f} in FY{last - 7}–FY{last - 5}, "
              f"${ch13['avg_recent']:,.2f} in FY{last - 2}–FY{last}")
    if shape == "sprint":
        jump = ch13.get("latest_vs_prior3")
        latest = (f" FY{last} alone came in {jump:+.0f}% above the three years before it."
                  if jump is not None and jump >= 50 else "")
        return {"kind": "Growth is recent", "text": (
            f"Smoothed earnings ran {levels}: {early:+.0f}% over the first half of the record "
            f"and {late:+.0f}% over the second. Whatever this business is now, it became so "
            f"recently — the ten-year growth Graham asks for is carried entirely by its last "
            f"three years.{latest}")}
    return {"kind": "Steady record", "text": (
        f"Smoothed earnings ran {levels}: {early:+.0f}% over the first half of the record and "
        f"{late:+.0f}% over the second, positive in all ten years, worst single-year fall "
        f"{ch13['max_decline']:.0f}%. Graham's stability test asks only for no deficit; this "
        "record also shows no pause.")}


# How far back a note about reported events may speak. Bounded by the company's
# own newest balance sheet rather than by the clock, so the same stored data
# always produces the same note.
_EVENT_WINDOW_YEARS = 5
# Two 8-Ks are often one auditor transition reported twice: the committee approves
# the change in one, and the outgoing firm's dismissal takes effect in another once
# it has finished the year it was already auditing. Fastenal's pair sit 197 days
# apart and name the same KPMG-to-Deloitte handover; MOG-A, MELI and RNR have the
# same shape at 110, 109 and 185 days. A full year is the bound, because a filer
# that genuinely changed auditor twice inside twelve months is the thing being
# looked for and would still be caught.
_ONE_TRANSITION_DAYS = 365


def _event_window_start(row: dict) -> str | None:
    """The earliest date a note may claim to cover: five years back, or the start
    of what the filing index could show, whichever is later. A prolific filer's
    index holds only its last thousand filings, and a window the data does not
    cover must not be claimed."""
    bs = row.get("balance_sheet_date")
    if not bs:
        return None
    try:
        start = date.fromisoformat(bs) - timedelta(days=365 * _EVENT_WINDOW_YEARS)
    except ValueError:
        return None
    scanned = row.get("events_from")
    return max(start.isoformat(), scanned) if scanned else start.isoformat()


def _dates(dates: list[str]) -> str:
    if len(dates) > 3:
        return f"{', '.join(dates[:3])} and {len(dates) - 3} more"
    if len(dates) == 1:
        return dates[0]
    return f"{', '.join(dates[:-1])} and {dates[-1]}"


# One 8-K item number, what it means, and why a Graham reader wants it beside
# the tests. Ordered by how much the event undermines the figures above it.
#
# These are the only item numbers the engine reads at all, and the list is
# short on purpose: an item number is evidence only where the form fixes its
# meaning. Measured against a 200-company sample, item 5.02 (departure or
# appointment of officers and directors) fires for 87% of companies and cannot
# tell a dismissal from an AGM election, and item 1.02 (termination of a
# material agreement) fires for 38% and cannot tell a lost customer from a
# refinanced credit line. Item 3.01 was read until the audit of 2026-08-21 and is
# not any more: it covers "Notice of Delisting or Failure to Satisfy a Continued
# Listing Rule or Standard; Transfer of Listing", and the code alone cannot tell
# Walmart, Palantir, Linde and Shopify moving exchange from a company in breach —
# 1,451 companies carried the note, and those four are not in trouble. Neither is
# read: a guess about which kind of event a code stands for is not evidence.
_EVENT_NOTES = (
    ("4.02", "Non-reliance",
     "financial statements the company had already published should no longer be relied upon. "
     "Every figure on this page is built from filed statements, and this filer has withdrawn "
     "some of its own."),
    ("1.03", "Bankruptcy",
     "bankruptcy or receivership. The balance-sheet tests here measure a going concern's "
     "cushion, which is not what a court supervises."),
    ("2.04", "Debt acceleration",
     "an event that accelerated or increased a direct financial obligation. Criterion 3 weighs "
     "debt against net current assets as though it comes due on schedule."),
    ("2.06", "Material impairment",
     "an impairment the company judged material enough to report between statements, rather "
     "than wait for the next one."),
)
# the one item whose meaning is a count rather than an occurrence
_AUDITOR_ITEM = "4.01"
# what the scan stores, so that nothing can be noted here without being kept
EVENT_ITEMS = frozenset({item for item, *_ in _EVENT_NOTES} | {_AUDITOR_ITEM})


def _transitions(dates: list[str]) -> list[str]:
    """Event dates with repeat filings about one transition collapsed into it."""
    kept: list[str] = []
    for d in sorted(dates):
        if not kept or (date.fromisoformat(d)
                        - date.fromisoformat(kept[-1])).days > _ONE_TRANSITION_DAYS:
            kept.append(d)
    return kept


def filing_event_notes(row: dict) -> list[dict]:
    """What the company's own filing index proves happened to it.

    An 8-K item number is fixed by the form: a filer cannot report a withdrawn
    financial statement under any code but 4.02. So these are read without ever
    opening the document — the code is the evidence, and a summary of the filing
    would be a guess. Items whose number cannot distinguish trouble from routine
    (officer changes, terminated agreements) are not read at all.
    """
    since = _event_window_start(row)
    if since is None:
        return []
    by_item: dict[str, list[str]] = {}
    for event in row.get("filing_events") or ():
        if event["filed"] >= since:
            by_item.setdefault(event["item"], []).append(event["filed"])
    notes = [{"kind": kind, "text": f"Item {item} filed {_dates(dates)}: {meaning}"}
             for item, kind, meaning in _EVENT_NOTES
             if (dates := sorted(by_item.get(item, ())))]
    # one change of auditor is ordinary; a succession of them is the disclosure
    if len(changes := _transitions(by_item.get(_AUDITOR_ITEM, ()))) >= 2:
        notes.append({"kind": "Auditor changes", "text": (
            f"{len(changes)} changes of certifying accountant since {since} "
            f"(item {_AUDITOR_ITEM}, {_dates(changes)}). The audited figures behind every test on this "
            "page were signed by a succession of firms, none of them for long.")})
    return notes


def _no_dividend_years_inside_the_window(row: dict, record: dict) -> bool:
    """Years inside the 20-year window where the company was demonstrably filing
    (its own annual earnings cover the year) and demonstrably paid nothing.

    Two or more such years disprove the uninterrupted record. Both halves of the
    evidence are required — an absent earnings year is silence, not a non-payment
    — and the years must be recent enough that a payer would certainly have
    tagged the dividend, or an early-XBRL tagging gap would read as a deficit.
    """
    latest_paid, first_paid = record.get("latest"), record.get("first")
    if latest_paid is None or first_paid is None:
        return False
    window_start = latest_paid - 19
    silent = [year for year in _eps_series(row)
              if window_start <= year < first_paid and year >= _DIVIDEND_TAGGING_RELIABLE]
    return len(silent) >= 2


def _defensive(row: dict, profile: str) -> dict:
    """Summarize defensive evidence without claiming a 20-year history that XBRL cannot prove."""
    eps = _eps_series(row)
    latest = _latest_eps_label(row, eps)
    first = min(eps) if eps else None
    recent10 = [eps.get(year) for year in range(latest - 9, latest + 1)] if latest else []
    # A record that only begins after full XBRL coverage belongs to a company with
    # a genuinely short public history, so its whole record IS the available
    # window. An older company's short series is dataset truncation, never a
    # licence to judge fewer years than Graham asked for. The company's own
    # first-ever SEC filing must corroborate the youth: ARCC's EPS record starts
    # in 2020 because the BDC per-share tag is young, but the company filed
    # since 2004 — without the corroboration it would earn unearned passes.
    first_filed = row.get("first_filed")
    listed_after_xbrl = bool(first_filed) and int(str(first_filed)[:4]) > _XBRL_FULL_COVERAGE
    span = latest - first + 1 if latest is not None and first is not None else 0
    short_history = (
        latest is not None
        and first is not None
        and first > _XBRL_FULL_COVERAGE
        and listed_after_xbrl
        and all(year in eps for year in range(first, latest + 1))
    )
    windowed: dict[str, str] = {}

    revenue = _number(row.get("ttm_revenue"))
    assets = _number(row.get("total_assets"))
    liabilities = _number(row.get("total_liabilities"))
    ca = _number(row.get("current_assets"))
    cl = _number(row.get("current_liabilities"))
    ltd = _number(row.get("long_term_debt"))
    shares = _number(row.get("shares"))
    bvps = _number(row.get("bvps"))
    price = _valuation_price(row)
    pe3 = _pe3(row)

    if profile == PROFILE_UTILITY:
        assets_usd = _amount_in_usd(row, assets)
        size = _status(assets_usd >= Decimal("50000000")
                       if assets_usd is not None else None)
        equity = assets - liabilities if assets is not None and liabilities is not None else None
        financial = _status(ltd <= Decimal("2") * equity if ltd is not None and equity is not None else None)
    elif profile == PROFILE_OPERATING:
        revenue_usd = _amount_in_usd(row, revenue)
        size = _status(revenue_usd >= Decimal("100000000")
                       if revenue_usd is not None else None)
        if ca is None or cl is None or ltd is None or cl <= 0:
            financial = "INSUFFICIENT_DATA"
        else:
            working_capital = ca - cl
            financial = _status(ca >= Decimal("2") * cl and ltd <= working_capital)
    else:
        size = "NOT_APPLICABLE"
        financial = "NOT_APPLICABLE"

    if len(recent10) == 10 and not any(value is None for value in recent10):
        # Chapter 14 says "some earnings".  In literal mode, zero is not a
        # deficit; a product may offer a stricter positive-EPS profile later.
        stability = _status(all(value >= 0 for value in recent10))
    elif short_history and span >= _MIN_WINDOW_YEARS:
        stability = _status(all(eps[year] >= 0 for year in range(first, latest + 1)))
        windowed["stability_10y"] = (
            f"judged over the company's full {span}-year record (first FY {first})"
        )
    else:
        stability = "INSUFFICIENT_DATA"

    ch13 = row.get("ch13") or {}
    growth10 = _number(ch13.get("growth_10y"))
    if growth10 is not None:
        # Product policy, the same one the valuation test states below: the
        # comparison is made at the precision the interface publishes. ch13 rounds
        # growth to one decimal, so a record that grew exactly 33 1/3 % arrives as
        # 33.3 and must not fail a threshold carried to ten.
        growth = _status(growth10 >= Decimal("33.3"))
    else:
        growth = "INSUFFICIENT_DATA"
        if short_history:
            spacing = latest - (first + 2)
            base = _avg3(eps, first + 2)
            recent = _avg3(eps, latest)
            if spacing >= _MIN_WINDOW_YEARS and base is not None and recent is not None and base > 0:
                pct = float(recent / base - 1) * 100
                # Graham's 33 1/3 % is a per-decade rate; a shorter spacing gets
                # the same compound rate over the years the record actually has
                required = ((4 / 3) ** (spacing / 10) - 1) * 100
                growth = _status(pct >= required)
                windowed["growth_10y"] = (
                    f"{pct:+.1f}% over the {spacing}-year spacing the record allows; "
                    f"threshold scaled to {required:.1f}%"
                )

    record = row.get("dividend_record") or {}
    latest_dividend_year = record.get("latest")
    streak_start = record.get("streak_from")
    if _criterion_status(row, 5) == "FAIL":
        # Criterion 5 FAIL means the filer verifiably pays nothing now, and a
        # current non-payer cannot hold an uninterrupted 20-year record —
        # however far back the paid years reach.
        dividend = "FAIL"
    elif latest_dividend_year is None or streak_start is None:
        dividend = "INSUFFICIENT_DATA"
    elif latest_dividend_year - streak_start + 1 >= 20:
        dividend = "PASS"
    elif (
        short_history
        and span >= _MIN_WINDOW_YEARS
        # a mid-year listing may push the first payout into the next calendar
        # year, and the latest payout may trail the latest fiscal year by one
        and streak_start <= first + 1
        and latest_dividend_year >= latest - 1
    ):
        dividend = "PASS"
        windowed["dividend_20y"] = (
            f"paid every year of the company's {span}-year public record; "
            "20 years is longer than the company has traded"
        )
    elif _no_dividend_years_inside_the_window(row, record):
        # The record does not merely fail to prove 20 years — it disproves them:
        # the company was filing (its own earnings series covers those years)
        # and paid nothing, so no uninterrupted 20-year record can exist.
        dividend = "FAIL"
    else:
        # A short XBRL record cannot prove a 20-year uninterrupted record and
        # must not be turned into a fail merely because the dataset is young.
        dividend = "INSUFFICIENT_DATA"

    if pe3 is None or bvps is None or bvps <= 0 or price is None:
        valuation = "INSUFFICIENT_DATA"
    else:
        pb = price / bvps
        # Product policy: the defensive valuation comparison follows the same
        # two-decimal ratios displayed in the interface.  Thus a displayed
        # 22.50× product passes; a displayed 22.51× product fails.
        displayed_pe3 = pe3.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        displayed_product = (pe3 * pb).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        valuation = _status(displayed_pe3 <= Decimal("15.00") and displayed_product <= Decimal("22.50"))

    result = _summary(
        {
            "size": size,
            "financial_position": financial,
            "stability_10y": stability,
            "dividend_20y": dividend,
            "growth_10y": growth,
            "valuation": valuation,
        },
        eligible=profile in {PROFILE_OPERATING, PROFILE_UTILITY},
    )
    if windowed:
        result["windowed"] = windowed
    result["profile_note"] = (
        "Dividend history is evidence-incomplete unless this dataset itself proves an "
        "uninterrupted 20-year record — or the record covers the whole public life of "
        "a company listed after 2011 (disclosed per test)."
    )
    return result


def prose_gaps(row: dict) -> list[dict]:
    """What this company's filings settle and its XBRL does not.

    Every other figure on this page comes from a tagged fact with an accession
    behind it. These are the places where the answer exists only as a sentence in
    a document — a cover page, the body of an 8-K — so instead of guessing, the
    screen states the question, names the figures it would change, and points at
    the filing to read. A number that might be wrong is worse than a number marked
    unverifiable, and the reader is the one who can open the document.
    """
    gaps: list[dict] = []
    latest_filing = ((row.get("sources") or {}).get("eps") or {}).get("accn")
    code, _, name = (row.get("incorporation") or "").partition("|")
    foreign = bool(code) and any(ch.isdigit() for ch in code)
    shares, cover = row.get("shares"), row.get("cover_shares")
    split = bool(shares and cover and max(shares, cover) / min(shares, cover) > 1.5)

    # Whether a tagged conversion is still to come or already inside the count is
    # the one question a cover page cannot settle, so it is asked before the
    # receipt shortcut below rather than after it.
    conversion = next((n for n in (row.get("context_notes") or ())
                       if n.get("kind") == "Convertible preferred"), None)
    if conversion:
        # The share-class axis in the quarterly datasets settles whether any preferred
        # is left; what it cannot settle is what the conversion figure counts.
        live = "still outstanding" in conversion["text"]
        gaps.append({
            "what": ("How many common shares the outstanding preferred actually becomes — "
                     "the tagged figure may be that total or a ceiling nobody reaches"
                     if live else
                     "Whether the convertible preferred has already converted — its shares "
                     "then sit inside the common count — or still stands ahead of the "
                     "common with the right to convert"),
            "affects": "The share count, and with it EPS, book value per share, market cap "
                       "and every criterion struck per share",
            "where": ("The capitalisation note of the latest annual report, which states the "
                      "conversion ratio" if live else
                      "The capitalisation note and the equity statement of the latest annual "
                      "report; the preferred is filed under a share-class axis, which is why "
                      "the count cannot answer this"),
            "accn": latest_filing,
        })

    # A cover that has been read answers both questions below — the class the
    # symbol belongs to is named, and any ratio has already been applied here.
    if (row.get("receipt") or {}).get("title"):
        return gaps

    if foreign and row.get("security_basis") != "PRIMARY_ORDINARY_SHARE":
        gaps.append({
            "what": "Whether the listed security is a depositary receipt, and how many "
                    "ordinary shares one receipt stands for"
                    + (f" — the cover count and the statement count are "
                       f"{max(shares, cover) / min(shares, cover):.1f}x apart, which is what "
                       "a ratio looks like" if split else ""),
            "affects": "Market cap, P/E, P/B, P/NCAV — all four divide by the share count "
                       "while the price belongs to the receipt",
            "where": f"Cover page of the latest 10-K, \u201cTitle of each class\u201d "
                     f"(incorporated in {name or code})",
            "accn": latest_filing,
        })
    elif split:
        gaps.append({
            "what": "Which share class the ticker represents: the cover page states "
                    f"{cover / 1e6:,.1f}M shares and the statements count {shares / 1e6:,.1f}M",
            "affects": "Market cap and every per-share figure",
            "where": "Cover page of the latest 10-K",
            "accn": latest_filing,
        })

    if row.get("listed") not in {"y", "external"}:
        gaps.append({
            "what": "Whether this symbol is still tradable. SEC's ticker file no longer "
                    "assigns it to this company — the equity may have been delisted, "
                    "deregistered or moved to a successor entity, while the company goes on "
                    "filing",
            "affects": "The price, and therefore P/E, P/TBV, P/NCAV, market cap and the "
                       "dividend yield — all of them rest on a quote that may belong to "
                       "another security or to none",
            "where": "The company's filing index — a Form 25 or Form 15 settles it",
            "accn": None,
        })

    since = _event_window_start(row)
    for event in (row.get("filing_events") or ()):
        if event["item"] == "3.01" and (since is None or event["filed"] >= since):
            gaps.append({
                "what": "Whether the item 3.01 filed on " + event["filed"] + " is a listing "
                        "deficiency or a routine transfer of listing between exchanges — the "
                        "item number covers both and only the document says which",
                "affects": "Nothing computed; it decides whether the company is in breach of "
                           "a listing rule",
                "where": "The 8-K itself",
                "accn": event["accn"],
            })
            break
    return gaps


def enrich(row: dict) -> dict:
    """Add compact, JSON-safe applicability and alignment fields to one dashboard row."""
    profile = profile_for(row.get("sector"))
    # the tax reading needs the company's industry, which the facts do not carry
    notes = list(row.get("context_notes") or ())
    for kind, builder in (("Income tax", tax_note),
                          ("Peer efficiency", peer_efficiency_note),
                          ("Margin", margin_note),
                          ("Foreign listing", foreign_listing_note),
                          ("Depositary receipt", depositary_note),
                          ("Acquisition-only book", acquisition_book_note)):
        if (note := builder(row)) is not None:
            notes.append({"kind": kind, "text": note})
    # these two decide their own kind: the shape of the record names it, and one
    # filing index can prove several different things at once
    if (shape := earnings_shape_note(row)) is not None:
        notes.append(shape)
    notes.extend(filing_event_notes(row))
    routes = analysis_routes(row)
    return {
        "graham_profile": profile,
        **({"analysis_routes": routes} if routes else {}),
        # what the reader must open a filing to settle; rendered in the panel and
        # marked on the table row, so a missing answer is visible before it is needed
        "prose_gaps": prose_gaps(row),
        "graham_profile_meta": PROFILE_META[profile],
        "context_notes": notes,
        "alignment": {
            "enterprising": _enterprising(row, profile),
            "defensive": _defensive(row, profile),
        },
    }
