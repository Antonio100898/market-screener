"""Synthetic definition/annual-owner controls; retained BCH proof stays separate."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json

import pytest

from helpers import dur, inst, tagdata
from screener import normalize, sync
from screener.models import CapturedSource
from screener.sources import sec_json


FAMILIES = [
    ('DilutedEarningsLossPerShareFromContinuingOperations', normalize.EPS_CONTINUING_TAG),
    ('BasicEarningsLossPerShareFromContinuingOperations', 'IncomeLossFromContinuingOperationsPerBasicShare'),
    ('BasicAndDilutedEarningsLossPerShare', 'EarningsPerShareBasicAndDiluted'),
]


def annual(year, value, **kw):
    return dur(f'{year}-01-01', f'{year}-12-31', value, form='20-F', **kw)


def taxonomy(ifrs, currency='CLP'):
    mapped = normalize._with_fiscal_calendar(normalize._ifrs_as_us_gaap(ifrs))
    mapped.reporting_currency = currency
    mapped.currency_adapter = True
    mapped.canonical_adapter = False
    return mapped


@pytest.mark.parametrize('native,canonical', FAMILIES)
def test_exact_family_keeps_source_unit_period_origin_and_input(native, canonical):
    row = annual(2025, 5.98, accn='a25', filed='2026-04-30')
    raw = json.dumps({'cik': 1234, 'facts': {'ifrs-full': {native: {'units': {
        'CLP/shares': [row], 'USD/shares': [{**row, 'val': 999}], 'CLP': [{**row, 'val': 999}],
    }}}}}).encode()
    captured = CapturedSource('company_facts', hashlib.sha256(raw).hexdigest(),
                             'https://data.sec.gov/api/xbrl/companyfacts/CIK0000001234.json',
                             1, 2, 3, 'EXACT_LEXICAL')
    parsed = sec_json.decode_company_facts(raw, source=captured)
    original = deepcopy(parsed)
    gaap = taxonomy(parsed['facts']['ifrs-full'])
    fact = normalize._annual_series(gaap, canonical)[2025]
    assert fact.value == Decimal('5.98')
    assert fact.provenance.tag == f'ifrs-full:{native}'
    assert fact.provenance.canonical_tag == canonical and fact.provenance.unit == 'CLP/shares'
    assert (fact.provenance.period_start, fact.provenance.period_end) == (date(2025, 1, 1), date(2025, 12, 31))
    assert fact.provenance.accession == 'a25' and fact.provenance.filed == date(2026, 4, 30)
    assert fact.exact_origin.namespace == 'ifrs-full' and fact.exact_origin.concept == native
    assert fact.exact_origin.pointer == f'/facts/ifrs-full/{native}/units/CLP~1shares/0/val'
    sec_json.verify_origin(fact.exact_origin, parsed)
    assert fact.exact_unassessed_reason is None and parsed == original


@pytest.mark.parametrize('native,canonical', FAMILIES)
def test_wrong_currency_or_money_only_unit_cannot_supply_reported_eps(native, canonical):
    gaap = taxonomy({native: {'units': {'USD/shares': [annual(2025, 999)], 'CLP': [annual(2025, 999)]}}})
    assert normalize._annual_series(gaap, canonical) == {}
    assert normalize._annual_eps(gaap) == {}


def test_continuing_diluted_priority_uses_existing_scope_abstention():
    gaap = taxonomy({
        'DilutedEarningsLossPerShare': tagdata('CLP/shares', [annual(2025, 99)]),
        'BasicEarningsLossPerShare': tagdata('CLP/shares', [annual(2025, 98)]),
        'BasicAndDilutedEarningsLossPerShare': tagdata('CLP/shares', [annual(2025, 97)]),
        'DilutedEarningsLossPerShareFromContinuingOperations': tagdata('CLP/shares', [annual(2025, 5.98)]),
        'BasicEarningsLossPerShareFromContinuingOperations': tagdata('CLP/shares', [annual(2025, 6)]),
        'ProfitLossAttributableToOwnersOfParent': tagdata('CLP', [annual(2025, -603633000000)]),
        'AdjustedWeightedAverageShares': tagdata('shares', [annual(2025, 101017081114)]),
    })
    fact = normalize._annual_eps(gaap)[2025]
    assert fact.value == Decimal('5.98')
    assert fact.provenance.tag == 'ifrs-full:DilutedEarningsLossPerShareFromContinuingOperations'
    assert not normalize._eps_uses_total_income(fact)
    parts = {'annual_eps': {2025: fact}}
    normalize._restate_onto_receipt(parts, Decimal(200), 'cover')
    assert parts['annual_eps'][2025].value == Decimal('1196')
    assert parts['annual_eps'][2025].exact_unassessed_reason == 'RECEIPT_BASIS_OPERATION_UNTRACED'


def test_stale_continuing_cannot_replace_current_diluted_but_fills_its_old_gap():
    gaap = taxonomy({
        'DilutedEarningsLossPerShare': tagdata('CLP/shares', [annual(2024, 4), annual(2025, 5)]),
        'DilutedEarningsLossPerShareFromContinuingOperations': tagdata('CLP/shares', [annual(2023, 3), annual(2024, 40)]),
    })
    series = normalize._annual_eps(gaap)
    assert {y: f.value for y, f in series.items()} == {2023: Decimal(3), 2024: Decimal(4), 2025: Decimal(5)}
    assert series[2023].provenance.tag.endswith('ContinuingOperations')
    assert series[2024].provenance.tag == 'ifrs-full:DilutedEarningsLossPerShare'


def test_distinct_year_gaps_preserve_existing_deeper_combined_series_priority():
    gaap = taxonomy({
        'DilutedEarningsLossPerShare': tagdata('CLP/shares', [annual(2024, 4)]),
        'BasicAndDilutedEarningsLossPerShare': tagdata('CLP/shares', [annual(2023, 3), annual(2024, 40)]),
        'BasicEarningsLossPerShare': tagdata('CLP/shares', [annual(2022, 2), annual(2023, 30), annual(2025, 5)]),
    })
    series = normalize._annual_eps(gaap)
    legacy = normalize._with_fiscal_calendar({
        'EarningsPerShareDiluted': tagdata('CLP/shares', [annual(2024, 4)]),
        'EarningsPerShareBasicAndDiluted': tagdata('CLP/shares', [annual(2023, 3), annual(2024, 40)]),
        'EarningsPerShareBasic': tagdata('CLP/shares', [annual(2022, 2), annual(2023, 30), annual(2025, 5)]),
    })
    legacy.reporting_currency, legacy.currency_adapter = 'CLP', True
    expected = {y: f.value for y, f in normalize._annual_eps(legacy).items()}
    assert expected == {2022: Decimal(2), 2023: Decimal(3), 2024: Decimal(40), 2025: Decimal(5)}
    assert {y: f.value for y, f in series.items()} == expected
    assert series[2023].provenance.tag == 'ifrs-full:BasicAndDilutedEarningsLossPerShare'
    assert series[2024].provenance.tag == 'ifrs-full:BasicAndDilutedEarningsLossPerShare'
    assert 'basic' in series[2025].provenance.concept


def test_basic_continuing_is_only_the_existing_basic_fallback():
    gaap = taxonomy({'BasicEarningsLossPerShareFromContinuingOperations': tagdata('CLP/shares', [annual(2025, 6)])})
    fact = normalize._annual_eps(gaap)[2025]
    assert fact.value == 6 and fact.provenance.tag.endswith('BasicEarningsLossPerShareFromContinuingOperations')
    assert not normalize._eps_uses_total_income(fact)


def test_combined_keeps_latest_filing_and_diluted_equal_recency_priority():
    gaap = taxonomy({
        'BasicAndDilutedEarningsLossPerShare': tagdata('CLP/shares', [
            annual(2025, 1, accn='old', filed='2026-02-01'),
            annual(2025, 2, accn='new', filed='2026-03-01'),
        ]),
    })
    assert normalize._annual_eps(gaap)[2025].value == 2
    gaap['EarningsPerShareDiluted'] = normalize._ifrs_as_us_gaap({
        'DilutedEarningsLossPerShare': tagdata('CLP/shares', [annual(2025, 3, accn='new', filed='2026-03-01')]),
    })['EarningsPerShareDiluted']
    assert normalize._annual_eps(gaap)[2025].value == 3


def test_ordinary_money_and_share_aliases_stay_unchanged_and_cf_input_is_immutable():
    original = {
        'Assets': tagdata('CLP', [inst('2025-12-31', 10)]),
        'ProfitLossAttributableToOwnersOfParent': tagdata('CLP', [annual(2025, -2)]),
        'ProfitLoss': tagdata('CLP', [annual(2025, 3)]),
        'WeightedAverageShares': tagdata('shares', [annual(2025, 4)]),
    }
    before = deepcopy(original)
    mapped = taxonomy(original)
    assert normalize._entries(mapped, 'NetIncomeLoss', ('USD',))[0]['val'] == -2
    assert normalize._entries(mapped, 'ProfitLoss', ('USD',))[0]['val'] == 3
    assert normalize._entries(mapped, 'WeightedAverageNumberOfSharesOutstandingBasic', ('shares',))[0]['val'] == 4
    assert normalize._latest_instant(mapped, 'assets', ('Assets',)).value == 10
    assert original == before


def test_existing_source_caller_catalog_receives_exact_native_per_share_family():
    wanted, per_share = sync._dera_tags()
    for native, canonical in FAMILIES:
        assert native in wanted and native in per_share
        assert canonical in wanted and canonical in per_share
        assert normalize._canonical_tag(f'ifrs-full:{native}') == canonical
    assert 'WeightedAverageShares' in wanted and 'WeightedAverageShares' not in per_share
