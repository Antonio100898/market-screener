"""Build display/query fields at publication, over the complete eligible release.

Inputs are JSON display rows already enriched by the existing financial engine.
These projections never replace reported observations or strict screen results.
Request handlers read the stored projections; they must not rank a result page.
"""
from __future__ import annotations

from bisect import bisect_right
from math import isfinite
from typing import Any, Mapping, Sequence

from .profiles import _latest_eps_label


PROJECTION_REVISION = 1
_METRICS = (("roe", False), ("roic", False), ("ronta", False),
            ("fcfRevenue", False), ("capexOcf", True), ("debtToEquity", True))


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and isfinite(value)


def _number(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _first(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _sum_numbers(values) -> float:
    # Python's compensated sum changes nearly cancelling EPS denominators.
    total = 0.0
    for value in values:
        total += float(value)
    return total


def criterion(row: Mapping[str, Any], number: int) -> Mapping[str, Any]:
    return next((item for item in row.get("criteria", ()) if item["n"] == number), {})


def valuation_price(row: Mapping[str, Any]) -> float | None:
    reporting = _first(row.get("reporting_currency"), row.get("currency"), "USD")
    quote = _first(row.get("quote_currency"), row.get("currency"), "USD")
    return row.get("price" if reporting == quote else "price_reporting_currency")


def pe3(row: Mapping[str, Any]) -> float | None:
    annual = row.get("annual_eps") or {}
    price = valuation_price(row)
    years = sorted((int(year) for year in annual), reverse=True)[:3]
    if not price or len(years) < 3:
        return None
    if _latest_eps_label(row, dict.fromkeys(years)) > years[0]:
        return None
    price_year = int((row.get("price_asof") or "")[:4] or 0)
    if price_year and years[0] < price_year - 2:
        return None
    # The existing table treats an explicit null EPS in a selected key as zero.
    average = _sum_numbers(annual.get(str(year), annual.get(year)) or 0 for year in years) / 3
    return float(price) / average if average > 0 else None


def eps_evidence(annual: Mapping[str, Any] | None, latest_fy: int | None = None) -> dict[str, Any] | None:
    annual = annual or {}
    known = sorted(int(year) for year in annual)
    latest = _latest_eps_label({"ch13": {"latest_fy": latest_fy}}, dict.fromkeys(known))
    if latest is None:
        return None
    years = list(range(latest - 9, latest + 1))
    value = lambda year: annual.get(str(year), annual.get(year))
    values = [value(year) for year in years]
    present = [item for item in values if item is not None]
    positive = sum(item > 0 for item in present)
    base_years = list(range(latest - 12, latest - 9))
    recent_years = list(range(latest - 2, latest + 1))
    base = [value(year) for year in base_years]
    recent = [value(year) for year in recent_years]
    growth = None
    if all(item is not None for item in base + recent):
        first_average = _sum_numbers(base) / 3
        last_average = _sum_numbers(recent) / 3
        if first_average > 0:
            growth = (last_average / first_average - 1) * 100
    return {"years": years, "values": values, "present": len(present),
            "positive": positive, "stable": len(present) == 10 and positive == 10,
            "growth": growth, "latest": latest, "growthBaseYears": base_years,
            "growthRecentYears": recent_years}


def _bridge_value(point: Any) -> Any:
    if isinstance(point, list):
        return point[0] if point else None
    return point.get("value") if isinstance(point, dict) else point


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return _number(median)


def _quality_inputs(row: Mapping[str, Any]) -> dict[str, Any]:
    overlay = row.get("return_quality_assumption") or {}
    assumption = overlay if overlay.get("status") == "APPLIED" else None
    overlay = row.get("operating_returns_estimate") or {}
    estimate = overlay if not assumption and overlay.get("status") == "CONSERVATIVE_LOWER_BOUND" else {}
    input_assumptions = (assumption or {}).get("input_assumptions") or {}
    roe = _number(assumption.get("roe") if assumption is not None
                  else (row.get("profitability") or {}).get("on_equity"))

    def metric(name: str) -> tuple[float | None, bool, bool]:
        if assumption is not None:
            assumed = input_assumptions.get(name) or []
            value = None if "operating_income" in assumed else _number(assumption.get(name))
            return value, bool(assumed), False
        exact = _number((row.get("operating_returns") or {}).get(name))
        lower_bound = _number(estimate.get(name))
        return (exact, False, False) if exact is not None else (lower_bound, False, lower_bound is not None)

    roic, roic_assumed, roic_estimated = metric("nopat_roic")
    ronta, ronta_assumed, ronta_estimated = metric("ronta")
    oe = row.get("owner_earnings") or {}
    latest = oe.get("fiscal_year")
    years = range(int(latest) - 9, int(latest) + 1) if _finite(latest) and int(latest) == latest else ()
    annual = oe.get("annual_per_share") or {}
    revenue = row.get("annual_revenue") or {}
    fcf_margins, capex_ratios = [], []
    has_fcf = has_ocf = False
    for year in years:
        cell = annual.get(str(year), annual.get(year)) or {}
        bridge = cell.get("cash_flow_bridge") or {}
        fcf = _number(cell.get("free_cash_flow"))
        sales = _number(revenue.get(str(year), revenue.get(year)))
        ocf = _number(_bridge_value(bridge.get("operating_cash_flow")))
        capex = _number(_bridge_value(bridge.get("total_capital_expenditure")))
        has_fcf |= fcf is not None
        has_ocf |= ocf is not None
        if fcf is not None and sales is not None and sales > 0:
            margin = fcf / sales * 100
            if isfinite(margin):
                fcf_margins.append(margin)
        if capex is not None and ocf is not None and ocf > 0:
            ratio = abs(capex) / ocf * 100
            if isfinite(ratio):
                capex_ratios.append(ratio)
    debt = _number(assumption.get("debt_to_equity") if assumption is not None else row.get("debt_to_equity"))
    debt = debt if debt is not None and debt >= 0 else None
    fcf_median, capex_median = _median(fcf_margins), _median(capex_ratios)
    names = {"ROE": roe, "ROIC": roic, "RONTA": ronta, "FCF/revenue": fcf_median,
             "D/E": debt, "CapEx/OCF": capex_median}
    missing = [name for name, value in names.items() if value is None]
    return {"roe": roe, "roic": roic, "ronta": ronta, "fcfRevenue": fcf_median,
            "fcfRevenueYears": len(fcf_margins), "capexOcf": capex_median,
            "capexOcfYears": len(capex_ratios), "debtToEquity": debt,
            "missingInputs": missing, "inputsPresent": 6 - len(missing),
            "noReturnEvidence": all(value is None for value in (roe, roic, ronta, fcf_median)),
            "cashFlowMissing": not has_fcf or not has_ocf,
            "assumptionMode": assumption is not None,
            "assumptionApplied": bool((assumption or {}).get("applied")),
            "assumedInputs": {"roe": bool(input_assumptions.get("roe")), "roic": roic_assumed,
                              "ronta": ronta_assumed and ronta is not None,
                              "debtToEquity": bool(input_assumptions.get("debt_to_equity"))},
            "estimated": roic_estimated or ronta_estimated,
            "estimatedInputs": {"roic": roic_estimated, "ronta": ronta_estimated},
            "assumptions": _first((assumption or {}).get("applied"), estimate.get("operating_return_assumptions"), []),
            "estimateNote": _first((assumption or {}).get("note"), estimate.get("note"))}


def return_quality_percentiles(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    qualities = [_quality_inputs(row) for row in rows]
    cohorts = {key: sorted(quality[key] for quality in qualities if quality[key] is not None)
               for key, _ in _METRICS}
    for quality in qualities:
        if quality["cashFlowMissing"]:
            quality.update(score=0, returnScore=0)
            continue
        high_debt = quality["debtToEquity"] is not None and quality["debtToEquity"] >= 1
        weights = (5.8333333333 if high_debt else 15.8333333333, 15.8333333333,
                   15.8333333333, 22.5, 20, 20 if high_debt else 10)
        weighted = present_weight = 0.0
        for (key, lower_better), weight in zip(_METRICS, weights):
            value, cohort = quality[key], cohorts[key]
            if value is None:
                continue
            rank = 1 if len(cohort) < 2 else (bisect_right(cohort, value) - 1) / (len(cohort) - 1)
            percentile = 100 * (1 - rank if lower_better else rank)
            weighted += weight * percentile
            present_weight += weight
        base = weighted / present_weight if present_weight else 0
        debt_divisor = 1 + min(quality["debtToEquity"], 1) if quality["debtToEquity"] is not None else 1
        capex_divisor = 1 + quality["capexOcf"] / 100 if quality["capexOcf"] is not None else 1
        quality.update(score=base / debt_divisor / capex_divisor,
                       returnScore=base, highDebtWeight=high_debt)
    return qualities


def project_companies(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return aligned, persistable projections; pass the entire release, never a page."""
    from .company_availability import validate_unavailable_row
    for row in rows:
        validate_unavailable_row(row)
    qualities = return_quality_percentiles(rows)
    projections = []
    for row, quality in zip(rows, qualities):
        price = valuation_price(row)
        ncav = row.get("ncavps")
        usable_ncav = ncav is not None and price is not None and ncav > 0
        dps = _first(row.get("recurring_dividend_per_share"), row.get("dividend_per_share"))
        incorporation = (row.get("incorporation") or "").split("|")
        fields = [row.get(key) for key in ("ticker", "name", "sector", "industry", "exchange", "graham_profile")]
        fields.append(incorporation[1] if len(incorporation) > 1 else None)
        projections.append({
            "mcap": float(row["price"]) * float(row["shares"]) if row.get("price") and row.get("shares") else None,
            "eps10": eps_evidence(row.get("annual_eps"), (row.get("ch13") or {}).get("latest_fy")),
            "unjudged": any(item["status"] not in ("PASS", "FAIL") for item in row.get("criteria", ())),
            "inapplicable": any(item["status"] == "NOT_APPLICABLE" for item in row.get("criteria", ())),
            "netNet": bool(usable_ncav and price <= ncav),
            "pncav": float(price) / float(ncav) if usable_ncav else None,
            "idx": _first(row.get("index_memberships"), []), "pe3": pe3(row),
            "pe": criterion(row, 1).get("value"), "returnQuality": quality,
            "dividendYield": float(dps) / float(price) * 100 if dps is not None and price is not None and price > 0 else None,
            "roic": (row.get("owner_earnings") or {}).get("all_capex_return"),
            "profile": _first(row.get("graham_profile"), "REVIEW"),
            "hay": " ".join(str(value) for value in fields if value).lower(),
        })
    return projections
