"""Populate and refresh the local store.

    python -m screener.sync bootstrap [--limit N]   from the cache already on disk
    python -m screener.sync bulk                    download SEC's 1.4GB companyfacts.zip
    python -m screener.sync daily                   catch up via the daily index
    python -m screener.sync derive                  recompute dashboard-eligible snapshots
    python -m screener.sync derive --all-snapshots  recompute every cached snapshot
    python -m screener.sync events                  material 8-K items from each filing index
    python -m screener.sync cover                   what each filing's cover says the ticker is
    python -m screener.sync inline --cik CIK        retain direct annual structured statements
    python -m screener.sync export                  write dashboard.json
    python -m screener.sync quotes                  refresh every dashboard quote
    python -m screener.sync dera --from 2021q1      dimensioned + extension facts
    python -m screener.sync status

Raw facts are never re-derived from the network when the engine changes — only
when the company actually files something new. A new filing can restate years we
already hold, so the trigger is "has it filed since we fetched", never "do we
have the latest period".
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import json
import os
import sys
import tempfile
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from . import ch13, evidence, pricestats, profiles, store
from . import normalize
from .normalize import PendingFilingFactsError, UnsupportedFilerError, build_snapshot
from .screens.enterprising import (PE_MAX, PRICE_TO_TBV_MAX, STALE_FOR_PRICING_DAYS,
                                   YIELD_IMPLAUSIBLE, evaluate, settled_debt)
from .sources import cover, dera, indexes, ifrs_workbook, inline_xbrl, jpx
from .sources.edinet import EdinetClient, annual_filings
from .sources.edinet_mapper import ADAPTER_KIND as EDINET_ADAPTER, build_edinet_companyfacts
from .sources.edgar import EdgarClient, EdgarError, NoXbrlDataError
from .sources.prices import YahooPriceProvider

BULK_FACTS_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
BULK_SUBMISSIONS_URL = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{qtr}/form.{ymd}.idx"
DASHBOARD_JSON = Path(__file__).parent / "static" / "dashboard.json"
PRICEABLE_LISTINGS = frozenset({"y", "external"})


def _statement_source_namespace(snap) -> str:
    """Source taxonomy for history, independent of one withheld current total."""
    statement_anchor = next(
        (fact for fact in (
            snap.total_assets, snap.total_liabilities,
            snap.current_assets, snap.current_liabilities,
        ) if fact is not None),
        None,
    )
    return (statement_anchor.provenance.tag.partition(":")[0]
            if statement_anchor is not None else "us-gaap")


def _print_progress(message: str, done: int = 0, total: int = 0) -> None:
    print(f"  {message}" if not total else f"  {message} ({done}/{total})", flush=True)


def _facts_path(edgar: EdgarClient, cik: str) -> Path:
    return edgar.cache_dir / f"companyfacts_{cik}.json"


def _derive_cached_worker(task: tuple):
    """Read and derive one cached filer in a process with no database handle."""
    cik, ticker, receipt, cache_dir, *options = task
    assume_absent_zero = bool(options[0]) if options else False
    cache = Path(cache_dir)
    fp = cache / f"companyfacts_{cik}.json"
    if not fp.exists():
        return cik, None
    facts, supplement_state = inline_xbrl.load_retained_supplement(
        cache, cik, json.loads(fp.read_text()),
    )
    bundle = evidence.EvidenceBundle(
        cik=cik,
        ticker=ticker,
        facts=facts,
        dimensioned=dera.load_sidecar(cache, cik),
        receipt=receipt,
        supplement_state=supplement_state,
    )
    return cik, _derive_evidence(
        bundle, assume_absent_zero=assume_absent_zero)


def _source(fact) -> dict | None:
    """Compact provenance for the payload: enough to open the exact filing.

    A summed or derived figure carries each component, because its own filing
    metadata belongs to whichever component was newest and describes none of the
    others."""
    if fact is None:
        return None

    def one(p) -> dict:
        src = {"tag": p.tag, "form": p.form, "accn": p.accession,
               "end": p.period_end.isoformat() if p.period_end else None,
               "filed": p.filed.isoformat() if p.filed else None}
        if p.unit:
            src["unit"] = p.unit
        if p.document:
            src["document"] = p.document
        if p.canonical_tag and p.document:
            src["canonical_tag"] = p.canonical_tag
        # A fact read on a share-class axis is not the one that tag holds without a
        # dimension: BCSS files 1,500,000 weighted shares dimension-free and
        # 10,000,000 for the class its ticker names. Provenance that omits the axis
        # points at a filing where the figure is a different number.
        if p.segments:
            src["segments"] = p.segments
        # The concept carries the caveat the tag cannot: "Dividends (aggregate —
        # may include preferred and noncontrolling)" was built for 203 rows and
        # then dropped here, so none of them ever showed it.
        if "(" in p.concept:
            src["concept"] = p.concept
        return src

    def leaves(p) -> list:
        # a component can itself be a sum; the reader wants the filings, not the
        # intermediate constructions
        if not p.components:
            return []
        out = []
        for child in p.components:
            out.extend(leaves(child) or [child])
        return out

    p = fact.provenance
    src = one(p)
    if (parts := leaves(p)):
        src["components"] = [one(c) for c in parts]
    return src


def _duration_source(fact, unit: str | None = None) -> dict | None:
    """A duration fact whose start is required to identify the exact context."""
    src = _source(fact)
    if src is not None and fact.provenance.period_start is not None:
        src["start"] = fact.provenance.period_start.isoformat()
    if src is not None and unit is not None:
        src.setdefault("unit", unit)
    return src


def _series_mix(series: dict) -> dict | None:
    """Which tag served which years — only when the series switched tags, so the
    reader sees a scope change (ProfitLoss beside NetIncomeLoss) instead of a
    silently uniform-looking history."""
    tags: dict[str, list[int]] = {}
    for year, fact in series.items():
        tags.setdefault(fact.provenance.tag, []).append(year)
    if len(tags) <= 1:
        return None
    return {tag: sorted(years) for tag, years in tags.items()}


def _without_filing(companyfacts: dict, filing: tuple[str, str]) -> dict:
    """A Company Facts view before an incomplete accession appeared.

    SEC normally retains every older fact when a new filing arrives. Removing only
    the pending accession therefore reconstructs the last complete evidence set and
    lets the current engine recompute it, instead of retaining stale arithmetic or
    dropping the company while SEC finishes ingesting the new filing.
    """
    filed, accn = filing
    facts = {}
    for namespace, taxonomy in (companyfacts.get("facts") or {}).items():
        kept_taxonomy = {}
        for tag, tagdata in taxonomy.items():
            units = {}
            for unit, entries in (tagdata.get("units") or {}).items():
                kept = [e for e in entries
                        if (e.get("filed"), e.get("accn", "")) != (filed, accn)]
                if kept:
                    units[unit] = kept
            if units:
                kept_taxonomy[tag] = {**tagdata, "units": units}
        if kept_taxonomy:
            facts[namespace] = kept_taxonomy
    return {**companyfacts, "facts": facts}


def _derive(cik: str, ticker: str, facts: dict, quote=None,
            dimensioned: dict | None = None,
            receipt: dict | None = None, *,
            assume_absent_zero: bool = False) -> tuple[str, dict | None]:
    """Snapshot + screen result, flattened for the dashboard."""
    pending: dict | None = None
    try:
        snap = build_snapshot(ticker, cik, facts, assume_absent_zero=assume_absent_zero,
                              dimensioned=dimensioned, receipt=receipt)
    except PendingFilingFactsError as exc:
        filed, accession = exc.filing
        pending = {
            "kind": "SEC_FACTS_PENDING",
            "filed": filed,
            "accession": accession,
            "note": ("A newer annual filing is indexed, but SEC structured statements "
                     "are not complete yet; calculations use the last complete filing."),
        }
        try:
            snap = build_snapshot(
                ticker, cik, _without_filing(facts, exc.filing),
                assume_absent_zero=assume_absent_zero,
                dimensioned=dimensioned, receipt=receipt,
                foreign_identity_filing=exc.filing)
        except UnsupportedFilerError:
            return "pending_facts", {"data_pending": pending}
        except Exception as fallback_exc:
            return "error", {"error": repr(fallback_exc)[:200], "data_pending": pending}
    except UnsupportedFilerError:
        return "foreign", None
    except Exception as exc:  # a malformed filing must not stop a 4,000-company run
        return "error", {"error": repr(exc)[:200]}
    r = evaluate(snap, quote)
    # A current cross-period balance mismatch can deliberately withhold total
    # assets from the UI. Statement history is independent evidence, so infer its
    # namespace from any surviving core balance fact instead of silently falling
    # back to US-GAAP (which erased BCS's valid IFRS annual ratios).
    source_namespace = _statement_source_namespace(snap)
    if (facts.get("_adapter") or {}).get("statement_basis") == "canonical":
        statement_taxonomy = normalize._with_fiscal_calendar(
            facts.get("facts", {}).get("canonical", {}))
        statement_taxonomy.canonical_adapter = True
    else:
        statement_taxonomy = (
            normalize._ifrs_as_us_gaap(facts.get("facts", {}).get("ifrs-full", {}))
            if source_namespace == "ifrs-full"
            else facts.get("facts", {}).get("us-gaap", {})
        )
        statement_taxonomy = normalize._with_fiscal_calendar(statement_taxonomy)
    statement_taxonomy.reporting_currency = snap.reporting_currency
    statement_taxonomy.currency_adapter = snap.reporting_currency != "USD" or bool(
        getattr(statement_taxonomy, "canonical_adapter", False))
    historical_ratios = normalize.annual_ratios(
        statement_taxonomy, snap.annual_net_income,
        snap.annual_revenue, snap.annual_operating_income,
        annual_eps=snap.annual_eps,
        annual_share_counts=snap.annual_share_counts,
        annual_gross_profit=snap.annual_gross_profit)
    operating_return_history = _operating_return_history(snap)
    conservative_return_history = _operating_return_history(
        snap, snap.conservative_operating_returns)
    for year, values in operating_return_history.items():
        historical_ratios.setdefault(year, {}).update(values)
    latest_operating_return = (
        {key: value for key, value in
         operating_return_history[max(operating_return_history)].items()
         if key != "operating_return_evidence"}
        if operating_return_history else None)
    latest_conservative_return = (
        {key: value for key, value in
         conservative_return_history[max(conservative_return_history)].items()
         if key != "operating_return_evidence"}
        if conservative_return_history else None)
    # An estimate is useful only when it replaces at least one strict blank. A
    # separately named payload field prevents the UI (or a downstream consumer)
    # from confusing this lower bound with a filing-exact operating return.
    estimated_metrics = ("nopat_roic", "ronta")
    fills_strict_gap = bool(
        latest_conservative_return
        and latest_conservative_return.get("nopat", 0) > 0
        and latest_conservative_return.get("operating_return_assumptions")
        and all(latest_conservative_return.get(metric, 0) > 0
                for metric in estimated_metrics)
        and any(
            latest_conservative_return.get(metric, 0) > 0
            and (latest_operating_return or {}).get(metric) is None
            for metric in estimated_metrics
        ))
    if fills_strict_gap:
        latest_conservative_return["status"] = "CONSERVATIVE_LOWER_BOUND"
        latest_conservative_return["note"] = (
            "Discovery-only lower bound: only absent optional deductions were "
            "bounded at zero; exact reported returns and Graham verdicts are unchanged."
        )
    else:
        latest_conservative_return = None
    if receipt and receipt.get("ratio"):
        _restate_historical_ratios(historical_ratios, Decimal(str(receipt["ratio"])))
    settled_debt_value = settled_debt(snap)[0]
    lease_adjusted_debt = (
        settled_debt_value + snap.operating_lease_liability.value
        if settled_debt_value is not None and snap.operating_lease_liability is not None
        else None)
    asset_quality, asset_sources = _asset_quality(snap, statement_taxonomy)
    row = {
        "cik": cik,
        "ticker": ticker,
        "verdict": r.verdict.value,
        "n_pass": sum(1 for c in r.criteria if c.status.value == "PASS"),
        "ttm_eps": float(snap.ttm_eps) if snap.ttm_eps is not None else None,
        # What the trailing figure actually is. The composite is FY + YTD - prior
        # YTD, but where a quarter is missing or contradicts, the engine falls back
        # to the audited year and said nothing: the panel labelled a figure eight
        # months old "latest 12 months", and a P/E built on it looked current.
        "ttm_basis": _ttm_basis(snap),
        "ttm_eps_vintage": {d: float(v) for d, v in snap.ttm_eps_vintage.items()},
        "balance_sheet_date": snap.balance_sheet_date.isoformat() if snap.balance_sheet_date else None,
        "annual_eps": {str(y): float(v) for y, v in r.annual_eps_series.items()},
        "annual_net_income": {str(y): float(f.value)
                              for y, f in sorted(snap.annual_net_income.items())},
        # The weighted denominator reported in the filing. The panel formerly
        # inferred this from total net income / EPS, which is not valid when the
        # EPS numerator has a narrower scope (ZWS continuing operations, LP units).
        "annual_weighted_shares": {str(y): float(f.value)
                                   for y, f in sorted(snap.annual_share_counts.items())},
        # Retained as engine evidence: EPS nets preferred dividends while the
        # income tag generally does not.
        "annual_preferred_dividends": {str(y): float(v) for y, v in
                                       sorted(snap.annual_preferred_dividends.items())} or None,
        "ttm_net_income": float(snap.ttm_net_income) if snap.ttm_net_income is not None else None,
        "assumptions": list(r.assumptions),
        "earnings_quality": list(snap.earnings_quality),
        "context_notes": list(snap.context_notes),
        "tax_record": snap.tax_record,
        # why the engine withheld the price criteria, when it did: apply_price must
        # not settle a criterion that was refused for a reason a price cannot fix
        "basis_conflict": snap.basis_conflict,
        # reported beside the verdict, never inside it — see models.EpsGrowth
        "eps_growth": {
            "base_fiscal_year": r.eps_growth.base_fiscal_year,
            "base_eps": float(r.eps_growth.base_eps),
            "latest_fiscal_year": r.eps_growth.latest_fiscal_year,
            "latest_eps": float(r.eps_growth.latest_eps),
        } if r.eps_growth else None,
        "criteria": [
            {"n": c.criterion, "status": c.status.value,
             "value": float(c.value) if c.value is not None else None,
             "note": c.note}
            for c in r.criteria
        ],
        # kept so criteria 1 and 7 (and market cap) can be recomputed against a live
        # price without refetching anything
        "tbvps": _tbvps(snap),
        "bvps": _bvps(snap),
        "ncavps": _ncavps(snap),
        "asset_quality": asset_quality,
        # chapter-13 comparison material; dollar figures repeated here so the UI
        # can show working capital and capitalization without a second request
        "annual_revenue": {str(y): float(f.value)
                           for y, f in sorted(snap.annual_revenue.items())},
        "annual_gross_profit": {str(y): float(f.value)
                                for y, f in sorted(snap.annual_gross_profit.items())},
        "ttm_revenue": float(snap.ttm_revenue) if snap.ttm_revenue is not None else None,
        "annual_operating_income": {str(y): float(f.value)
                                    for y, f in sorted(snap.annual_operating_income.items())},
        "dividend_record": snap.dividend_record,
        "ch13": ch13.eps_stats({y: f.value for y, f in snap.annual_eps.items()}),
        # profitability: never a criterion, the same way ROIC is not
        "profitability": _profitability(snap, historical_ratios),
        "debt_to_equity": _debt_to_equity(snap),
        # the same ratios at each of the last fiscal year ends, each struck on its
        # own year's report. The price multiples are completed at export, where the
        # price history lives; the vintage EPS series is their denominator.
        "annual_ratios": historical_ratios,
        "operating_returns": latest_operating_return,
        "operating_returns_estimate": latest_conservative_return,
        "current_assets": float(snap.current_assets.value) if snap.current_assets else None,
        "current_liabilities": (float(snap.current_liabilities.value)
                                if snap.current_liabilities else None),
        "long_term_debt": (float(snap.long_term_debt.value) if snap.long_term_debt
                           else 0.0 if "debt" in snap.assumed_zero else None),
        "total_debt": (float(snap.total_debt.value) if snap.total_debt
                       else 0.0 if "debt" in snap.assumed_zero else None),
        "operating_lease_liability": (
            float(snap.operating_lease_liability.value)
            if snap.operating_lease_liability else None),
        "lease_adjusted_debt": (
            float(lease_adjusted_debt) if lease_adjusted_debt is not None else None),
        "lease_cost": float(snap.lease_cost.value) if snap.lease_cost else None,
        "fixed_charge_coverage": (
            float(snap.fixed_charge_coverage.value)
            if snap.fixed_charge_coverage else None),
        # Employee options as a share of the count they will dilute. Absent for the
        # filers that grant restricted stock instead, and absent is not zero.
        "options": (float(snap.options_outstanding.value)
                    if snap.options_outstanding else None),
        "rsus": float(snap.rsus_outstanding.value) if snap.rsus_outstanding else None,
        **_equity_awards(snap),
        # What criterion 3 actually weighed, rollup and parts already reconciled.
        # The panel adds it to the market value of the common to price the whole
        # enterprise, and None here means unknown rather than debt-free.
        "debt": float(settled_debt_value) if settled_debt_value is not None else None,
        "total_assets": float(snap.total_assets.value) if snap.total_assets else None,
        "total_liabilities": (float(snap.total_liabilities.value)
                              if snap.total_liabilities else None),
        "preferred_stock": float(snap.preferred_stock.value) if snap.preferred_stock else None,
        "earnings_asof": max((f.provenance.period_end for f in snap.ttm_eps_inputs
                              if f.provenance.period_end), default=None) and
                         max(f.provenance.period_end for f in snap.ttm_eps_inputs
                             if f.provenance.period_end).isoformat(),
        "shares": float(snap.shares_outstanding.value) if snap.shares_outstanding else None,
        # what the cover of a named filing says this ticker is, when it was read
        "receipt": receipt,
        # the cover page's own count, kept only so a depositary ratio can be seen:
        # a receipt count on the cover beside an ordinary count in the statements
        "cover_shares": float(snap.cover_shares.value) if snap.cover_shares else None,
        "dividend_per_share": float(snap.dividend_per_share)
                              if snap.dividend_per_share is not None else None,
        "recurring_dividend_per_share": (
            float(snap.recurring_dividend_per_share.value)
            if snap.recurring_dividend_per_share is not None else None),
        "owner_earnings": _owner_earnings_row(snap),
        "short_term_debt": (float(snap.short_term_debt.value) if snap.short_term_debt
                            else 0.0 if {"debt", "short_term_debt"}
                            & snap.assumed_zero else None),
        "goodwill": float(snap.goodwill.value) if snap.goodwill else None,
        "intangibles": float(snap.intangibles.value) if snap.intangibles else None,
        "noncontrolling_interest": (float(snap.noncontrolling_interest.value)
                                    if snap.noncontrolling_interest else None),
        "temporary_equity": (float(snap.temporary_equity.value)
                             if snap.temporary_equity else None),
        # the payment fact behind criterion 5 — covers whatever span the filer
        # tagged (its period end sits in sources.dividend)
        "dividend": float(snap.dividend.value) if snap.dividend else None,
        # per-figure provenance: which tag, in which filing, dated when — the
        # reader can open the exact document behind every number
        "sources": {name: src for name, src in (
            # the two headline flows, which had no provenance at all: a reader
            # comparing the panel against a filing needs to know WHICH concept the
            # profit is — Occidental and Rhinebeck tag only income available to the
            # common, which their statements never print beside the consolidated line
            ("net_income", _source(_newest(snap.annual_net_income))),
            ("revenue", _source(_newest(snap.annual_revenue))),
            ("gross_profit", _source(_newest(snap.annual_gross_profit))),
            ("operating_income", _source(_newest(snap.annual_operating_income))),
            ("total_assets", _source(snap.total_assets)),
            ("total_liabilities", _source(snap.total_liabilities)),
            ("current_assets", _source(snap.current_assets)),
            ("current_liabilities", _source(snap.current_liabilities)),
            ("long_term_debt", _source(snap.long_term_debt)),
            ("short_term_debt", _source(snap.short_term_debt)),
            ("total_debt", _source(snap.total_debt)),
            ("operating_lease_liability", _source(snap.operating_lease_liability)),
            ("lease_cost", _duration_source(snap.lease_cost, snap.reporting_currency)),
            ("fixed_charge_coverage", _source(snap.fixed_charge_coverage)),
            *((name, _source(fact)) for name, fact in asset_sources.items()),
            ("options", _source(snap.options_outstanding)),
            ("rsus", _source(snap.rsus_outstanding)),
            ("goodwill", _source(snap.goodwill)),
            ("intangibles", _source(snap.intangibles)),
            ("preferred_stock", _source(snap.preferred_stock)),
            ("temporary_equity", _source(snap.temporary_equity)),
            ("noncontrolling_interest", _source(snap.noncontrolling_interest)),
            ("shares", _source(snap.shares_outstanding)),
            ("weighted_shares", _source(_newest(snap.annual_share_counts))),
            ("dividend", _source(snap.dividend)),
            ("recurring_dividend_per_share",
             _duration_source(
                 snap.recurring_dividend_per_share,
                 f"{snap.reporting_currency}/shares")),
            # the newest annual earnings figure: which element stated it, and in
            # which filing — a restatement changes both
            ("eps", _source(snap.annual_eps[max(snap.annual_eps)]) if snap.annual_eps else None),
        ) if src is not None},
        # scope-switch disclosure: annual series stitched from more than one tag
        "series_mix": {name: mix for name, mix in (
            ("eps", _series_mix(snap.annual_eps)),
            ("weighted_shares", _series_mix(snap.annual_share_counts)),
            ("net_income", _series_mix(snap.annual_net_income)),
            ("revenue", _series_mix(snap.annual_revenue)),
            ("gross_profit", _series_mix(snap.annual_gross_profit)),
        ) if mix is not None} or None,
    }
    adapter = facts.get("_adapter") or {}
    if snap.reporting_currency != "USD":
        row.update(
            currency=snap.reporting_currency,
            reporting_currency=snap.reporting_currency,
            quote_currency=(adapter.get("quote_currency", snap.reporting_currency)
                            if adapter.get("statement_basis") == "canonical"
                            else "USD"),
        )
    if adapter.get("statement_basis") == "canonical":
        row.update(
            currency=snap.reporting_currency,
            reporting_currency=snap.reporting_currency,
            quote_currency=adapter.get("quote_currency", snap.reporting_currency),
            data_source=adapter.get("kind"),
            security_basis=adapter.get("security_basis"),
            source_reports=adapter.get("reports"),
        )
    if pending is not None:
        row["data_pending"] = pending
    return ("pending_facts" if pending is not None else "ok"), row


def _derive_evidence(bundle: evidence.EvidenceBundle, quote=None, *,
                     assume_absent_zero: bool = False) -> tuple[str, dict | None]:
    """The sole production entry point from assembled evidence to a snapshot.

    Stored dashboard rows remain filing-strict for every Graham criterion.  They
    also carry a compact, separately labelled all-missing-as-zero calculation for
    the Return Quality discovery column, matching the detail panel's default
    assumption mode without allowing those assumptions into a verdict.
    """
    result = _derive(
        bundle.cik, bundle.ticker, bundle.facts, quote=quote,
        dimensioned=bundle.dimensioned, receipt=bundle.receipt,
        assume_absent_zero=assume_absent_zero,
    )
    status, row = result
    if assume_absent_zero or row is None or status not in {"ok", "pending_facts"}:
        return result

    assumed_status, assumed_row = _derive(
        bundle.cik, bundle.ticker, bundle.facts, quote=quote,
        dimensioned=bundle.dimensioned, receipt=bundle.receipt,
        assume_absent_zero=True,
    )
    if assumed_row is not None and assumed_status in {"ok", "pending_facts"}:
        row["return_quality_assumption"] = _return_quality_assumption(assumed_row)
    else:
        row["return_quality_assumption"] = {
            "status": "UNAVAILABLE",
            "applied": [],
            "note": "The zero-assumption Return Quality calculation was unavailable.",
        }
    return status, row


def _return_quality_assumption(row: dict) -> dict:
    """Compact assumption-mode inputs for client-side Return Quality ranking."""
    returns = row.get("operating_returns") or {}
    operating_assumptions = sorted(set(
        returns.get("operating_return_assumptions") or ()))
    capital_assumptions = sorted(
        {"debt", "short_term_debt"} & set(row.get("assumptions") or ()))
    applied = sorted(set(operating_assumptions) | set(capital_assumptions))
    return {
        "status": "APPLIED",
        "roe": (row.get("profitability") or {}).get("on_equity"),
        "nopat_roic": returns.get("nopat_roic"),
        "ronta": returns.get("ronta"),
        "debt_to_equity": row.get("debt_to_equity"),
        "applied": applied,
        "input_assumptions": {
            "roe": [],
            "nopat_roic": operating_assumptions,
            "ronta": operating_assumptions,
            "debt_to_equity": capital_assumptions,
        },
        "note": (
            "Return Quality uses the same explicit zero-assumption convention as "
            "the company detail. These values are discovery estimates, not lower "
            "bounds, and do not change Graham criteria or verdicts."
        ),
    }


def _restate_historical_ratios(ratios: dict, receipt_ratio: Decimal) -> None:
    """Put historical per-share book figures onto the same receipt as its prices.

    Percentages and entity-level totals do not move. The conversion is valid for
    any positive ratio: some receipts represent a fraction of one ordinary share.
    """
    if receipt_ratio <= 0 or receipt_ratio == 1:
        return
    for values in ratios.values():
        for key in ("bvps", "tbvps", "ncavps"):
            if values.get(key) is not None:
                values[key] *= float(receipt_ratio)


# A margin needs a base worth taking a percentage of, and both figures must belong
# to one fiscal year — the trailing composites for income and revenue can close on
# different dates, and a ratio across two windows is not a margin.
_MARGIN_FLOOR = Decimal("1000000")     # revenue below this makes the percentage noise
_RETURN_ON_BOOK_LAG = 800              # a year's earnings over a balance sheet this much newer is not a return


def _profitability(snap, historical_ratios: dict | None = None) -> dict | None:
    """Profit against sales, book, and Finkle's net tangible assets.

    Chapter 13 compares four companies on the first two — the margin says how much
    of each dollar of sales the business keeps, and the return on book value says
    what the shareholders' own capital earns. Neither decides anything here: the six
    criteria are cheapness, stability and solvency, and a company earning two cents
    on the dollar is not thereby disqualified. But it is a different business from
    one earning twenty, and the screen was showing neither figure.

    Return on book is deliberately absent where a fiscal year's earnings would have
    to be divided by a balance sheet from a different era — the defect the audit
    found in ROIC, where Johnson & Johnson's 2014 flows were being divided by a 2026
    balance sheet and shown as a current return.
    """
    revenue, income = snap.annual_revenue, snap.annual_net_income
    operating = snap.annual_operating_income
    years = sorted(y for y in set(revenue) & set(income)
                   if revenue[y].value >= _MARGIN_FLOOR)
    if not years:
        return None
    latest = years[-1]
    def pct(numerator, year):
        return (float(numerator[year].value / revenue[year].value * 100)
                if year in numerator else None)
    series = {y: round(float(income[y].value / revenue[y].value * 100), 2) for y in years[-10:]}

    equity = _common_equity(snap)
    tangible = _net_tangible_assets(snap)
    year_end = income[latest].provenance.period_end
    stale = (snap.balance_sheet_date is None or year_end is None
             or (snap.balance_sheet_date - year_end).days > _RETURN_ON_BOOK_LAG)
    on_book = (round(float(income[latest].value / equity * 100), 2)
               if equity and equity > 0 and not stale else None)
    on_net_tangible_assets = (
        round(float(income[latest].value / tangible * 100), 2)
        if tangible and tangible > 0 and not stale else None)
    historical = (historical_ratios or {}).get(latest, {})
    gross_margin = historical.get("gross_margin")
    on_equity = historical.get("return_on_equity")
    average_equity = historical.get("average_common_equity")
    return {
        "fiscal_year": latest,
        "net": round(pct(income, latest), 2),
        "gross": round(gross_margin, 2) if gross_margin is not None else None,
        "operating": round(pct(operating, latest), 2) if latest in operating else None,
        "on_book": on_book,
        "on_equity": round(on_equity, 2) if on_equity is not None else None,
        "on_net_tangible_assets": on_net_tangible_assets,
        "revenue": float(revenue[latest].value),
        "net_income": float(income[latest].value),
        "book_value": float(equity) if equity else None,
        "average_common_equity": average_equity,
        "net_tangible_assets": float(tangible) if tangible is not None else None,
        # the direction matters more than the level: Graham's warning is a margin
        # that erodes while the earnings still look adequate
        "by_year": series,
    }


def _common_equity(snap):
    """What the common shareholders own — the same deductions every per-share figure
    on this page makes: preferred, the minority's share, and mezzanine."""
    if snap.total_assets is None or snap.total_liabilities is None:
        return None
    other = sum(f.value for f in (snap.preferred_stock, snap.noncontrolling_interest,
                                  snap.temporary_equity) if f)
    return snap.total_assets.value - snap.total_liabilities.value - other


def _net_tangible_assets(snap):
    """Common equity left after goodwill and other intangibles are removed.

    This is the denominator in Todd Finkle's return-on-net-tangible-assets
    measure and the numerator behind this screener's tangible book per share.
    Missing intangible evidence stays missing rather than being assumed zero.
    """
    equity = _common_equity(snap)
    if equity is None or snap.goodwill is None or snap.intangibles is None:
        return None
    return equity - snap.goodwill.value - snap.intangibles.value


def _debt_to_equity(snap) -> float | None:
    """Reconciled current plus noncurrent debt / common shareholders' equity."""
    equity = _common_equity(snap)
    debt = settled_debt(snap)[0]
    if (debt is None or equity is None or equity <= 0
            or debt < 0
            or (snap.total_assets is not None
                and debt > snap.total_assets.value)):
        return None
    return round(float(debt / equity), 4)


def _ttm_basis(snap) -> str:
    """The period behind the trailing EPS, named. One annual fact standing alone is
    the audited year, not a trailing twelve months."""
    inputs = snap.ttm_eps_inputs
    if len(inputs) == 1:
        p = inputs[0].provenance
        if p.period_start and p.period_end and (p.period_end - p.period_start).days > 300:
            return f"fiscal year to {p.period_end.isoformat()}"
    return "latest 12 months"


def _asset_quality(snap, gaap: dict) -> tuple[dict, dict]:
    """Current asset-protection diagnostics with exact balance-sheet evidence.

    Haircut-adjusted NCAV is deliberately absent: a liquidation haircut is an
    investor assumption, not a filed fact. These ratios keep the reported book
    and NCAV lenses factual and expose the composition that can weaken them.
    """
    end = snap.balance_sheet_date
    inventory = normalize._at_period_end(
        gaap, "Inventory", normalize.INVENTORY_TAGS, end)
    receivables = normalize._at_period_end(
        gaap, "Receivables", normalize.RECEIVABLE_TAGS, end)
    cash = normalize._at_period_end(
        gaap, "Cash", normalize.CASH_TAGS, end)
    investments = normalize._at_period_end(
        gaap, "ShortTermInvestments", normalize.SHORT_TERM_INVESTMENT_TAGS, end)
    common_equity = _common_equity(snap)
    ncav = (
        Decimal(str(_ncavps(snap))) * snap.shares_outstanding.value
        if _ncavps(snap) is not None and snap.shares_outstanding is not None
        else None)

    def pct(value: Decimal | None, base: Decimal | None) -> float | None:
        return (float(value / base * 100)
                if value is not None and base is not None and base > 0 else None)

    settled = settled_debt(snap)[0]
    # The combined cash/restricted-cash tag does not prove deployable cash by
    # itself. Net cash is withheld unless the plain cash tag and investments are
    # both filed at the exact balance-sheet date.
    plain_cash = cash if cash is not None and normalize._tag_of(
        cash) == "CashAndCashEquivalentsAtCarryingValue" else None
    net_cash = (
        plain_cash.value + investments.value - settled
        if plain_cash is not None and investments is not None and settled is not None
        else None)
    goodwill = snap.goodwill.value if snap.goodwill is not None else None
    intangibles = snap.intangibles.value if snap.intangibles is not None else None
    values = {
        "common_equity": float(common_equity) if common_equity is not None else None,
        "goodwill_to_common_equity": pct(goodwill, common_equity),
        "intangibles_to_common_equity": pct(intangibles, common_equity),
        "goodwill_and_intangibles_to_common_equity": (
            pct(goodwill + intangibles, common_equity)
            if goodwill is not None and intangibles is not None else None),
        "inventory": float(inventory.value) if inventory is not None else None,
        "inventory_to_ncav": pct(inventory.value if inventory else None, ncav),
        "receivables": float(receivables.value) if receivables is not None else None,
        "receivables_to_ncav": pct(receivables.value if receivables else None, ncav),
        "cash": float(plain_cash.value) if plain_cash is not None else None,
        "short_term_investments": (
            float(investments.value) if investments is not None else None),
        "net_cash": float(net_cash) if net_cash is not None else None,
        "market_cap_to_net_cash": None,
    }
    sources = {
        name: fact for name, fact in (
            ("inventory", inventory),
            ("receivables", receivables),
            ("cash", plain_cash),
            ("short_term_investments", investments),
        ) if fact is not None
    }
    return {key: value for key, value in values.items() if value is not None}, sources


def _fcf_reconciliation(snap, oe) -> dict:
    """Finkle's three FCF equations, independently assembled where evidence permits.

    Methods two and three are algebraic rearrangements. Method one comes from the
    cash-flow statement, so agreement with it is the useful cross-statement check.
    A mismatch is disclosed; no value is adjusted to make the equations agree.
    """
    year = oe.fiscal_year
    target = oe.all_capex_floor.provenance

    def aligned(fact):
        return (fact if fact is not None
                and fact.provenance.period_start == target.period_start
                and fact.provenance.period_end == target.period_end else None)

    cash_flow = aligned(oe.free_cash_flow)
    revenue = aligned(snap.annual_revenue.get(year))
    operating_income = aligned(snap.annual_operating_income.get(year))
    cash_taxes = aligned(oe.cash_taxes_paid)
    beginning, ending = oe.invested_capital_beginning, oe.invested_capital_ending
    net_investment = (ending.value - beginning.value
                      if beginning is not None and ending is not None else None)

    methods = {
        "cash_flow_statement": {
            "label": "CFO − capital expenditure",
            "formula": "Cash flow from operating activities − capital expenditures",
            "value": float(cash_flow.value) if cash_flow is not None else None,
            "components": ([[label, float(value)]
                            for label, value in oe.free_cash_flow_components]
                           if cash_flow is not None else []),
        },
        "nopat_less_investment": {
            "label": "Operating profit after cash taxes − net operating-capital investment",
            "formula": "Operating income − cash taxes paid − net investment in operating capital",
            "value": None,
            "components": [],
        },
        "revenue_less_costs_and_investment": {
            "label": "Revenue − operating costs/taxes − operating-capital investment",
            "formula": "Revenue − operating costs − cash taxes paid − net investment in operating capital",
            "value": None,
            "components": [],
        },
    }
    if operating_income is not None and cash_taxes is not None and net_investment is not None:
        value = operating_income.value - cash_taxes.value - net_investment
        methods["nopat_less_investment"].update(
            value=float(value),
            components=[
                ["operating income", float(operating_income.value)],
                ["- cash taxes paid", float(-cash_taxes.value)],
                ["- net investment in operating capital", float(-net_investment)],
            ],
        )
    if (revenue is not None and operating_income is not None
            and cash_taxes is not None and net_investment is not None):
        operating_costs = revenue.value - operating_income.value
        value = revenue.value - operating_costs - cash_taxes.value - net_investment
        methods["revenue_less_costs_and_investment"].update(
            value=float(value),
            components=[
                ["revenue", float(revenue.value)],
                ["- operating costs", float(-operating_costs)],
                ["- cash taxes paid", float(-cash_taxes.value)],
                ["- net investment in operating capital", float(-net_investment)],
            ],
        )

    values = [method["value"] for method in methods.values() if method["value"] is not None]
    complete = len(values) == 3
    spread = max(values) - min(values) if complete else None
    scale = max((abs(value) for value in values), default=0.0)
    tolerance = max(scale * 0.01, 1.0) if complete else None
    status = ("MATCH" if complete and spread <= tolerance
              else "MISMATCH" if complete else "INCOMPLETE")
    missing = []
    if cash_flow is None:
        missing.append("same-period operating cash flow and cash capital expenditure")
    if revenue is None:
        missing.append("same-period revenue")
    if operating_income is None:
        missing.append("same-period operating income")
    if cash_taxes is None:
        missing.append("same-period cash taxes paid")
    if net_investment is None:
        missing.append("exact beginning and ending operating capital")
    return {
        "fiscal_year": year,
        "status": status,
        "methods": methods,
        "spread": spread,
        "tolerance": tolerance,
        "net_investment_in_operating_capital": (
            float(net_investment) if net_investment is not None else None),
        "missing": missing,
    }


def _operating_return_history(snap, returns=None) -> dict[int, dict]:
    """Serialize the independent ten-year NOPAT/ROIC/RONTA record.

    Dollar inputs and endpoint denominators remain beside the percentages so
    audits can recompute every result. Compact source objects retain the filing,
    accession, period, and any leaf components behind a reconciled subtotal.
    """
    pass_through = bool((snap.tax_record or {}).get("pass_through"))

    def number(value, *, percent: bool = False):
        if value is None:
            return None
        result = float(value * 100 if percent else value)
        return round(result, 4) if percent else result

    def source_point(provenance) -> list:
        # [tag, form, accession, period start, period end]. Arrays keep ten years
        # of repeated provenance practical in the one-shot dashboard payload.
        return [
            provenance.tag,
            provenance.form,
            provenance.accession,
            (provenance.period_start.isoformat()
             if provenance.period_start else None),
            provenance.period_end.isoformat() if provenance.period_end else None,
        ]

    def source_leaves(provenance) -> list:
        if not provenance.components:
            return [source_point(provenance)]
        out = []
        for child in provenance.components:
            out.extend(source_leaves(child))
        return out

    def fact_source(fact) -> list | None:
        if fact is None:
            return None
        return source_leaves(fact.provenance)

    def capital_point(fact) -> dict | None:
        if fact is None:
            return None
        return {
            "value": float(fact.value),
            "end": (fact.provenance.period_end.isoformat()
                    if fact.provenance.period_end else None),
            "formula": fact.provenance.tag,
            "sources": source_leaves(fact.provenance),
        }

    out: dict[int, dict] = {}
    returns = snap.annual_operating_returns if returns is None else returns
    for year, item in sorted(returns.items()):
        caveats = list(item.caveats)
        if pass_through:
            caveats.append(
                "the filing record indicates pass-through or persistently untaxed "
                "profits; the strict screen withholds corporate NOPAT, while the "
                "explicit detail assumption mode displays the zero-tax calculation"
                if item.assumption_mode else
                "the filing record indicates pass-through or persistently untaxed "
                "profits, so a corporate NOPAT tax normalization is not applicable")
        numeric_allowed = not pass_through or item.assumption_mode
        nopat_value = item.nopat.value if item.nopat is not None else None

        def undefined_return(value, denominator, label):
            if not item.assumption_mode or value is not None or nopat_value is None:
                return None
            if denominator is not None and denominator <= 0:
                return (f"{label} is not mathematically measurable: NOPAT is "
                        f"{nopat_value} but the assumed/reported denominator is "
                        f"{denominator}, not greater than 0")
            if denominator is None:
                return (f"{label} is not mathematically measurable after all absent "
                        "inputs were replaced by 0 because the resulting denominator "
                        "is still invalid; see the calculation warnings")
            return None

        sources = {key: value for key, value in {
            "operating_income": fact_source(item.operating_income),
            "tax_expense": [fact_source(fact)
                            for fact in item.tax_expense_inputs],
            "pretax_income": [fact_source(fact)
                               for fact in item.pretax_income_inputs],
            "invested_capital_beginning": capital_point(
                item.invested_capital_beginning),
            "invested_capital_ending": capital_point(
                item.invested_capital_ending),
            "capital_including_cash_beginning": capital_point(
                item.capital_including_cash_beginning),
            "capital_including_cash_ending": capital_point(
                item.capital_including_cash_ending),
            "net_tangible_operating_assets_beginning": capital_point(
                item.net_tangible_operating_assets_beginning),
            "net_tangible_operating_assets_ending": capital_point(
                item.net_tangible_operating_assets_ending),
            "lease_neutral_net_tangible_operating_assets_beginning": capital_point(
                item.lease_neutral_net_tangible_operating_assets_beginning),
            "lease_neutral_net_tangible_operating_assets_ending": capital_point(
                item.lease_neutral_net_tangible_operating_assets_ending),
            "nopat_formula": (item.nopat.provenance.tag
                              if item.nopat is not None else None),
        }.items() if value not in (None, [], ())}
        cell = {
            "operating_income_for_nopat": (
                float(item.operating_income.value)
                if item.operating_income is not None else None),
            "normalized_tax_rate": (
                number(item.normalized_tax_rate, percent=True)
                if numeric_allowed else None),
            "nopat": (number(item.nopat.value)
                      if numeric_allowed and item.nopat is not None else None),
            "invested_capital": number(item.invested_capital),
            "capital_including_cash": number(item.capital_including_cash),
            "average_net_tangible_operating_assets": number(
                item.average_net_tangible_operating_assets),
            "average_lease_neutral_net_tangible_operating_assets": number(
                item.average_lease_neutral_net_tangible_operating_assets),
            "nopat_roic": (number(item.nopat_roic)
                           if numeric_allowed else None),
            "nopat_return_including_cash": (
                number(item.nopat_return_including_cash)
                if numeric_allowed else None),
            "ronta": (number(item.ronta) if numeric_allowed else None),
            "lease_neutral_ronta": (
                number(item.lease_neutral_ronta) if numeric_allowed else None),
            "nopat_roic_undefined": undefined_return(
                item.nopat_roic, item.invested_capital, "NOPAT ROIC"),
            "nopat_return_including_cash_undefined": undefined_return(
                item.nopat_return_including_cash, item.capital_including_cash,
                "NOPAT return including cash"),
            "ronta_undefined": undefined_return(
                item.ronta, item.average_net_tangible_operating_assets, "RONTA"),
            "lease_neutral_ronta_undefined": undefined_return(
                item.lease_neutral_ronta,
                item.average_lease_neutral_net_tangible_operating_assets,
                "Lease-neutral RONTA"),
            "operating_return_assumptions": list(item.assumed_zero),
            "operating_return_caveats": list(dict.fromkeys(caveats)),
            "operating_return_evidence": sources,
        }
        if item.conservative_estimate:
            cell["conservative_estimate"] = True
        out[year] = {
            key: value for key, value in cell.items()
            if value not in (None, [], ())
        }
    return out


def _strip_detail_only_evidence(row: dict) -> None:
    """Keep the one-shot universe payload small; detail rebuilds retain evidence.

    The browser automatically requests the per-company evidence row when a panel
    opens. Repeating six endpoint provenance trees across every company and year
    in ``dashboard.json`` roughly doubles an already large universe download.
    Values, assumptions, and exact missing reasons remain in the static payload.
    """
    for cell in (row.get("annual_ratios") or {}).values():
        if isinstance(cell, dict):
            cell.pop("operating_return_evidence", None)
    estimate = row.get("operating_returns_estimate")
    if isinstance(estimate, dict):
        estimate.pop("operating_return_evidence", None)


def _owner_earnings_row(snap) -> dict | None:
    """Serialize owner-earnings evidence without inventing maintenance capex.

    The legacy definitive fields stay present as null so an old client cannot silently
    relabel the all-capex proxy as Buffett owner earnings. The named estimates and FCF
    are the only numeric fields consumers may use.
    """
    oe = snap.owner_earnings
    if oe is None:
        return None

    def source_row(provenance) -> dict:
        source = {
            "tag": provenance.tag,
            "form": provenance.form,
            "accn": provenance.accession,
            "end": (provenance.period_end.isoformat()
                    if provenance.period_end else None),
            "filed": provenance.filed.isoformat() if provenance.filed else None,
        }
        if provenance.unit:
            source["unit"] = provenance.unit
        if provenance.document:
            source["document"] = provenance.document
        return source

    def fact_value(fact) -> float | None:
        return float(fact.value) if fact is not None else None

    def capital_point(fact) -> dict | None:
        if fact is None:
            return None
        return {
            "value": float(fact.value),
            "end": (fact.provenance.period_end.isoformat()
                    if fact.provenance.period_end else None),
            "formula": fact.provenance.tag,
            "sources": [source_row(source)
                        for source in fact.provenance.components],
        }
    # Keep the latest eleven fiscal-year slots in the payload. The panel displays
    # ten; the oldest is calculation support for the three-year averaged start
    # of its ten-slot CAGR. Sparse evidence stays sparse instead of reaching
    # farther into the past to fill the quota.
    years = [year for year in sorted(oe.annual)
             if oe.fiscal_year - 10 <= year <= oe.fiscal_year]
    floor_sources = oe.all_capex_floor.provenance.components
    owner_sources = {}
    for name, provenance in zip(
        ("reported_earnings", "depreciation_and_amortisation", "total_capex"),
        floor_sources,
    ):
        owner_sources[name] = source_row(provenance)
    if oe.free_cash_flow is not None and oe.free_cash_flow.provenance.components:
        provenance = oe.free_cash_flow.provenance.components[0]
        owner_sources["operating_cash_flow"] = source_row(provenance)
    for name, fact in (
        ("stock_compensation", oe.stock_compensation),
        ("cash_acquisitions", oe.cash_acquisitions),
        ("capitalized_intangible_investment", oe.capitalized_intangible_investment),
        ("working_capital_cash_effect", oe.working_capital_cash_effect),
        ("cash_taxes_paid", oe.cash_taxes_paid),
        ("revenue", snap.annual_revenue.get(oe.fiscal_year)),
        ("operating_income", snap.annual_operating_income.get(oe.fiscal_year)),
    ):
        if fact is not None:
            # A reported fact has one filing location. The NIKE operating-income
            # fallback is a reconciled subtotal, so retain every filed component
            # instead of reducing its provenance to the newest component alone.
            owner_sources[name] = (_source(fact) if name == "operating_income"
                                   else source_row(fact.provenance))

    def annual_cell(year: int) -> dict:
        item = oe.annual[year]
        shares = (item.diluted_shares.value
                  if item.diluted_shares is not None else None)

        def per_share(fact) -> float | None:
            return (float(fact.value / shares)
                    if fact is not None and shares is not None and shares > 0 else None)

        def bridge_point(fact, *, absolute: bool = False) -> list | None:
            if fact is None:
                return None
            value = abs(fact.value) if absolute else fact.value
            source = source_row(fact.provenance)
            # This structure occurs hundreds of thousands of times in the UI
            # payload. Keep the required provenance without repeating JSON keys:
            # [value, tag, form, accession, period end].
            point = [float(value), source.get("tag"), source.get("form"),
                     source.get("accn"), source.get("end")]
            if source.get("unit") or source.get("document"):
                point.extend([source.get("unit"), source.get("document")])
            return point

        cell = {
            # Compatibility names are retained while the clearer display names
            # keep consumers from treating either proxy as definitive owner earnings.
            "all_capex_floor": float(item.all_capex_floor.value),
            "maintenance_estimate": float(item.maintenance_estimate.value),
            "earnings_after_total_capex": float(item.all_capex_floor.value),
            "reported_earnings_assumption": float(item.maintenance_estimate.value),
            "free_cash_flow": fact_value(item.free_cash_flow),
            "free_cash_flow_after_stock_compensation": fact_value(
                item.free_cash_flow_after_stock_compensation),
            "free_cash_flow_after_acquisitions": fact_value(
                item.free_cash_flow_after_acquisitions),
            "expanded_free_cash_flow": fact_value(item.expanded_free_cash_flow),
            "all_capex_floor_per_share": fact_value(item.all_capex_floor_per_share),
            "maintenance_estimate_per_share": fact_value(
                item.maintenance_estimate_per_share),
            "earnings_after_total_capex_per_share": fact_value(
                item.all_capex_floor_per_share),
            "reported_earnings_assumption_per_share": fact_value(
                item.maintenance_estimate_per_share),
            "free_cash_flow_per_share": fact_value(item.free_cash_flow_per_share),
            "free_cash_flow_after_stock_compensation_per_share": fact_value(
                item.free_cash_flow_after_stock_compensation_per_share),
            "free_cash_flow_after_acquisitions_per_share": fact_value(
                item.free_cash_flow_after_acquisitions_per_share),
            "expanded_free_cash_flow_per_share": fact_value(
                item.expanded_free_cash_flow_per_share),
            "stock_compensation_per_share": per_share(item.stock_compensation),
            "cash_acquisitions_per_share": per_share(item.cash_acquisitions),
            "capitalized_intangible_investment_per_share": per_share(
                item.capitalized_intangible_investment),
            "working_capital_cash_effect_per_share": per_share(
                item.working_capital_cash_effect),
            "operating_cash_flow_before_working_capital_per_share": per_share(
                item.operating_cash_flow_before_working_capital),
            # Direct filing facts and audited derivations for the transposed
            # ten-year cash bridge. Each direct figure owns compact provenance;
            # the two derived values reconcile from the direct rows beside them.
            "cash_flow_bridge": {key: value for key, value in {
                "net_income": bridge_point(item.reported_earnings),
                "depreciation_and_amortisation": bridge_point(
                    item.depreciation_and_amortisation),
                "stock_compensation": bridge_point(
                    item.stock_compensation, absolute=True),
                "working_capital_cash_effect": bridge_point(
                    item.working_capital_cash_effect),
                "operating_cash_flow": bridge_point(item.operating_cash_flow),
                "total_capital_expenditure": bridge_point(
                    item.total_capital_expenditure, absolute=True),
                # Both are derived from direct rows already carrying provenance.
                "other_operating_cash_flow_adjustments": fact_value(
                    item.other_operating_cash_flow_adjustments),
                "free_cash_flow": fact_value(item.free_cash_flow),
                # A financing use shown after FCF; it does not change FCF.
                "share_repurchases": bridge_point(
                    item.share_repurchases, absolute=True),
            }.items() if value is not None},
            "diluted_shares": float(shares) if shares is not None else None,
            "end": (item.maintenance_estimate.provenance.period_end.isoformat()
                    if item.maintenance_estimate.provenance.period_end
                    else None),
        }
        # Preserve the original three per-share keys even when FCF is unavailable;
        # new supplemental fields are omitted when missing so sparse evidence does
        # not inflate every company-year with a page of nulls.
        always = {
            "all_capex_floor", "maintenance_estimate",
            "earnings_after_total_capex", "reported_earnings_assumption",
            "all_capex_floor_per_share", "maintenance_estimate_per_share",
            "earnings_after_total_capex_per_share",
            "reported_earnings_assumption_per_share", "free_cash_flow_per_share",
            "diluted_shares", "end",
        }
        return {key: value for key, value in cell.items()
                if value is not None or key in always}

    return {
        "fiscal_year": oe.fiscal_year,
        "status": "ESTIMATE_ONLY",
        "owner_earnings": None,
        "maintenance_capex": None,
        "maintenance_basis": "UNAVAILABLE_PRIMARY_XBRL",
        "all_capex_label": "Earnings after total capital expenditure",
        "maintenance_estimate_label": (
            "Reported earnings — maintenance capex assumed equal to D&A"),
        "all_capex_floor": float(oe.all_capex_floor.value),
        "maintenance_estimate": float(oe.maintenance_estimate.value),
        "free_cash_flow": fact_value(oe.free_cash_flow),
        "fcf_reconciliation": _fcf_reconciliation(snap, oe),
        "free_cash_flow_after_stock_compensation": fact_value(
            oe.free_cash_flow_after_stock_compensation),
        "free_cash_flow_after_acquisitions": fact_value(
            oe.free_cash_flow_after_acquisitions),
        "expanded_free_cash_flow": fact_value(oe.expanded_free_cash_flow),
        "stock_compensation": fact_value(oe.stock_compensation),
        "cash_acquisitions": fact_value(oe.cash_acquisitions),
        "capitalized_intangible_investment": fact_value(
            oe.capitalized_intangible_investment),
        "working_capital_cash_effect": fact_value(oe.working_capital_cash_effect),
        "operating_cash_flow_before_working_capital": fact_value(
            oe.operating_cash_flow_before_working_capital),
        "average_working_capital_cash_effect_3y": (
            float(oe.average_working_capital_cash_effect_3y)
            if oe.average_working_capital_cash_effect_3y is not None else None),
        "stock_compensation_to_revenue": (
            float(oe.stock_compensation_to_revenue)
            if oe.stock_compensation_to_revenue is not None else None),
        "stock_compensation_to_free_cash_flow": (
            float(oe.stock_compensation_to_free_cash_flow)
            if oe.stock_compensation_to_free_cash_flow is not None else None),
        "acquisitions_to_free_cash_flow": (
            float(oe.acquisitions_to_free_cash_flow)
            if oe.acquisitions_to_free_cash_flow is not None else None),
        "acquisition_years_10": oe.acquisition_years_10,
        "acquisitions_to_capex_10": (
            float(oe.acquisitions_to_capex_10)
            if oe.acquisitions_to_capex_10 is not None else None),
        "invested_capital": float(oe.invested_capital) if oe.invested_capital is not None else None,
        "invested_capital_basis": "AVERAGE_BEGINNING_END_EXCLUDING_CASH_AND_SHORT_TERM_INVESTMENTS",
        "invested_capital_evidence": {
            "beginning": capital_point(oe.invested_capital_beginning),
            "ending": capital_point(oe.invested_capital_ending),
        },
        "capital_including_cash": (
            float(oe.capital_including_cash)
            if oe.capital_including_cash is not None else None),
        "capital_including_cash_evidence": {
            "beginning": capital_point(oe.capital_including_cash_beginning),
            "ending": capital_point(oe.capital_including_cash_ending),
        },
        "roic": None,
        "all_capex_return": (float(oe.all_capex_return)
                             if oe.all_capex_return is not None else None),
        "maintenance_estimate_return": (
            float(oe.maintenance_estimate_return)
            if oe.maintenance_estimate_return is not None else None),
        "all_capex_return_including_cash": (
            float(oe.all_capex_return_including_cash)
            if oe.all_capex_return_including_cash is not None else None),
        "maintenance_estimate_return_including_cash": (
            float(oe.maintenance_estimate_return_including_cash)
            if oe.maintenance_estimate_return_including_cash is not None else None),
        "normalized_tax_rate": (
            float(oe.normalized_tax_rate * 100)
            if oe.normalized_tax_rate is not None
            and not (snap.tax_record or {}).get("pass_through") else None),
        "nopat": (float(oe.nopat) if oe.nopat is not None
                  and not (snap.tax_record or {}).get("pass_through") else None),
        "nopat_roic": (float(oe.nopat_roic) if oe.nopat_roic is not None
                       and not (snap.tax_record or {}).get("pass_through") else None),
        "nopat_return_including_cash": (
            float(oe.nopat_return_including_cash)
            if oe.nopat_return_including_cash is not None
            and not (snap.tax_record or {}).get("pass_through") else None),
        "average_net_tangible_operating_assets": (
            float(oe.average_net_tangible_operating_assets)
            if oe.average_net_tangible_operating_assets is not None else None),
        "net_tangible_operating_assets_basis": (
            "AVERAGE_BEGINNING_END_ASSETS_LESS_GOODWILL_INTANGIBLES_CASH_"
            "SHORT_AND_NONCURRENT_INVESTMENTS_AND_NIB_CURRENT_LIABILITIES_"
            "WITH_REPORTED_CURRENT_OPERATING_LEASE_LIABILITY_AS_FINANCING"),
        "net_tangible_operating_assets_evidence": {
            "beginning": capital_point(oe.net_tangible_operating_assets_beginning),
            "ending": capital_point(oe.net_tangible_operating_assets_ending),
        },
        "ronta": (float(oe.ronta) if oe.ronta is not None
                  and not (snap.tax_record or {}).get("pass_through") else None),
        "average_lease_neutral_net_tangible_operating_assets": (
            float(oe.average_lease_neutral_net_tangible_operating_assets)
            if oe.average_lease_neutral_net_tangible_operating_assets is not None
            else None),
        "lease_neutral_net_tangible_operating_assets_basis": (
            "RONTA_NTOA_LESS_REPORTED_OPERATING_LEASE_ROU_ASSETS;_"
            "PRE_RECOGNITION_YEARS_UNCHANGED;_OFF_BALANCE_LEASES_NOT_REBUILT"),
        "lease_neutral_net_tangible_operating_assets_evidence": {
            "beginning": capital_point(
                oe.lease_neutral_net_tangible_operating_assets_beginning),
            "ending": capital_point(
                oe.lease_neutral_net_tangible_operating_assets_ending),
        },
        "lease_neutral_ronta": (
            float(oe.lease_neutral_ronta)
            if oe.lease_neutral_ronta is not None
            and not (snap.tax_record or {}).get("pass_through") else None),
        "components": [[label, float(v)] for label, v in oe.components],
        "free_cash_flow_components": [
            [label, float(value)] for label, value in oe.free_cash_flow_components],
        # One compact source per filed input. The three derived measures share
        # these inputs; repeating their full provenance trees inflated the UI
        # payload by tens of megabytes without adding evidence.
        "sources": owner_sources,
        "caveats": list(oe.caveats),
        # Ten displayed fiscal years plus one older CAGR-support year, all on
        # today's split and traded-security basis. A missing year is omitted
        # rather than imputed; the UI withholds an incomplete averaged basis.
        "annual_per_share": {str(year): annual_cell(year) for year in years},
    }


def _ncavps(snap) -> float | None:
    """Net current asset value per share — Graham's most conservative yardstick:
    current assets less every liability, ignoring fixed assets entirely, divided by
    shares. Distinct from the net current assets in criterion 3, which subtracts
    only current liabilities."""
    need = (snap.current_assets, snap.total_liabilities, snap.shares_outstanding)
    if any(f is None for f in need) or snap.shares_outstanding.value <= 0:
        return None
    # mezzanine equity is senior to the common the same way preferred is, and
    # tangible book already deducts it; leaving it in here would credit the
    # common with assets it stands behind
    optional = sum(f.value for f in (snap.preferred_stock, snap.noncontrolling_interest,
                                     snap.temporary_equity) if f)
    return float((snap.current_assets.value - snap.total_liabilities.value - optional)
                 / snap.shares_outstanding.value)


def _bvps(snap) -> float | None:
    """Plain book value per share — intangibles included, unlike criterion 7's
    tangible variant, because chapter 13's P/B and earnings-on-book use the full
    equity. Preferred and minority interest are deducted the same way _tbvps does."""
    need = (snap.total_assets, snap.total_liabilities, snap.shares_outstanding)
    if any(f is None for f in need) or snap.shares_outstanding.value <= 0:
        return None
    optional = sum(f.value for f in (snap.preferred_stock, snap.noncontrolling_interest,
                                     snap.temporary_equity) if f)
    book = snap.total_assets.value - snap.total_liabilities.value - optional
    return float(book / snap.shares_outstanding.value)


def _tbvps(snap) -> float | None:
    tangible = _net_tangible_assets(snap)
    if (tangible is None or snap.shares_outstanding is None
            or snap.shares_outstanding.value <= 0):
        return None
    return float(tangible / snap.shares_outstanding.value)


def _index_tickers(conn, edgar: EdgarClient) -> dict[str, tuple[str, str]]:
    """CIK -> (ticker, name) from SEC's mapping; refreshed with the ticker file."""
    store.migrate(conn)
    mapping = edgar._cached("company_tickers", "https://www.sec.gov/files/company_tickers.json")
    candidates: dict[str, list[tuple[str, str]]] = {}
    for row in mapping.values():
        cik = f"{int(row['cik_str']):010d}"
        candidates.setdefault(cik, []).append((row["ticker"], row["title"]))
    covers = store.covers_by_cik(conn)
    existing = {
        row["cik"]: row["ticker"]
        for row in conn.execute("SELECT cik, ticker FROM company WHERE ticker IS NOT NULL")
    }
    out = {}
    for cik, choices in candidates.items():
        # SEC lists every live symbol for a CIK and commonly puts a SPAC unit
        # before its common share. The screener prices common equity, so an exact
        # current cover match outranks JSON order. LPAAU is a unit containing a
        # warrant; LPAA is the Class A ordinary share whose earnings/book we own.
        cover_by_symbol = {
            security["symbol"]: security for security in covers.get(cik, ())
        }
        supported = {
            symbol
            for symbol, security in cover_by_symbol.items()
            if cover.is_common_equity_security(security.get("title") or "")
            and not cover.is_untraded_underlying(security.get("title") or "")
        }
        current = existing.get(cik)
        # Preserve an established live SEC symbol. A partial or historically
        # malformed cover must not move UNM to a listed debt symbol, or a saved
        # common row to a warrant/preferred ticker. Cover rank resolves only a
        # newly encountered multi-symbol CIK; explicit security cleanup remains a
        # separate audited operation.
        keep_current = current in {choice[0] for choice in choices}
        out[cik] = (
            next(choice for choice in choices if choice[0] == current)
            if keep_current else
            next((choice for choice in choices if choice[0] in supported), choices[0])
        )
    for cik, (ticker, name) in out.items():
        store.upsert_company(conn, cik, ticker, name)
    # ...and take the symbol back from whoever used to hold it
    store.resolve_ticker_conflicts(conn, out)
    conn.commit()
    return out


def bootstrap(conn, limit: int | None = None, progress=_print_progress) -> None:
    """Derive from whatever raw facts are already cached locally — no network."""
    edgar = EdgarClient()
    tickers = _index_tickers(conn, edgar)
    loader = evidence.EvidenceLoader(conn, edgar)
    cached = sorted(edgar.cache_dir.glob("companyfacts_*.json"))
    if limit:
        cached = cached[:limit]
    progress(f"deriving from {len(cached)} cached companies", 0, len(cached))
    for i, fp in enumerate(cached, 1):
        cik = fp.stem.replace("companyfacts_", "")
        ticker, name = tickers.get(cik, (None, None))
        try:
            facts = json.loads(fp.read_text())
        except ValueError:
            continue
        status, data = _derive_evidence(loader.load(cik, ticker, facts))
        store.upsert_company(conn, cik, ticker, name, facts_synced=True)
        store.put_snapshot(conn, cik, status, data)
        if i % 100 == 0:
            conn.commit()
            progress("deriving cached companies", i, len(cached))
    conn.commit()
    progress("done")


def bulk(conn, limit: int | None = None, progress=_print_progress) -> None:
    """One 1.4GB download instead of thousands of rate-limited requests."""
    edgar = EdgarClient()
    tickers = _index_tickers(conn, edgar)
    loader = evidence.EvidenceLoader(conn, edgar)
    progress("downloading companyfacts.zip from SEC (1.4 GB)")
    with httpx.stream("GET", BULK_FACTS_URL, headers={"User-Agent": edgar.user_agent},
                      timeout=None, follow_redirects=True) as resp:
        resp.raise_for_status()
        buf = io.BytesIO()
        for chunk in resp.iter_bytes(1 << 20):
            buf.write(chunk)
            if buf.tell() % (100 << 20) < (1 << 20):
                progress(f"downloading — {buf.tell() / 1e9:.2f} GB of ~1.4 GB")
    progress("extracting archive")
    with zipfile.ZipFile(buf) as z:
        names = [n for n in z.namelist() if n.startswith("CIK") and n.endswith(".json")]
        if limit:
            names = names[:limit]
        for i, n in enumerate(names, 1):
            cik = n[3:-5]
            ticker, name = tickers.get(cik, (None, None))
            try:
                facts = json.loads(z.read(n))
            except ValueError:
                continue
            (edgar.cache_dir / f"companyfacts_{cik}.json").write_bytes(z.read(n))
            status, data = _derive_evidence(loader.load(cik, ticker, facts))
            store.upsert_company(conn, cik, ticker, name, facts_synced=True)
            store.put_snapshot(conn, cik, status, data)
            if i % 500 == 0:
                conn.commit()
                progress("deriving companies", i, len(names))
    conn.commit()
    progress("done")


def metadata(conn, progress=_print_progress) -> None:
    """Sector, exchange and filer size from SEC's submissions archive.

    Yahoo has this too, but rate-limits after roughly one lookup; SEC serves the
    whole market in a single archive and it is the authoritative record anyway.
    """
    edgar = EdgarClient()
    store.migrate(conn)
    progress("downloading submissions.zip from SEC (1.6 GB)")
    with httpx.stream("GET", BULK_SUBMISSIONS_URL, headers={"User-Agent": edgar.user_agent},
                      timeout=None, follow_redirects=True) as resp:
        resp.raise_for_status()
        buf = io.BytesIO()
        for chunk in resp.iter_bytes(1 << 20):
            buf.write(chunk)
            if buf.tell() % (100 << 20) < (1 << 20):
                progress(f"downloading — {buf.tell() / 1e9:.2f} GB of ~1.6 GB")
    progress("reading company metadata")
    with zipfile.ZipFile(buf) as z:
        # skip the -submissions-001 shards: the base file carries the header fields
        names = [n for n in z.namelist()
                 if n.startswith("CIK") and n.endswith(".json") and "submissions" not in n]
        for i, n in enumerate(names, 1):
            try:
                d = json.loads(z.read(n))
            except ValueError:
                continue
            cik = n[3:-5]
            tickers, exchanges = d.get("tickers") or [], d.get("exchanges") or []
            store.set_metadata(
                conn, cik,
                sic=d.get("sic") or None,
                industry=d.get("sicDescription") or None,
                exchange=exchanges[0] if exchanges else None,
                filer_size=d.get("category") or None,
                ticker=tickers[0] if tickers else None,
                name=d.get("name") or None,
            )
            filings = d.get("filings") or {}
            first = [f["filingFrom"] for f in (filings.get("files") or []) if f.get("filingFrom")]
            recent_dates = (filings.get("recent") or {}).get("filingDate") or []
            if recent_dates:
                first.append(min(recent_dates))
            if first:
                store.set_first_filed(conn, cik, min(first))
            if i % 2000 == 0:
                conn.commit()
                progress("reading company metadata", i, len(names))
    conn.commit()
    progress("done", len(names), len(names))


def _dera_tags() -> tuple[frozenset[str], frozenset[str]]:
    """The concepts worth carrying over from the quarterly datasets, and which of
    them are per-share. Both come from the chains themselves, so the two sources
    can never drift apart."""
    per_share = frozenset(
        normalize.EPS_TAGS + normalize.EPS_BASIC_TAGS + (normalize.EPS_CONTINUING_TAG,)
        + tuple(tag for tag, unit in normalize.DIVIDEND_TAGS if "USD/shares" in unit)
        + tuple(normalize.IFRS_PER_SHARE_TAGS)
    )
    wanted = frozenset(
        tuple(per_share)
        + tuple(normalize._WEIGHTED_SHARE_TAGS)
        + normalize.NET_INCOME_TAGS + normalize.REVENUE_TAGS
        + normalize.OPERATING_INCOME_TAGS + normalize.PRETAX_TAGS
        + normalize.DA_EXTENSION_TAGS
        + tuple(tag for tag, _ in normalize.DIVIDEND_TAGS)
        + tuple(normalize.IFRS_SOURCE_TAGS)
        + ("Assets", "Liabilities", "AssetsCurrent", "LiabilitiesCurrent",
           "CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding",
           "Goodwill", "IntangibleAssetsNetExcludingGoodwill")
        # Preferred counts live on a share-class axis, so Company Facts returns
        # nothing for them and a convertible preferred cannot be told from one
        # that has already converted. Here the axis survives.
        + normalize.PREFERRED_COUNT_TAGS
    )
    return wanted, per_share


def dera_sync(conn, start: str | None = None, progress=_print_progress) -> None:
    """Carry the dimension-qualified and issuer-extension facts that Company
    Facts cannot express into a sidecar cache beside the raw filings.

    Changed sidecars invalidate their companies' snapshots. The next derive or
    export reads them through the same evidence bundle as every other path."""
    edgar = EdgarClient()
    cache = edgar.cache_dir
    ciks = {r["cik"] for r in conn.execute(
        "SELECT cik FROM company WHERE ticker IS NOT NULL").fetchall()}
    wanted, per_share = _dera_tags()
    first = dera.parse_quarter(start) if start else dera.Quarter(date.today().year - 2, 1)
    quarters = dera.quarters_through(first, dera.latest_published(date.today()))
    progress(f"{len(quarters)} quarters to read for {len(ciks)} companies")
    for i, quarter in enumerate(quarters, 1):
        path = dera.download(quarter, cache, edgar.user_agent)
        if path is None:
            progress(f"{quarter} is not published yet", i, len(quarters))
            continue
        harvested = dera.harvest(path, ciks, wanted, per_share)
        changed = dera.merge_into_sidecars(harvested, cache, quarter)
        for cik in changed:
            store.mark_snapshot_dirty(conn, cik, f"DERA evidence updated through {quarter}")
        conn.commit()
        progress(f"{quarter}: {len(changed)} companies changed", i, len(quarters))
    progress("done", len(quarters), len(quarters))


def listing_age(conn, progress=_print_progress) -> None:
    """First-ever SEC filing date, for companies whose EPS record starts after
    2011. The windowed defensive tests need this corroboration: a record that
    begins late can mean a young company (LEVI, 2019) or an old company whose
    tag is young (ARCC's BDC per-share element starts 2020, the company 2004) —
    XBRL alone cannot tell the two apart."""
    edgar = EdgarClient()
    store.migrate(conn)
    todo = []
    for row in store.dashboard_rows(conn):
        if row.get("first_filed"):
            continue
        years = [int(y) for y in (row.get("annual_eps") or {})]
        if years and min(years) > 2011:
            todo.append(row["cik"])
    progress(f"{len(todo)} companies need a listing age")
    for i, cik in enumerate(todo, 1):
        try:
            d = edgar.submissions(cik)
        except (EdgarError, NoXbrlDataError):
            continue
        filings = d.get("filings") or {}
        dates = [f["filingFrom"] for f in (filings.get("files") or []) if f.get("filingFrom")]
        recent = (filings.get("recent") or {}).get("filingDate") or []
        if recent:
            dates.append(min(recent))
        if dates:
            store.set_first_filed(conn, cik, min(dates))
        if i % 100 == 0:
            conn.commit()
            progress("listing ages", i, len(todo))
    conn.commit()
    progress("done", len(todo), len(todo))


def material_events(submissions: dict) -> tuple[list[dict], str | None]:
    """Material 8-K items in a submissions index, and the oldest filing it shows.

    Which items count is `profiles.EVENT_ITEMS`, beside the notes they become —
    an item is stored only where something is prepared to say what it means.

    The index holds the filer's most recent thousand filings. For nearly every
    company that is its whole history, but a prolific one buries years under
    Form 4s — Wells Fargo's thousand reach back fourteen months — so the scan
    reports the date it could see back to. A window is only claimable when the
    data covers it.
    """
    recent = (submissions.get("filings") or {}).get("recent") or {}
    dates = recent.get("filingDate") or []
    events = []
    for form, filed, codes, accn in zip(recent.get("form") or [], dates,
                                        recent.get("items") or [],
                                        recent.get("accessionNumber") or []):
        if not form.startswith("8-K"):
            continue
        for item in (codes or "").split(","):
            if (item := item.strip()) in profiles.EVENT_ITEMS:
                events.append({"filed": filed, "item": item, "accn": accn})
    return events, min(dates) if dates else None


def events(conn, progress=_print_progress) -> None:
    """What each company's own filing index says happened to it.

    Restatements, bankruptcy, delisting notices, accelerated debt and auditor
    changes are reported as 8-K item numbers, which makes them the only company
    events readable without opening a document. XBRL cannot express any of them:
    a withdrawn financial statement is a statement about facts, not a fact.
    """
    edgar = EdgarClient()
    store.migrate(conn)
    ciks = store.dashboard_ciks(conn)
    progress(f"scanning filing indexes for {len(ciks)} companies", 0, len(ciks))

    def scan(cik: str):
        """Fetch and read one index inside the worker: an index is megabytes and
        what it yields is a handful of dates, so only the dates travel back."""
        try:
            d = edgar.submissions(cik)
            return (cik, *material_events(d), d.get("stateOfIncorporation"),
                    d.get("stateOfIncorporationDescription"))
        except Exception:  # one unreadable index must not stop a 6,000-company scan
            return cik, None, None, None, None

    found = done = 0
    # the SEC's ten-per-second cap is enforced inside the client, so a small pool
    # spends the wait on network latency instead of adding to it
    with ThreadPoolExecutor(max_workers=8) as pool:
        for cik, seen, scanned_from, inc, inc_name in pool.map(scan, ciks):
            done += 1
            if seen is None:
                continue
            store.set_events(conn, cik, seen, scanned_from)
            store.set_incorporation(conn, cik, inc, inc_name)
            found += len(seen)
            if done % 200 == 0:
                conn.commit()
                progress(f"scanning filing indexes — {found} events", done, len(ciks))
    conn.commit()
    progress("done", len(ciks), len(ciks))


def _current_supported_annual(facts: dict) -> tuple[str, str] | None:
    """Newest foreign annual accession whose cover should settle its security.

    Cover identity is useful even while numeric Company Facts lag the filing, so
    the newest represented 20-F/40-F is returned before its statement basis is
    complete. Normalization independently refuses an incoherent statement.
    """
    namespaces = (facts.get("facts") or {})
    newest, _ = normalize._current_supported_foreign_annual(namespaces)
    if newest is None or not newest[1]:
        return None
    return newest[1], newest[0]


def _cover_accession(row: dict) -> str | None:
    """Newest annual cover the security-identity cache must represent.

    A usable fallback snapshot can carry an older statement while SEC Company
    Facts is still ingesting a new 20-F. Its pending accession nevertheless owns
    today's listed class and ADS ratio, so source provenance from the old
    statement must not make the cover scanner stop one filing early (TM 2026).
    """
    pending = row.get("data_pending") or {}
    if pending.get("accession"):
        return pending["accession"]
    filings = [
        (source.get("filed"), source.get("accn"))
        for source in (row.get("sources") or {}).values()
        if source.get("accn")
    ]
    return max(filings)[1] if filings else None


def _submissions_identity(d: dict) -> tuple[str | None, str | None, str | None, str | None]:
    """Incorporation plus the visible filing-history span from one SEC header."""
    filings = d.get("filings") or {}
    recent = (filings.get("recent") or {}).get("filingDate") or []
    first = [f["filingFrom"] for f in (filings.get("files") or []) if f.get("filingFrom")]
    if recent:
        first.append(min(recent))
    return (d.get("stateOfIncorporation"), d.get("stateOfIncorporationDescription"),
            min(first) if first else None, max(recent) if recent else None)


def _retain_source_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(raw)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


_DIRECT_ANNUAL_FORMS = frozenset({"20-F", "20-F/A", "40-F", "40-F/A"})


def _latest_direct_annual(submissions: dict) -> dict:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []

    def at(field: str, position: int, default=None):
        values = recent.get(field) or []
        return values[position] if position < len(values) else default

    candidates = []
    for position, accession in enumerate(accessions):
        form = at("form", position, "")
        if form not in _DIRECT_ANNUAL_FORMS:
            continue
        candidates.append({
            "accession": accession,
            "form": form,
            "filed": at("filingDate", position, ""),
            "report_date": at("reportDate", position, ""),
            "document": at("primaryDocument", position, ""),
        })
    if not candidates:
        raise ValueError("no current direct 20-F/40-F filing")
    selected = max(candidates, key=lambda row: (row["filed"], row["accession"]))
    if not all(selected.values()):
        raise ValueError("current annual SEC metadata is incomplete")
    return selected


def _one_index_document(names: set[str], suffix: str, label: str) -> str:
    matches = sorted(name for name in names if name.casefold().endswith(suffix.casefold()))
    if len(matches) != 1:
        raise ValueError(f"current annual has {len(matches)} {label} files")
    return matches[0]


def _safe_archive_document(document: str) -> str:
    # The manifest reader applies the same allowlist. Reject before a path is built.
    return inline_xbrl.safe_document_name(document)


def _direct_annual_documents(index: dict, primary_document: str) -> dict[str, str]:
    items = ((index.get("directory") or {}).get("item") or [])
    names = {
        _safe_archive_document(str(item.get("name") or ""))
        for item in items if item.get("name")
    }
    primary_document = _safe_archive_document(primary_document)
    if primary_document not in names:
        raise ValueError("current annual primary document is absent from SEC index")
    try:
        instance = _one_index_document(names, "_htm.xml", "extracted instance")
    except ValueError as exc:
        raise inline_xbrl.UnsupportedInlineXbrlRelationship(
            "incorporated_filing_relationship"
        ) from exc
    summary = "FilingSummary.xml"
    if summary not in names:
        raise ValueError("current annual FilingSummary.xml is absent from SEC index")
    schema = _one_index_document(names, ".xsd", "issuer schema")
    presentations = sorted(name for name in names if name.casefold().endswith("_pre.xml"))
    if len(presentations) > 1:
        raise ValueError("current annual has ambiguous presentation linkbases")
    return {
        "primary_document": primary_document,
        "instance": instance,
        "filing_summary": summary,
        "schema": schema,
        # Some SEC-generated filings have no separate presentation linkbase. The
        # parser accepts the verified schema as an empty-role document in that case.
        "presentation": presentations[0] if presentations else schema,
    }


def _archive_base(cik: str, accession: str) -> str:
    if not cik.isdigit() or not accession.replace("-", "").isdigit():
        raise ValueError("invalid SEC filing identity")
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{int(cik)}/{accession.replace('-', '')}/"
    )


def _retained_record(document: str, url: str, raw: bytes) -> dict:
    return {
        "document": document,
        "url": url,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }


def _verified_previous_bytes(path: Path, record: dict | None) -> bytes | None:
    if not record or not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if (record.get("size") == len(raw)
            and record.get("sha256") == hashlib.sha256(raw).hexdigest()):
        return raw
    return None


def _retain_direct_inline_annual(edgar: EdgarClient, cik: str) -> tuple[str, str]:
    cik = str(int(cik)).zfill(10) if cik.isdigit() else cik
    if not (len(cik) == 10 and cik.isdigit()):
        raise ValueError("SEC issuer identifier must be a numeric CIK")
    submissions = edgar.submissions(cik)
    annual = _latest_direct_annual(submissions)
    accession = annual["accession"]
    base = _archive_base(cik, accession)
    try:
        previous = inline_xbrl.read_current_manifest(edgar.cache_dir, cik)
    except ValueError:
        previous = None
    if (previous and (previous.get("annual") or {}).get("accession") == accession):
        try:
            inline_xbrl.verify_current_manifest(edgar.cache_dir, cik)
        except ValueError:
            pass
        else:
            return "reused", accession

    previous_files = (
        previous.get("files") or {}
        if previous and (previous.get("annual") or {}).get("accession") == accession
        else {}
    )
    directory = inline_xbrl.accession_directory(edgar.cache_dir, accession)
    index_path = directory / "index.json"
    index_record = previous_files.get("index")
    index_raw = _verified_previous_bytes(index_path, index_record)
    if index_raw is None:
        index_raw = edgar._request(base + "index.json").content
        _retain_source_bytes(index_path, index_raw)
    try:
        index = json.loads(index_raw)
    except ValueError as exc:
        raise ValueError("invalid SEC filing index") from exc
    documents = _direct_annual_documents(index, annual["document"])

    records = {"index": _retained_record("index.json", base + "index.json", index_raw)}
    document_payloads: dict[str, bytes] = {}
    for role, document in documents.items():
        path = directory / document
        raw = document_payloads.get(document)
        if raw is None:
            raw = _verified_previous_bytes(path, previous_files.get(role))
            if raw is None:
                raw = edgar._request(base + document).content
                _retain_source_bytes(path, raw)
            document_payloads[document] = raw
        records[role] = _retained_record(document, base + document, raw)

    manifest = {
        "schema": inline_xbrl.MANIFEST_SCHEMA,
        "parser_contract_revision": inline_xbrl.PARSER_CONTRACT_REVISION,
        "relationship": "direct_annual",
        "cik": cik,
        "entity_name": str(submissions.get("name") or ""),
        "annual": annual,
        "source": {
            "accession": accession,
            "form": annual["form"],
            "filed": annual["filed"],
            "document": annual["document"],
        },
        "files": records,
    }
    # Parsing all verified retained inputs is the publication gate. A partial or
    # unsupported filing remains only an inactive accession directory.
    inline_xbrl.verify_manifest(manifest, edgar.cache_dir, cik)
    encoded = inline_xbrl.manifest_bytes(manifest)
    manifest_path = inline_xbrl.current_manifest_path(edgar.cache_dir, cik)
    changed = not manifest_path.exists() or manifest_path.read_bytes() != encoded
    if changed:
        _retain_source_bytes(manifest_path, encoded)
    return ("activated" if changed else "reused"), accession


def retain_inline_statements(
    conn,
    ciks: set[str],
    progress=_print_progress,
    *,
    edgar: EdgarClient | None = None,
) -> dict[str, dict]:
    """Retain verified direct annual statements for an explicit bounded CIK set."""
    if not ciks:
        raise ValueError("inline statement acquisition requires an explicit CIK set")
    edgar = edgar or EdgarClient()
    results = {}
    for done, requested in enumerate(sorted(ciks), 1):
        cik = str(int(requested)).zfill(10) if requested.isdigit() else requested
        try:
            state, accession = _retain_direct_inline_annual(edgar, cik)
        except inline_xbrl.UnsupportedInlineXbrlRelationship as exc:
            results[cik] = {"state": "unsupported_relationship", "relationship": str(exc)}
        except Exception as exc:
            results[cik] = {"state": "error", "error": str(exc)}
        else:
            results[cik] = {"state": state, "accession": accession}
            if state == "activated":
                store.mark_snapshot_dirty(
                    conn, cik, f"retained Inline-XBRL statement activated: {accession}"
                )
        progress(f"retaining Inline-XBRL statements: {results[cik]['state']}", done, len(ciks))
    conn.commit()
    return results


_TICKER_CONTINUITY_FORMS = frozenset({
    "6-K", "6-K/A", "8-K", "8-K/A", "8-A12B", "8-A12B/A",
    "F-3", "F-3/A", "S-3", "S-3/A",
})


def _later_ticker_continuity(
    edgar: EdgarClient,
    cik: str,
    ticker: str,
    exchange: str | None,
    annual_filed: str,
    prior_securities: list[dict],
) -> tuple[dict, str, str, Path] | None:
    """Newest retained later SEC filing that explicitly proves one ticker move."""
    prior_securities = [
        security for security in prior_securities
        if security.get("title")
        and cover.is_common_equity_security(security["title"])
        and not cover.is_untraded_underlying(security["title"])
    ]
    if not prior_securities:
        return None
    try:
        submissions = edgar.submissions(cik)
    except Exception:
        return None
    recent = (submissions.get("filings") or {}).get("recent") or {}
    rows = zip(
        recent.get("filingDate") or [],
        recent.get("form") or [],
        recent.get("accessionNumber") or [],
        recent.get("primaryDocument") or [],
    )
    for filed, form, accn, document_name in rows:
        if filed <= annual_filed or form not in _TICKER_CONTINUITY_FORMS:
            continue
        try:
            path = cover.primary_document_cache_path(
                edgar.cache_dir, accn, document_name
            )
        except ValueError:
            continue
        if path.exists():
            try:
                raw = path.read_bytes()
            except OSError:
                continue
        else:
            try:
                url = (
                    "https://www.sec.gov/Archives/edgar/data/"
                    f"{int(cik)}/{accn.replace('-', '')}/{document_name}"
                )
                _retain_source_bytes(path, edgar._request(url).content)
                raw = path.read_bytes()
            except Exception:
                continue
        security = evidence.continuity_security(
            prior_securities,
            raw.decode("utf-8", errors="replace"),
            ticker,
            exchange,
        )
        if security is not None:
            return security, accn, filed, path
    return None


def cover_pages(conn, progress=_print_progress, *, foreign_only: bool = False,
                ciks: set[str] | None = None, reparse: bool = False) -> None:
    """Read the cover of each company's newest annual filing.

    Two facts there decide what every per-share figure on this dashboard means,
    and no XBRL feed carries either: which share class the ticker prices, and —
    for a depositary receipt — how many ordinary shares one receipt stands for.
    Onconova's cover says "each representing 13 Ordinary Shares" and its market
    capitalisation was thirteen times too large without it.

    Only companies whose figures the answer would change are fetched: a domestic
    filer with one class of common has nothing here that the statements do not
    already say.
    """
    if reparse and ciks is None:
        raise ValueError("cover reparse requires an explicit CIK set")

    edgar = EdgarClient()
    store.migrate(conn)
    # Every company, not only the ones whose data betrays a question. Onconova is
    # the reason: both its share counts are ordinary, its incorporation field is
    # empty, and nothing anywhere in its XBRL hints that the price belongs to a
    # receipt worth thirteen of them. Targeting the detectable cases would have
    # skipped precisely the case this exists for.
    # the NEWEST filing, not the one that happened to supply the earnings: a filer
    # whose per-share element is dimension-only has an EPS accession years old, and
    # Hershey's was a 2015 cover that predates cover-page tagging entirely
    todo = [] if foreign_only else [
        (r["cik"], r["ticker"], _cover_accession(r), False)
        for r in store.dashboard_rows(conn)
        if ciks is None or r["cik"] in ciks
    ]

    # Foreign-form rows are not on the dashboard yet, so they cannot be reached
    # through dashboard_rows(). Read the cached facts just far enough to select
    # current coherent US-GAAP/IFRS 20-F/40-F filers.
    foreign_rows = conn.execute(
        """SELECT c.cik, c.ticker, c.name, c.exchange
           FROM snapshot s JOIN company c USING (cik)
             LEFT JOIN pending_filing p USING (cik)
           WHERE (s.status IN ('foreign', 'pending_facts') OR p.cik IS NOT NULL)
             AND c.listed = 'y' AND c.ticker IS NOT NULL
             AND NOT (c.ticker GLOB '*-P' OR c.ticker GLOB '*-P[A-Z]')
           ORDER BY c.cik"""
    ).fetchall()
    annual_filings: dict[str, tuple[str, str]] = {}
    for row in foreign_rows:
        if ciks is not None and row["cik"] not in ciks:
            continue
        path = _facts_path(edgar, row["cik"])
        if not path.exists():
            continue
        try:
            current = _current_supported_annual(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
        if current:
            accn, filed = current
            annual_filings[row["cik"]] = (accn, filed)
            store.upsert_company(conn, row["cik"], row["ticker"], row["name"],
                                 last_filing=filed)
            todo.append((row["cik"], row["ticker"], accn, True))

    # An unchanged cover is immutable. Do not make another SEC request merely
    # because the derivation engine changed; parser improvements are applied to
    # the preserved title by EvidenceLoader.
    stored_covers = store.covers_by_cik(conn)
    covered = {
        (cik, security["accn"])
        for cik, securities in stored_covers.items()
        for security in securities
    }
    todo = list({
        (cik, ticker, accn): (cik, ticker, accn, foreign)
        for cik, ticker, accn, foreign in todo if accn
    }.values())
    if not reparse:
        todo = [(cik, ticker, accn, foreign) for cik, ticker, accn, foreign in todo
                if (cik, accn) not in covered]
    progress(f"reading cover pages for {len(todo)} companies", 0, len(todo))

    def read(item):
        cik, ticker, accn, foreign = item
        identity = (None, None, None, None)
        primary_document = None
        identity_loaded = False

        def load_identity():
            nonlocal identity, primary_document, identity_loaded
            if not foreign or identity_loaded:
                return
            identity_loaded = True
            try:
                submissions = edgar.submissions(cik)
                identity = _submissions_identity(submissions)
                recent = (submissions.get("filings") or {}).get("recent") or {}
                accessions = recent.get("accessionNumber") or []
                if accn in accessions:
                    primary = recent.get("primaryDocument") or []
                    position = accessions.index(accn)
                    primary_document = primary[position] if position < len(primary) else None
            except Exception:
                pass

        def primary_document_bytes() -> bytes | None:
            primary_dir = cover.primary_document_cache_dir(edgar.cache_dir, accn)
            cached = sorted(path for path in primary_dir.glob("*") if path.is_file())
            if len(cached) == 1:
                return cached[0].read_bytes()
            load_identity()
            if not primary_document:
                return None
            try:
                path = cover.primary_document_cache_path(
                    edgar.cache_dir, accn, primary_document
                )
            except ValueError:
                return None
            if path.exists():
                return path.read_bytes()
            try:
                url = (
                    "https://www.sec.gov/Archives/edgar/data/"
                    f"{int(cik)}/{accn.replace('-', '')}/{primary_document}"
                )
                raw = edgar._request(url).content
                _retain_source_bytes(path, raw)
                return path.read_bytes()
            except Exception:
                return None

        def parsed_result(found, report_raw):
            for security in found:
                security["ratio"] = cover.depositary_ratio(security["title"])
                if security["ratio"] is None:
                    previous = next((
                        row for row in stored_covers.get(cik, ())
                        if row["accn"] == accn
                        and row["symbol"] == security["symbol"]
                        and row.get("ratio") is not None
                    ), None)
                    if previous is not None:
                        security["ratio"] = Decimal(str(previous["ratio"]))
            unresolved = [
                security
                for security in found
                if cover.symbol_matches(
                    str(security.get("symbol") or ""),
                    str(security.get("title") or ""),
                    ticker,
                )
                and cover.is_depositary_security(security["title"])
                and not security.get("ratio")
            ]
            if foreign and len(unresolved) == 1:
                report_ratio = cover.depositary_ratio(
                    cover.text_of(report_raw.decode("utf-8", errors="replace"))
                )
                if report_ratio is not None:
                    unresolved[0]["ratio"] = report_ratio
            if foreign and len(unresolved) == 1 and not unresolved[0].get("ratio"):
                primary_raw = primary_document_bytes()
                if primary_raw is not None:
                    filing_ratio = cover.depositary_ratio(
                        cover.text_of(primary_raw.decode("utf-8", errors="replace"))
                    )
                    if filing_ratio is not None:
                        unresolved[0]["ratio"] = filing_ratio
            return cik, accn, found, identity

        paths = [
            cover.report_cache_path(edgar.cache_dir, accn, n)
            for n in cover.COVER_REPORTS
        ]
        for path in paths:
            if not path.exists():
                continue
            try:
                raw = path.read_bytes()
                found = cover.securities(raw.decode("utf-8", errors="replace"))
            except Exception:
                continue
            if found:
                return parsed_result(found, raw)

        for n, path in zip(cover.COVER_REPORTS, paths):
            if path.exists():
                continue
            try:
                load_identity()
                url = cover.R_URL.format(
                    cik=int(cik), accn=accn.replace("-", ""), n=n
                )
                raw = edgar._request(url).content
                _retain_source_bytes(path, raw)
                found = cover.securities(raw.decode("utf-8", errors="replace"))
            except Exception:
                continue
            if found:
                return parsed_result(found, raw)
        return cik, accn, [], identity

    done = ratios = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for cik, accn, found, identity in pool.map(read, todo):
            done += 1
            inc, inc_name, first_filed, last_filing = identity
            if inc:
                store.set_incorporation(conn, cik, inc, inc_name)
            if first_filed:
                store.set_first_filed(conn, cik, first_filed)
            if last_filing:
                store.upsert_company(conn, cik, None, None, last_filing=last_filing)
            if found:
                annual = annual_filings.get(cik)
                store.set_cover(conn, cik, found, accn, annual[1] if annual else None)
                ratios += sum(1 for s in found if s["ratio"])
            if done % 100 == 0:
                conn.commit()
                progress(f"reading cover pages — {ratios} depositary ratios", done, len(todo))
    conn.commit()

    # A later SEC filing may explicitly move the same registered class to a new
    # ticker. The evidence resolver fails closed unless class, ticker, and
    # exchange all agree; the annual and later filing bytes remain separate.
    current_covers = store.covers_by_cik(conn)
    for row in foreign_rows:
        annual = annual_filings.get(row["cik"])
        if annual is None:
            continue
        prior = current_covers.get(row["cik"], [])
        if any(cover.symbol_matches(
            security.get("symbol") or "", security.get("title") or "", row["ticker"]
        ) for security in prior):
            continue
        proved = _later_ticker_continuity(
            edgar, row["cik"], row["ticker"], row["exchange"], annual[1], prior
        )
        if proved is None:
            continue
        security, accn, filed, _ = proved
        store.set_cover_continuity(conn, row["cik"], security, accn, filed)
    conn.commit()
    progress("done", len(todo), len(todo))


def daily(conn, days: int = 7, progress=_print_progress) -> None:
    """One ~1MB file per day names every company that filed. Refetch only those."""
    edgar = EdgarClient()
    tickers = _index_tickers(conn, edgar)
    loader = evidence.EvidenceLoader(conn, edgar)
    end = store.today()
    last = store.get_state(conn, "last_daily_index")
    start = max(end - timedelta(days=days),
                (store.today() - timedelta(days=days)) if not last else
                __import__("datetime").date.fromisoformat(last) + timedelta(days=1))
    filed: dict[str, str] = {}
    day = start
    while day <= end:
        url = DAILY_INDEX_URL.format(year=day.year, qtr=(day.month - 1) // 3 + 1,
                                     ymd=day.strftime("%Y%m%d"))
        try:
            text = edgar._get_text(url)
        except EdgarError:
            day += timedelta(days=1)
            continue  # weekends and holidays have no index
        for line in text.splitlines():
            if not line.startswith(normalize.FINANCIAL_FORMS):
                continue
            parts = [p for p in line.split("  ") if p.strip()]
            if len(parts) < 3:
                continue
            cik_field = next((p.strip() for p in parts if p.strip().isdigit()), None)
            if cik_field:
                filed[f"{int(cik_field):010d}"] = day.isoformat()
        progress(f"scanning {day} — {len(filed)} filers found")
        store.set_state(conn, "last_daily_index", day.isoformat())
        day += timedelta(days=1)
    for cik, when in filed.items():
        store.upsert_company(conn, cik, *tickers.get(cik, (None, None)), last_filing=when)
    conn.commit()

    pending = store.needs_refetch(conn)
    progress(f"{len(pending)} companies filed since last sync", 0, len(pending))
    for i, cik in enumerate(pending, 1):
        ticker, name = tickers.get(cik, (None, None))
        try:
            facts = edgar.company_facts(cik)
        except NoXbrlDataError:
            store.put_snapshot(conn, cik, "no_xbrl", None)
            store.upsert_company(conn, cik, ticker, name, facts_synced=True)
            continue
        except EdgarError as exc:
            print(f"  {ticker}: {exc}")
            continue
        status, data = _derive_evidence(loader.load(cik, ticker, facts))
        store.upsert_company(conn, cik, ticker, name, facts_synced=True)
        store.put_snapshot(conn, cik, status, data)
        progress("refetching filers", i, len(pending))
        if i % 50 == 0:
            conn.commit()
    conn.commit()
    progress("done")

    # A newly filed 20-F/40-F can change the registered class or its receipt
    # ratio. The stale cover deliberately made the first derivation unsupported;
    # refresh just those new foreign annual filers, then retry from cached facts.
    if filed:
        cover_pages(conn, progress, foreign_only=True, ciks=set(filed))
        derive(conn, progress)


def import_ifrs_workbooks(
    conn, directory: str | Path, *, ticker: str = ifrs_workbook.DEFAULT_TICKER,
    entity_id: str = ifrs_workbook.DEFAULT_ENTITY_ID, progress=_print_progress,
) -> tuple[str, dict | None]:
    """Import and derive the explicitly configured adidas primary listing."""
    edgar = EdgarClient()
    destination = _facts_path(edgar, entity_id)
    progress(f"mapping adidas IFRS workbooks from {Path(directory).resolve()}")
    facts = ifrs_workbook.write_adidas_companyfacts(
        directory, destination, entity_id=entity_id, ticker=ticker)
    facts["_adapter"]["security_basis"] = "PRIMARY_ORDINARY_SHARE"
    # Persist the security declaration added above, atomically, beside the
    # canonical facts. It is configuration provenance, not a workbook-derived
    # financial figure.
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(facts, separators=(",", ":")), encoding="utf-8")
    temporary.replace(destination)
    latest = max(report["published"] for report in facts["_adapter"]["reports"])
    store.upsert_company(
        conn, entity_id, ticker, facts.get("entityName"),
        last_filing=latest, facts_synced=True)
    store.set_metadata(
        conn, entity_id, sic=None, industry="Footwear and sporting goods",
        exchange="XETRA", filer_size=None, ticker=ticker,
        name=facts.get("entityName"))
    conn.execute(
        "UPDATE company SET listed = 'external', incorporation = ? WHERE cik = ?",
        ("DE|Germany", entity_id),
    )
    result = _derive_evidence(evidence.EvidenceBundle(
        cik=entity_id, ticker=ticker, facts=facts,
        dimensioned=None, receipt=None))
    store.put_snapshot(conn, entity_id, *result)
    conn.commit()
    progress(
        f"imported {len(facts['_adapter']['reports'])} reports and derived {ticker} "
        f"as {result[0]}"
    )
    return result


def _merge_edinet_facts(previous: dict | None, incoming: dict) -> dict:
    """Keep earlier filings while later annual reports can restate their years."""
    if not previous or (previous.get("_adapter") or {}).get("kind") != EDINET_ADAPTER:
        return incoming
    if previous.get("cik") != incoming.get("cik"):
        raise ValueError("EDINET company identity changed during import")
    out = previous
    prior_reports = (out.get("_adapter") or {}).get("reports") or []
    new_report = incoming["_adapter"]["reports"][0]
    prior_reports = [report for report in prior_reports
                     if report["document"] != new_report["document"]]
    for tagdata in out.get("facts", {}).get("canonical", {}).values():
        for unit, entries in tagdata.get("units", {}).items():
            tagdata["units"][unit] = [entry for entry in entries
                                      if entry.get("accn") != new_report["document"]]
    for tag, tagdata in incoming["facts"]["canonical"].items():
        target = out.setdefault("facts", {}).setdefault("canonical", {}) \
            .setdefault(tag, {"units": {}})["units"]
        for unit, entries in tagdata["units"].items():
            target.setdefault(unit, []).extend(entries)
    out["_adapter"]["reports"] = sorted(
        [*prior_reports, new_report], key=lambda report: report["published"])
    if new_report["published"] >= out["_adapter"]["reports"][-1]["published"]:
        out["_adapter"]["reporting_currency"] = incoming["_adapter"]["reporting_currency"]
        out["_adapter"]["quote_currency"] = incoming["_adapter"]["quote_currency"]
        out["_adapter"]["ticker"] = incoming["_adapter"]["ticker"]
        out["entityName"] = incoming["entityName"]
    return out


def import_edinet(conn, start: date, end: date, *, client: EdinetClient | None = None,
                  listings: dict | None = None, progress=_print_progress,
                  limit: int | None = None, only_code: str | None = None,
                  cache_dir: Path | None = None) -> int:
    """Import TSE-listed annual EDINET reports into the shared dashboard store."""
    if end < start:
        raise ValueError("EDINET end date precedes start date")
    client = client or EdinetClient()
    listings = listings if listings is not None else jpx.fetch_listed_companies()
    cache = Path(cache_dir) if cache_dir is not None else EdgarClient().cache_dir
    imported = 0
    skipped = 0
    day = start
    while day <= end:
        records = annual_filings(client.documents_on(day.isoformat()))
        for record in records:
            code = record.get("secCode") or ""
            if len(code) != 5 or not code.endswith("0"):
                continue
            local_code = code[:-1]
            if only_code is not None and local_code != only_code:
                continue
            listed = listings.get(local_code)
            if listed is None:
                continue
            ticker = f"{local_code}.T"
            document_id = record["docID"]
            archive_path = cache / "edinet" / f"{document_id}.zip"
            if archive_path.exists():
                archive = archive_path.read_bytes()
            else:
                archive = client.xbrl_archive(document_id)
            try:
                source = build_edinet_companyfacts(record, archive, ticker=ticker)
            except ValueError as exc:
                skipped += 1
                progress(f"EDINET {document_id} skipped: {exc}")
                continue
            entity_id = source["cik"]
            destination = cache / f"companyfacts_{entity_id}.json"
            previous = (json.loads(destination.read_text()) if destination.exists()
                        else None)
            bundle = _merge_edinet_facts(previous, source)
            status, data = _derive_evidence(evidence.EvidenceBundle(
                cik=entity_id, ticker=ticker, facts=bundle,
                dimensioned=None, receipt=None))
            if status != "ok":
                skipped += 1
                progress(f"EDINET {document_id} skipped: derivation status {status}")
                continue
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            if not archive_path.exists():
                archive_path.write_bytes(archive)
            temporary = destination.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(bundle, separators=(",", ":")), encoding="utf-8")
            temporary.replace(destination)
            store.upsert_company(conn, entity_id, ticker, listed["name"],
                                 last_filing=record["submitDateTime"][:10],
                                 facts_synced=True)
            store.set_metadata(conn, entity_id, sic=None,
                               industry=listed["industry"], exchange="TSE",
                               filer_size=None, ticker=ticker, name=listed["name"])
            conn.execute(
                "UPDATE company SET listed = 'external', incorporation = ? WHERE cik = ?",
                ("M0|Japan", entity_id),
            )
            store.put_snapshot(conn, entity_id, status, data)
            conn.commit()
            imported += 1
            progress(f"EDINET {ticker}: {document_id} ({status})")
            if limit is not None and imported >= limit:
                return imported
        day += timedelta(days=1)
    progress(f"imported {imported} EDINET annual reports; skipped {skipped}")
    return imported

def derive(conn, progress=_print_progress, *, all_snapshots: bool = False,
           workers: int = 4) -> None:
    """Recompute snapshots after an engine change from cached raw facts.

    Filing reads and normalization are independent per CIK and CPU-bound, so
    separate processes do that expensive work concurrently. SQLite writes stay on
    this calling process: one writer preserves the existing WAL/commit behavior.
    """
    edgar = EdgarClient()
    tickers = _index_tickers(conn, edgar)
    loader = evidence.EvidenceLoader(conn, edgar)
    stale = store.needs_recompute(conn, eligible_only=not all_snapshots)
    scope = "cached" if all_snapshots else "dashboard-eligible"
    progress(f"{len(stale)} {scope} snapshots predate engine v{store.ENGINE_VERSION}",
             0, len(stale))

    tasks = []
    for cik in stale:
        ticker, name = tickers.get(cik, (None, None))
        ticker, receipt = loader.identity(cik, ticker)
        tasks.append((cik, ticker, receipt, str(edgar.cache_dir)))

    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_derive_cached_worker, task) for task in tasks]
        for i, future in enumerate(as_completed(futures), 1):
            cik, result = future.result()
            if result is not None:
                status, data = result
                store.put_snapshot(conn, cik, status, data)
            if i % 200 == 0:
                conn.commit()
                progress("recomputing snapshots", i, len(stale))
    conn.commit()
    progress("done")

_DORMANT_DAYS = 200   # a filer silent this long has stopped, not merely gone stale


def _span(days: int) -> str:
    """A gap in words. `days // 365` printed "1 years" for everything from 451 to
    723 days, most of which are nearer two."""
    if days < 545:
        return f"{round(days / 30.4)} months"
    years = days / 365
    return f"{years:.1f} years" if years < 10 else f"{round(years)} years"


def _too_stale(asof: str | None, price_asof: str | None) -> int | None:
    """Days between the fundamentals and the price, when that gap is too wide to value."""
    if not asof or not price_asof:
        return None
    gap = (date.fromisoformat(price_asof[:10]) - date.fromisoformat(asof)).days
    return gap if gap > STALE_FOR_PRICING_DAYS else None


def apply_price(row: dict, price: float | None) -> dict:
    """Criteria 1 and 7 are the only price-dependent tests. Snapshots are stored
    price-free (they change only when the company files), so the valuation
    criteria are settled here — pure arithmetic over ttm_eps and tbvps, no I/O.
    Portfolio execution snapshots mirror these two thresholds server-side; the
    browser only displays settled criteria."""
    # A symbol absent from SEC's current company/ticker mapping has no verified
    # security identity.  Yahoo may still return a stale quote, or may later reuse
    # the symbol for another issuer; neither may be joined to this CIK's filings.
    # Rows without a `listed` key are direct/unit-test callers and retain the
    # historical API contract.  Exported rows always carry the key.
    if "listed" in row and row.get("listed") not in PRICEABLE_LISTINGS:
        price = None
        for field in ("price", "price_asof", "price_session", "market_state",
                      "market_timezone", "market_state_asof", "price_source"):
            row.pop(field, None)
    crit = {c["n"]: c for c in row["criteria"]}
    valuation_price = _valuation_price(row, price)
    if (_quote_currency(row) != _reporting_currency(row)
            and valuation_price is not None):
        row["price_reporting_currency"] = float(valuation_price)
    else:
        row.pop("price_reporting_currency", None)
    asset_quality = row.get("asset_quality") or {}
    asset_quality.pop("market_cap_to_net_cash", None)
    if (valuation_price is not None and row.get("shares") is not None
            and asset_quality.get("net_cash") is not None
            and asset_quality["net_cash"] > 0):
        asset_quality["market_cap_to_net_cash"] = (
            float(valuation_price) * row["shares"] / asset_quality["net_cash"])
    if valuation_price is not None and not row.get("basis_conflict"):
        eps, tbvps = row.get("ttm_eps"), row.get("tbvps")
        # Snapshots are intentionally price-free.  Once export has supplied a
        # live price, do not retain a stale "price quote" token in an otherwise
        # incomplete tangible-book explanation.
        for n in (1, 7):
            note = crit[n].get("note")
            if isinstance(note, str) and note.startswith("missing: "):
                missing = [item.strip() for item in note.removeprefix("missing: ").split(",")]
                missing = [item for item in missing if item != "price quote"]
                crit[n]["note"] = "missing: " + ", ".join(missing) if missing else None
        pa = row.get("price_asof")
        # a dormant filer keeps its ticker; valuing today's price against its last
        # figures from years ago produces a confident, meaningless number
        stale_eps = _too_stale(row.get("earnings_asof"), pa)
        stale_bs = _too_stale(row.get("balance_sheet_date"), pa)
        if stale_eps:
            # Whether the company stopped filing is a question the filing index
            # answers, and the store already holds it: Brookfield filed a 10-Q nine
            # days before this price and was being told it had gone quiet. Absent a
            # filing date, the gap alone is stated and nothing is inferred from it.
            filed = row.get("last_filing")
            dormant = not filed or (date.fromisoformat(pa[:10])
                                    - date.fromisoformat(filed[:10])).days > _DORMANT_DAYS
            crit[1].update(status="INSUFFICIENT_DATA", value=None,
                           note=f"newest earnings are {_span(stale_eps)} older than this price"
                                + ("; the company appears to have stopped filing" if dormant
                                   else f", though it filed on {filed[:10]} — the earnings "
                                        "element it uses has gone stale, not the company"))
            eps = None
        if stale_bs:
            crit[7].update(status="INSUFFICIENT_DATA", value=None,
                           note=f"balance sheet is {_span(stale_bs)} older than this price")
            tbvps = None
        if eps is not None:
            if eps <= 0:
                crit[1].update(status="FAIL", value=None,
                               note="TTM EPS non-positive; P/E undefined")
            else:
                price_d, eps_d = valuation_price, Decimal(str(eps))
                pe = round(float(price_d) / eps, 2)  # display only
                crit[1].update(status="PASS" if price_d < PE_MAX * eps_d else "FAIL",
                               value=pe, note=None)
        dps = row.get("recurring_dividend_per_share")
        if dps is not None and crit[5]["status"] == "PASS":
            pct = round(dps / float(valuation_price) * 100, 2)
            # The engine refuses to publish a yield above par — it means the price
            # and the payment describe different securities — and this pass used to
            # publish it anyway, up to 2,240,506%.
            if pct <= float(YIELD_IMPLAUSIBLE):
                crit[5]["value"] = pct
                # ...and the engine's own note is evidence, not decoration: appending
                # keeps the aggregate-tag and unknown-payer disclosures it wrote.
                source = ((row.get("sources") or {}).get("recurring_dividend_per_share") or {})
                quarter = source.get("end")
                currency = _reporting_currency(row)
                amount = (f"${dps:,.2f}" if currency == "USD"
                          else f"{dps:,.2f} {currency}")
                paid = (f"{amount} per share annualized from the latest ordinary "
                        "quarterly rate"
                        + (f" reported for the quarter ended {quarter}" if quarter else "")
                        + "; special dividends excluded")
                trailing = row.get("dividend_per_share")
                if trailing is not None and trailing != dps:
                    trailing_amount = (f"${trailing:,.2f}" if currency == "USD"
                                       else f"{trailing:,.2f} {currency}")
                    paid += (f"; trailing cash was {trailing_amount} per share "
                             "including any specials")
                crit[5]["note"] = f"{crit[5]['note']}; {paid}" if crit[5].get("note") else paid
            else:
                crit[5]["value"] = None
                crit[5]["note"] = ((crit[5].get("note") or "")
                                   + f"; yield of {pct}% is not meaningful against this price").lstrip("; ")
        elif crit[5]["status"] == "PASS":
            missing = ("pays a dividend, but no reliable recurring rate is available "
                       "from direct quarterly per-share filing facts")
            crit[5]["value"] = None
            crit[5]["note"] = (f"{crit[5]['note']}; {missing}"
                               if crit[5].get("note") and missing not in crit[5]["note"]
                               else crit[5].get("note") or missing)
        if tbvps is not None:
            if tbvps <= 0:
                crit[7].update(status="FAIL", value=None, note="non-positive tangible book value")
            else:
                price_d, tbvps_d = valuation_price, Decimal(str(tbvps))
                ptbv = round(float(price_d) / tbvps, 2)  # display only
                crit[7].update(status="PASS" if price_d < PRICE_TO_TBV_MAX * tbvps_d else "FAIL",
                               value=ptbv, note=None)
    elif price is not None and _quote_currency(row) != _reporting_currency(row):
        missing_fx = (f"{_quote_currency(row)} to {_reporting_currency(row)} "
                      "exchange rate")
        for number in (1, 7):
            note = crit[number].get("note")
            if isinstance(note, str) and note.startswith("missing: "):
                items = [item.strip() for item in note.removeprefix("missing: ").split(",")]
                items = [missing_fx if item == "price quote" else item for item in items]
                crit[number]["note"] = "missing: " + ", ".join(dict.fromkeys(items))
    statuses = {c["status"] for c in row["criteria"]}
    row["n_pass"] = sum(1 for c in row["criteria"] if c["status"] == "PASS")
    # a measured failure outranks an unknown: see _verdict in screens/enterprising
    row["verdict"] = (
        "FAIL" if "FAIL" in statuses
        else "INDETERMINATE" if "INSUFFICIENT_DATA" in statuses
        else "INDETERMINATE" if "NOT_APPLICABLE" in statuses
        else "PASS"
    )
    return row


def _newest(series: dict) -> object | None:
    """The latest year's fact in an annual series, for its provenance."""
    return series[max(series)] if series else None


_HISTORY_COVERAGE_SLACK = 45
_HISTORY_POINT_SLACK = 8
_HISTORY_REVISION_TOLERANCE = Decimal("0.05")
_SPLIT_EVENT_LOOKBACK = 7


def _validated_price_history(
    old_fetched: datetime | None,
    old_closes,
    new_closes,
    split_events=(),
    per_share_restatement_factors=(),
) -> tuple[tuple | None, str | None]:
    """Accept a provider history only when revisions have an evidenced cause.

    Small candle corrections are harmless. A split can legitimately rescale every
    pre-event close only after the filing-derived per-share history proves the same
    factor. Until then, reconstruct contemporaneous closes: a split-adjusted price
    divided by an unadjusted EPS or BVPS silently corrupts every historical multiple.
    Unexplained rescaling and suddenly truncated coverage retain the stored series.
    """
    new = tuple((d, Decimal(str(value))) for d, value in new_closes
                if Decimal(str(value)).is_finite() and Decimal(str(value)) > 0)
    if not new:
        return None, "the provider returned no positive historical closes"
    if any(new[i][0] <= new[i - 1][0] for i in range(1, len(new))):
        return None, "the provider returned duplicate or unordered history dates"

    old = tuple((d, Decimal(str(value))) for d, value in old_closes
                if Decimal(str(value)).is_finite() and Decimal(str(value)) > 0)
    if not old:
        return new, None
    if new[0][0] > old[0][0] + timedelta(days=_HISTORY_COVERAGE_SLACK):
        return None, "the refreshed history lost its oldest coverage"
    if new[-1][0] < old[-1][0] - timedelta(days=14):
        return None, "the refreshed history ends before the stored history"
    if len(new) + _HISTORY_POINT_SLACK < len(old):
        return None, "the refreshed history unexpectedly lost weekly observations"

    old_by_date = dict(old)
    overlap = [(d, old_by_date[d], value) for d, value in new if d in old_by_date]
    if len(old) >= 20 and len(overlap) < 8:
        return None, "too few dates overlap the stored history to validate it"

    floor = ((old_fetched.date() - timedelta(days=_SPLIT_EVENT_LOOKBACK))
             if old_fetched is not None else date.max)
    recent_splits = [(d, Decimal(str(factor))) for d, factor in split_events
                     if d >= floor and Decimal(str(factor)) > 0]
    basis_factors = tuple(Decimal(str(factor)) for factor in per_share_restatement_factors
                          if Decimal(str(factor)).is_finite()
                          and Decimal(str(factor)) > 0)

    def expected_factor(day: date) -> Decimal:
        expected = Decimal(1)
        for split_day, factor in recent_splits:
            if day < split_day:
                expected *= factor
        return expected

    def close_to(actual: Decimal, expected: Decimal) -> bool:
        return abs(actual / expected - 1) <= _HISTORY_REVISION_TOLERANCE

    def filing_proves(expected: Decimal) -> bool:
        return any(close_to(factor, expected) for factor in basis_factors)

    bad = 0
    needs_contemporaneous_basis = False
    for day, old_value, new_value in overlap:
        expected = expected_factor(day)
        actual = new_value / old_value
        if close_to(actual, Decimal(1)):
            if expected != 1 and not filing_proves(expected):
                needs_contemporaneous_basis = True
            continue
        if expected != 1 and close_to(actual, expected):
            if filing_proves(expected):
                continue
            needs_contemporaneous_basis = True
            continue
        bad += 1
    if bad > max(2, len(overlap) // 20):
        return None, "historical closes were rescaled without matching split evidence"
    if needs_contemporaneous_basis:
        contemporaneous = tuple(
            (day, value / expected_factor(day)) for day, value in new)
        return contemporaneous, (
            "the provider declared a split that the filing-derived per-share history "
            "does not yet reflect; contemporaneous historical closes remain in use")
    return new, None


def _per_share_restatement_factors(previous: dict | None, row: dict) -> tuple[Decimal, ...]:
    """Factors independently proved by a change in filing-derived per-share history."""
    if not previous:
        return ()
    factors: list[Decimal] = []

    def observe(old_value, new_value) -> None:
        try:
            old = Decimal(str(old_value))
            new = Decimal(str(new_value))
        except (ValueError, TypeError, InvalidOperation):
            return
        if old == 0 or not old.is_finite() or not new.is_finite():
            return
        factor = abs(new / old)
        if abs(factor - 1) > _HISTORY_REVISION_TOLERANCE:
            factors.append(factor)

    old_eps = previous.get("annual_eps") or {}
    new_eps = row.get("annual_eps") or {}
    for year in set(old_eps) & set(new_eps):
        observe(old_eps[year], new_eps[year])
    old_ratios = previous.get("annual_ratios") or {}
    new_ratios = row.get("annual_ratios") or {}
    for year in set(old_ratios) & set(new_ratios):
        for field in ("bvps", "tbvps", "ncavps"):
            observe(old_ratios[year].get(field), new_ratios[year].get(field))
    return tuple(factors)


def _equity_awards(snap) -> dict:
    """Options and restricted stock together, and which of the two are in the figure.

    They dilute the same shareholders and belong in one number, but a company that
    tags only options has said nothing about its restricted stock — so the total
    carries the basis it was struck on, the way a trailing P/E carries `ttm_basis`.
    "options only" is a statement about the evidence, not about the company.
    """
    parts = {"options": snap.options_outstanding, "RSUs": snap.rsus_outstanding}
    present = [k for k, f in parts.items() if f is not None]
    if not present:
        return {"equity_awards": None, "awards_basis": None}
    total = sum((parts[k].value for k in present), Decimal(0))
    basis = " + ".join(present) if len(present) > 1 else f"{present[0]} only"
    return {"equity_awards": float(total), "awards_basis": basis}


def _price_the_ratio_history(row: dict, closes, fx_closes=()) -> None:
    """Turn each past year's book figures into the multiples the panel shows.

    The price of that year comes from the stored weekly closes, and the earnings
    denominator from `ttm_eps_vintage` — trailing EPS as it was knowable at that
    year end, computed only from facts filed by then. Both sides are therefore
    contemporaries: no ratio prices a 2022 balance sheet against today's quote,
    and none of them knows what the company would report in February.

    "That year end" is the company's own, not the calendar's. Microsoft's fiscal
    2025 closed on 2025-06-30, and pricing it at the following December divided a
    June balance sheet into a December market — its price/book read 10.28 where
    the contemporaneous figure is 10.84. A June filer's newest year fared worse
    still: the cutoff fell in a December that has not arrived, so no vintage EPS
    existed for it and the P/E column was simply blank for 206 companies.
    """
    ratios = row.get("annual_ratios") or {}
    if not ratios or not closes:
        return
    vintage = row.get("ttm_eps_vintage") or {}
    by_date = sorted((d.isoformat() if hasattr(d, "isoformat") else str(d), c) for d, c in closes)
    cross_currency = _quote_currency(row) != _reporting_currency(row)
    fx_by_date = sorted(
        (day.isoformat() if hasattr(day, "isoformat") else str(day), rate)
        for day, rate in fx_closes
    )
    for year, values in ratios.items():
        # the fiscal year end this row's figures were struck at; only a December
        # filer's is the December the label suggests
        cutoff = values.get("end") or f"{year}-12-31"
        prior = [c for d, c in by_date if d <= cutoff]
        if not prior:
            continue
        quote_price = float(prior[-1])
        price = quote_price
        if cross_currency:
            prior_fx = [rate for day, rate in fx_by_date if day <= cutoff]
            if not prior_fx:
                continue
            rate = float(prior_fx[-1])
            if not rate > 0:
                continue
            price = quote_price * rate
            values["quote_price"] = round(quote_price, 4)
            values["fx_rate"] = round(rate, 8)
        values["price"] = round(price, 4)
        eps = vintage.get(cutoff)
        if eps and eps > 0:
            values["pe"] = round(price / float(eps), 2)
        for key, book in (("pb", "bvps"), ("ptbv", "tbvps"), ("pncav", "ncavps")):
            if values.get(book, 0) > 0:
                values[key] = round(price / values[book], 2)


def _price_stats_row(row: dict, closes) -> dict | None:
    """Where the price sits in its own five-year history. Never a criterion."""
    warning = (row.get("price_history_warning") or {}).get("note", "")
    if ("filing-derived per-share history does not yet reflect" in warning
            or not closes or not row.get("price")):
        return None
    series = tuple((d, Decimal(str(c))) for d, c in closes)
    stats = pricestats.compute(series, Decimal(str(row["price"])))
    if stats is None:
        return None
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in stats.items()}


_PEER_MINIMUM = 5           # fewer than this and the median describes nothing
_EFFICIENCY_GAP = 0.66      # a margin this far under the peer median is worth stating


def _mark_peer_efficiency(rows: list[dict]) -> None:
    """Operating margin against the median of the company's own industry.

    Graham's sixth Penn Central signal was that its operating ratio had long run
    far worse than a comparable railroad's — the kind of gap that says the
    business is weaker than its peers whatever the reported earnings say. It is
    the one measure here that no single filing can produce, because it needs
    every other filer in the industry, so it is computed at export.
    """
    margins: dict[str, list[float]] = {}
    for row in rows:
        margin = _operating_margin(row)
        if margin is None:
            continue
        row["_margin"] = margin
        industry = row.get("industry")
        if industry:
            margins.setdefault(industry, []).append(margin)
    for row in rows:
        margin = row.pop("_margin", None)
        industry = row.get("industry")
        peers = margins.get(industry or "", ())
        if margin is None or len(peers) < _PEER_MINIMUM:
            continue
        ordered = sorted(peers)
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        row["peer_efficiency"] = {
            "margin": round(margin * 100, 1),
            "industry_median": round(median * 100, 1),
            "peers": len(ordered),
            # only a shortfall is worth a reader's attention, and only against a
            # peer group that is itself profitable
            "behind": bool(median > 0 and margin < median * _EFFICIENCY_GAP),
        }


def _operating_margin(row: dict) -> float | None:
    """Latest fiscal year where the company reported both operating income and
    revenue. Banks and insurers report no operating subtotal and get none."""
    income = row.get("annual_operating_income") or {}
    revenue = row.get("annual_revenue") or {}
    shared = sorted(set(income) & set(revenue), reverse=True)
    for year in shared[:1]:
        sales = revenue[year]
        if sales and sales > 0:
            return income[year] / sales
    return None


def _mark_memberships(rows: list[dict], progress) -> None:
    """Tag each row with the indexes it currently belongs to. A list that cannot
    be read is skipped WHOLE — a half-parsed index would read as reconstitution —
    and the skip is said out loud rather than silently shipping blanks."""
    sp = indexes.sp500()
    dj = indexes.djia_ciks(sp) if sp else None
    n100 = indexes.nasdaq100()
    if not (sp and dj and n100):
        progress("index lists unavailable: "
                 + ", ".join(n for n, v in (("S&P 500", sp), ("DJIA", dj),
                                            ("Nasdaq 100", n100)) if not v))
    for r in rows:
        m = []
        if dj and r["cik"] in dj:
            m.append("DJIA")
        if sp and r["cik"] in sp:
            m.append("S&P 500")
        if n100 and r.get("ticker") in n100:
            m.append("Nasdaq 100")
        if r.get("exchange") == "Nasdaq":
            m.append("Nasdaq Comp")
        if m:
            r["index_memberships"] = m


_QUOTE_FIELDS = (
    "price", "price_asof", "price_session", "market_state",
    "market_timezone", "market_state_asof", "price_source",
)


def _previous_dashboard_rows() -> dict[str, dict]:
    """Read the snapshot that readers are using before replacing it.

    An hourly provider miss must not turn a real, dated quote into missing data.
    The previous quote is safe to retain because its timestamp remains attached.
    """
    try:
        payload = json.loads(DASHBOARD_JSON.read_text(encoding="utf-8"))
        return {row["cik"]: row for row in payload.get("rows", []) if row.get("cik")}
    except (OSError, TypeError, ValueError):
        return {}


def _set_quote(row: dict, quote) -> None:
    row.update({
        "price": float(quote.price),
        "price_asof": quote.asof.isoformat(),
        "price_session": quote.session,
        "market_state": quote.market_state,
        "market_timezone": quote.market_timezone,
        "market_state_asof": (
            quote.market_state_asof.isoformat() if quote.market_state_asof else None
        ),
        "price_source": quote.source,
    })


def _reporting_currency(row: dict) -> str:
    return (row.get("reporting_currency") or row.get("currency") or "USD").upper()


def _quote_currency(row: dict) -> str:
    return (row.get("quote_currency") or row.get("currency") or "USD").upper()


def _set_fx(row: dict, record: dict | None) -> None:
    """Attach an explicit USD-to-reporting-currency rate to one payload row."""
    reporting = _reporting_currency(row)
    if reporting == "USD" or record is None:
        return
    row["fx"] = {
        "base": record["base"],
        "counter": record["counter"],
        "rate": float(record["rate"]),
        "asof": record["asof"],
        "source": record["source"],
    }


def _valuation_price(row: dict, price) -> Decimal | None:
    """Raw traded price restated into the monetary statement unit."""
    try:
        value = Decimal(str(price))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    quote_currency = _quote_currency(row)
    reporting_currency = _reporting_currency(row)
    if quote_currency == reporting_currency:
        return value
    fx = row.get("fx") or {}
    try:
        rate = Decimal(str(fx.get("rate")))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if (fx.get("base") != quote_currency or fx.get("counter") != reporting_currency
            or not rate.is_finite() or rate <= 0):
        return None
    return value * rate


def _retain_previous_quote(row: dict, previous: dict | None) -> bool:
    if previous is None or previous.get("price") is None:
        return False
    for field in _QUOTE_FIELDS:
        if field in previous:
            row[field] = previous[field]
    for field in ("fx", "price_reporting_currency", "fx_warning"):
        if field in previous:
            row[field] = previous[field]
    return True


def export(conn, with_prices: bool = True, progress=_print_progress,
           *, quote_only: bool = False) -> None:
    """Write the whole universe as one JSON file — the UI fetches it once.
    Snapshots below the current engine are recomputed first, so a refresh never
    ships stale arithmetic — recomputation is automatic, not a separate button.

    ``quote_only`` is the hourly path: one small intraday request per ticker and
    the already-validated local weekly histories. A full export still refreshes
    those histories and current index membership lists.
    """
    stale = store.needs_recompute(conn, eligible_only=True)
    if stale:
        derive(conn, progress=progress)
    rows = store.dashboard_rows(conn)
    previous_rows = _previous_dashboard_rows()
    quote_updated = 0
    quote_failed = 0
    if with_prices:
        prices = YahooPriceProvider()
        priceable = [r for r in rows
                     if r.get("ticker") and r.get("listed") in PRICEABLE_LISTINGS]
        label = "quotes" if quote_only else "prices and history"
        progress(f"fetching {label} for {len(priceable)} verified tickers", 0,
                 len(priceable))
        done = 0
        # Calls are independent. Hourly refreshes ask only for the one-day chart;
        # full exports also replace the five-year weekly history.
        try:
            with ThreadPoolExecutor(max_workers=12) as pool:
                fetch = prices.quote if quote_only else prices.history

                def fetch_row(row):
                    currency = _quote_currency(row)
                    # Preserve compatibility with simple one-argument providers
                    # used in tests and local substitutions for ordinary USD rows.
                    return (fetch(row["ticker"]) if currency == "USD"
                            else fetch(row["ticker"], expected_currency=currency))

                futures = {pool.submit(fetch_row, r): r for r in priceable}
                for fut in as_completed(futures):
                    row = futures[fut]
                    try:
                        result = fut.result()
                    except Exception:
                        result = None
                    quote = result if quote_only else (result.quote if result else None)
                    if quote is not None:
                        _set_quote(row, quote)
                        quote_updated += 1
                        if not quote_only:
                            row["_history"] = result
                    else:
                        quote_failed += 1
                        retained = quote_only and _retain_previous_quote(
                            row, previous_rows.get(row["cik"])
                        )
                        if quote_only:
                            row["quote_refresh_warning"] = {
                                "kind": "QUOTE_REFRESH_FAILED",
                                "note": (
                                    "the hourly provider request failed; the previous dated quote "
                                    "remains in use" if retained else
                                    "the hourly provider request failed and no previous quote is available"
                                ),
                            }
                    done += 1
                    if done % 25 == 0 or done == len(futures):
                        progress(f"fetching {label}", done, len(futures))

                reporting_currencies = sorted({
                    _reporting_currency(row) for row in priceable
                    if _reporting_currency(row) != "USD"
                })
                fx_futures = {
                    pool.submit(prices.exchange_rate_history, "USD", currency): currency
                    for currency in reporting_currencies
                }
                fx_records = {}
                for future in as_completed(fx_futures):
                    currency = fx_futures[future]
                    try:
                        history = future.result()
                    except Exception:
                        history = None
                    if history is not None:
                        store.set_fx_history(conn, "USD", currency, history)
                        fx_records[currency] = {
                            "base": "USD", "counter": currency,
                            "rate": float(history.quote.price),
                            "asof": history.quote.asof.isoformat(),
                            "source": history.quote.source,
                            "closes": list(history.closes),
                        }
                    else:
                        cached = store.fx_history(conn, "USD", currency)
                        if cached is not None:
                            fx_records[currency] = cached
                for row in rows:
                    reporting = _reporting_currency(row)
                    if reporting == "USD":
                        continue
                    record = fx_records.get(reporting)
                    _set_fx(row, record)
                    if record is None:
                        row["fx_warning"] = {
                            "kind": "FX_UNAVAILABLE",
                            "note": (f"No USD to {reporting} exchange rate was available; "
                                     "accounting ratios remain usable, but price-based "
                                     "valuation is withheld"),
                        }
        finally:
            prices.close()
        # the series is written once per company, so a later `derive` can rebuild
        # the statistics without asking the provider for five more years of data
        closes_by_cik = {}
        for row in rows:
            history = row.pop("_history", None)
            closes = None
            if history is not None:
                old_fetched, old_closes = store.price_history_record(conn, row["cik"])
                closes, warning = _validated_price_history(
                    old_fetched, old_closes, history.closes, history.splits,
                    _per_share_restatement_factors(
                        previous_rows.get(row["cik"]), row))
                if warning is not None:
                    row["price_history_warning"] = {
                        "kind": "HISTORY_REJECTED",
                        "note": (warning if closes else
                                 warning + "; the previously validated history remains in use"),
                    }
                if closes:
                    store.set_price_history(conn, row["cik"], closes)
            elif quote_only:
                prior = previous_rows.get(row["cik"], {})
                if prior.get("price_history_warning"):
                    row["price_history_warning"] = prior["price_history_warning"]
            apply_price(row, row.get("price"))
            closes_by_cik[row["cik"]] = [] if row.get("listed") not in PRICEABLE_LISTINGS else (
                closes or store.price_history(conn, row["cik"])
            )
            row["price_stats"] = _price_stats_row(row, closes_by_cik[row["cik"]])
        conn.commit()
    else:
        closes_by_cik = {
            r["cik"]: (store.price_history(conn, r["cik"])
                       if r.get("listed") in PRICEABLE_LISTINGS else [])
            for r in rows
        }
        for row in rows:
            row["price_stats"] = _price_stats_row(row, closes_by_cik[row["cik"]])
    DASHBOARD_JSON.parent.mkdir(parents=True, exist_ok=True)
    if with_prices and not quote_only:  # full refresh owns the changing index lists
        _mark_memberships(rows, progress)
    elif quote_only:  # hourly prices must not erase memberships during a list outage
        for row in rows:
            prior = previous_rows.get(row["cik"], {})
            if prior.get("index_memberships"):
                row["index_memberships"] = prior["index_memberships"]
    _mark_peer_efficiency(rows)
    for row in rows:
        row.update(profiles.enrich(row))
    for row in rows:
        reporting = _reporting_currency(row)
        fx_record = (store.fx_history(conn, "USD", reporting)
                     if reporting != "USD" else None)
        _price_the_ratio_history(
            row, closes_by_cik.get(row["cik"]) or [],
            (fx_record or {}).get("closes") or [])
    for row in rows:  # engine-internal series with no reader in the payload
        row.pop("ttm_eps_vintage", None)
        _strip_detail_only_evidence(row)
        # the event scan is read by the notes above; the raw item codes would be
        # a second, unrendered copy of what those notes already say
        row.pop("filing_events", None)
        row.pop("events_from", None)
        row.pop("last_filing", None)   # read by apply_price, not by the panel
    refreshed_at = store._now()
    payload = {"generated": refreshed_at, "engine_version": store.ENGINE_VERSION, "rows": rows}
    # Readers see either the complete old payload or the complete new one, never
    # a partially-written 35 MB JSON document during an hourly replacement.
    temporary = DASHBOARD_JSON.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.replace(DASHBOARD_JSON)
    store.set_state(conn, "last_export", refreshed_at)
    if with_prices:
        store.set_state(conn, "last_quote_refresh", refreshed_at)
        store.set_state(conn, "last_quote_refresh_updated", str(quote_updated))
        store.set_state(conn, "last_quote_refresh_failed", str(quote_failed))
    conn.commit()
    suffix = (f"; {quote_updated} quotes updated, {quote_failed} retained/missing"
              if quote_only else "")
    progress(f"wrote dashboard.json — {DASHBOARD_JSON.stat().st_size / 1e6:.2f} MB, "
             f"{len(rows)} companies{suffix}", len(rows), len(rows))


def quotes(conn, progress=_print_progress) -> None:
    """Refresh all eligible universe quotes using stored price histories."""
    export(conn, with_prices=True, progress=progress, quote_only=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["bootstrap", "bulk", "metadata", "daily",
                                        "derive", "export", "quotes", "listing-age", "events",
                                        "cover", "inline", "dera", "ifrs-import", "edinet-import",
                                        "status"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--no-prices", action="store_true")
    ap.add_argument("--all-snapshots", action="store_true",
                    help="with derive, include cached filers that cannot enter the dashboard")
    ap.add_argument("--from", dest="start", help="first quarter for dera, e.g. 2021q1")
    ap.add_argument("--path", help="directory containing IFRS statement workbooks")
    ap.add_argument("--to", dest="end", help="last EDINET filing date (YYYY-MM-DD)")
    ap.add_argument("--edinet-code", help="one Japanese four-character security code")
    ap.add_argument("--ticker", default=ifrs_workbook.DEFAULT_TICKER)
    ap.add_argument("--entity-id", default=ifrs_workbook.DEFAULT_ENTITY_ID)
    ap.add_argument("--cik", action="append", dest="ciks",
                    help="explicit SEC CIK; repeat for multiple issuers")
    args = ap.parse_args(argv)
    if args.command == "inline" and not args.ciks:
        ap.error("inline requires at least one --cik")
    conn = store.connect()
    if args.command == "bootstrap":
        bootstrap(conn, args.limit)
    elif args.command == "bulk":
        bulk(conn, args.limit)
    elif args.command == "metadata":
        metadata(conn)
    elif args.command == "daily":
        daily(conn, args.days)
    elif args.command == "derive":
        derive(conn, all_snapshots=args.all_snapshots)
    elif args.command == "listing-age":
        listing_age(conn)
    elif args.command == "events":
        events(conn)
    elif args.command == "cover":
        cover_pages(conn)
    elif args.command == "inline":
        retain_inline_statements(conn, set(args.ciks))
    elif args.command == "dera":
        dera_sync(conn, args.start)
    elif args.command == "ifrs-import":
        if not args.path:
            ap.error("ifrs-import requires --path")
        import_ifrs_workbooks(
            conn, args.path, ticker=args.ticker, entity_id=args.entity_id)
    elif args.command == "edinet-import":
        if not args.start:
            ap.error("edinet-import requires --from YYYY-MM-DD")
        client = EdinetClient(os.environ.get("EDINET_API_KEY")
                              or getpass.getpass("EDINET API key: "))
        import_edinet(conn, date.fromisoformat(args.start),
                      date.fromisoformat(args.end or args.start), limit=args.limit,
                      only_code=args.edinet_code, client=client)
    elif args.command == "export":
        export(conn, not args.no_prices)
    elif args.command == "quotes":
        quotes(conn)
    else:
        print(json.dumps(store.stats(conn), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
