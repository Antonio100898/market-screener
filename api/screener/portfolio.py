"""Portfolio accounting and immutable trade-time screening evidence.

The screener answers whether a security meets a set of tests now.  A portfolio
also has to remember what the same tests said when capital was committed.  This
module deliberately keeps that history in the trade ledger instead of mutating
the financial snapshots used by the research universe.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

from .profiles import _latest_eps_label


ZERO = Decimal("0")
CENT = Decimal("0.01")


class PortfolioError(ValueError):
    pass


def decimal_value(value, label: str, *, positive: bool = False,
                  nonnegative: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PortfolioError(f"{label} must be a valid number")
    if not number.is_finite():
        raise PortfolioError(f"{label} must be finite")
    if positive and number <= 0:
        raise PortfolioError(f"{label} must be greater than zero")
    if nonnegative and number < 0:
        raise PortfolioError(f"{label} cannot be negative")
    return number


def canonical_decimal(value: Decimal) -> str:
    """Plain decimal text for SQLite; never exponent notation or binary float."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _number(value) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _ratio(numerator: Decimal, denominator) -> Decimal | None:
    bottom = _number(denominator)
    return numerator / bottom if bottom is not None and bottom > 0 else None


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _price_on_reporting_basis(row: dict, price: Decimal) -> Decimal | None:
    from .sync import _valuation_price
    return _valuation_price(row, price)


def _quote_after_trade(price_asof: str | None, executed_at: str | None) -> bool | None:
    """Whether a quote can measure performance after the latest ledger event."""
    if not price_asof or not executed_at:
        return None
    try:
        quote_time = datetime.fromisoformat(price_asof)
        trade_time = datetime.fromisoformat(executed_at)
    except (TypeError, ValueError):
        return None
    if quote_time.tzinfo is None or trade_time.tzinfo is None:
        return None
    return quote_time.astimezone(timezone.utc) >= trade_time.astimezone(timezone.utc)


def _pe3(row: dict, price: Decimal) -> Decimal | None:
    annual = row.get("annual_eps") or {}
    years = sorted((int(year) for year in annual), reverse=True)[:3]
    if len(years) < 3:
        return None
    if _latest_eps_label(row, dict.fromkeys(years)) > years[0]:
        return None
    values = [_number(annual.get(str(year), annual.get(year))) for year in years]
    if any(value is None for value in values):
        return None
    average = sum(values, ZERO) / Decimal(3)
    return price / average if average > 0 else None


def valuation_at_price(row: dict, price) -> dict:
    """Multiples tied to the actual execution price and the row's filing basis."""
    price_d = decimal_value(price, "price", positive=True)
    financial_price = _price_on_reporting_basis(row, price_d)
    pe = _ratio(financial_price, row.get("ttm_eps")) if financial_price else None
    pe3 = _pe3(row, financial_price) if financial_price else None
    pb = _ratio(financial_price, row.get("bvps")) if financial_price else None
    ptbv = _ratio(financial_price, row.get("tbvps")) if financial_price else None
    pncav = _ratio(financial_price, row.get("ncavps")) if financial_price else None

    recurring = _number(row.get("recurring_dividend_per_share"))
    dividend_yield = (recurring / financial_price * 100
                      if recurring is not None and recurring >= 0 and financial_price else None)

    all_capex_floor_per_share = maintenance_estimate_per_share = free_cash_flow_per_share = None
    owner = row.get("owner_earnings") or {}
    fiscal_year = owner.get("fiscal_year")
    if fiscal_year is not None:
        cell = (owner.get("annual_per_share") or {}).get(str(fiscal_year), {})
        all_capex_floor_per_share = _number(cell.get("all_capex_floor_per_share"))
        maintenance_estimate_per_share = _number(cell.get("maintenance_estimate_per_share"))
        free_cash_flow_per_share = _number(cell.get("free_cash_flow_per_share"))

    def cash_yield(value):
        return value / financial_price * 100 if value is not None and financial_price else None

    defensive_product = pe3 * pb if pe3 is not None and pb is not None else None
    defensive_valuation = None
    if pe3 is not None and defensive_product is not None:
        shown_pe3 = pe3.quantize(CENT, rounding=ROUND_HALF_UP)
        shown_product = defensive_product.quantize(CENT, rounding=ROUND_HALF_UP)
        defensive_valuation = (
            "PASS" if shown_pe3 <= Decimal("15") and shown_product <= Decimal("22.5")
            else "FAIL"
        )

    return {
        "price": float(price_d),
        "price_reporting_currency": _float(financial_price),
        "pe": _float(pe),
        "pe3": _float(pe3),
        "pb": _float(pb),
        "ptbv": _float(ptbv),
        "pncav": _float(pncav),
        "recurring_dividend_yield": _float(dividend_yield),
        # These legacy names are deliberately null. Maintenance capex is absent from
        # primary XBRL, so none of the available figures is definitive owner earnings.
        "price_to_owner_earnings": None,
        "owner_earnings_yield": None,
        "price_to_all_capex_floor": _float(
            _ratio(financial_price, all_capex_floor_per_share)) if financial_price else None,
        "all_capex_floor_yield": _float(cash_yield(all_capex_floor_per_share)),
        "price_to_maintenance_estimate": _float(
            _ratio(financial_price, maintenance_estimate_per_share)
            if financial_price else None),
        "maintenance_estimate_yield": _float(cash_yield(maintenance_estimate_per_share)),
        "price_to_free_cash_flow": _float(
            _ratio(financial_price, free_cash_flow_per_share)) if financial_price else None,
        "free_cash_flow_yield": _float(cash_yield(free_cash_flow_per_share)),
        "defensive_product": _float(defensive_product),
        "defensive_valuation": defensive_valuation,
    }


def criteria_at_price(row: dict, price) -> list[dict]:
    """Keep filing tests intact and resettle only criteria 1 and 7 at the fill."""
    price_d = decimal_value(price, "price", positive=True)
    financial_price = _price_on_reporting_basis(row, price_d)
    criteria = deepcopy(row.get("criteria") or [])
    by_number = {criterion.get("n"): criterion for criterion in criteria}

    earnings = _number(row.get("ttm_eps"))
    c1 = by_number.get(1)
    if (c1 is not None and financial_price is not None and earnings is not None
            and earnings > 0 and c1.get("value") is not None):
        c1["value"] = float((financial_price / earnings).quantize(CENT, rounding=ROUND_HALF_UP))
        c1["status"] = "PASS" if financial_price < Decimal("10") * earnings else "FAIL"

    tbvps = _number(row.get("tbvps"))
    c7 = by_number.get(7)
    if (c7 is not None and financial_price is not None and tbvps is not None
            and tbvps > 0 and c7.get("value") is not None):
        c7["value"] = float((financial_price / tbvps).quantize(CENT, rounding=ROUND_HALF_UP))
        c7["status"] = "PASS" if financial_price < Decimal("1.2") * tbvps else "FAIL"

    return criteria


def _alignment_at_price(row: dict, criteria: list[dict], valuation: dict) -> dict:
    alignment = deepcopy(row.get("alignment") or {})
    statuses = {criterion.get("n"): criterion.get("status") for criterion in criteria}
    enterprising = alignment.get("enterprising")
    if enterprising and enterprising.get("verdict") != "OUT_OF_SCOPE":
        tests = {
            "valuation": statuses.get(1, "INSUFFICIENT_DATA"),
            "liquidity": statuses.get(2, "INSUFFICIENT_DATA"),
            "debt": statuses.get(3, "INSUFFICIENT_DATA"),
            "stability": statuses.get(4, "INSUFFICIENT_DATA"),
            "dividend": statuses.get(5, "INSUFFICIENT_DATA"),
            "tangible_assets": statuses.get(7, "INSUFFICIENT_DATA"),
        }
        enterprising.update(_alignment_summary(tests))

    defensive = alignment.get("defensive")
    if defensive and defensive.get("verdict") != "OUT_OF_SCOPE":
        tests = deepcopy(defensive.get("tests") or {})
        tests["valuation"] = valuation.get("defensive_valuation") or "INSUFFICIENT_DATA"
        defensive.update(_alignment_summary(tests))
    return alignment


def _alignment_summary(tests: dict[str, str]) -> dict:
    passed = sum(status == "PASS" for status in tests.values())
    failed = sum(status == "FAIL" for status in tests.values())
    unknown = len(tests) - passed - failed
    verdict = "BLOCKED" if failed else "EVIDENCE_INCOMPLETE" if unknown else "ALIGNED"
    return {"verdict": verdict, "passed": passed, "total": len(tests),
            "unknown": unknown, "tests": tests}


def decision_snapshot(dashboard: dict, row: dict, execution_price,
                      *, captured_at: str | None = None) -> dict:
    captured_at = captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    valuation = valuation_at_price(row, execution_price)
    criteria = criteria_at_price(row, execution_price)
    return {
        "captured_at": captured_at,
        "dashboard_generated": dashboard.get("generated"),
        "engine_version": dashboard.get("engine_version"),
        "displayed": deepcopy(row),
        "execution": {
            "valuation": valuation,
            "criteria": criteria,
            "n_pass": sum(c.get("status") == "PASS" for c in criteria),
            "alignment": _alignment_at_price(row, criteria, valuation),
        },
    }


def criterion_changes(before: Iterable[dict], after: Iterable[dict]) -> list[dict]:
    old = {criterion.get("n"): criterion for criterion in before}
    new = {criterion.get("n"): criterion for criterion in after}
    changes = []
    for number in sorted(set(old) | set(new)):
        left, right = old.get(number, {}), new.get(number, {})
        if left.get("status") != right.get("status"):
            changes.append({
                "n": number,
                "from": left.get("status"),
                "to": right.get("status"),
                "from_value": left.get("value"),
                "to_value": right.get("value"),
                "price_dependent": number in {1, 7},
            })
    return changes


def build_portfolio(portfolio: dict, trades: list[dict], current_rows: dict[str, dict],
                    manual_assets: list[dict] | None = None,
                    cash: dict | None = None) -> dict:
    """FIFO positions and P&L, using Decimal until the API boundary."""
    states: dict[str, dict] = {}
    serialised_trades = []
    for raw in trades:
        trade = dict(raw)
        quantity = decimal_value(trade["quantity"], "quantity", positive=True)
        price = decimal_value(trade["price"], "price", positive=True)
        fees = decimal_value(trade.get("fees", "0"), "fees", nonnegative=True)
        side = trade["side"]
        state = states.setdefault(trade["cik"], {
            "lots": [], "realized_pnl": ZERO, "fees": ZERO, "trades": [],
            "ticker": trade["ticker"],
        })
        state["ticker"] = trade["ticker"]
        state["fees"] += fees

        if side == "BUY":
            cost = quantity * price + fees
            state["lots"].append({
                "quantity": quantity,
                "cost_per_share": cost / quantity,
                "execution_price": price,
                "snapshot": trade["decision_snapshot"],
                "executed_at": trade["executed_at"],
                "trade_id": trade["id"],
            })
        elif side == "SELL":
            remaining = quantity
            basis = ZERO
            for lot in state["lots"]:
                if remaining <= 0:
                    break
                used = min(lot["quantity"], remaining)
                basis += used * lot["cost_per_share"]
                lot["quantity"] -= used
                remaining -= used
            if remaining > 0:
                raise PortfolioError(
                    f"{trade['ticker']} sell exceeds the shares held at {trade['executed_at']}"
                )
            state["realized_pnl"] += quantity * price - fees - basis
        else:
            raise PortfolioError(f"unsupported trade side {side!r}")

        public_trade = {key: value for key, value in trade.items()}
        public_trade.update({"quantity": float(quantity), "price": float(price), "fees": float(fees),
                             "gross_value": float(quantity * price)})
        state["trades"].append(public_trade)
        serialised_trades.append(public_trade)

    positions = []
    for cik, state in states.items():
        lots = [lot for lot in state["lots"] if lot["quantity"] > 0]
        quantity = sum((lot["quantity"] for lot in lots), ZERO)
        if quantity <= 0:
            continue
        cost_basis = sum((lot["quantity"] * lot["cost_per_share"] for lot in lots), ZERO)
        entry_value = sum((lot["quantity"] * lot["execution_price"] for lot in lots), ZERO)
        average_cost = cost_basis / quantity
        average_entry = entry_value / quantity
        current = current_rows.get(cik)
        current_price = _number(current.get("price")) if current else None
        market_value = quantity * current_price if current_price is not None else None
        unrealized = market_value - cost_basis if market_value is not None else None
        pnl_pct = unrealized / cost_basis * 100 if unrealized is not None and cost_basis > 0 else None
        price_change = ((current_price / average_entry - 1) * 100
                        if current_price is not None and average_entry > 0 else None)
        latest_lot = max(lots, key=lambda lot: (lot["executed_at"], lot["trade_id"]))
        entry = latest_lot["snapshot"]
        entry_execution = entry.get("execution") or {}
        entry_meta = {
            "captured_at": entry.get("captured_at"),
            "dashboard_generated": entry.get("dashboard_generated"),
            "engine_version": entry.get("engine_version"),
            "displayed_price": (entry.get("displayed") or {}).get("price"),
            "displayed_price_asof": (entry.get("displayed") or {}).get("price_asof"),
            "displayed_price_session": (entry.get("displayed") or {}).get("price_session"),
            "displayed_market_state": (entry.get("displayed") or {}).get("market_state"),
            "displayed_market_state_asof": (entry.get("displayed") or {}).get("market_state_asof"),
        }
        current_valuation = valuation_at_price(current, current_price) if current and current_price else None
        changes = criterion_changes(
            entry_execution.get("criteria") or [], current.get("criteria") or [] if current else []
        )
        trade_dates = [trade["executed_at"] for trade in state["trades"]]
        latest_trade_at = max(trade_dates)
        price_asof = current.get("price_asof") if current else None
        quote_after_latest_trade = _quote_after_trade(price_asof, latest_trade_at)
        positions.append({
            "cik": cik,
            "ticker": current.get("ticker") if current else state["ticker"],
            "name": current.get("name") if current else state["ticker"],
            "quantity": float(quantity),
            "average_entry_price": float(average_entry),
            "average_cost": float(average_cost),
            "cost_basis": float(cost_basis),
            "current_price": _float(current_price),
            "price_asof": price_asof,
            "price_session": current.get("price_session") if current else None,
            "market_state": current.get("market_state") if current else None,
            "market_timezone": current.get("market_timezone") if current else None,
            "market_state_asof": current.get("market_state_asof") if current else None,
            "price_source": current.get("price_source") if current else None,
            "quote_refresh_warning": current.get("quote_refresh_warning") if current else None,
            "quote_after_latest_trade": quote_after_latest_trade,
            "market_value": _float(market_value),
            "unrealized_pnl": _float(unrealized),
            "unrealized_pnl_pct": _float(pnl_pct),
            "price_change_pct": _float(price_change),
            "realized_pnl": float(state["realized_pnl"]),
            "fees": float(state["fees"]),
            "first_trade_at": min(trade_dates),
            "latest_trade_at": latest_trade_at,
            # The complete immutable row remains on every trade.  Repeating it
            # here would double a portfolio response for no additional evidence.
            "entry_snapshot": entry_meta,
            "entry_valuation": entry_execution.get("valuation"),
            "entry_criteria": entry_execution.get("criteria") or [],
            "entry_alignment": entry_execution.get("alignment") or {},
            "current_valuation": current_valuation,
            "current_criteria": current.get("criteria") or [] if current else [],
            "current_alignment": current.get("alignment") or {} if current else {},
            "criterion_changes": changes,
            "trades": state["trades"],
        })

    total_cost = sum((_number(position["cost_basis"]) or ZERO for position in positions), ZERO)
    market_values = [_number(position["market_value"]) for position in positions]
    total_market = (sum((value for value in market_values if value is not None), ZERO)
                    if any(value is not None for value in market_values) else None)
    total_unrealized = total_market - total_cost if total_market is not None else None
    total_realized = sum((_number(position["realized_pnl"]) or ZERO for position in positions), ZERO)
    total_fees = sum((_number(trade["fees"]) or ZERO for trade in serialised_trades), ZERO)
    pre_trade_quotes = sum(position["quote_after_latest_trade"] is False for position in positions)
    post_trade_quotes = sum(position["quote_after_latest_trade"] is True for position in positions)
    unavailable_quote_times = len(positions) - pre_trade_quotes - post_trade_quotes
    for position in positions:
        value = _number(position["market_value"])
        position["weight_pct"] = (
            float(value / total_market * 100) if value is not None and total_market and total_market > 0
            else None
        )

    positions.sort(key=lambda position: position["ticker"] or "")
    result = {
        "portfolio": portfolio,
        "summary": {
            "positions": len(positions),
            "cost_basis": float(total_cost),
            "market_value": _float(total_market),
            "unrealized_pnl": _float(total_unrealized),
            "unrealized_pnl_pct": _float(total_unrealized / total_cost * 100)
            if total_unrealized is not None and total_cost > 0 else None,
            "realized_pnl": float(total_realized),
            "fees": float(total_fees),
            "pre_trade_quotes": pre_trade_quotes,
            "post_trade_quotes": post_trade_quotes,
            "unavailable_quote_times": unavailable_quote_times,
        },
        "positions": positions,
    }
    return _add_manual_assets(result, manual_assets or [], cash)


def _add_manual_assets(result: dict, raw_assets: list[dict], cash: dict | None) -> dict:
    """Add non-stock holdings and an honest all-assets allocation.

    Stock quotes remain the dashboard's filing/market-backed values. Bond prices
    are manual and crypto prices are enriched by the API from Coinbase. An absent
    cash balance or missing stock quote withholds the combined total instead of
    silently becoming zero.
    """
    assets = {"bonds": [], "crypto": []}
    totals = {"BOND": ZERO, "CRYPTO": ZERO}
    for raw in raw_assets:
        asset_type = raw.get("asset_type")
        if asset_type not in totals:
            raise PortfolioError(f"unsupported portfolio asset type {asset_type!r}")
        quantity = decimal_value(raw.get("quantity"), "quantity", positive=True)
        current_price = decimal_value(
            raw.get("current_price"), "current price", nonnegative=True)
        market_value = quantity * current_price
        totals[asset_type] += market_value
        item = dict(raw)
        item.update({
            "quantity": float(quantity),
            "current_price": float(current_price),
            "market_value": float(market_value),
        })
        assets["bonds" if asset_type == "BOND" else "crypto"].append(item)

    positions = result["positions"]
    missing_stock_values = sum(position.get("market_value") is None for position in positions)
    stock_value = (
        sum((_number(position["market_value"]) or ZERO for position in positions), ZERO)
        if not missing_stock_values else None
    )
    cash_amount = (
        decimal_value(cash.get("amount"), "liquid cash", nonnegative=True)
        if cash is not None else None
    )
    invested_assets = (
        stock_value + totals["BOND"] + totals["CRYPTO"]
        if stock_value is not None else None
    )
    total_assets = (
        invested_assets + cash_amount
        if invested_assets is not None and cash_amount is not None else None
    )

    allocation = [
        {"type": "STOCK", "label": "Stocks", "value": _float(stock_value)},
        {"type": "BOND", "label": "Bonds", "value": float(totals["BOND"])},
        {"type": "CRYPTO", "label": "Crypto", "value": float(totals["CRYPTO"])},
        {"type": "CASH", "label": "Liquid cash", "value": _float(cash_amount)},
    ]
    for category in allocation:
        value = _number(category["value"])
        category["weight_pct"] = (
            float(value / total_assets * 100)
            if value is not None and total_assets is not None and total_assets > 0 else None
        )

    for position in positions:
        value = _number(position.get("market_value"))
        position["portfolio_weight_pct"] = (
            float(value / total_assets * 100)
            if value is not None and total_assets is not None and total_assets > 0 else None
        )
    for group in assets.values():
        for item in group:
            value = _number(item["market_value"])
            item["weight_pct"] = (
                float(value / total_assets * 100)
                if value is not None and total_assets is not None and total_assets > 0 else None
            )

    result["assets"] = assets
    result["cash"] = {
        "amount": _float(cash_amount),
        "updated_at": cash.get("updated_at") if cash else None,
    }
    result["allocation"] = allocation
    result["summary"].update({
        "holdings": len(positions) + len(raw_assets),
        "stock_value": _float(stock_value),
        "bond_value": float(totals["BOND"]),
        "crypto_value": float(totals["CRYPTO"]),
        "liquid_cash": _float(cash_amount),
        "invested_assets": _float(invested_assets),
        "total_assets": _float(total_assets),
        "unpriced_stock_positions": missing_stock_values,
    })
    return result
