"""One existing proxy must use the same selected annual monetary context."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from helpers import OE_GAAP, dur, facts_doc, tagdata
from screener.models import Fact, Provenance
from screener.normalize import (INTEREST_EXPENSE_TAGS, _annual_union,
    _fixed_charge_inputs_compatible, build_snapshot)


def proxy_facts():
    gaap = deepcopy(OE_GAAP)
    gaap['OperatingLeaseCost'] = tagdata('USD', [dur('2025-01-01', '2025-12-31', 8e9, accn='k25', filed='2026-02-15')])
    gaap['InterestExpenseDebt'] = tagdata('USD', [dur('2025-01-01', '2025-12-31', 2e9, accn='k25', filed='2026-02-15')])
    return gaap


@pytest.mark.parametrize('tag', ['OperatingIncomeLoss', 'InterestExpenseDebt', 'OperatingLeaseCost'])
@pytest.mark.parametrize('assumed', [False, True])
def test_same_end_different_selected_start_withholds_only_proxy(tag, assumed):
    gaap = proxy_facts()
    baseline = build_snapshot('TEST', '0000000001', facts_doc(gaap), assume_absent_zero=assumed)
    assert float(baseline.fixed_charge_coverage.value) == pytest.approx(10.8)
    gaap[tag]['units']['USD'][0]['start'] = '2025-01-05'
    snapshot = build_snapshot('TEST', '0000000001', facts_doc(gaap), assume_absent_zero=assumed)
    assert snapshot.fixed_charge_coverage is None
    assert snapshot.lease_cost.value == baseline.lease_cost.value
    assert snapshot.annual_operating_income[2025].value == baseline.annual_operating_income[2025].value
    interest = _annual_union(gaap, INTEREST_EXPENSE_TAGS)[2025]
    assert interest.value == Decimal('2000000000.0')
    selected = snapshot.lease_cost if tag == 'OperatingLeaseCost' else snapshot.annual_operating_income[2025] if tag == 'OperatingIncomeLoss' else interest
    assert selected.provenance.period_start.isoformat() == '2025-01-05'


def selected_sources():
    return tuple(Fact(Decimal(value), Provenance(concept=name, tag=f'us-gaap:{name}',
        fiscal_year=2025, form='10-K', accession=name, filed=date(2026, 2, 15),
        period_start=date(2025, 1, 1), period_end=date(2025, 12, 31), unit='USD'))
        for name, value in [('OperatingIncomeLoss', '100'), ('InterestExpenseDebt', '2'), ('OperatingLeaseCost', '8')])


@pytest.mark.parametrize('field,value', [('period_start', None), ('period_end', None),
    ('period_start', date(2025, 1, 5)), ('period_end', date(2025, 12, 30)),
    ('unit', None), ('unit', 'JPY'), ('unit', 'USD/shares'), ('unit', 'shares'),
    ('segments', 'Consolidated|CODM')])
def test_root_context_missing_or_conflicting_is_not_compatible(field, value):
    sources = selected_sources()
    changed = replace(sources[1], provenance=replace(sources[1].provenance, **{field: value}))
    assert not _fixed_charge_inputs_compatible((sources[0], changed, sources[2]))


@pytest.mark.parametrize('leaves', [6, 10])
def test_derived_root_keeps_repeated_leaves_without_a_synthetic_unit(leaves):
    operating, interest, lease = selected_sources()
    derived = replace(operating, provenance=replace(operating.provenance, unit=None,
        components=tuple(interest.provenance if i == 0 else operating.provenance for i in range(leaves - 2))))
    assert _fixed_charge_inputs_compatible((derived, interest, lease))
    assert derived.provenance.unit is None and len(derived.provenance.components) == leaves - 2


@pytest.mark.parametrize('field,value', [('period_start', None), ('period_end', None),
    ('period_start', date(2025, 1, 5)), ('period_end', date(2025, 12, 30)),
    ('unit', None), ('unit', 'CNY'), ('segments', 'SubsidiaryOnly')])
def test_nested_leaf_context_cannot_borrow_derived_parent(field, value):
    operating, interest, lease = selected_sources()
    invalid = replace(operating.provenance, **{field: value})
    nested = replace(operating.provenance, unit=None, components=(operating.provenance, invalid))
    derived = replace(operating, provenance=replace(operating.provenance, unit=None, components=(nested,)))
    assert not _fixed_charge_inputs_compatible((derived, interest, lease))


@pytest.mark.parametrize('days', [362, 363, 364, 365, 366, 368, 370, 371])
@pytest.mark.parametrize('currency', ['USD', 'JPY', 'CAD', 'CNY', 'EUR'])
def test_same_admitted_annual_period_and_non_usd_terminal_units_are_compatible(days, currency):
    end = date(2025, 12, 31)
    sources = tuple(replace(f, provenance=replace(f.provenance, period_start=end-timedelta(days=days-1), unit=currency)) for f in selected_sources())
    assert _fixed_charge_inputs_compatible(sources)


def test_different_filings_equal_declared_scope_and_repeated_dependencies_remain_compatible():
    sources = tuple(replace(f, provenance=replace(f.provenance, segments='DeclaredSameSlice',
        filed=date(2026, 2, 15+i), accession=f'filing-{i}')) for i, f in enumerate(selected_sources()))
    assert _fixed_charge_inputs_compatible(sources)
    assert len({f.provenance.accession for f in sources}) == 3


@pytest.mark.parametrize('assumed', [False, True])
def test_proxy_rejection_does_not_select_lower_priority_same_period_interest(assumed):
    gaap = proxy_facts()
    gaap['InterestExpenseDebt']['units']['USD'][0]['start'] = '2025-01-05'
    gaap['InterestExpenseNonoperating'] = tagdata('USD', [dur('2025-01-01', '2025-12-31', 3e9, accn='k25', filed='2026-02-15')])
    snapshot = build_snapshot('TEST', '0000000001', facts_doc(gaap), assume_absent_zero=assumed)
    assert snapshot.fixed_charge_coverage is None
    selected = _annual_union(gaap, INTEREST_EXPENSE_TAGS)[2025]
    assert selected.provenance.tag == 'us-gaap:InterestExpenseDebt'
    assert selected.provenance.period_start.isoformat() == '2025-01-05' and selected.value == Decimal('2000000000.0')
