"""What the company earned and sold: the per-share chains, the annual
series behind them, and the trailing windows."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from screener.normalize import UnsupportedFilerError, _fy_label, build_snapshot
from helpers import *  # noqa: F403 — the shared fixtures, by design
from helpers import EPS, GAAP, build, dur, facts_doc, inst, tagdata, texts
from helpers import _dimensioned, _reported, _shares, _years, _yr  # underscored, so `import *` skips them


@pytest.mark.parametrize('rows,share_rows,events,factors', [
    ([('2023-03-01', '10'), ('2024-03-01', '1')], [], [('2024-03-01', '10')], ('10', '1')),
    ([('2023-03-01', '10'), ('2024-03-01', '1'), ('2024-06-01', '0.1')], [], [('2024-03-01', '10')], ('10', '1')),
    ([('2023-03-01', '10'), ('2024-03-01', '1'), ('2025-05-01', '0.25')], [], [('2024-03-01', '10'), ('2025-05-01', '4')], ('40', '4')),
    ([('2023-03-01', '0.02'), ('2024-03-01', '0.01')], [], [], ('1', '1')),
    ([('2023-03-01', '10'), ('2023-03-01', '10'), ('2024-03-01', '1')], [], [('2024-03-01', '10')], ('10', '1')),
    ([('2023-03-01', '0'), ('2024-03-01', '1')], [], [], ('1', '1')),
    ([('2023-03-01', '10'), ('2024-03-01', '1')], [('2023-03-01', '100'), ('2024-03-01', '1000')], [('2024-03-01', '10')], ('10', '1')),
    ([('2023-03-01', '10'), ('2024-03-01', '1')], [('2023-03-01', '100'), ('2024-03-01', '200')], [], ('1', '1')),
])
def test_occurrence_metadata_preserves_period_events_and_scalar_factors(rows, share_rows, events, factors):
    from screener import normalize as current
    entries = [dict(start='2022-01-01', end='2022-12-31', filed=f, val=v) for f, v in rows]
    counts = [dict(start='2022-01-01', end='2022-12-31', filed=f, val=v) for f, v in share_rows]
    periods = current._by_period(entries, tag='eps')
    shares = current._by_period(counts, tag='shares')
    assert periods == {('eps', '2022-01-01', '2022-12-31'): sorted((f, Decimal(v)) for f, v in rows)}
    assert all(len(pair) == 2 for values in periods.values() for pair in values)
    support = current._DecisionSupport()
    assert current._split_events(periods, shares, decision_support=support) == [(f, Decimal(k)) for f, k in events]
    assert current._split_factor(periods, '2023-01-01', shares) == Decimal(factors[0])
    assert current._split_factor(periods, '2025-01-01', shares) == Decimal(factors[1])
    assert support.retained()['qualification'] == 'NOT_ASSESSED'


@pytest.mark.parametrize('metadata_failure', [False, True])
def test_occurrence_metadata_retains_duplicate_pointer_identity_and_merge_rejection(monkeypatch, metadata_failure):
    import hashlib
    import json
    from screener import normalize as current
    from screener.models import CapturedSource
    from screener.sources.sec_json import decode_company_facts, ORIGIN_KEY
    entry = dict(start='2020-01-01', end='2020-12-31', filed='2021-03-01', form='10-K', accn='k', val=2)
    raw = json.dumps({'cik':1234, 'facts':{'us-gaap':{'EarningsPerShareDiluted':tagdata('USD/shares', [entry, entry])}}}).encode()
    source = CapturedSource('company_facts', hashlib.sha256(raw).hexdigest(), 'https://data.sec.gov/fixture', 1, 2, 3)
    entries = decode_company_facts(raw, source=source)['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares']
    original = current._period_occurrence
    if metadata_failure:
        def fail_first(entry):
            if entry is entries[0]:
                raise ValueError('metadata failure')
            return original(entry)
        monkeypatch.setattr(current, '_period_occurrence', fail_first)
    periods = current._by_period(entries, tag='EarningsPerShareDiluted')
    key = ('EarningsPerShareDiluted', '2020-01-01', '2020-12-31')
    sources = periods.occurrences[key]
    assert sources[0][:2] == sources[1][:2]
    if metadata_failure:
        assert sources[0][2]['origin'] is None
        assert sources[0][2]['metadata_error'] == 'ValueError'
        assert sources[0][2]['qualification'] == 'NOT_ASSESSED'
    else:
        assert sources[0][2]['origin'] is entries[0][ORIGIN_KEY]
        assert sources[0][2]['origin'].pointer != sources[1][2]['origin'].pointer
    assert sources[1][2]['origin'] is entries[1][ORIGIN_KEY]
    assert periods[key] == [('2021-03-01', Decimal('2')), ('2021-03-01', Decimal('2'))]
    additional = current._by_period([entries[1], entries[1]], tag='EarningsPerShareDiluted')
    current._merge_period_values(periods, additional)
    assert len(periods[key]) == 2 and len(periods.rejected_occurrences) == 2
    assert all(row['source']['origin'] is entries[1][ORIGIN_KEY] for row in periods.rejected_occurrences)
    for merge in (False, True):
        numeric_only = current._PeriodValues()
        numeric_only[key] = [('2020-03-01', Decimal('20'))]
        if merge:
            current._merge_period_values(numeric_only, current._by_period([entries[1]], tag=key[0]))
        else:
            current._by_period([entries[1]], tag=key[0], into=numeric_only)
        assert numeric_only[key] == [('2020-03-01', Decimal('20')), ('2021-03-01', Decimal('2'))]
        assert current._period_sources(numeric_only, key, 0) is None
        assert current._period_sources(numeric_only, key, 1)['origin'] is entries[1][ORIGIN_KEY]


def test_decision_support_uses_one_numeric_invocation_and_explicit_existing_frontier(monkeypatch):
    from screener import normalize as current
    periods = current._by_period([
        dict(start='2020-01-01', end='2020-12-31', filed='2021-03-01', val=10),
        dict(start='2020-01-01', end='2020-12-31', filed='2022-03-01', val=1)], tag='eps')
    original = current._as_split
    calls = []
    def counted(ratio):
        calls.append(ratio)
        return original(ratio)
    monkeypatch.setattr(current, '_as_split', counted)
    monkeypatch.setattr(current._DecisionSupport, 'LIMIT', 2)
    support = current._DecisionSupport()
    assert current._split_factor(periods, '2020-01-01', decision_support=support) == Decimal('10')
    assert len(calls) == 1
    retained = support.retained()
    assert len(retained['records']) == 2 and retained['observed_count'] > 2
    assert not retained['complete'] and retained['qualification'] == 'NOT_ASSESSED'


@pytest.mark.parametrize('assume', [False, True])
def test_private_decision_support_preserves_public_snapshot_and_scalar_callers(assume):
    from screener import normalize as current
    import copy
    from helpers import OE_GAAP
    document = facts_doc(copy.deepcopy(OE_GAAP))
    after = current.build_snapshot('TEST', '0000001234', copy.deepcopy(document), assume_absent_zero=assume)
    assert {year: fact.value for year, fact in after.annual_eps.items()} == {
        2021: Decimal('3.0'), 2022: Decimal('3.5'), 2023: Decimal('4.1'),
        2024: Decimal('5.0'), 2025: Decimal('6.0')}
    assert after.annual_share_counts == {}
    assert current.vintage_ttm_eps(copy.deepcopy(OE_GAAP)) == {
        '2022-12-31': Decimal('3.0'), '2023-12-31': Decimal('3.5'),
        '2024-12-31': Decimal('4.0'), '2025-12-31': Decimal('5.0')}
    assert getattr(after.annual_eps, 'decision_support', None)


def test_filer_that_moved_to_domestic_forms_keeps_foreign_history():
    """Old 20-F facts stay readable once the newest financial filing is domestic."""
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", [
        {**EPS[0], "form": "20-F"},  # FY2021 filed 2022 on the pre-transition form
        *EPS[1:],
    ])
    s = build(gaap)
    assert s.annual_eps[2021].provenance.form == "20-F"
    assert float(s.annual_eps[2021].value) == 3.0


def test_annual_eps_from_10k_only_latest_filed_wins():
    s = build()
    assert {y: float(f.value) for y, f in s.annual_eps.items()} == {
        2021: 3.0, 2022: 3.5, 2023: 4.1, 2024: 5.0, 2025: 6.0,
    }
    # restated FY2023 traces to the later filing
    assert s.annual_eps[2023].provenance.accession == "k24"
    # quarterly facts are never promoted to annual figures
    assert 2026 not in s.annual_eps


def test_ttm_is_annual_plus_ytd_delta():
    s = build()
    assert s.ttm_eps == Decimal("6.0") + Decimal("1.6") - Decimal("1.4")
    assert len(s.ttm_eps_inputs) == 3
    forms = [f.provenance.form for f in s.ttm_eps_inputs]
    assert forms == ["10-K", "10-Q", "10-Q"]


def test_ttm_falls_back_to_annual_when_no_newer_quarter():
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", EPS[:6])  # 10-K facts only
    s = build(gaap)
    assert s.ttm_eps == Decimal("6.0")
    assert len(s.ttm_eps_inputs) == 1


def test_basic_eps_fills_year_diluted_chain_lacks():
    # Lennar FY2025 pattern: the newest 10-K tags only EarningsPerShareBasic.
    # Without the fill the series freezes a year back and stales criteria 4/6 + TTM.
    diluted = [dur(f"{y}-01-01", f"{y}-12-31", 14.31, accn=f"d{y}", filed=f"{y + 1}-01-26")
               for y in range(2020, 2025)]
    basic = diluted + [dur("2025-01-01", "2025-12-31", 7.98, accn="b25", filed="2026-01-28")]
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", diluted)
    gaap["EarningsPerShareBasic"] = tagdata("USD/shares", basic)
    s = build(gaap)
    assert max(s.annual_eps) == 2025
    assert float(s.annual_eps[2025].value) == 7.98
    assert "basic" in s.annual_eps[2025].provenance.concept
    # years the diluted chain covers keep their diluted values
    assert "basic" not in s.annual_eps[2024].provenance.concept


def test_current_continuing_series_beats_longer_dead_plain_series():
    # FCX pattern: EarningsPerShareDiluted stopped in 2021 (15 years of history),
    # continuing-ops runs through 2025 with fewer years — recency must win
    plain = [dur(f"{y}-01-01", f"{y}-12-31", 1.0, accn=f"p{y}", filed=f"{y + 1}-02-15")
             for y in range(2007, 2022)]
    cont = [dur(f"{y}-01-01", f"{y}-12-31", 2.0, accn=f"c{y}", filed=f"{y + 1}-02-15")
            for y in range(2013, 2026)]
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", plain)
    gaap["IncomeLossFromContinuingOperationsPerDilutedShare"] = tagdata("USD/shares", cont)
    s = build(gaap)
    assert max(s.annual_eps) == 2025
    assert "ContinuingOperations" in s.annual_eps[2025].provenance.tag


def test_later_exact_scale_comparative_cannot_corrupt_statement_history():
    """GSUN's later 20-F repeated whole-dollar digits under a false thousands header."""
    from screener.normalize import (
        _annual_gross_profit, _annual_net_income, _annual_operating_income,
        _annual_revenue,
    )

    def history(tag, negative=False):
        sign = -1 if negative else 1
        return tagdata("USD", [
            dur("2021-01-01", "2021-12-31", sign * 8_800_000,
                accn=f"{tag}-21", filed="2022-03-01"),
            dur("2022-01-01", "2022-12-31", sign * 4_800_000,
                accn=f"{tag}-22-original", filed="2023-03-01"),
            dur("2022-01-01", "2022-12-31", sign * 4_800_000_000,
                accn="0001213900-25-013985", filed="2025-03-01"),
            dur("2023-01-01", "2023-12-31", sign * 1_800_000,
                accn=f"{tag}-23", filed="2024-03-01"),
        ])

    gaap = {
        "Revenues": history("revenue"),
        "GrossProfit": history("gross"),
        "OperatingIncomeLoss": history("operating", negative=True),
        "NetIncomeLoss": history("income", negative=True),
    }

    series = (
        _annual_revenue(gaap), _annual_gross_profit(gaap),
        _annual_operating_income(gaap), _annual_net_income(gaap),
    )
    for annual in series:
        assert abs(annual[2022].value) == Decimal("4800000")
        assert annual[2022].provenance.accession.endswith("22-original")
        assert "1000x presentation-scale contradiction" in (
            annual[2022].provenance.concept)


def test_two_earlier_filings_prove_a_cash_flow_scale_error_without_smooth_neighbors():
    """GSUN's lumpy OCF is proved by two filings, not a company-size heuristic."""
    from screener.normalize import _annual_union

    tag = "NetCashProvidedByUsedInOperatingActivities"
    gaap = {tag: tagdata("USD", [
        dur("2020-10-01", "2021-09-30", 31_893,
            accn="ocf-21", filed="2022-03-01"),
        dur("2021-10-01", "2022-09-30", 910_251,
            accn="ocf-22-original", filed="2023-03-01"),
        dur("2021-10-01", "2022-09-30", 910_251,
            accn="ocf-22-repeated", filed="2024-03-01"),
        dur("2021-10-01", "2022-09-30", 910_251_000,
            accn="0001213900-25-013985", filed="2025-03-01"),
        dur("2022-10-01", "2023-09-30", -4_216_061,
            accn="ocf-23", filed="2026-03-01"),
    ])}

    annual = _annual_union(gaap, (tag,))

    assert annual[2022].value == Decimal("910251")
    assert annual[2022].provenance.accession == "ocf-22-repeated"
    assert "two earlier annual filings" in annual[2022].provenance.concept


def test_unreviewed_alternating_scale_history_is_not_automatically_rewritten():
    """IOR proves that adjacent malformed filings can point in opposite directions."""
    from screener.normalize import _annual_net_income

    gaap = {"NetIncomeLoss": tagdata("USD", [
        dur("2009-01-01", "2009-12-31", 920,
            accn="ior-2011", filed="2012-03-30"),
        dur("2010-01-01", "2010-12-31", 1_838,
            accn="ior-2011", filed="2012-03-30"),
        dur("2010-01-01", "2010-12-31", 1_838_000,
            accn="ior-2012", filed="2013-04-16"),
        dur("2011-01-01", "2011-12-31", 669,
            accn="ior-2013", filed="2014-03-31"),
    ])}

    annual = _annual_net_income(gaap)

    assert annual[2010].value == Decimal("1838000")
    assert annual[2010].provenance.accession == "ior-2012"



def test_stale_continuing_ops_series_not_preferred():
    cont = [dur(f"{y}-01-01", f"{y}-12-31", 1.0, accn=f"c{y}", filed=f"{y + 1}-02-15")
            for y in (2015, 2016, 2017, 2018, 2019)]
    gaap = dict(GAAP)
    gaap["IncomeLossFromContinuingOperationsPerDilutedShare"] = tagdata("USD/shares", cont)
    s = build(gaap)
    assert max(s.annual_eps) == 2025  # current diluted series wins over stale continuing-ops


def test_overlapping_period_never_invents_a_later_year():
    # A fiscal-year change leaves two annual periods overlapping in the same year.
    # Pushing the second one to the next label invents a year that has not happened;
    # the framed period wins and the overlap is dropped instead.
    eps = [
        {**dur("2021-07-01", "2022-06-30", -1.0, accn="k22a", filed="2022-09-15"),
         "frame": "CY2022"},
        dur("2021-10-01", "2022-09-30", 2.0, accn="k22b", filed="2023-01-15"),
    ]
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", eps)
    s = build(gaap)
    assert sorted(s.annual_eps) == [2022]
    assert float(s.annual_eps[2022].value) == -1.0  # SEC's own label is authoritative


def test_eighteen_day_bankruptcy_stub_is_not_a_redated_fiscal_year():
    """Weatherford's predecessor ended December 13 and its successor covered the
    remaining 18 days.  Those are separate reporting periods, not two 52/53-week
    renditions of one year, so only one may own FY2019."""
    from screener.normalize import _fy_labels

    ends = ["2019-12-13", "2019-12-31", "2020-12-31"]
    by_end = {
        end: {"end": end, "fy": 2020 if end == "2020-12-31" else 2019}
        for end in ends
    }
    labels = _fy_labels(ends, {"2020-12-31": 2020}, by_end)

    assert labels["2019-12-13"] == 2019
    assert "2019-12-31" not in labels


def test_fresh_start_balance_dates_do_not_replace_the_fiscal_year_end():
    """A predecessor close and successor opening balance in the annual report
    must not supply the capital base for the combined full-year earnings."""
    gaap = {
        "NetIncomeLoss": tagdata("USD", [
            {**dur("2024-01-01", "2024-12-31", -120,
                   accn="k25", filed="2026-03-16"), "frame": "CY2024"},
            {**dur("2025-01-01", "2025-12-31", -276,
                   accn="ka25", filed="2026-04-30"), "frame": "CY2025"},
        ]),
        "Assets": tagdata("USD", [
            inst("2025-03-12", 872, form="10-K", accn="k25",
                 filed="2026-03-16"),
            inst("2025-03-13", 872, form="10-K", accn="k25",
                 filed="2026-03-16"),
            inst("2025-12-31", 599, form="10-K", accn="k25",
                 filed="2026-03-16"),
        ]),
        "Liabilities": tagdata("USD", [
            inst("2025-03-12", 799, form="10-K", accn="k25",
                 filed="2026-03-16"),
            inst("2025-03-13", 799, form="10-K", accn="k25",
                 filed="2026-03-16"),
            inst("2025-12-31", 808, form="10-K", accn="k25",
                 filed="2026-03-16"),
        ]),
        "AssetsCurrent": tagdata("USD", [
            inst("2025-03-12", 300, form="10-K", accn="k25",
                 filed="2026-03-16"),
            inst("2025-12-31", 100, form="10-K", accn="k25",
                 filed="2026-03-16"),
        ]),
        "LiabilitiesCurrent": tagdata("USD", [
            inst("2025-03-12", 100, form="10-K", accn="k25",
                 filed="2026-03-16"),
            inst("2025-12-31", 50, form="10-K", accn="k25",
                 filed="2026-03-16"),
        ]),
    }
    from screener.normalize import _annual_net_income, annual_ratios, fiscal_year_ends

    income = _annual_net_income(gaap)
    assert fiscal_year_ends(gaap)[2025] == "2025-12-31"

    ratios = annual_ratios(gaap, income, {}, {})
    assert ratios[2025]["end"] == "2025-12-31"
    assert ratios[2025]["current_ratio"] == 2
    assert "return_on_book" not in ratios[2025]
    assert "return_on_equity" not in ratios[2025]


def test_label_never_lands_after_the_year_the_period_ends_in():
    # Tyson pattern: many old unframed years ahead of a few framed recent ones.
    # Inference used to cascade forward and label a Sept-2025 year as FY2027.
    eps = [dur(f"{y}-10-01", f"{y + 1}-09-28", 1.0 + y % 3, accn=f"k{y}",
               filed=f"{y + 1}-11-15") for y in range(2010, 2023)]
    eps.append({**dur("2024-09-29", "2025-09-27", 1.33, accn="k25", filed="2025-11-14"),
                "frame": "CY2025"})
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", eps)
    s = build(gaap)
    assert max(s.annual_eps) == 2025
    for fy, fact in s.annual_eps.items():
        end_year = fact.provenance.period_end.year
        assert end_year - 1 <= fy <= end_year, f"FY{fy} labels a period ending {end_year}"


def test_frame_field_labels_comparative_only_years():
    # RDDT-style: pre-IPO 2022 exists only as a comparative (fy overshoots to 2024)
    # but SEC's frame field still labels it CY2022
    eps = [
        {**dur("2022-01-01", "2022-12-31", 1.0, accn="k24", filed="2025-02-15"),
         "fy": 2024, "frame": "CY2022"},
        {**dur("2023-01-01", "2023-12-31", 2.0, accn="k24", filed="2025-02-15"),
         "fy": 2024, "frame": "CY2023"},
        {**dur("2024-01-01", "2024-12-31", 3.0, accn="k24", filed="2025-02-15"),
         "fy": 2024, "frame": "CY2024"},
    ]
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", eps)
    s = build(gaap)
    assert sorted(s.annual_eps) == [2022, 2023, 2024]


def test_frameless_end_filled_from_nearest_framed_neighbor():
    eps = [
        {**dur("2022-01-01", "2022-12-31", 1.0, accn="k22", filed="2023-02-15"), "fy": 2022},
        {**dur("2023-01-01", "2023-12-31", 2.0, accn="k23", filed="2024-02-15"),
         "fy": 2023, "frame": "CY2023"},
    ]
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", eps)
    assert sorted(build(gaap).annual_eps) == [2022, 2023]


def test_current_filer_fy_realigns_an_old_calendar_frame_deck_style():
    """An obsolete calendar-year anchor must not label a new March FY2026 as
    FY2025. The current 10-K's credible fy is the common anchor across tags."""
    eps = [
        {**dur("2024-01-01", "2024-12-31", 2.0, accn="old", filed="2025-02-15"),
         "fy": 2024, "frame": "CY2024"},
        {**dur("2025-04-01", "2026-03-31", 3.0, accn="k26", filed="2026-05-29"),
         "fy": 2026},
    ]
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", eps)
    s = build(gaap)
    assert 2026 in s.annual_eps and 2025 not in s.annual_eps
    assert s.annual_eps[2026].provenance.period_end == date(2026, 3, 31)


def test_comparative_filing_fy_never_invents_a_future_year_googl_style():
    comparative = [{
        **dur("2014-01-01", "2014-12-31", 1.25, accn="k15", filed="2015-02-15"),
        "fy": 2015,
    }]
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", comparative)
    s = build(gaap)
    assert sorted(s.annual_eps) == [2014]


def test_march_period_ignores_stale_prior_year_fy_crus_style():
    entries = [{
        **dur("2009-04-01", "2010-03-31", 1.0, accn="k10", filed="2010-06-01"),
        "fy": 2009,
    }]
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", entries)
    s = build(gaap)
    assert sorted(s.annual_eps) == [2010]


def test_retail_fiscal_years_use_filers_own_fy_labels():
    # Target-style calendar: year end floats across the Jan/Feb boundary. A pure
    # calendar heuristic mislabels the Feb-ending years; the fy field must win.
    def kdur(start, end, val, fy, filed):
        return {**dur(start, end, val, accn=f"k{fy}", filed=filed), "fy": fy}

    eps = [
        kdur("2021-01-31", "2022-01-29", 2.0, 2021, "2022-03-09"),
        kdur("2022-01-30", "2023-01-28", 2.5, 2022, "2023-03-08"),
        kdur("2023-01-29", "2024-02-03", 3.0, 2023, "2024-03-13"),  # 53-week, ends in Feb
        kdur("2024-02-04", "2025-02-01", 3.5, 2024, "2025-03-12"),  # ends in Feb
        kdur("2025-02-02", "2026-01-31", 4.0, 2025, "2026-03-11"),
        # FY2023 comparative inside the FY2025 10-K: fy overshoots -> min() must keep 2023
        kdur("2023-01-29", "2024-02-03", 3.1, 2025, "2026-03-11"),
    ]
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", eps)
    s = build(gaap)
    assert sorted(s.annual_eps) == [2021, 2022, 2023, 2024, 2025]
    assert float(s.annual_eps[2023].value) == 3.1  # restated value, original label


def test_ttm_refuses_to_mix_pre_and_post_ipo_share_counts():
    """A per-share figure struck on 50M shares cannot be subtracted from one struck
    on 283M. Caris Life Sciences produced +9.58 that way, out of three loss years."""
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", [
        dur("2025-01-01", "2025-12-31", -3.22, accn="k25", filed="2026-02-20"),
        dur("2026-01-01", "2026-06-30", 0.0, form="10-Q", accn="q126", filed="2026-08-05"),
        dur("2025-01-01", "2025-06-30", -12.80, form="10-Q", accn="q126", filed="2026-08-05"),
    ])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2026-01-01", "2026-06-30", 283e6, form="10-Q", accn="q126", filed="2026-08-05"),
        dur("2025-01-01", "2025-06-30", 50e6, form="10-Q", accn="q126", filed="2026-08-05"),
    ])
    s = build(gaap)
    assert s.ttm_eps == Decimal("-3.22"), "must fall back to the audited annual figure"
    assert len(s.ttm_eps_inputs) == 1


def test_ttm_composite_survives_ordinary_buybacks():
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", [
        dur("2025-01-01", "2025-12-31", 6.0, accn="k25", filed="2026-02-20"),
        dur("2026-01-01", "2026-06-30", 3.4, form="10-Q", accn="q126", filed="2026-08-05"),
        dur("2025-01-01", "2025-06-30", 3.0, form="10-Q", accn="q126", filed="2026-08-05"),
    ])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2026-01-01", "2026-06-30", 94e6, form="10-Q", accn="q126", filed="2026-08-05"),
        dur("2025-01-01", "2025-06-30", 100e6, form="10-Q", accn="q126", filed="2026-08-05"),
    ])
    s = build(gaap)
    assert s.ttm_eps == Decimal("6.4"), "a 6% buyback is normal and must not block the composite"
    assert len(s.ttm_eps_inputs) == 3


def test_net_income_follows_a_tag_switch_forward():
    """Advanced Energy stopped tagging NetIncomeLoss after 2024 and continued under
    ProfitLoss. Taking the first tag that returns anything freezes the series."""
    old = [dur(f"{y}-01-01", f"{y}-12-31", 50e6, accn=f"k{y}", filed=f"{y + 1}-02-18")
           for y in range(2018, 2025)]
    new = [dur(f"{y}-01-01", f"{y}-12-31", 60e6, accn=f"p{y}", filed=f"{y + 1}-02-18")
           for y in range(2020, 2026)]
    gaap = dict(GAAP)
    gaap["NetIncomeLoss"] = tagdata("USD", old)
    gaap["ProfitLoss"] = tagdata("USD", new)
    s = build(gaap)
    assert max(s.annual_net_income) == 2025, "must follow the series that is still current"
    assert 2018 in s.annual_net_income, "older years from the dropped tag are kept"


def test_depreciation_series_survives_a_tag_change_midway():
    """A filer that drops the combined tag keeps reporting the parts; the series must
    follow rather than freeze at the year of the change."""
    gaap = {k: v for k, v in OE_GAAP.items() if k != "DepreciationDepletionAndAmortization"}
    gaap["DepreciationDepletionAndAmortization"] = tagdata("USD", [
        dur("2021-01-01", "2021-12-31", 5e9, accn="k21", filed="2022-02-15")])
    gaap["Depreciation"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 9e9, accn="k25", filed="2026-02-15")])
    gaap["AmortizationOfIntangibleAssets"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 3e9, accn="k25", filed="2026-02-15")])
    oe = build(gaap).owner_earnings
    assert oe.fiscal_year == 2025          # not 2021, where the combined tag stopped
    assert float(oe.all_capex_floor.value) == 70e9  # 70 + (9+3) - 12


def test_non_december_filer_keeps_its_own_fiscal_year_label():
    """Dorian LPG pattern: SEC frames a March-ending year by the calendar year it
    mostly falls in, one behind the fiscal year the filer reports. Honouring the frame
    shifted EPS a year against net income — which carries no frames at all — and the
    two series stopped describing the same period."""
    def mar(y, val, filed, frame=None):
        e = dur(f"{y - 1}-04-01", f"{y}-03-31", val, accn=f"k{y}", filed=filed)
        return {**e, "fy": y, **({"frame": f"CY{y - 1}"} if frame else {})}

    eps = [mar(2024, 7.6, "2024-05-29", frame=True),
           mar(2025, 2.14, "2025-05-29", frame=True),
           mar(2026, 4.54, "2026-05-27", frame=True)]
    ni = [mar(2024, 307e6, "2024-05-29"),   # net income is framed by nobody
          mar(2025, 90e6, "2025-05-29"),
          mar(2026, 193e6, "2026-05-27")]
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", eps)
    gaap["NetIncomeLoss"] = tagdata("USD", ni)
    s = build(gaap)
    assert sorted(s.annual_eps)[-3:] == [2024, 2025, 2026]
    # the pair must describe one period: implied share count stays put across years
    for y in (2024, 2025, 2026):
        implied = float(s.annual_net_income[y].value) / float(s.annual_eps[y].value)
        assert 38e6 < implied < 46e6, f"FY{y} implies {implied:,.0f} shares"


def test_january_filer_uses_one_filing_declared_calendar_across_every_series():
    """Veeva pattern: its January close belongs to the year it ends in, while SEC
    calendar frames call the same periods one year earlier on only some tags. The
    filing's FY declaration must align every series and the historical table."""
    def annual(end_year, value, tag_frame=False):
        end = f"{end_year}-01-31"
        entry = dur(f"{end_year - 1}-02-01", end, value,
                    accn=f"k{end_year}", filed=f"{end_year}-03-15")
        return {**entry, "fy": end_year,
                **({"frame": f"CY{end_year - 1}"} if tag_frame else {})}

    eps = [annual(2024, 3.22, True), annual(2025, 4.32, True),
           annual(2026, 5.44, True)]
    income = [annual(2024, 525.705e6), annual(2025, 714.138e6),
              annual(2026, 908.906e6)]
    revenue = [annual(2024, 2.36e9), annual(2025, 2.75e9), annual(2026, 3.10e9)]
    gross = [annual(2024, 1.18e9), annual(2025, 1.65e9), annual(2026, 1.86e9)]
    operating = [annual(2024, 500e6), annual(2025, 620e6), annual(2026, 760e6)]
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", eps),
        "NetIncomeLoss": tagdata("USD", income),
        "Revenues": tagdata("USD", revenue),
        "GrossProfit": tagdata("USD", gross),
        "OperatingIncomeLoss": tagdata("USD", operating),
        "Assets": tagdata("USD", [
            {**inst(f"{y}-01-31", 5e9 + y, form="10-K", accn=f"k{y}",
                    filed=f"{y}-03-15"), "fy": y}
            for y in (2024, 2025, 2026)]),
        "AssetsCurrent": tagdata("USD", [
            {**inst(f"{y}-01-31", 2e9, form="10-K", accn=f"k{y}",
                    filed=f"{y}-03-15"), "fy": y}
            for y in (2024, 2025, 2026)]),
        "LiabilitiesCurrent": tagdata("USD", [
            {**inst(f"{y}-01-31", 1e9, form="10-K", accn=f"k{y}",
                    filed=f"{y}-03-15"), "fy": y}
            for y in (2024, 2025, 2026)]),
    }

    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_operating_income,
        _annual_revenue, annual_ratios, fiscal_year_ends,
    )
    annual_eps = _annual_eps(gaap)
    annual_income = _annual_net_income(gaap)
    annual_sales = _annual_revenue(gaap)
    annual_op = _annual_operating_income(gaap)

    assert sorted(annual_eps) == [2024, 2025, 2026]
    assert sorted(annual_income) == [2024, 2025, 2026]
    assert sorted(annual_sales) == [2024, 2025, 2026]
    assert sorted(annual_op) == [2024, 2025, 2026]
    assert fiscal_year_ends(gaap)[2025] == "2025-01-31"
    ratios = annual_ratios(gaap, annual_income, annual_sales, annual_op,
                           annual_eps=annual_eps)
    assert ratios[2025]["end"] == "2025-01-31"
    assert ratios[2025]["revenue"] == 2.75e9
    assert ratios[2025]["gross_margin"] == 60.0
    assert ratios[2025]["net_margin"] == round(714.138e6 / 2.75e9 * 100, 4)


def test_ratio_history_defaults_to_latest_ten_fiscal_years():
    from screener.normalize import _annual_net_income, _annual_revenue, annual_ratios

    years = range(2015, 2027)
    gaap = {
        "NetIncomeLoss": tagdata("USD", [
            dur(f"{year}-01-01", f"{year}-12-31", 10, accn=f"k{year}",
                filed=f"{year + 1}-02-15") for year in years]),
        "Revenues": tagdata("USD", [
            dur(f"{year}-01-01", f"{year}-12-31", 100, accn=f"k{year}",
                filed=f"{year + 1}-02-15") for year in years]),
        "GrossProfit": tagdata("USD", [
            dur(f"{year}-01-01", f"{year}-12-31", 40, accn=f"k{year}",
                filed=f"{year + 1}-02-15") for year in years]),
        "Assets": tagdata("USD", [
            inst(f"{year}-12-31", 1000, form="10-K", accn=f"k{year}",
                 filed=f"{year + 1}-02-15") for year in years]),
    }

    revenue = _annual_revenue(gaap)
    ratios = annual_ratios(gaap, _annual_net_income(gaap), revenue, {})

    assert list(ratios) == list(range(2017, 2027))
    assert ratios[2026]["revenue"] == 100
    assert ratios[2026]["gross_margin"] == 40


def test_current_january_convention_overrides_inconsistent_old_sec_fy_metadata():
    entries = [
        {**dur("2021-02-01", "2022-01-31", 1.0, accn="k22", filed="2022-03-15"),
         "fy": 2021},
        {**dur("2022-02-01", "2023-01-31", 2.0, accn="k23", filed="2023-03-15"),
         "fy": 2022},
        {**dur("2025-02-01", "2026-01-31", 5.0, accn="k26", filed="2026-03-15"),
         "fy": 2026, "frame": "CY2025"},
    ]
    from screener.normalize import _annual_eps
    series = _annual_eps({"EarningsPerShareDiluted": tagdata("USD/shares", entries)})
    assert sorted(series) == [2022, 2023, 2026]


def test_current_retail_convention_labels_january_with_the_prior_year():
    entries = [
        {**dur("2023-01-29", "2024-01-27", 2.0, accn="k23", filed="2024-03-15"),
         "fy": 2023},
        {**dur("2025-02-02", "2026-01-31", 3.0, accn="k25", filed="2026-03-15"),
         "fy": 2025},
    ]
    from screener.normalize import _annual_eps
    series = _annual_eps({"EarningsPerShareDiluted": tagdata("USD/shares", entries)})
    assert sorted(series) == [2023, 2025]


def test_split_rebases_years_the_filer_never_restated():
    """Chipotle pattern. A 50:1 split restates only the comparatives the newest 10-K
    carries; older years keep a figure struck on a share count that no longer exists,
    and criterion 6 compares straight across that discontinuity."""
    eps = [
        _yr(2020, 12.52, "2021-02-10", "k20"),
        _yr(2021, 22.90, "2022-02-11", "k21"),
        _yr(2022, 32.04, "2023-02-09", "k22"),      # as filed, pre-split
        _yr(2022, 0.64, "2025-02-05", "k24"),       # restated in the post-split 10-K
        _yr(2023, 0.89, "2025-02-05", "k24"),
        _yr(2024, 1.11, "2025-02-05", "k24"),
    ]
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", eps)
    s = build(gaap)
    assert round(float(s.annual_eps[2020].value), 4) == 0.2504   # 12.52 / 50
    assert round(float(s.annual_eps[2021].value), 4) == 0.458
    assert float(s.annual_eps[2024].value) == 1.11               # already post-split


def test_one_split_restated_across_two_filings_is_counted_once():
    """Booking Holdings pattern: successive 10-Qs each restate a different period onto
    the same new basis. Treating those as separate events squared 25:1 into 625."""
    eps = [
        _yr(2023, 100.0, "2024-02-01", "k23"),
        _yr(2024, 120.0, "2025-02-01", "k24"),
        dur("2025-01-01", "2025-06-30", 37.38, form="10-Q", accn="q225", filed="2025-08-01"),
        dur("2025-01-01", "2025-06-30", 1.4952, form="10-Q", accn="q226", filed="2026-08-01"),
        dur("2025-01-01", "2025-03-31", 20.0, form="10-Q", accn="q125", filed="2025-05-01"),
        dur("2025-01-01", "2025-03-31", 0.80, form="10-Q", accn="q126", filed="2026-05-01"),
    ]
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", eps)
    s = build(gaap)
    assert round(float(s.annual_eps[2023].value), 4) == 4.0      # 100 / 25, not / 625
    assert round(float(s.annual_eps[2024].value), 4) == 4.8


def test_filing_that_explicitly_restated_a_split_is_not_adjusted_twice():
    """Zoomcar's FY2025 10-K already reflects the March 2025 1-for-20 split.

    A later quarter is when the same restatement became observable in Company
    Facts.  The detector therefore dates the event after the annual filing, but
    the primary report itself proves the annual comparative is already rebased.
    """
    accession = "0001213900-25-059675"
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            _yr(2024, -3839.73, "2025-06-30", accession),
            dur("2025-04-01", "2025-06-30", -2,
                form="10-Q", accn="q1-original", filed="2025-08-14"),
            dur("2025-04-01", "2025-06-30", -40,
                form="10-Q", accn="q1-restated", filed="2025-11-14"),
        ]),
    }
    from screener.normalize import _annual_eps, _with_fiscal_calendar

    eps = _annual_eps(_with_fiscal_calendar(gaap, cik="0001854275"))

    assert eps[2024].value == Decimal("-3839.73")


def test_two_genuine_splits_a_year_apart_both_apply():
    """Texas Pacific Land split 3:1 twice. Collapsing them would leave the oldest years
    understated by a factor of three."""
    eps = [
        _yr(2021, 90.0, "2022-02-01", "k21"),
        _yr(2022, 57.77, "2023-02-01", "k22"),
        _yr(2022, 19.26, "2025-02-01", "k24"),      # first 3:1
        _yr(2023, 17.59, "2025-02-01", "k24"),
        _yr(2023, 5.86, "2026-02-01", "k25"),       # second 3:1
        _yr(2024, 6.57, "2026-02-01", "k25"),
    ]
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", eps)
    s = build(gaap)
    assert round(float(s.annual_eps[2021].value), 2) == 10.0     # 90 / 9
    assert round(float(s.annual_eps[2024].value), 2) == 6.57


def test_conversion_exchange_ratio_is_not_rounded_into_a_stock_split():
    """Marathon Bancorp restated an old quarter by its 1.3728 conversion ratio.

    Rounded EPS moved from $0.09 to $0.06, which looks exactly like 1.5:1 in
    isolation.  The same-period weighted share count moved by 1.3728 instead and
    must veto that false split; otherwise every annual EPS is divided by 1.5.
    """
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2024-07-01", "2025-06-30", 0.02,
                accn="annual", filed="2025-09-26"),
            dur("2024-07-01", "2024-09-30", 0.09,
                form="10-Q", accn="old-q", filed="2024-11-13"),
            dur("2024-07-01", "2024-09-30", 0.06,
                form="10-Q", accn="conversion-q", filed="2025-11-12"),
        ]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2024-07-01", "2024-09-30", 2_035_131,
                form="10-Q", accn="old-q", filed="2024-11-13"),
            dur("2024-07-01", "2024-09-30", 2_793_828,
                form="10-Q", accn="conversion-q", filed="2025-11-12"),
        ]),
    }
    from screener.normalize import _annual_eps, _with_fiscal_calendar

    wrapped = _with_fiscal_calendar(gaap, cik="0001835385")
    assert _annual_eps(wrapped)[2025].value == Decimal("0.02")


def test_a_quarter_and_a_year_to_date_closing_together_are_not_a_split():
    """Both end on the same day and differ threefold by length alone; comparing them
    across periods rather than within one would read a 3:1 split that never happened."""
    eps = [
        _yr(2024, 4.0, "2025-02-01", "k24"),
        _yr(2025, 5.0, "2026-02-01", "k25"),
        dur("2026-07-01", "2026-09-30", 1.0, form="10-Q", accn="q326", filed="2026-11-01"),
        dur("2026-01-01", "2026-09-30", 3.0, form="10-Q", accn="q326", filed="2026-11-01"),
        dur("2025-01-01", "2025-09-30", 2.7, form="10-Q", accn="q325", filed="2025-11-01"),
    ]
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", eps)
    s = build(gaap)
    assert float(s.annual_eps[2024].value) == 4.0    # untouched
    assert s.ttm_eps == Decimal("5.0") + Decimal("3.0") - Decimal("2.7")


def test_quarters_dwarfing_their_own_year_are_refused():
    """Taboola tags 220.00 and -40.00 a share in quarters of a year that earned 0.13,
    giving a trailing 260.13 and a price/earnings of 0.02. No corporate action explains
    it, so the audited year stands rather than the composite."""
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", [
        dur("2025-01-01", "2025-12-31", 0.13, accn="k25", filed="2026-02-15"),
        dur("2026-01-01", "2026-06-30", 220.0, form="10-Q", accn="q226", filed="2026-08-05"),
        dur("2025-01-01", "2025-06-30", -40.0, form="10-Q", accn="q225", filed="2025-08-05"),
    ])
    s = build(gaap)
    assert float(s.ttm_eps) == 0.13
    assert len(s.ttm_eps_inputs) == 1


def test_a_real_recovery_off_a_small_base_still_composes():
    """The absolute bound exists so the ratio test cannot veto a genuine turnaround."""
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", [
        dur("2025-01-01", "2025-12-31", 0.05, accn="k25", filed="2026-02-15"),
        dur("2026-01-01", "2026-06-30", 2.10, form="10-Q", accn="q226", filed="2026-08-05"),
        dur("2025-01-01", "2025-06-30", 0.10, form="10-Q", accn="q225", filed="2025-08-05"),
    ])
    s = build(gaap)
    assert s.ttm_eps == Decimal("0.05") + Decimal("2.10") - Decimal("0.10")


def test_a_dollar_total_tagged_as_earnings_per_share_is_discarded():
    """GRUSF files 243,446,152 into EarningsPerShareDiluted — its net income, not a
    per-share figure. Carried through, a price divided by it passes criterion 1 on a
    price/earnings of nearly zero."""
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", [
        dur("2024-01-01", "2024-12-31", 209441723.0, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 243446152.0, accn="k25", filed="2026-02-15"),
    ])
    s = build(gaap)
    assert s.annual_eps == {}
    assert s.ttm_eps is None


def test_vintage_ttm_sees_only_what_was_filed():
    from screener.normalize import vintage_ttm_eps
    v = {d: float(x) for d, x in vintage_ttm_eps(GAAP).items()}
    # end of 2025: FY2025's 10-K (filed Feb 2026) is future knowledge; the newest
    # filed figure is FY2024, and Q1'25 has no prior-year comparative to roll with
    assert v["2025-12-31"] == 5.0
    # end of 2024: FY2023 stands at its ORIGINAL 4.0 — the 4.1 restatement was
    # filed in 2025 and must be invisible a year earlier
    assert v["2024-12-31"] == 4.0
    assert v["2022-12-31"] == 3.0   # only the FY2021 10-K existed then
    assert "2021-12-31" not in v    # nothing at all was filed yet


def test_vintage_history_fills_from_the_selected_dimensioned_class():
    """Berkshire's recent Class B EPS lives only on the share-class axis. The
    current screen already selects it; historical P/E must use that same selected
    basis instead of leaving every fiscal-year column blank."""
    gaap = {k: v for k, v in GAAP.items() if k != "EarningsPerShareDiluted"}
    sidecar = dimensioned("EarningsPerShareBasic", "USD/shares", [
        dict(dur("2023-01-01", "2023-12-31", 4.0,
                 accn="k23", filed="2024-02-15"),
             segments="ClassOfStock=EquivalentClassB;"),
        dict(dur("2024-01-01", "2024-12-31", 5.0,
                 accn="k24", filed="2025-02-15"),
             segments="ClassOfStock=EquivalentClassB;"),
    ])

    s = build_snapshot("TEST", "0000000001", facts_doc(gaap), dimensioned=sidecar)

    # At the end of FY2024 only FY2023's report had been filed. This is the same
    # no-look-ahead rule as the undimensioned path, now on the selected class.
    assert s.ttm_eps_vintage["2024-12-31"] == Decimal("4.0")


def test_revenue_series_survives_the_asc606_tag_switch():
    gaap = dict(GAAP)
    gaap["SalesRevenueNet"] = tagdata("USD", REVENUE)
    gaap["RevenueFromContractWithCustomerExcludingAssessedTax"] = tagdata("USD", REVENUE_606)
    s = build(gaap)
    assert {y: f.value for y, f in s.annual_revenue.items()} == {
        2021: 140e9, 2022: 150e9, 2023: 160e9, 2024: 180e9, 2025: 200e9,
    }
    # TTM = FY2025 + Q1'26 - Q1'25
    assert s.ttm_revenue == Decimal("2.2E+11")


def test_no_revenue_tags_leaves_revenue_empty():
    s = build()
    assert s.annual_revenue == {} and s.ttm_revenue is None


def test_operating_income_annual_series():
    gaap = dict(GAAP)
    gaap["OperatingIncomeLoss"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 30e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 35e9, accn="k25", filed="2026-02-15"),
    ])
    s = build(gaap)
    assert {y: f.value for y, f in s.annual_operating_income.items()} == {
        2024: 30e9, 2025: 35e9,
    }


def test_revenue_fill_rejects_a_different_scope():
    """ConAgra pattern: umbrella `Revenues` carries a $1.6B sub-scope while the
    goods element holds the true $13B history — the small series must not fill."""
    gaap = dict(GAAP)
    gaap["RevenueFromContractWithCustomerExcludingAssessedTax"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 12e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 12.5e9, accn="k25", filed="2026-02-15"),
    ])
    gaap["SalesRevenueGoodsNet"] = tagdata("USD", [
        dur("2022-01-01", "2022-12-31", 13e9, accn="k22", filed="2023-02-15"),
        dur("2023-01-01", "2023-12-31", 11e9, accn="k23", filed="2024-02-15"),
    ])
    gaap["Revenues"] = tagdata("USD", [   # wrong scope, an order of magnitude off
        dur("2022-01-01", "2022-12-31", 1.6e9, accn="k22", filed="2023-02-15"),
        dur("2021-01-01", "2021-12-31", 1.5e9, accn="k21", filed="2022-02-15"),
    ])
    s = build(gaap)
    got = {y: f.value for y, f in s.annual_revenue.items()}
    # goods element joins via the continuous 2023 boundary, then extends to 2022;
    # the umbrella's 1.6B is 8x off its neighbour and 2021 never gets a foothold
    assert got == {2022: 13e9, 2023: 11e9, 2024: 12e9, 2025: 12.5e9}


def test_revenue_fill_refuses_a_disconnected_island():
    gaap = dict(GAAP)
    gaap["RevenueFromContractWithCustomerExcludingAssessedTax"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 12e9, accn="k25", filed="2026-02-15"),
    ])
    gaap["SalesRevenueNet"] = tagdata("USD", [   # ends three years before the winner starts
        dur("2020-01-01", "2020-12-31", 11e9, accn="k20", filed="2021-02-15"),
    ])
    s = build(gaap)
    # no adjacent year to prove the scopes match, so the island stays out
    assert sorted(s.annual_revenue) == [2025]


def test_da_part_sum_keeps_amortization_only_years_and_names_both_tags():
    gaap = {k: v for k, v in OE_GAAP.items() if k != "DepreciationDepletionAndAmortization"}
    gaap["Depreciation"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 9e9, accn="k25", filed="2026-02-15")])
    gaap["AmortizationOfIntangibleAssets"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 2e9, accn="k24", filed="2025-02-15"),  # amortization-only year
        dur("2025-01-01", "2025-12-31", 3e9, accn="k25", filed="2026-02-15")])
    s = build(gaap)
    oe = s.owner_earnings
    # 2025 (latest shared year): 70 reported earnings + (9+3) D&A - 12 capex
    assert float(oe.all_capex_floor.value) == 70e9
    da = dict(oe.components)["+ depreciation & amortisation"]
    assert float(da) == 12e9


def test_bdc_investment_income_becomes_the_revenue_series_arcc_style():
    gaap = dict(GAAP)
    gaap["GrossInvestmentIncomeOperating"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 2.9e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 3.052e9, accn="k25", filed="2026-02-15")])
    s = build(gaap)
    assert float(s.annual_revenue[2025].value) == 3.052e9


def test_investment_company_per_share_element_yields_eps_bxsl_style():
    gaap = {k: v for k, v in GAAP.items() if k != "EarningsPerShareDiluted"}
    gaap["InvestmentCompanyInvestmentIncomeLossFromOperationsPerShare"] = tagdata("USD/shares", [
        dur("2024-01-01", "2024-12-31", 3.1, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 3.4, accn="k25", filed="2026-02-15")])
    s = build(gaap)
    assert float(s.annual_eps[2025].value) == 3.4


def test_eps_derived_when_no_per_share_element_exists_kkr_style():
    """KKR tags EPS only on a share-class axis, which Company Facts omits; its
    own income and share count still divide."""
    gaap = {k: v for k, v in GAAP.items() if k != "EarningsPerShareDiluted"}
    gaap["NetIncomeLoss"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 2400e6, accn="k25", filed="2026-02-15")])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2025-01-01", "2025-12-31", 900e6, accn="k25", filed="2026-02-15")])
    s = build(gaap)
    assert round(float(s.annual_eps[2025].value), 4) == round(2400 / 900, 4)
    assert "derived" in s.annual_eps[2025].provenance.concept
    assert s.annual_eps[2025].provenance.tag == (
        "us-gaap:NetIncomeLoss / us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding")


def test_derived_eps_nets_preferred_dividends_first():
    gaap = {k: v for k, v in GAAP.items() if k != "EarningsPerShareDiluted"}
    gaap["NetIncomeLoss"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 1000e6, accn="k25", filed="2026-02-15")])
    gaap["DividendsPreferredStock"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 100e6, accn="k25", filed="2026-02-15")])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2025-01-01", "2025-12-31", 100e6, accn="k25", filed="2026-02-15")])
    assert float(build(gaap).annual_eps[2025].value) == 9.0  # 900M to common, not 1,000M


def test_profit_loss_never_derives_eps_for_a_filer_with_minority_interest_ares_style():
    """ProfitLoss includes noncontrolling interests. Ares tags no MinorityInterest
    at all, so the equity pair is the evidence: EPS would read 2.60 against a
    genuine 3.00-odd — the false-bargain direction."""
    gaap = {k: v for k, v in GAAP.items() if k != "EarningsPerShareDiluted"}
    gaap["ProfitLoss"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 465e6, accn="k25", filed="2026-02-15")])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2025-01-01", "2025-12-31", 180e6, accn="k25", filed="2026-02-15")])
    gaap["StockholdersEquity"] = tagdata("USD", [inst("2026-03-31", 4025e6, accn="q126")])
    gaap["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"] = \
        tagdata("USD", [inst("2026-03-31", 8602e6, accn="q126")])
    assert 2025 not in build(gaap).annual_eps

    # a filer whose equity shows no minority holders keeps the derivation (co-op)
    gaap["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"] = \
        tagdata("USD", [inst("2026-03-31", 4025e6, accn="q126")])
    assert 2025 in build(gaap).annual_eps


def test_a_year_without_its_own_weighted_average_derives_nothing():
    """Measured against reported figures, the count on a report cover produced
    errors of exactly five, ten and a hundred and fifty times: a reverse split
    leaves a small current count against an old year's income, and a
    multi-class cover names one class while the income belongs to all."""
    gaap = {k: v for k, v in GAAP.items() if k != "EarningsPerShareDiluted"}
    gaap["NetIncomeLoss"] = tagdata("USD", [
        dur("2021-01-01", "2021-12-31", 1000e6, accn="k21", filed="2022-02-15")])
    gaap["CommonStockSharesOutstanding"] = tagdata("shares", [
        inst("2026-03-31", 100e6, accn="q126")])
    assert 2021 not in build(gaap).annual_eps


def test_a_reported_eps_is_never_replaced_by_a_derived_one():
    s = build()  # GAAP has EarningsPerShareDiluted for every year
    assert all("derived" not in f.provenance.concept for f in s.annual_eps.values())


def test_a_summed_figure_keeps_every_component_filing():
    gaap = dict(GAAP)
    gaap["LongTermDebtNoncurrent"] = tagdata("USD", [
        inst("2025-12-31", 30e9, form="10-K", accn="k25", filed="2026-02-15")])
    gaap["FinanceLeaseLiabilityNoncurrent"] = tagdata("USD", [inst("2026-03-31", 1e9, accn="q126")])
    parts = build(gaap).long_term_debt.provenance.components
    assert len(parts) == 2
    assert {p.accession for p in parts} == {"k25", "q126"}
    # the summary line's own date belongs to the newest part, which is exactly
    # why the parts must state their own
    assert {str(p.period_end) for p in parts} == {"2025-12-31", "2026-03-31"}


def test_a_negative_revenue_is_not_a_top_line():
    """The gold trusts tag a net investment loss as Revenues; the size test
    would otherwise read a company that sold less than nothing."""
    gaap = dict(GAAP)
    gaap["Revenues"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", -606e6, accn="k25", filed="2026-02-15")])
    s = build(gaap)
    assert 2025 not in s.annual_revenue
    assert s.ttm_revenue is None


def test_a_trailing_figure_anchored_a_decade_back_is_not_trailing():
    gaap = dict(GAAP)
    gaap["Revenues"] = tagdata("USD", [
        dur("2013-01-01", "2013-12-31", 500e6, accn="k13", filed="2014-02-15")])
    s = build(gaap)  # GAAP's balance sheet is 2026
    assert s.annual_revenue[2013] is not None   # the year itself is real history
    assert s.ttm_revenue is None                # but it is not the trailing twelve months


def test_a_lone_share_class_is_the_companys_own_figure_kkr_style():
    """KKR reports earnings per share only under ClassOfStock=CommonStock, so
    Company Facts returns nothing at all for it."""
    gaap = {k: v for k, v in GAAP.items() if k != "EarningsPerShareDiluted"}
    dim = dimensioned("EarningsPerShareDiluted", "USD/shares", [
        classed_entry("2025-01-01", "2025-12-31", 2.34, "ClassOfStock=CommonStock;")])
    s = build_snapshot("TEST", "0000000001", facts_doc(gaap), dimensioned=dim)
    assert float(s.annual_eps[2025].value) == 2.34
    assert s.annual_eps[2025].provenance.segments == "ClassOfStock=CommonStock;"
    assert s.annual_eps.decision_support[2025]['qualification'] == 'NOT_ASSESSED'


def test_a_listed_lp_unit_class_supplies_the_reported_eps_paa_style():
    """PAA reports per-unit EPS only on the standard partnership class axis.
    Company Facts drops it, but the cover names Common Units and the DERA sidecar
    carries the exact filed figure. A preferred cover must not inherit it."""
    from screener.normalize import _annual_eps, _unambiguous_dimensioned

    sidecar = {"facts": {"us-gaap": {
        "NetIncomeLossNetOfTaxPerOutstandingLimitedPartnershipUnitDiluted": {
            "units": {"USD/shares": [dict(
                dur("2025-01-01", "2025-12-31", 1.66, accn="paa-k25",
                    filed="2026-02-27"),
                segments="LimitedPartnersCapitalAccountByClass=CommonUnits;",
            )]}},
        # A member elsewhere in the same filing lets the cover prove that Series
        # B is a different listed security, even though EPS itself has only common.
        "ProfitLoss": {"units": {"USD": [dict(
            dur("2025-01-01", "2025-12-31", 10, accn="paa-k25",
                filed="2026-02-27"),
            segments="LimitedPartnersCapitalAccountByClass=SeriesBPreferredUnits;",
        )]}},
    }}}

    common = _annual_eps(_unambiguous_dimensioned(sidecar, "Common Units"))
    assert float(common[2025].value) == 1.66
    assert common[2025].provenance.segments.endswith("=CommonUnits;")

    preferred = _annual_eps(_unambiguous_dimensioned(
        sidecar, "9.50% Series B Fixed Rate Cumulative Redeemable"))
    assert preferred == {}


def test_continuing_operations_eps_never_falls_back_to_an_ancient_share_basis():
    """ZWS's current EPS is continuing-operations income. Skipping it and walking
    back to FY2009 compared a 17-year-old 69M count with today's 167M, blocking a
    valid current TTM P/E. An ineligible newest year is an abstention."""
    gaap = {
        "IncomeLossFromContinuingOperationsPerDilutedShare": tagdata("USD/shares", [
            dur("2025-01-01", "2025-12-31", 1.12, accn="k25", filed="2026-02-09")]),
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2009-01-01", "2009-12-31", 0.50, accn="k09", filed="2010-02-09")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2009-01-01", "2009-12-31", 34.6e6, accn="k09", filed="2010-02-09"),
            dur("2025-01-01", "2025-12-31", 107e6, accn="k25", filed="2026-02-09"),
        ]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2009-01-01", "2009-12-31", 69.2e6, accn="k09", filed="2010-02-09"),
            dur("2025-01-01", "2025-12-31", 171.258e6, accn="k25", filed="2026-02-09"),
        ]),
    }
    from screener.normalize import (
        _annual_eps, _annual_net_income, _basis_conflict, _implied_shares,
    )
    eps, income = _annual_eps(gaap), _annual_net_income(gaap)
    shares = build_snapshot("ZWS", "0001439288", facts_doc({
        **gaap,
        "Assets": tagdata("USD", [inst("2026-06-30", 1e9)]),
        "CommonStockSharesOutstanding": tagdata("shares", [inst("2026-06-30", 167e6)]),
    })).shares_outstanding

    assert _basis_conflict(gaap, {}, eps, income, {}, False, shares) is None
    assert _implied_shares(gaap, eps, income) is None


def test_a_consolidated_figure_always_outranks_a_classed_one():
    dim = dimensioned("EarningsPerShareDiluted", "USD/shares", [
        classed_entry("2025-01-01", "2025-12-31", 99.0, "ClassOfStock=CommonStock;")])
    s = build_snapshot("TEST", "0000000001", facts_doc(GAAP), dimensioned=dim)
    assert float(s.annual_eps[2025].value) == 6.0   # the undimensioned fact stands


def test_coca_colas_allowance_is_a_seventh_of_its_deferred_assets():
    """KO reserves 388M against 5,514M at 2025-12-31 — the ordinary case, and
    the counterexample that keeps the note from firing on every filer."""
    gaap = deferred_tax_gaap(
        DeferredTaxAssetsGross=[k25("2025-12-31", 5514e6)],
        DeferredTaxAssetsValuationAllowance=[k25("2025-12-31", 388e6)],
    )
    assert not any("valuation allowance" in n for n in texts(build(gaap)))


def test_a_partnership_never_divides_the_whole_groups_profit_by_its_own_units():
    """Westlake Chemical Partners consolidates an OpCo whose sponsor owns two thirds of
    it. The corporate equity pair the guard reads does not exist for a partnership: WLKP
    files PartnersCapital 253.7M inside 769.4M including the minority. FY2019 ProfitLoss
    is 332,895k against 60,981k attributable to unitholders on 34,488,058 units, and the
    10-K reports $1.77 a unit (accn 0001604665-22-000009), not the $9.65 that shipped."""
    gaap = {k: v for k, v in GAAP.items() if k != "EarningsPerShareDiluted"}
    gaap["ProfitLoss"] = tagdata("USD", [
        dur("2019-01-01", "2019-12-31", 332_895_000, accn="k21", filed="2022-03-02")])
    gaap["NetIncomeLoss"] = tagdata("USD", [
        dur("2019-01-01", "2019-12-31", 60_981_000, accn="k21", filed="2022-03-02")])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2019-01-01", "2019-12-31", 34_488_058, accn="k21", filed="2022-03-02")])
    gaap["PartnersCapital"] = tagdata("USD", [inst("2026-03-31", 253_713_000, accn="q126")])
    gaap["PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest"] = tagdata(
        "USD", [inst("2026-03-31", 769_413_000, accn="q126")])
    eps = build(gaap).annual_eps[2019]
    assert round(float(eps.value), 2) == 1.77


def test_a_canadian_filers_earnings_are_not_read_as_dollars():
    """Enbridge, Canadian Pacific and Imperial Oil tag earnings per share only in
    CAD/shares. Taking whatever unit came first divided a New York price by a Canadian
    figure and understated every P/E by the exchange rate."""
    gaap = {k: v for k, v in GAAP.items() if k != "EarningsPerShareDiluted"}
    gaap["EarningsPerShareDiluted"] = tagdata("CAD/shares", [
        dur("2025-01-01", "2025-12-31", 3.22, accn="k25", filed="2026-02-15")])
    s = build(gaap)
    assert s.annual_eps == {} and s.ttm_eps is None


def test_the_cover_names_which_share_class_the_ticker_is():
    """Hershey reports earnings per share for its Common Stock and its Class B in the
    same filing. Company Facts drops both because they are dimensioned, and the
    ambiguity rule refuses both because there are two — so its series stopped in 2014
    and criterion 4 was decided on a decade-old window. The cover of its own filing
    names the one the symbol registers: "Common Stock, one dollar par value"."""
    facts = facts_doc({k: v for k, v in GAAP.items() if k != "EarningsPerShareDiluted"})
    dimensioned = {"facts": {"us-gaap": {"EarningsPerShareDiluted": {"units": {"USD/shares": [
        dict(dur("2025-01-01", "2025-12-31", 4.34, accn="k25", filed="2026-02-15"),
             segments="ClassOfStock=CommonStock"),
        dict(dur("2025-01-01", "2025-12-31", 4.05, accn="k25", filed="2026-02-15"),
             segments="ClassOfStock=CommonClassB"),
    ]}}}}}
    blind = build_snapshot("HSY", "0000047111", facts, dimensioned=dimensioned)
    assert 2025 not in blind.annual_eps, "two classes and no cover: the refusal must stand"

    told = build_snapshot("HSY", "0000047111", facts, dimensioned=dimensioned,
                          receipt={"title": "Common Stock, one dollar par value"})
    assert float(told.annual_eps[2025].value) == 4.34

    # a cover naming a class that matches neither member changes nothing
    other = build_snapshot("HSY", "0000047111", facts, dimensioned=dimensioned,
                           receipt={"title": "Class C Capital Stock"})
    assert 2025 not in other.annual_eps


def test_a_receipt_rebases_every_per_security_figure_and_no_dollar_total():
    """One receipt represents two ordinary shares: all per-security history moves
    together, while entity income and preferred dividends remain company totals."""
    gaap = dict(GAAP)
    gaap["CommonStockDividendsPerShareDeclared"] = tagdata("USD/shares", [
        dur("2025-01-01", "2025-03-31", 0.25, form="10-Q", accn="q125"),
        dur("2025-04-01", "2025-06-30", 0.25, form="10-Q", accn="q225"),
        dur("2025-07-01", "2025-09-30", 0.25, form="10-Q", accn="q325"),
        dur("2025-10-01", "2025-12-31", 0.25, accn="k25", filed="2026-02-15"),
        dur("2025-01-01", "2025-12-31", 1.25, accn="k25", filed="2026-02-15")])
    facts = facts_doc(gaap)
    ordinary = build_snapshot("ADR", "0000000001", facts)
    receipt = build_snapshot(
        "ADR", "0000000001", facts,
        receipt={"ratio": "2", "accn": "cover-1", "title": "ADS, each representing 2 shares"},
    )

    assert receipt.shares_outstanding.value == ordinary.shares_outstanding.value / 2
    assert receipt.ttm_eps == ordinary.ttm_eps * 2
    assert receipt.annual_eps[2025].value == ordinary.annual_eps[2025].value * 2
    assert receipt.dividend_per_share == ordinary.dividend_per_share * 2
    assert (receipt.recurring_dividend_per_share.value
            == ordinary.recurring_dividend_per_share.value * 2)
    assert receipt.ttm_eps_vintage == {k: v * 2 for k, v in ordinary.ttm_eps_vintage.items()}
    assert [f.value for f in receipt.ttm_eps_inputs] == [
        f.value * 2 for f in ordinary.ttm_eps_inputs]
    assert receipt.ttm_net_income == ordinary.ttm_net_income
    assert receipt.ttm_preferred_dividends == ordinary.ttm_preferred_dividends


def test_a_fractional_receipt_ratio_rebases_in_the_same_direction():
    """HKD-style receipts can represent less than one ordinary share; the same
    underlying-shares-per-receipt identity still applies."""
    facts = facts_doc(GAAP)
    ordinary = build_snapshot("ADR", "0000000001", facts)
    receipt = build_snapshot(
        "ADR", "0000000001", facts,
        receipt={"ratio": "0.4", "accn": "cover-1",
                 "title": "ADS, each representing 0.4 ordinary shares"},
    )

    assert receipt.shares_outstanding.value == ordinary.shares_outstanding.value / Decimal("0.4")
    assert receipt.ttm_eps == ordinary.ttm_eps * Decimal("0.4")
    assert receipt.annual_eps[2025].value == ordinary.annual_eps[2025].value * Decimal("0.4")


def test_an_ads_dimension_without_a_ratio_withholds_the_security_basis():
    """AMRN states both ordinary-share and ADS EPS, about 20x apart, while its
    tagged cover names only the symbol. The difference detects the problem but
    does not authorize guessing the legal receipt ratio."""
    gaap = dict(GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", [
        dur("2025-01-01", "2025-12-31", -0.09, accn="k25", filed="2026-03-02")])
    dim = dimensioned("EarningsPerShareDiluted", "USD/shares", [dict(
        dur("2025-01-01", "2025-12-31", -1.87, accn="k25", filed="2026-03-02"),
        segments="ClassOfStock=AmericanDepositaryShare;",
    )])

    s = build_snapshot(
        "AMRN", "0000897448", facts_doc(gaap), dimensioned=dim,
        receipt={"symbol": "AMRN", "title": "", "ratio": None, "accn": "k25"},
    )

    assert "per ordinary share" in s.basis_conflict
    assert "per depositary share" in s.basis_conflict


def test_fiscal_years_end_when_the_company_says_they_do():
    """Microsoft's fiscal 2026 closed 2026-06-30. Reading it as 2026-12-31 asked the
    price history for a December that has not arrived, so the newest P/E column was
    blank for 206 companies, and every older column divided a June balance sheet
    into the following December's market."""
    from screener.normalize import fiscal_year_ends
    ends = fiscal_year_ends(JUNE_FILER)
    assert ends[2025] == "2025-06-30"
    assert ends[2026] == "2026-06-30"


def test_vintage_earnings_are_cut_at_those_same_ends():
    """One helper feeds both series, so the keys cannot drift apart — and the newest
    fiscal year now HAS a figure, which is what the blank column was missing."""
    from screener.normalize import vintage_ttm_eps
    v = {d: float(x) for d, x in vintage_ttm_eps(JUNE_FILER).items()}
    assert "2026-06-30" in v          # the year the December cutoff could not reach
    # no look-ahead: the FY2026 10-K was filed in August, so a reader on 2026-06-30
    # had only the trailing four quarters through Q1 — 13.0 + 3.5 - 3.0
    assert v["2026-06-30"] == 13.5
    assert not any(d.endswith("-12-31") for d in v)


def test_an_eps_its_own_filing_contradicts_is_replaced():
    """The Eastern Company's 2022 10-K tags $1.76 against fiscal 2020 while leaving
    that year's income and share count untouched at $5,405,522 on 6,264,521 shares —
    $0.86. A per-share figure moving while the two numbers it is made of stand still
    is a mis-tag wearing a restatement's clothes, and it withheld Eastern's P/E."""
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2019-12-29", "2021-01-02", 0.86, accn="k20", filed="2021-03-16"),
            dur("2019-12-29", "2021-01-02", 1.76, accn="k21", filed="2022-03-17"),
        ]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2019-12-29", "2021-01-02", 5405522, accn="k20", filed="2021-03-16"),
            dur("2019-12-29", "2021-01-02", 5405522, accn="k21", filed="2022-03-17"),
        ]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2019-12-29", "2021-01-02", 6264521, accn="k20", filed="2021-03-16"),
            dur("2019-12-29", "2021-01-02", 6264521, accn="k21", filed="2022-03-17"),
        ]),
    }
    from screener.normalize import _annual_eps
    assert float(_annual_eps(gaap)[2020].value) == 0.86


def test_a_real_restatement_moves_the_income_with_it_and_is_kept():
    """Gyre's fiscal 2022 went from -$8.2M on 31.5M shares to +$4.3M on 75.7M after a
    reverse merger. Both inputs moved, so the newer per-share figure supersedes the
    older one and must survive — the check only fires when they stand still."""
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2022-01-01", "2022-12-31", -0.26, accn="k22", filed="2023-03-30"),
            dur("2022-01-01", "2022-12-31", 0.03, accn="k23", filed="2024-03-27"),
        ]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2022-01-01", "2022-12-31", -8242000, accn="k22", filed="2023-03-30"),
            dur("2022-01-01", "2022-12-31", 4314000, accn="k23", filed="2024-03-27"),
        ]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2022-01-01", "2022-12-31", 31545723, accn="k22", filed="2023-03-30"),
            dur("2022-01-01", "2022-12-31", 75686406, accn="k23", filed="2024-03-27"),
        ]),
    }
    from screener.normalize import _annual_eps
    assert float(_annual_eps(gaap)[2022].value) == 0.03


def test_a_common_class_eps_replaces_a_proven_mis_scaled_bare_fact(monkeypatch):
    """MOBX's FY2025 bare EPS is -10.10, but the same filing reports a $46M
    loss on 45.5M shares and -1.01 for listed Class A. The filing's arithmetic,
    not the dimension by itself, proves which same-tag value is usable."""
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2023-10-01", "2024-09-30", -7.50, accn="k25",
                filed="2026-01-13"),
            dur("2025-01-01", "2025-12-31", -10.10, accn="k25",
                filed="2026-01-13")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2023-10-01", "2024-09-30", -20_034_000, accn="k25",
                filed="2026-01-13"),
            dur("2025-01-01", "2025-12-31", -45_919_754, accn="k25",
                filed="2026-01-13")]),
        "NetIncomeLossAvailableToCommonStockholdersBasic": tagdata("USD", [
            dur("2023-10-01", "2024-09-30", -20_695_000, accn="k25",
                filed="2026-01-13")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2023-10-01", "2024-09-30", 29_483_021, accn="k25",
                filed="2026-01-13"),
            dur("2025-01-01", "2025-12-31", 45_465_103, accn="k25",
                filed="2026-01-13")]),
        "Assets": tagdata("USD", [inst("2025-12-31", 100e6, form="10-K", accn="k25")]),
        "CommonStockSharesOutstanding": tagdata("shares", [inst("2025-12-31", 45e6)]),
    }
    dim = dimensioned("EarningsPerShareDiluted", "USD/shares", [
        dict(dur("2023-10-01", "2024-09-30", -0.75, accn="k25",
                 filed="2026-01-13"), segments="ClassOfStock=CommonClassA;"),
        dict(dur("2025-01-01", "2025-12-31", -1.01, accn="k25",
                 filed="2026-01-13"), segments="ClassOfStock=CommonClassA;"),
    ])

    from screener import normalize as current
    original = current._annual_eps
    gathered = []
    def collect(*args, **kwargs):
        series = original(*args, **kwargs)
        gathered.append(series)
        return series
    monkeypatch.setattr(current, '_annual_eps', collect)
    s = build_snapshot(
        "MOBX", "0001855467", facts_doc(gaap), dimensioned=dim,
        receipt={"title": "Class A Common Stock, par value $0.00001 per share"},
    )

    assert float(s.annual_eps[2024].value) == -0.75
    assert float(s.annual_eps[2025].value) == -1.01
    assert s.annual_eps[2025].provenance.segments == "ClassOfStock=CommonClassA;"
    classed = next(series for series in gathered
                   if 2025 in series and series[2025].provenance.segments == 'ClassOfStock=CommonClassA;')
    assert s.annual_eps.decision_support[2025] is classed.decision_support[2025]
    assert s.annual_eps.decision_support[2025]['qualification'] == 'NOT_ASSESSED'


def test_a_preferred_dividend_is_not_a_contradiction():
    """Markel's fiscal 2020 EPS of $55.63 divides to $59.03 on its own net income,
    and the $47M gap is its preferred dividend — earnings per share is struck on
    income available to the common. The same abstention `_basis_conflict` makes."""
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2020-01-01", "2020-12-31", 55.63, accn="k20", filed="2021-02-19")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2020-01-01", "2020-12-31", 816030000, accn="k20", filed="2021-02-19")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2020-01-01", "2020-12-31", 13823000, accn="k20", filed="2021-02-19")]),
        "PreferredStockDividendsAndOtherAdjustments": tagdata("USD", [
            dur("2020-01-01", "2020-12-31", 47060000, accn="k20", filed="2021-02-19")]),
    }
    from screener.normalize import _annual_eps
    assert float(_annual_eps(gaap)[2020].value) == 55.63


def test_income_available_to_common_does_not_redefine_parent_profit():
    gaap = {
        "EarningsPerShareBasic": tagdata("USD/shares", [
            dur("2025-01-01", "2025-12-31", -35.71, accn="k25", filed="2026-06-15")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2025-01-01", "2025-12-31", -6486000, accn="k25", filed="2026-06-15")]),
        "NetIncomeLossAvailableToCommonStockholdersDiluted": tagdata("USD", [
            dur("2025-01-01", "2025-12-31", -17128000, accn="k25", filed="2026-06-15")]),
        "WeightedAverageNumberOfSharesOutstandingBasic": tagdata("shares", [
            dur("2025-01-01", "2025-12-31", 479613, accn="k25", filed="2026-06-15")]),
    }

    from screener.normalize import _annual_net_income

    income = _annual_net_income(gaap)

    assert float(income[2025].value) == -6486000
    assert "NetIncomeLoss" in income[2025].provenance.tag


def test_statement_share_scale_is_reconciled_from_same_filing_arithmetic():
    """McDonald's states shares in millions but exposes 716.4 under the plain
    `shares` unit. EPS, income and count from one accession prove the multiplier."""
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2025-01-01", "2025-12-31", 11.95, accn="k25", filed="2026-02-24")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2025-01-01", "2025-12-31", 8563e6, accn="k25", filed="2026-02-24")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2025-01-01", "2025-12-31", 716.4, accn="k25", filed="2026-02-24")]),
    }
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts, _basis_conflict,
    )
    eps, income = _annual_eps(gaap), _annual_net_income(gaap)
    counts = _annual_share_counts(gaap, {}, eps, income)

    assert counts[2025].value == Decimal("716400000.0")
    assert "scaled 1000000x" in counts[2025].provenance.concept
    assert _basis_conflict(gaap, {}, eps, income, {}, False) is None


def test_eps_sign_can_use_direct_common_income_even_with_nci():
    """Neonode's common numerator settles a bad positive loss-per-share sign.

    The consolidated parent has NCI, but the same filing separately reports the
    common loss.  Once the sign is corrected, that filing also proves that its
    displayed 9,989 weighted shares mean 9.989 million.
    """
    accession = "0001213900-22-011462"
    gaap = {
        "EarningsPerShareBasicAndDiluted": tagdata("USD/shares", [
            dur("2020-01-01", "2020-12-31", 0.56,
                accn=accession, filed="2022-03-10")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2020-01-01", "2020-12-31", -6_282_000,
                accn=accession, filed="2022-03-10")]),
        "NetIncomeLossAvailableToCommonStockholdersBasic": tagdata("USD", [
            dur("2020-01-01", "2020-12-31", -5_638_000,
                accn=accession, filed="2022-03-10")]),
        "WeightedAverageNumberOfSharesOutstandingBasic": tagdata("shares", [
            dur("2020-01-01", "2020-12-31", 9_989,
                accn=accession, filed="2022-03-10")]),
        "MinorityInterest": tagdata("USD", [
            inst("2020-12-31", 1_000_000, accn=accession)]),
    }
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts,
        _with_fiscal_calendar,
    )

    gaap = _with_fiscal_calendar(gaap, cik="0000087050")
    eps, income = _annual_eps(gaap), _annual_net_income(gaap)
    counts = _annual_share_counts(gaap, {}, eps, income)

    assert eps[2020].value == Decimal("-0.56")
    assert "primary statement presentation verified" in eps[2020].provenance.concept
    assert counts[2020].value == Decimal("9989000")


def test_common_income_xbrl_sign_does_not_override_unreviewed_eps():
    """Same-filing arithmetic is insufficient because either sign can be bad."""
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2025-01-01", "2025-12-31", 0.34,
                accn="unreviewed", filed="2026-03-01")]),
        "NetIncomeLossAvailableToCommonStockholdersDiluted": tagdata("USD", [
            dur("2025-01-01", "2025-12-31", -3_400_000,
                accn="unreviewed", filed="2026-03-01")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2025-01-01", "2025-12-31", 10_000_000,
                accn="unreviewed", filed="2026-03-01")]),
    }
    from screener.normalize import _annual_eps

    assert _annual_eps(gaap)[2025].value == Decimal("0.34")


def test_share_comparative_scale_does_not_use_current_count_as_period_proof():
    """Today's count cannot decide the basis of an older comparative.

    Issuance, splits, classes and depositary ratios can all move the count by an
    exact-looking factor.  Without filing-local arithmetic or a reviewed primary
    statement, the latest same-period observation remains untouched.
    """
    gaap = {
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2018-01-01", "2018-12-31", 6_000_000,
                accn="k18", filed="2019-03-01"),
            dur("2019-01-01", "2019-12-31", 7_000_000,
                accn="k19", filed="2020-03-01"),
            dur("2019-01-01", "2019-12-31", 7_000,
                accn="k20-bad", filed="2021-03-01"),
            dur("2020-01-01", "2020-12-31", 8_000_000,
                accn="k20", filed="2021-03-01"),
            dur("2021-01-01", "2021-12-31", 8_200,
                accn="k21-bad", filed="2022-03-01"),
            dur("2021-01-01", "2021-12-31", 8_200_000,
                accn="k22-fixed", filed="2023-03-01"),
            dur("2022-01-01", "2022-12-31", 8_400_000,
                accn="k22-fixed", filed="2023-03-01"),
        ]),
        "CommonStockSharesOutstanding": tagdata("shares", [
            inst("2022-12-31", 8_500_000, accn="k22-fixed")]),
    }
    from screener.normalize import _annual_share_counts

    counts = _annual_share_counts(gaap, {}, {}, {})

    assert counts[2019].value == Decimal("7000")
    assert counts[2019].provenance.accession == "k20-bad"
    assert counts[2021].value == Decimal("8200000")
    assert counts[2021].provenance.accession == "k22-fixed"


def test_filing_local_rounded_eps_proves_a_thousands_share_scale():
    """A cent-rounded EPS is tested as a displayed value, not false precision."""
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2019-01-01", "2019-12-31", -0.04,
                accn="k21", filed="2022-03-01")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2019-01-01", "2019-12-31", -20_812_000,
                accn="k21", filed="2022-03-01")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2018-01-01", "2018-12-31", 440_016_000,
                accn="k20", filed="2021-03-01"),
            dur("2019-01-01", "2019-12-31", 564_188,
                accn="k21", filed="2022-03-01"),
            dur("2020-01-01", "2020-12-31", 725_129_000,
                accn="k21", filed="2022-03-01"),
        ]),
    }
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts,
    )

    eps, income = _annual_eps(gaap), _annual_net_income(gaap)
    counts = _annual_share_counts(gaap, {}, eps, income)

    assert counts[2019].value == Decimal("564188000")
    assert "reconciled to EPS and income" in counts[2019].provenance.concept


def test_zero_diluted_count_falls_back_but_contradictory_eps_is_withheld():
    """A loss year's zero diluted denominator is missing, never zero evidence."""
    accession = "0001654954-19-005469"
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2017-01-01", "2017-12-31", 0,
                accn=accession, filed="2019-04-01")]),
        "EarningsPerShareBasic": tagdata("USD/shares", [
            dur("2017-01-01", "2017-12-31", 997.64,
                accn=accession, filed="2019-04-01")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2017-01-01", "2017-12-31", -10_696_000,
                accn=accession, filed="2019-04-01")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2017-01-01", "2017-12-31", 0,
                accn=accession, filed="2019-04-01")]),
        "WeightedAverageNumberOfSharesOutstandingBasic": tagdata("shares", [
            dur("2017-01-01", "2017-12-31", 11,
                accn=accession, filed="2019-04-01")]),
    }
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts,
        _with_fiscal_calendar,
    )

    gaap = _with_fiscal_calendar(gaap, cik="0000924515")
    eps, income = _annual_eps(gaap), _annual_net_income(gaap)
    counts = _annual_share_counts(gaap, {}, eps, income)

    assert 2017 not in eps
    assert counts[2017].value == Decimal("11000")
    assert "primary statement share presentation verified" in (
        counts[2017].provenance.concept)


def test_share_scale_uses_raw_filing_eps_before_later_split_restatement():
    """A later split changes history EPS, not the old filing's own identity.

    PACCAR's FY2016 report says 351.8 shares in millions, $1.48 EPS and
    $521.7M net income. Its split-adjusted history carries $0.9867 EPS, so the
    selected public series cannot be the evidence used to recover the scale.
    """
    from dataclasses import replace
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts,
    )

    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2016-01-01", "2016-12-31", 1.48,
                accn="k18", filed="2019-02-26")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2016-01-01", "2016-12-31", 521.7e6,
                accn="k18", filed="2019-02-26")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2016-01-01", "2016-12-31", 351.8,
                accn="k18", filed="2019-02-26")]),
    }
    filed_eps = _annual_eps(gaap)
    split_adjusted = {2016: replace(
        filed_eps[2016], value=Decimal("0.9866666666666667"))}

    counts = _annual_share_counts(
        gaap, {}, split_adjusted, _annual_net_income(gaap))

    assert counts[2016].value == Decimal("351800000.0")
    assert "scaled 1000000x" in counts[2016].provenance.concept


def test_share_scale_reconciliation_repairs_an_over_scaled_raw_fact():
    """Northern Trust's renderer displays 224,053,430 shares while its raw
    comparative carries 224,053,430,000,000 under the plain shares unit."""
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts,
    )

    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2008-01-01", "2008-12-31", 3.47,
                accn="k10", filed="2011-02-25")]),
        "NetIncomeLossAvailableToCommonStockholdersBasic": tagdata("USD", [
            dur("2008-01-01", "2008-12-31", 776.5e6,
                accn="k10", filed="2011-02-25")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2007-01-01", "2007-12-31", 223_079_180,
                accn="k09", filed="2010-02-26"),
            dur("2008-01-01", "2008-12-31", 224_053_430_000_000,
                accn="k10", filed="2011-02-25")]),
    }

    counts = _annual_share_counts(
        gaap, {}, _annual_eps(gaap), _annual_net_income(gaap))

    assert counts[2008].value == Decimal("224053430.000000")
    assert "scaled 0.000001x" in counts[2008].provenance.concept


def test_a_bad_income_eps_pair_cannot_shrink_a_continuous_share_count():
    """An exact three-cell identity does not by itself identify the bad cell.

    TCI's surrounding reports keep the weighted count near 8.1M; a filing-local
    loss/EPS pair implying 8.1 thousand therefore cannot rescale that count.
    """
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts,
    )

    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2011-01-01", "2011-12-31", -8.41,
                accn="bad", filed="2013-03-29")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2011-01-01", "2011-12-31", -67_196,
                accn="bad", filed="2013-03-29")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2010-01-01", "2010-12-31", 8_220_000,
                accn="prior", filed="2012-03-30"),
            dur("2011-01-01", "2011-12-31", 8_113_575,
                accn="bad", filed="2013-03-29"),
            dur("2012-01-01", "2012-12-31", 8_370_729,
                accn="next", filed="2014-03-28"),
        ]),
    }

    counts = _annual_share_counts(
        gaap, {}, _annual_eps(gaap), _annual_net_income(gaap))

    assert counts[2011].value == Decimal("8113575")
    assert "scaled" not in counts[2011].provenance.concept


def test_reconciled_income_scale_cannot_be_transferred_into_share_count():
    """GSUN's bad comparative income must not make its good count 1,000x larger.

    The later 20-F repeats the prior $2.139M loss as $2.139B.  Annual income has
    already retained the corroborated original value, so the malformed raw
    numerator is not independent evidence for changing 1.443M filed shares.
    """
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts,
        _with_fiscal_calendar,
    )

    bad_accession = "0001213900-25-013985"
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2021-10-01", "2022-09-30", -1.48,
                accn=bad_accession, filed="2025-02-14")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2020-10-01", "2021-09-30", -8_800_000,
                accn="income-21", filed="2022-03-01"),
            dur("2021-10-01", "2022-09-30", -2_139_320,
                accn="income-22-original", filed="2023-03-01"),
            dur("2021-10-01", "2022-09-30", -2_139_320,
                accn="income-22-repeated", filed="2024-03-01"),
            dur("2021-10-01", "2022-09-30", -2_139_320_000,
                accn=bad_accession, filed="2025-02-14"),
            dur("2022-10-01", "2023-09-30", -1_800_000,
                accn="income-23", filed="2024-03-01"),
        ]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2021-10-01", "2022-09-30", 1_443_316,
                accn=bad_accession, filed="2025-02-14")]),
    }
    gaap = _with_fiscal_calendar(gaap, cik="0001826376")
    eps, income = _annual_eps(gaap), _annual_net_income(gaap)

    counts = _annual_share_counts(gaap, {}, eps, income)

    assert income[2022].value == Decimal("-2139320")
    assert counts[2022].value == Decimal("1443316")
    assert "scaled" not in counts[2022].provenance.concept


def test_diluted_share_scale_can_use_same_filing_if_converted_numerator():
    """SBFG's diluted EPS adds convertible preferred dividends back.

    Its primary FY2017 statement prints all dollar figures and share counts in
    thousands: $7.619M total income, $6.663M basic common income, 6.423M diluted
    shares and $1.19 diluted EPS.  The basic numerator must not hide the exact
    diluted identity carried by the same report.
    """
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts,
    )

    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2015-01-01", "2015-12-31", 1.19,
                accn="0001213900-18-002757", filed="2018-03-09")]),
        "NetIncomeLossAvailableToCommonStockholdersBasic": tagdata("USD", [
            dur("2015-01-01", "2015-12-31", 6_663_000,
                accn="0001213900-18-002757", filed="2018-03-09")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2015-01-01", "2015-12-31", 7_619_000,
                accn="0001213900-18-002757", filed="2018-03-09")]),
        "PreferredStockDividendsAndOtherAdjustments": tagdata("USD", [
            dur("2015-01-01", "2015-12-31", 956_000,
                accn="0001213900-18-002757", filed="2018-03-09")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2015-01-01", "2015-12-31", 6_423,
                accn="0001213900-18-002757", filed="2018-03-09")]),
    }
    eps, income = _annual_eps(gaap), _annual_net_income(gaap)

    counts = _annual_share_counts(gaap, {}, eps, income)

    assert counts[2015].value == Decimal("6423000")
    assert "scaled 1000x" in counts[2015].provenance.concept


def test_direct_common_identity_blocks_malformed_preferred_fallback():
    """A bad preferred-dividend fact cannot inflate NOTVQ's sound 7.96M count."""
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts,
    )

    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2013-10-01", "2014-09-30", -0.13,
                accn="notvq-14", filed="2015-12-01")]),
        "NetIncomeLossAvailableToCommonStockholdersBasic": tagdata("USD", [
            dur("2013-10-01", "2014-09-30", -1_070_000,
                accn="notvq-14", filed="2015-12-01")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2013-10-01", "2014-09-30", -1_070_000,
                accn="notvq-14", filed="2015-12-01")]),
        "DividendsPreferredStock": tagdata("USD", [
            dur("2013-10-01", "2014-09-30", 991_080_000,
                accn="notvq-14", filed="2015-12-01")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2013-10-01", "2014-09-30", 7_960_000,
                accn="notvq-14", filed="2015-12-01")]),
    }
    eps, income = _annual_eps(gaap), _annual_net_income(gaap)

    counts = _annual_share_counts(gaap, {}, eps, income)

    assert counts[2014].value == Decimal("7960000")
    assert "scaled" not in counts[2014].provenance.concept


def test_rounded_eps_recovers_scale_without_inventing_false_precision():
    """CCBG's $0.01 EPS can still prove that 17,220 means 17.220M.

    $108K / 17.220M is $0.0063, which the statement legitimately displays as
    one cent.  Testing the displayed EPS interval recovers the exact thousands
    scale without pretending that the rounded cent is infinitely precise.
    """
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts,
    )

    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2011-01-01", "2011-12-31", 0.29,
                accn="k11", filed="2012-03-01"),
            dur("2012-01-01", "2012-12-31", 0.01,
                accn="k12", filed="2013-03-01"),
            dur("2013-01-01", "2013-12-31", 0.59,
                accn="k13", filed="2014-03-01"),
        ]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2011-01-01", "2011-12-31", 4_897_000,
                accn="k11", filed="2012-03-01"),
            dur("2012-01-01", "2012-12-31", 108_000,
                accn="k12", filed="2013-03-01"),
            dur("2013-01-01", "2013-12-31", 10_265_000,
                accn="k13", filed="2014-03-01"),
        ]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2011-01-01", "2011-12-31", 17_140_000,
                accn="k11", filed="2012-03-01"),
            dur("2012-01-01", "2012-12-31", 17_220,
                accn="k12", filed="2013-03-01"),
            dur("2013-01-01", "2013-12-31", 17_399_000,
                accn="k13", filed="2014-03-01"),
        ]),
    }
    eps, income = _annual_eps(gaap), _annual_net_income(gaap)

    counts = _annual_share_counts(gaap, {}, eps, income)

    assert counts[2012].value == Decimal("17220000")
    assert "reconciled to EPS and income" in counts[2012].provenance.concept


def test_berkshire_old_derived_history_is_put_on_class_b_basis():
    """Berkshire's statement expressly makes one Class A share equal to 1,500
    Class B shares. Old whole-company arithmetic must use the priced B basis."""
    from screener.models import Fact, Provenance
    from screener.normalize import _restate_verified_equivalent_class_history

    source = Provenance(
        concept="EarningsPerShare (derived: earnings available to common / share count)",
        tag="us-gaap:NetIncomeLoss / us-gaap:WeightedAverageNumberOfSharesOutstandingBasic",
        fiscal_year=2009, form="10-K", accession="0001193125-12-079022",
        filed=date(2012, 2, 27), period_end=date(2009, 12, 31),
        period_start=date(2009, 1, 1),
    )
    count_source = Provenance(
        concept="WeightedAverageNumberOfSharesOutstandingBasic",
        tag="us-gaap:WeightedAverageNumberOfSharesOutstandingBasic",
        fiscal_year=2009, form="10-K", accession="0001193125-12-079022",
        filed=date(2012, 2, 27), period_end=date(2009, 12, 31),
        period_start=date(2009, 1, 1),
    )

    eps, counts = _restate_verified_equivalent_class_history(
        ("0001067983", "BRK-B"),
        {2009: Fact(Decimal("5193"), source)},
        {2009: Fact(Decimal("1551174"), count_source)},
    )

    assert eps[2009].value == Decimal("3.462")
    assert counts[2009].value == Decimal("2326761000")
    assert "Class A to Class B basis" in eps[2009].provenance.concept


def test_wrong_common_income_scale_cannot_rescale_a_correct_share_count():
    """KFFB's common-income tag is 1,000x above its printed net income.

    The filed EPS already reconciles the ordinary NetIncomeLoss and the raw
    weighted count, which proves the count without trusting the malformed
    common-income sibling.
    """
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2023-07-01", "2024-06-30", -0.21,
                accn="0001213900-25-093967", filed="2025-09-30")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2023-07-01", "2024-06-30", -1721000,
                accn="0001213900-25-093967", filed="2025-09-30")]),
        "NetIncomeLossAvailableToCommonStockholdersDiluted": tagdata("USD", [
            dur("2023-07-01", "2024-06-30", -1721000000,
                accn="0001213900-25-093967", filed="2025-09-30")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2023-07-01", "2024-06-30", 8098715,
                accn="0001213900-25-093967", filed="2025-09-30")]),
    }
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts,
        _common_owner_earnings, _with_fiscal_calendar,
    )

    gaap = _with_fiscal_calendar(gaap, cik="0001297341")
    eps, income = _annual_eps(gaap), _annual_net_income(gaap)
    counts = _annual_share_counts(gaap, {}, eps, income)

    assert counts[2024].value == Decimal("8098715")
    assert "scaled" not in counts[2024].provenance.concept
    owner = _common_owner_earnings(gaap)[2024]
    assert owner.value == Decimal("-1721000")
    assert owner.provenance.tag == "us-gaap:NetIncomeLoss"


def test_verified_statement_share_scale_handles_rounded_one_cent_eps():
    """Zion labels the count itself as thousands; one-cent EPS is too rounded
    for the ordinary income/EPS identity to prove the exact multiplier."""
    gaap = {
        "EarningsPerShareBasic": tagdata("USD/shares", [
            dur("2025-01-01", "2025-12-31", -0.01,
                accn="0001437749-26-009073", filed="2026-03-19")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2025-01-01", "2025-12-31", -7627000,
                accn="0001437749-26-009073", filed="2026-03-19")]),
        "WeightedAverageNumberOfSharesOutstandingBasic": tagdata("shares", [
            dur("2025-01-01", "2025-12-31", 1083276,
                accn="0001437749-26-009073", filed="2026-03-19")]),
    }
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts,
        _with_fiscal_calendar,
    )

    gaap = _with_fiscal_calendar(gaap, cik="0001131312")
    counts = _annual_share_counts(
        gaap, {}, _annual_eps(gaap), _annual_net_income(gaap))

    assert counts[2025].value == Decimal("1083276000")
    assert "primary statement share presentation verified" in (
        counts[2025].provenance.concept)


def test_verified_statement_share_scale_can_reduce_a_pre_ipo_comparative():
    """BCAX's primary statement settles the old denominator despite the IPO.

    A current-count continuity heuristic would reject the genuine 580,109-share
    pre-IPO denominator because the 2024 IPO expanded the company to millions of
    common shares. The exact reviewed accession is therefore the proof.
    """
    accession = "0002023658-25-000012"
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2023-01-01", "2023-12-31", -89.61,
                accn=accession, filed="2025-03-27")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2023-01-01", "2023-12-31", -51_985_000,
                accn=accession, filed="2025-03-27")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2023-01-01", "2023-12-31", 580_109_000,
                accn=accession, filed="2025-03-27")]),
    }
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts,
        _with_fiscal_calendar,
    )

    gaap = _with_fiscal_calendar(gaap, cik="0002023658")
    counts = _annual_share_counts(
        gaap, {}, _annual_eps(gaap), _annual_net_income(gaap))

    assert counts[2023].value == Decimal("580109.000")
    assert "primary statement share presentation verified" in (
        counts[2023].provenance.concept)


def test_verified_statement_share_scale_is_exactly_accession_scoped():
    """A reviewed PAGS comparative does not authorize a company-wide guess."""
    gaap = {
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2021-01-01", "2021-12-31", 332_174_824_000,
                accn="0001628280-24-018744", filed="2024-04-25"),
            dur("2022-01-01", "2022-12-31", 329_234_693,
                accn="unreviewed-accession", filed="2025-04-25"),
        ]),
    }
    from screener.normalize import _annual_share_counts, _with_fiscal_calendar

    gaap = _with_fiscal_calendar(gaap, cik="0001712807")
    counts = _annual_share_counts(gaap, {}, {}, {})

    assert counts[2021].value == Decimal("332174824.000")
    assert counts[2022].value == Decimal("329234693")
    assert "scaled" not in counts[2022].provenance.concept


def test_verified_cross_tag_eps_scale_uses_the_primary_statement_value():
    """Zion's FY2021 primary 10-K says $(0.04); the next comparative's
    Inline-XBRL says $(40) under a different EPS tag."""
    gaap = {
        "EarningsPerShareBasicAndDiluted": tagdata("USD/shares", [
            dur("2020-01-01", "2020-12-31", -0.04,
                accn="k20", filed="2021-03-24")]),
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2021-01-01", "2021-12-31", -40,
                accn="0001213900-23-023118", filed="2023-03-27"),
            dur("2022-01-01", "2022-12-31", -0.12,
                accn="k22", filed="2024-03-20")]),
    }
    from screener.normalize import _annual_eps, _with_fiscal_calendar

    eps = _annual_eps(_with_fiscal_calendar(gaap, cik="0001131312"))

    assert eps[2021].value == Decimal("-0.040")
    assert "primary statement per-share presentation verified" in (
        eps[2021].provenance.concept)


def test_verified_eps_scale_is_exactly_accession_scoped():
    """ADSE's reviewed FY2020 comparative cannot authorize a broad EPS guess."""
    gaap = {
        "EarningsPerShareBasic": tagdata("USD/shares", [
            dur("2020-01-01", "2020-12-31", -320.86,
                accn="0001213900-23-038592", filed="2023-05-11", form="20-F"),
            dur("2021-01-01", "2021-12-31", -3.46,
                accn="unreviewed-accession", filed="2024-05-11", form="20-F"),
        ]),
    }
    from screener.normalize import _annual_eps, _with_fiscal_calendar

    eps = _annual_eps(_with_fiscal_calendar(gaap, cik="0001879248"))

    assert eps[2020].value == Decimal("-0.32086")
    assert eps[2021].value == Decimal("-3.46")
    assert "primary statement per-share presentation verified" in (
        eps[2020].provenance.concept)
    assert "scaled" not in eps[2021].provenance.concept


def test_verified_monetary_table_scale_repairs_income_and_operating_profit():
    """TCI's primary table states both rows in thousands, beside $8.41 EPS."""
    accession = "0001010549-13-000233"
    gaap = {
        "NetIncomeLoss": tagdata("USD", [
            dur("2010-01-01", "2010-12-31", -67_196,
                accn=accession, filed="2013-03-28")]),
        "OperatingIncomeLoss": tagdata("USD", [
            dur("2010-01-01", "2010-12-31", -16_337,
                accn=accession, filed="2013-03-28")]),
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2010-01-01", "2010-12-31", -8.41,
                accn=accession, filed="2013-03-28")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2010-01-01", "2010-12-31", 8_113_575,
                accn=accession, filed="2013-03-28")]),
    }
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_operating_income,
        _annual_share_counts, _with_fiscal_calendar,
    )

    gaap = _with_fiscal_calendar(gaap, cik="0000733590")
    income = _annual_net_income(gaap)
    operating = _annual_operating_income(gaap)
    counts = _annual_share_counts(gaap, {}, _annual_eps(gaap), income)

    assert income[2010].value == Decimal("-67196000")
    assert operating[2010].value == Decimal("-16337000")
    assert counts[2010].value == Decimal("8113575")
    assert "primary statement monetary presentation verified" in (
        income[2010].provenance.concept)


def test_verified_monetary_scale_is_exactly_tag_and_accession_scoped():
    gaap = {
        "NetIncomeLoss": tagdata("USD", [
            dur("2010-01-01", "2010-12-31", -67_196,
                accn="0001010549-13-000233", filed="2013-03-28"),
            dur("2011-01-01", "2011-12-31", -46_603,
                accn="unreviewed-accession", filed="2014-03-28"),
        ]),
        "ProfitLoss": tagdata("USD", [
            dur("2012-01-01", "2012-12-31", -12_345,
                accn="0001010549-13-000233", filed="2015-03-28"),
        ]),
    }
    from screener.normalize import _annual_net_income, _with_fiscal_calendar

    income = _annual_net_income(_with_fiscal_calendar(gaap, cik="0000733590"))

    assert income[2010].value == Decimal("-67196000")
    assert income[2011].value == Decimal("-46603")
    assert income[2012].value == Decimal("-12345")


def test_verified_amendment_scale_can_restore_a_loss_sign():
    accession = "0001193125-25-107562"
    gaap = {"NetIncomeLoss": tagdata("USD", [
        dur("2020-01-01", "2020-12-31", 428_700,
            accn=accession, filed="2025-04-30", form="10-K/A"),
    ])}
    from screener.normalize import _annual_net_income, _with_fiscal_calendar

    income = _annual_net_income(_with_fiscal_calendar(gaap, cik="0001498710"))

    assert income[2020].value == Decimal("-428700000")
    assert "sign-corrected" in income[2020].provenance.concept


def test_verified_apptech_comparative_restores_money_scale_and_loss_sign():
    accession = "0001903596-23-000201"
    gaap = {
        "GrossProfit": tagdata("USD", [
            dur("2021-01-01", "2021-12-31", 204,
                accn=accession, filed="2023-03-24")]),
        "OperatingIncomeLoss": tagdata("USD", [
            dur("2021-01-01", "2021-12-31", 77_320,
                accn=accession, filed="2023-03-24")]),
    }
    from screener.normalize import (
        _annual_gross_profit, _annual_operating_income, _with_fiscal_calendar,
    )

    gaap = _with_fiscal_calendar(gaap, cik="0001070050")
    gross = _annual_gross_profit(gaap)
    operating = _annual_operating_income(gaap)

    assert gross[2021].value == Decimal("204000")
    assert operating[2021].value == Decimal("-77320000")
    assert "sign-corrected" in operating[2021].provenance.concept


def test_verified_amendment_cannot_replace_repeated_income_at_a_million_x():
    """Identiv's amendment has no replacement income statement, while its
    machine comparative is one million times three identical annual reports."""
    gaap = {"NetIncomeLoss": tagdata("USD", [
        dur("2021-01-01", "2021-12-31", 1620000,
            accn="k21", filed="2022-03-14"),
        dur("2021-01-01", "2021-12-31", 1620000,
            accn="k22", filed="2023-03-16"),
        dur("2021-01-01", "2021-12-31", 1620000000000,
            accn="0001193125-24-122187", filed="2024-04-29", form="10-K/A"),
    ])}
    from screener.normalize import _annual_net_income

    income = _annual_net_income(gaap)

    assert income[2021].value == Decimal("1620000")
    assert "exact 1000000x presentation-scale contradiction" in (
        income[2021].provenance.concept)


def test_dover_continuing_eps_repairs_the_comparative_share_scale():
    """Dover's FY2011 10-K prints FY2009 diluted shares as 186,736 in
    thousands and continuing earnings of 373,423 thousand beside $2.00 EPS.

    The later comparative XBRL fact lost the table scale and reached Company
    Facts as 186,736 shares.  The same accession's continuing-income numerator
    proves 186,736,000 without mixing discontinued operations into the check.
    """
    gaap = {
        "IncomeLossFromContinuingOperationsPerDilutedShare": tagdata(
            "USD/shares", [dur("2009-01-01", "2009-12-31", 2.00,
                               accn="0000029905-12-000008", filed="2012-02-10")]),
        "IncomeLossFromContinuingOperations": tagdata(
            "USD", [dur("2009-01-01", "2009-12-31", 373_423_000,
                        accn="0000029905-12-000008", filed="2012-02-10")]),
        "NetIncomeLoss": tagdata(
            "USD", [dur("2009-01-01", "2009-12-31", 356_438_000,
                        accn="0000029905-12-000008", filed="2012-02-10")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata(
            "shares", [dur("2009-01-01", "2009-12-31", 186_736,
                           accn="0000029905-12-000008", filed="2012-02-10")]),
    }
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts,
    )

    eps, income = _annual_eps(gaap), _annual_net_income(gaap)
    counts = _annual_share_counts(gaap, {}, eps, income)

    assert counts[2009].value == Decimal("186736000")
    assert "scaled 1000x" in counts[2009].provenance.concept
    assert {part.tag for part in counts[2009].provenance.components} == {
        "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
        "us-gaap:IncomeLossFromContinuingOperationsPerDilutedShare",
        "us-gaap:IncomeLossFromContinuingOperations",
    }

    # A continuing-operations EPS cannot be checked against total net income.
    # Without its own numerator the wrong scale stays visible rather than guessed.
    del gaap["IncomeLossFromContinuingOperations"]
    counts = _annual_share_counts(gaap, {}, eps, income)
    assert counts[2009].value == Decimal("186736")


def test_non_decimal_security_basis_mismatch_is_not_scaled_away():
    """A 13x ADR/class mismatch is not a thousands-or-millions presentation scale."""
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2025-01-01", "2025-12-31", 11.95, accn="k25", filed="2026-02-24")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2025-01-01", "2025-12-31", 8563e6, accn="k25", filed="2026-02-24")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2025-01-01", "2025-12-31", 55e6, accn="k25", filed="2026-02-24")]),
    }
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts, _basis_conflict,
    )
    eps, income = _annual_eps(gaap), _annual_net_income(gaap)
    counts = _annual_share_counts(gaap, {}, eps, income)

    assert counts[2025].value == Decimal("55000000.0")
    assert _basis_conflict(gaap, {}, eps, income, {}, False) is not None


def test_the_parents_own_profit_outranks_a_longer_group_series():
    """Starwood Property Trust files fourteen years of NetIncomeLoss — $411.5M, what
    its printed income statement calls "Net income attributable to Starwood Property
    Trust" — beside sixteen years of ProfitLoss, the group's $443.1M including the
    minority holders. Ranking by depth let two extra years of history swap one for
    the other, and every margin and per-share figure then divided profit the
    shareholders do not own."""
    gaap = {
        "NetIncomeLoss": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 411.5e6, accn=f"k{y}", filed=f"{y+1}-02-15")
            for y in range(2012, 2026)]),
        "ProfitLoss": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 443.1e6, accn=f"k{y}", filed=f"{y+1}-02-15")
            for y in range(2010, 2026)]),
    }
    from screener.normalize import _annual_net_income
    series = _annual_net_income(gaap)
    assert float(series[2025].value) == 411.5e6
    assert "NetIncomeLoss" in series[2025].provenance.tag


def test_a_dead_parent_series_still_yields_to_a_live_group_one():
    """Advanced Energy's NetIncomeLoss stops while ProfitLoss runs on. Recency is
    still decided first: a figure of the right scope that stopped years ago is
    worse than a live one of the wrong scope, which is at least disclosed."""
    gaap = {
        "NetIncomeLoss": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 100e6, accn=f"k{y}", filed=f"{y+1}-02-15")
            for y in range(2015, 2024)]),
        "ProfitLoss": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 120e6, accn=f"k{y}", filed=f"{y+1}-02-15")
            for y in range(2015, 2026)]),
    }
    from screener.normalize import _annual_net_income
    assert float(_annual_net_income(gaap)[2025].value) == 120e6


def test_revenue_takes_the_total_over_its_own_contract_component():
    """Ovintiv files $8,663M of contract revenue beside $8,908M of `Revenues`, and
    its income statement prints the second as "Total Revenues" — the difference is
    revenue that did not come from a contract with a customer."""
    gaap = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 8663e6, accn=f"k{y}", filed=f"{y+1}-02-15")
            for y in range(2020, 2026)]),
        "Revenues": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 8908e6, accn=f"k{y}", filed=f"{y+1}-02-15")
            for y in range(2020, 2026)]),
    }
    from screener.normalize import _annual_revenue
    assert float(_annual_revenue(gaap)[2025].value) == 8908e6


def test_regulated_utility_revenue_continues_after_generic_tag_stops():
    """ONE Gas stopped tagging `Revenues` after FY2022 and continued the same
    consolidated top line as `RegulatedOperatingRevenue`. The utility tag must
    carry FY2023-FY2025 into both historical margin denominators."""
    gaap = {
        "Revenues": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", value, accn=f"k{y}",
                filed=f"{y+1}-02-20")
            for y, value in ((2020, 1530268e3), (2021, 1808597e3), (2022, 2578005e3))
        ]),
        "RegulatedOperatingRevenue": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", value, accn=f"k{y}",
                filed=f"{y+1}-02-20")
            for y, value in (
                (2020, 1530268e3), (2021, 1808597e3), (2022, 2578005e3),
                (2023, 2371990e3), (2024, 2083558e3), (2025, 2427428e3),
            )
        ]),
    }
    from screener.normalize import _annual_revenue
    series = _annual_revenue(gaap)

    assert {year: float(series[year].value) for year in (2023, 2024, 2025)} == {
        2023: 2371990e3,
        2024: 2083558e3,
        2025: 2427428e3,
    }
    assert series[2025].provenance.tag == "us-gaap:RegulatedOperatingRevenue"


def test_regulated_and_unregulated_total_outranks_its_regulated_part():
    """DTE's regulated-only component has more history than the utility group's
    combined top line. Scope outranks that extra depth when both reach today."""
    gaap = {
        "RegulatedAndUnregulatedOperatingRevenue": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 15e9, accn=f"k{y}",
                filed=f"{y+1}-02-20")
            for y in range(2016, 2026)
        ]),
        "RegulatedOperatingRevenue": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 9e9, accn=f"k{y}",
                filed=f"{y+1}-02-20")
            for y in range(2013, 2026)
        ]),
    }
    from screener.normalize import _annual_revenue
    series = _annual_revenue(gaap)

    assert float(series[2025].value) == 15e9
    assert series[2025].provenance.tag == (
        "us-gaap:RegulatedAndUnregulatedOperatingRevenue"
    )


def test_and_the_other_way_round_when_the_umbrella_tag_is_the_narrow_one():
    """Ares files the reverse: $5,601M of contract revenue against a $4,756M
    `Revenues` covering less than its own statement's "Total revenues". Whichever
    element holds the bigger figure is the total; the other is a piece of it."""
    gaap = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 5601482e3, accn=f"k{y}", filed=f"{y+1}-02-15")
            for y in range(2020, 2026)]),
        "Revenues": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 4755618e3, accn=f"k{y}", filed=f"{y+1}-02-15")
            for y in range(2020, 2026)]),
    }
    from screener.normalize import _annual_revenue
    assert float(_annual_revenue(gaap)[2025].value) == 5601482e3


def test_sales_tax_collected_for_the_state_is_never_revenue():
    """The comparison is only ever between a total and its own part. The assessed-tax
    pair does not stand in that relation, and taking the larger of THOSE would count
    sales taxes collected for the state as the company's own revenue."""
    gaap = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 1000e6, accn=f"k{y}", filed=f"{y+1}-02-15")
            for y in range(2020, 2026)]),
        "RevenueFromContractWithCustomerIncludingAssessedTax": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 1080e6, accn=f"k{y}", filed=f"{y+1}-02-15")
            for y in range(2020, 2026)]),
    }
    from screener.normalize import _annual_revenue
    assert float(_annual_revenue(gaap)[2025].value) == 1000e6


def test_a_brokers_top_line_is_revenue_net_of_interest_expense():
    """Interactive Brokers reports $7,782M of gross interest income against $6,205M
    of revenue net of interest expense — the figure its own income statement totals
    to. The component is the larger of the two, so no comparison by size can rank
    them; the concepts do it."""
    gaap = {
        "InterestIncomeOperating": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 7782e6, accn=f"k{y}", filed=f"{y+1}-02-15")
            for y in range(2018, 2026)]),
        "RevenuesNetOfInterestExpense": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 6205e6, accn=f"k{y}", filed=f"{y+1}-02-15")
            for y in range(2020, 2026)]),
    }
    from screener.normalize import _annual_revenue
    series = _annual_revenue(gaap)
    assert float(series[2025].value) == 6205e6


def test_interest_income_still_stands_where_there_is_no_net_line():
    """A filer that reports interest income and nothing wider keeps it: the rule
    replaces a component with its own total, never with nothing."""
    gaap = {
        "InterestIncomeOperating": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 500e6, accn=f"k{y}", filed=f"{y+1}-02-15")
            for y in range(2020, 2026)]),
    }
    from screener.normalize import _annual_revenue
    assert float(_annual_revenue(gaap)[2025].value) == 500e6


def test_verified_nonoperating_interest_is_not_published_as_revenue():
    """Bion's 10-K prints no revenue and $62 of interest below operations.

    Its Company Facts record calls that InterestIncomeOperating.  The exact
    filing is withheld instead of turning non-operating interest into sales or
    inventing a zero-valued fact without provenance.
    """
    gaap = {
        "InterestIncomeOperating": tagdata("USD", [
            dur("2024-07-01", "2025-06-30", 62,
                accn="0001079973-25-001516", filed="2025-09-29"),
        ]),
    }
    from screener.normalize import _annual_revenue, _with_fiscal_calendar

    wrapped = _with_fiscal_calendar(gaap, cik="0000875729")
    assert _annual_revenue(wrapped) == {}


def test_newer_basic_only_filing_supersedes_obsolete_diluted_comparative():
    """A post-split annual report can stop tagging diluted EPS altogether.

    Flash Sports' current report restates FY2024 basic EPS to -$73.12.  Keeping
    -$2.62 from the older diluted-only report and mechanically applying the
    reverse split produces -$65.50, a number no current statement reports.
    """
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2024-01-01", "2024-12-31", -2.62,
                accn="old-k", filed="2026-01-16"),
        ]),
        "EarningsPerShareBasic": tagdata("USD/shares", [
            dur("2024-01-01", "2024-12-31", -73.12,
                accn="0001213900-26-046211", filed="2026-04-21"),
            dur("2025-01-01", "2025-12-31", -41.83,
                accn="0001213900-26-046211", filed="2026-04-21"),
        ]),
    }
    from screener.normalize import _annual_eps, _with_fiscal_calendar

    series = _annual_eps(_with_fiscal_calendar(gaap, cik="0001706524"))
    assert series[2024].value == Decimal("-73.12")
    assert series[2024].provenance.accession == "0001213900-26-046211"


def test_sales_tax_cannot_be_larger_than_the_sale():
    """Thirty filers tag an assessed-tax pair no rate of tax explains — Precision
    Optics at 2.2x, SS Innovations at exactly 1000x, which is a units error wearing
    a revenue tag. The wider element is then not revenue.

    Dropping it matters beyond the choice: the sub-scope guard anchors on the
    LARGEST candidate, so one inflated element pushes every honest one below the
    threshold. Precision Optics' own $24.2M was being discarded as a scrap beside a
    $53.5M figure it never earned."""
    gaap = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 24.246e6, accn=f"k{y}") for y in range(2020, 2026)]),
        "RevenueFromContractWithCustomerIncludingAssessedTax": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 53.492e6, accn=f"k{y}") for y in range(2020, 2026)]),
    }
    from screener.normalize import _annual_revenue
    assert float(_annual_revenue(gaap)[2025].value) == 24.246e6


def test_a_real_sales_tax_leaves_the_pair_whole_and_the_chain_decides():
    """The check is a ceiling on the absurd, not a rule against the element. With a
    genuine 7% tax both elements stay, and the chain's own ordering then prefers the
    excluding one — which is right on its own terms: sales tax collected for the
    state was never the company's revenue."""
    gaap = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 100e6, accn=f"k{y}") for y in range(2020, 2026)]),
        "RevenueFromContractWithCustomerIncludingAssessedTax": tagdata("USD", [
            dur(f"{y}-01-01", f"{y}-12-31", 107e6, accn=f"k{y}") for y in range(2020, 2026)]),
    }
    from screener.normalize import _annual_revenue
    assert float(_annual_revenue(gaap)[2025].value) == 100e6   # excluding the tax


def test_a_retail_year_is_labelled_the_way_its_own_filer_labels_it():
    """A year ending 2026-02-01 is fiscal 2025 to SEC's frame and 2026 to the month
    it ends in. The earnings reader used the first and the balance-sheet reader the
    second, so GameStop, Kohl's and Macy's printed the same date against two years
    and paired one year's balance sheet with another year's earnings."""
    end, prior = "2026-01-31", "2025-02-01"
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2025-02-02", end, 3.0, accn="k25"),
            dur("2024-02-04", prior, 2.0, accn="k24")]),
        "Assets": tagdata("USD", [
            inst(end, 5e9, form="10-K", accn="k25"),
            inst(prior, 4e9, form="10-K", accn="k24")]),
        "AssetsCurrent": tagdata("USD", [
            inst(end, 2e9, form="10-K", accn="k25"),
            inst(prior, 1.8e9, form="10-K", accn="k24")]),
    }
    from screener.normalize import fiscal_year_ends
    ends = fiscal_year_ends(gaap)
    assert ends[2025] == end and ends[2024] == prior
    # ...and one date is claimed by one year only, never by two
    assert len(set(ends.values())) == len(ends)


def test_old_calendar_dates_do_not_shift_the_current_filing_calendar():
    """MAMA retains two old December periods alongside its January fiscal years.
    They cannot all fit in the company map, but their absence must not make the
    operating-income tag abandon the current filing's FY2024/FY2025 labels."""
    from screener.normalize import _FiscalTaxonomy, _annual_series

    current = [
        {**dur("2023-02-01", "2024-01-31", 8.890e6,
               accn="k26", filed="2026-04-14"), "fy": 2026, "frame": "CY2023"},
        {**dur("2024-02-01", "2025-01-31", 4.877e6,
               accn="k26", filed="2026-04-14"), "fy": 2026, "frame": "CY2024"},
        {**dur("2025-02-01", "2026-01-31", 7.112e6,
               accn="k26", filed="2026-04-14"), "fy": 2026, "frame": "CY2025"},
    ]
    gaap = _FiscalTaxonomy({"OperatingIncomeLoss": tagdata("USD", [
        dur("2011-01-01", "2011-12-31", 1e6, accn="old11"),
        dur("2012-01-01", "2012-12-31", 2e6, accn="old12"),
        *current,
    ])})
    gaap.fiscal_labels = {
        "2024-01-31": 2024,
        "2025-01-31": 2025,
        "2026-01-31": 2026,
    }

    series = _annual_series(gaap, "OperatingIncomeLoss", unit=("USD",))

    assert float(series[2024].value) == 8.890e6
    assert float(series[2025].value) == 4.877e6
    assert float(series[2026].value) == 7.112e6


def test_tag_local_calendar_collision_does_not_shift_shared_statement_years():
    """Value Line carries two calendar-year NetIncomeLoss contexts beside its
    April fiscal years.  Those isolated dates must lose to the company-wide
    statement calendar instead of shifting every April result backward a year."""
    from screener.normalize import _FiscalTaxonomy, _annual_series

    correct = [
        {**dur("2018-05-01", "2019-04-30", 12.009e6,
               accn="k20", filed="2020-07-29"), "fy": 2020},
        {**dur("2019-05-01", "2020-04-30", 14.943e6,
               accn="k20", filed="2020-07-29"), "fy": 2020},
        {**dur("2021-05-01", "2022-04-30", 23.822e6,
               accn="k24", filed="2024-07-26"), "fy": 2024},
        {**dur("2022-05-01", "2023-04-30", 18.069e6,
               accn="k24", filed="2024-07-26"), "fy": 2024},
        {**dur("2023-05-01", "2024-04-30", 19.016e6,
               accn="k24", filed="2024-07-26"), "fy": 2024},
    ]
    calendar_noise = [
        {**dur("2019-01-01", "2019-12-31", 12.009e6,
               accn="old", filed="2021-07-28"), "frame": "CY2019"},
        {**dur("2020-01-01", "2020-12-31", 14.943e6,
               accn="old", filed="2021-07-28"), "frame": "CY2020"},
    ]
    gaap = _FiscalTaxonomy({
        "NetIncomeLoss": tagdata("USD", [*calendar_noise, *correct]),
    })
    gaap.fiscal_labels = {
        "2019-04-30": 2019,
        "2020-04-30": 2020,
        "2022-04-30": 2022,
        "2023-04-30": 2023,
        "2024-04-30": 2024,
    }

    series = _annual_series(gaap, "NetIncomeLoss", unit=("USD",))

    assert {year: float(fact.value) for year, fact in series.items()} == {
        2019: 12.009e6,
        2020: 14.943e6,
        2022: 23.822e6,
        2023: 18.069e6,
        2024: 19.016e6,
    }


def test_unmatched_element_date_cannot_reuse_a_company_wide_fiscal_year():
    """A predecessor/calendar context on one tag must not enter beside a different
    company-wide year end merely because that tag lacks the winning date."""
    from screener.normalize import _FiscalTaxonomy, _annual_series

    gaap = _FiscalTaxonomy({
        "GrossProfit": tagdata("USD", [
            dur("2023-01-01", "2023-12-31", 129.707e6,
                accn="predecessor", filed="2024-02-23"),
        ]),
    })
    gaap.fiscal_labels = {"2023-09-30": 2023}

    assert _annual_series(gaap, "GrossProfit", unit=("USD",)) == {}


def test_nearby_unmatched_element_date_keeps_the_shared_fiscal_year():
    """A statement row may end a few days from the dominant 52/53-week date.
    It is the same fiscal year, not a reason to erase that row from history."""
    from screener.normalize import _FiscalTaxonomy, _annual_series

    gaap = _FiscalTaxonomy({
        "GrossProfit": tagdata("USD", [
            dur("2021-11-28", "2022-11-26", 248.339e6,
                accn="k25", filed="2025-02-14"),
        ]),
    })
    gaap.fiscal_labels = {"2022-11-25": 2022}

    series = _annual_series(gaap, "GrossProfit", unit=("USD",))

    assert series[2022].value == Decimal("248339000")
    assert series[2022].provenance.period_end == date(2022, 11, 26)


def test_note_only_calendar_dates_cannot_hijack_the_statement_calendar():
    """Caleres' statements end in January/February while annual note contexts
    also end on December 31.  A note date cannot evict the audited statement."""
    from screener.normalize import _with_fiscal_calendar

    gaap = {
        "NetIncomeLoss": tagdata("USD", [
            dur("2017-01-29", "2018-02-03", 87e6,
                accn="k18", filed="2018-03-30"),
            dur("2018-02-04", "2019-02-02", -5e6,
                accn="k19", filed="2019-03-29"),
        ]),
        "OperatingLeaseCost": tagdata("USD", [
            dur("2017-01-01", "2017-12-31", 10e6,
                accn="note18", filed="2018-03-30"),
            dur("2018-01-01", "2018-12-31", 11e6,
                accn="note19", filed="2019-03-29"),
        ]),
    }

    wrapped = _with_fiscal_calendar(gaap)

    assert wrapped.fiscal_labels == {
        "2018-02-03": 2018,
        "2019-02-02": 2019,
    }


def test_early_january_53_week_end_does_not_skip_a_fiscal_year():
    """A late-December year followed 371 days later by early January crosses two
    calendar numbers, but it is still one ordinary successive fiscal year."""
    from screener.normalize import _with_fiscal_calendar

    gaap = {
        "NetIncomeLoss": tagdata("USD", [
            {**dur("2023-12-31", "2024-12-28", -336e6,
                   accn="k24", filed="2025-02-28"), "fy": 2024},
            {**dur("2024-12-29", "2026-01-03", 44e6,
                   accn="k25", filed="2026-02-27"), "fy": 2026},
        ]),
    }

    wrapped = _with_fiscal_calendar(gaap)

    assert wrapped.fiscal_labels == {
        "2024-12-28": 2024,
        "2026-01-03": 2025,
    }


def test_each_historical_early_january_close_keeps_its_local_frame():
    """A later March calendar must not shift old Saturday-nearest-December
    periods forward. V.F. Corp has several independent Jan 1-3 year ends, and
    each SEC frame supplies the missing local anchor."""
    from screener.normalize import _with_fiscal_calendar

    entries = [
        {**dur("2007-12-30", "2009-01-03", 1.0,
               accn="k10", filed="2011-02-25"), "fy": 2010, "frame": "CY2008"},
        {**dur("2009-01-04", "2010-01-02", 2.0,
               accn="k11", filed="2012-02-24"), "fy": 2011, "frame": "CY2009"},
        {**dur("2010-01-03", "2011-01-01", 3.0,
               accn="k12", filed="2013-02-27"), "fy": 2012, "frame": "CY2010"},
        {**dur("2011-01-02", "2011-12-31", 4.0,
               accn="k13", filed="2014-02-26"), "fy": 2013, "frame": "CY2011"},
        {**dur("2013-12-29", "2015-01-03", 5.0,
               accn="k16", filed="2017-02-27"), "fy": 2016, "frame": "CY2014"},
        {**dur("2015-01-04", "2016-01-02", 6.0,
               accn="k17", filed="2018-02-28"), "fy": 2017, "frame": "CY2015"},
        {**dur("2025-03-30", "2026-03-28", 7.0,
               accn="k26", filed="2026-05-22"), "fy": 2026},
    ]
    gaap = {
        "NetIncomeLoss": tagdata("USD", entries),
        "Revenues": tagdata("USD", [dict(entry) for entry in entries]),
    }

    wrapped = _with_fiscal_calendar(gaap)

    assert wrapped.fiscal_labels["2009-01-03"] == 2008
    assert wrapped.fiscal_labels["2010-01-02"] == 2009
    assert wrapped.fiscal_labels["2011-01-01"] == 2010
    assert wrapped.fiscal_labels["2011-12-31"] == 2011
    assert wrapped.fiscal_labels["2015-01-03"] == 2014
    assert wrapped.fiscal_labels["2016-01-02"] == 2015
    assert wrapped.fiscal_labels["2026-03-28"] == 2026


def test_abnormal_gap_anchors_both_sides_of_a_fiscal_calendar_change():
    """A short transition period is not a comparable annual fact, but it leaves
    the surrounding full years more than 400 days apart. Their own filing labels
    keep the pre-change and post-change calendars from shifting each other."""
    from screener.normalize import _with_fiscal_calendar

    old = {
        **dur("2017-01-01", "2017-12-30", 1.0,
              accn="k17", filed="2018-02-23"),
        "fy": 2017,
    }
    new = {
        **dur("2018-04-01", "2019-03-30", 2.0,
              accn="k19", filed="2019-05-24"),
        "fy": 2019,
    }
    current = {
        **dur("2025-03-30", "2026-03-28", 3.0,
              accn="k26", filed="2026-05-22"),
        "fy": 2026,
    }
    entries = [old, new, current]
    gaap = {
        "NetIncomeLoss": tagdata("USD", entries),
        "Revenues": tagdata("USD", [dict(entry) for entry in entries]),
    }

    wrapped = _with_fiscal_calendar(gaap)

    assert wrapped.fiscal_labels["2017-12-30"] == 2017
    assert wrapped.fiscal_labels["2019-03-30"] == 2019
    assert wrapped.fiscal_labels["2026-03-28"] == 2026


def test_post_transition_regime_uses_its_latest_filing_convention():
    """Dycom's own reports call the year ended 2019-01-26 fiscal 2019, but
    Company Facts labels the first three post-transition annual accessions one
    year behind. A later correct filing must settle the whole January regime
    without shifting the old July regime or inventing a full FY2018."""
    from screener.normalize import _annual_net_income, _with_fiscal_calendar

    transition = {
        **dur("2017-07-30", "2018-01-27", 68.835e6,
              accn="k18", filed="2019-03-04"),
        "fy": 2018,
    }
    entries = [
        {**dur("2016-07-31", "2017-07-29", 157.217e6,
               accn="k17", filed="2017-09-01"), "fy": 2017},
        transition,
        {**dur("2018-01-28", "2019-01-26", 62.907e6,
               accn="k18", filed="2019-03-04"), "fy": 2018},
        {**dur("2019-01-27", "2020-01-25", 57.215e6,
               accn="k19", filed="2020-03-02"), "fy": 2019},
        {**dur("2020-01-26", "2021-01-30", 34.337e6,
               accn="k20", filed="2021-03-05"), "fy": 2020},
        {**dur("2021-01-31", "2022-01-29", 48.574e6,
               accn="k22", filed="2022-03-04"), "fy": 2022},
        {**dur("2022-01-30", "2023-01-28", 142.213e6,
               accn="k23", filed="2023-03-03"), "fy": 2023},
    ]
    gaap = {
        "NetIncomeLoss": tagdata("USD", entries),
        "RevenueFromContractWithCustomerExcludingAssessedTax": tagdata(
            "USD", [dict(entry) for entry in entries]),
    }

    wrapped = _with_fiscal_calendar(gaap, cik="67215")
    series = _annual_net_income(wrapped)

    assert wrapped.fiscal_labels["2017-07-29"] == 2017
    assert wrapped.fiscal_labels["2019-01-26"] == 2019
    assert wrapped.fiscal_labels["2020-01-25"] == 2020
    assert wrapped.fiscal_labels["2021-01-30"] == 2021
    assert wrapped.fiscal_labels["2022-01-29"] == 2022
    assert sorted(series) == [2017, 2019, 2020, 2021, 2022, 2023]


def test_verified_filing_end_rejects_a_contradictory_sibling_context():
    """RBC Bearings' FY2022 10-K/A contains stray April-30 revenue and gross-
    profit contexts, while the audited statements and period of report end April
    2. The verified filing date must not erase net income, EPS and cash flow."""
    from screener.normalize import _annual_net_income, _with_fiscal_calendar

    def annual(tag_value, end="2022-04-02"):
        return {
            **dur("2021-04-04", end, tag_value,
                  accn="0001213900-22-045106", filed="2022-08-05"),
            "form": "10-K/A", "fy": 2022,
        }

    gaap = {
        "Revenues": tagdata("USD", [
            annual(942.937e6), annual(942.937e6, "2022-04-30")]),
        "GrossProfit": tagdata("USD", [
            annual(357.068e6), annual(357.068e6, "2022-04-30")]),
        "NetIncomeLoss": tagdata("USD", [annual(54.710e6)]),
        "OperatingIncomeLoss": tagdata("USD", [annual(121.094e6)]),
        "EarningsPerShareDiluted": tagdata("USD/shares", [annual(1.56)]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata(
            "shares", [annual(27_311_029)]),
        "NetCashProvidedByUsedInOperatingActivities": tagdata(
            "USD", [annual(180.293e6)]),
    }

    wrapped = _with_fiscal_calendar(gaap, cik="1324948")
    income = _annual_net_income(wrapped)

    assert wrapped.fiscal_labels["2022-04-02"] == 2022
    assert "2022-04-30" not in wrapped.fiscal_labels
    assert income[2022].value == Decimal("54710000")


def test_pre_ipo_comparative_before_short_transition_uses_right_anchor():
    """ServiceNow's June-2011 year has no annual accession of its own; the next
    10-K anchors December-2012 after a six-month transition. The last full year
    before that transition is FY2011, not FY2010."""
    from screener.normalize import _with_fiscal_calendar

    prior = dur("2010-07-01", "2011-06-30", 1.0,
                accn="comparative", filed="2013-03-01")
    current = {
        **dur("2012-01-01", "2012-12-31", 2.0,
              accn="k12", filed="2013-03-01"),
        "fy": 2012,
    }
    gaap = {
        "NetIncomeLoss": tagdata("USD", [prior, current]),
        "Revenues": tagdata("USD", [dict(prior), dict(current)]),
    }

    wrapped = _with_fiscal_calendar(gaap)

    assert wrapped.fiscal_labels == {
        "2011-06-30": 2011,
        "2012-12-31": 2012,
    }


def test_variable_january_retail_calendar_uses_the_filers_preceding_year():
    """Build-A-Bear's SEC metadata says FY2026 for the 10-K ending 2026-01-31,
    while the audited report calls it fiscal 2025.  A Saturday-nearest-January
    calendar crosses Jan/Feb without skipping or duplicating an issuer year."""
    from screener.normalize import _with_fiscal_calendar

    gaap = {
        "NetIncomeLoss": tagdata("USD", [
            {**dur("2023-01-29", "2024-02-03", 52.805e6,
                   accn="k25", filed="2026-04-16"), "fy": 2026},
            {**dur("2024-02-04", "2025-02-01", 51.785e6,
                   accn="k25", filed="2026-04-16"), "fy": 2026},
            {**dur("2025-02-02", "2026-01-31", 52.203e6,
                   accn="k25", filed="2026-04-16"), "fy": 2026},
        ]),
    }

    wrapped = _with_fiscal_calendar(gaap, cik="1113809")

    assert wrapped.fiscal_labels == {
        "2024-02-03": 2023,
        "2025-02-01": 2024,
        "2026-01-31": 2025,
    }


def test_latest_filing_wins_a_distant_same_year_period_collision():
    """RUSHA has a stale 2022-12-12 ProfitLoss context beside the later-filed
    audited 2022-12-31 statements. Traversal order must not preserve the typo."""
    from screener.normalize import _annual_net_income, _with_fiscal_calendar

    gaap = {
        "ProfitLoss": tagdata("USD", [
            {**dur("2022-01-01", "2022-12-12", 392.085e6,
                   accn="k22-old", filed="2023-02-24"), "fy": 2022},
        ]),
        # More old tags must not outweigh a newer filing. This also protects a
        # current issuer from broader predecessor facts under the same CIK.
        "Revenues": tagdata("USD", [
            {**dur("2022-01-01", "2022-12-12", 1e9,
                   accn="k22-old", filed="2023-02-24"), "fy": 2022},
        ]),
        "NetCashProvidedByUsedInOperatingActivities": tagdata("USD", [
            {**dur("2022-01-01", "2022-12-12", 100e6,
                   accn="k22-old", filed="2023-02-24"), "fy": 2022},
        ]),
        "NetIncomeLoss": tagdata("USD", [
            {**dur("2022-01-01", "2022-12-31", 391.382e6,
                   accn="k24", filed="2025-02-21"), "fy": 2024},
            {**dur("2023-01-01", "2023-12-31", 347.055e6,
                   accn="k24", filed="2025-02-21"), "fy": 2024},
            {**dur("2024-01-01", "2024-12-31", 304.153e6,
                   accn="k24", filed="2025-02-21"), "fy": 2024},
        ]),
        "RevenueFromContractWithCustomerExcludingAssessedTax": tagdata("USD", [
            {**dur("2022-01-01", "2022-12-31", 7.10167e9,
                   accn="k24", filed="2025-02-21"), "fy": 2024},
        ]),
        "OperatingIncomeLoss": tagdata("USD", [
            {**dur("2022-01-01", "2022-12-31", 506.113e6,
                   accn="k24", filed="2025-02-21"), "fy": 2024},
        ]),
    }

    wrapped = _with_fiscal_calendar(gaap)
    series = _annual_net_income(wrapped)

    assert "2022-12-12" not in wrapped.fiscal_labels
    assert wrapped.fiscal_labels["2022-12-31"] == 2022
    assert series[2022].value == Decimal("391382000")


def test_primary_statement_breadth_wins_a_same_filing_period_collision():
    """CODI and Timken contain one note-like concept on a wrong annual end in
    the same accession as many rows on the audited statement end."""
    from screener.normalize import _with_fiscal_calendar

    gaap = {
        "Revenues": tagdata("USD", [
            {**dur("2016-01-01", "2016-12-13", 1e6,
                   accn="later-note", filed="2020-02-27"), "fy": 2019},
            {**dur("2016-01-01", "2016-12-31", 978.309e6,
                   accn="k18", filed="2019-02-27"), "fy": 2018},
            {**dur("2017-01-01", "2017-12-31", 1.002783e9,
                   accn="k18", filed="2019-02-27"), "fy": 2018},
            {**dur("2018-01-01", "2018-12-31", 1.35732e9,
                   accn="k18", filed="2019-02-27"), "fy": 2018},
        ]),
        "NetIncomeLoss": tagdata("USD", [
            {**dur("2016-01-01", "2016-12-31", 54.685e6,
                   accn="k18", filed="2019-02-27"), "fy": 2018},
        ]),
        "NetCashProvidedByUsedInOperatingActivities": tagdata("USD", [
            {**dur("2016-01-01", "2016-12-31", 111.372e6,
                   accn="k18", filed="2019-02-27"), "fy": 2018},
        ]),
    }

    wrapped = _with_fiscal_calendar(gaap)

    assert "2016-12-13" not in wrapped.fiscal_labels
    assert wrapped.fiscal_labels["2016-12-31"] == 2016


def test_the_balance_sheet_follows_the_earnings_labelling():
    """The whole table has to agree on which year a date is, or a column pairs a
    balance sheet with the wrong year's profit."""
    from screener.normalize import _annual_balances
    gaap = {"Assets": tagdata("USD", [inst("2026-02-01", 5e9, form="10-K", accn="k25")])}
    naive = _annual_balances(gaap, ("Assets",))
    assert 2026 in naive                                    # by the month it ends in
    aligned = _annual_balances(gaap, ("Assets",), labels={"2026-02-01": 2025})
    assert 2025 in aligned and 2026 not in aligned          # by the filer's own frame
