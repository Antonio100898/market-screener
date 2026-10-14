"""A rendered deduction is qualified independently of the compared quantity."""
from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal, localcontext

import pytest

from screener import audit
from screener.sources import statements


CONCEPTS = ('Assets', 'CurrentLiabilities', 'NoncurrentLiabilities', 'Liabilities',
            'NetAssetsLiabilities', 'Equity')
VALUES = ('100', '(20)', '(30)', '(50)', '50', '50')
CONTEXT = statements.SourceContext(date(2025, 12, 31), None, 'MONEY', 'USD')


def document(values=VALUES, *, concepts=CONCEPTS, header='Dec. 31, 2025 USD',
             scale='USD in Millions', extra='', second=None):
    dates = (header,) if second is None else (header, second[0])
    return (f'<table><tr><th>{scale}</th>' + ''.join(f'<th>{d}</th>' for d in dates)
            + '</tr>' + ''.join(
                f'<tr><td><a href="defref_ifrs-full_{concept}">{concept}</a></td>'
                f'<td>{value}</td>' + (f'<td>{second[1][index]}</td>' if second else '')
                + '</tr>' for index, (concept, value) in enumerate(zip(concepts, values)))
            + extra + '</table>')


class Edgar:
    def __init__(self, body):
        self.body = body

    def _get_text(self, url):
        if url.endswith('FilingSummary.xml'):
            return ('<Reports><Report><HtmlFileName>R1.htm</HtmlFileName>'
                    '<ShortName>Balance Sheets</ShortName></Report></Reports>')
        return self.body


def compared(body, shown, **source_changes):
    source = dict(accn='a', end='2025-12-31', unit='USD',
                  tag='ifrs-full:CurrentLiabilities', **source_changes)
    anchor = dict(accn='a', end='2025-12-31', unit='USD', tag='ifrs-full:Assets')
    row = dict(cik='123', current_liabilities=shown,
               sources=dict(total_assets=anchor, current_liabilities=source))
    results = audit.against_filing(row, Edgar(body))
    return next((item for item in results if item[1] == 'current_liabilities'), results[0])


def witness(parsed=None, context=CONTEXT, concept='ifrs-full:CurrentLiabilities'):
    return statements.liability_deduction_witness(
        statements.lines(parsed or statements.read(document())), context, concept=concept)


def changed_row(parsed, index, **changes):
    table = parsed.tables[0]
    rows = list(table.rows)
    rows[index] = replace(rows[index], **changes)
    return statements.Statement((replace(table, rows=tuple(rows)),))


def changed_cell(**changes):
    parsed = statements.read(document())
    row = parsed.tables[0].rows[2]
    return changed_row(parsed, 2, cells=(replace(row.cells[0], **changes),))


def test_actual_audit_admits_only_independently_fixed_positive_quantity():
    body = document()
    positive = compared(body, 20_000_000)
    assert positive[0] == 'FILING-OK' and positive[3] == 20_000_000
    for amount in (-20_000_000, 19_000_000, 30_000_000):
        result = compared(body, amount)
        assert result[0] == 'FILING' and result[3] == 20_000_000
        assert result[4] == positive[4]
    assert 'NET_ASSETS_DEDUCTION' in positive[4]
    assert 'RENDERED_TABLE_LAYOUT' in positive[4]
    assert 'NOT_ASSESSED' in positive[4] and '(20)' in positive[4]


def test_witness_is_frozen_and_keeps_all_literal_operands():
    parsed = statements.read(document())
    before = (statements.lines(parsed), statements.elements(parsed), statements.columns(parsed))
    result = witness(parsed)
    assert result.comparand == Decimal(20_000_000)
    assert result.cell.lexical == '(20)' and result.cell.raw == Decimal(-20)
    assert result.cell.value == Decimal(-20_000_000)
    assert result.table is parsed.tables[0]
    assert result.row is parsed.tables[0].rows[1]
    assert result.header is parsed.tables[0].headers[0]
    assert tuple(row.concept for row in result.supporting_rows) == tuple(
        'ifrs-full_' + concept for concept in CONCEPTS)
    assert result.native_scope == result.native_sign_role == 'NOT_ASSESSED'
    with pytest.raises(FrozenInstanceError):
        result.comparand = Decimal(-20_000_000)
    assert before == (statements.lines(parsed), statements.elements(parsed), statements.columns(parsed))


@pytest.mark.parametrize('context', [
    replace(CONTEXT, end=date(2024, 12, 31)), replace(CONTEXT, start=date(2025, 1, 1)),
    replace(CONTEXT, currency=None), replace(CONTEXT, currency='CNY'),
    replace(CONTEXT, currency='ZZZ'), replace(CONTEXT, measurement='SHARES'),
    replace(CONTEXT, measurement='PER_SHARE'), replace(CONTEXT, basis='RECEIPT'),
])
def test_missing_or_incompatible_selected_context_has_no_witness(context):
    assert witness(context=context) is None


@pytest.mark.parametrize('concept', ['us-gaap:LiabilitiesCurrent',
    'issuer:CurrentLiabilities', 'ext:ifrs/2025/CurrentLiabilities',
    'ifrs-full:NoncurrentLiabilities', 'ifrs-full:CurrentLiabilities + issuer:Other', None])
def test_only_exact_initial_source_concept_is_supported(concept):
    assert witness(concept=concept) is None


@pytest.mark.parametrize('index', range(6))
@pytest.mark.parametrize('substitution', ['Other', 'issuer_CurrentLiabilities'])
def test_every_exact_supporting_qname_is_required(index, substitution):
    concepts = list(CONCEPTS)
    concepts[index] = substitution
    assert witness(statements.read(document(concepts=concepts))) is None


@pytest.mark.parametrize('index', range(6))
def test_duplicate_required_row_is_ambiguous_even_when_equal(index):
    extra = (f'<tr><td><a href="defref_ifrs-full_{CONCEPTS[index]}">Same caption</a>'
             f'</td><td>{VALUES[index]}</td></tr>')
    assert witness(statements.read(document(extra=extra))) is None


@pytest.mark.parametrize('index', range(6))
@pytest.mark.parametrize('token', ['nil', '', 'NaN'])
def test_every_missing_or_rejected_supporting_cell_removes_witness(index, token):
    values = list(VALUES)
    values[index] = token
    assert witness(statements.read(document(values))) is None


@pytest.mark.parametrize('header', ['12 Months Ended Dec. 31, 2025 USD',
    '53 Weeks Ended Dec. 31, 2025 USD',
    'Jan. 1, 2025 to Dec. 31, 2025 USD', '1.12 Months Ended Dec. 31, 2025 USD',
    'Dec. 31, 2025 CNY', 'Unknown USD'])
def test_duration_conflicting_or_unknown_header_cannot_supply_instant_layout(header):
    assert witness(statements.read(document(header=header))) is None


@pytest.mark.parametrize('changes', [
    dict(raw=Decimal('NaN'), value=Decimal('NaN')),
    dict(raw=Decimal('-Infinity'), value=Decimal('-Infinity')),
    dict(multiplier=None), dict(multiplier=Decimal(0)), dict(multiplier=Decimal(-1)),
    dict(multiplier=Decimal('Infinity')), dict(multiplier=Decimal(1000)),
    dict(currency=None), dict(currency='CNY'), dict(column_span=2),
    dict(physical_column=2), dict(column=1), dict(status='NIL'),
    dict(value=Decimal(-29_000_000)), dict(raw=Decimal(-29)),
])
def test_inconsistent_physical_numeric_unit_or_scale_records_fail_closed(changes):
    assert witness(changed_cell(**changes)) is None


@pytest.mark.parametrize('values', [
    ('100', '(20)', '(30)', '(49)', '51', '51'),
    ('100', '(20)', '(30)', '(50)', '49', '49'),
    ('100', '(20)', '(30)', '(50)', '50', '49'),
    ('100', '20', '(70)', '(50)', '50', '50'),
    ('100', '(70)', '20', '(50)', '50', '50'),
    ('(100)', '(20)', '(30)', '(50)', '(150)', '(150)'),
    ('0', '(20)', '(30)', '(50)', '(50)', '(50)'),
])
def test_fixed_equations_and_literal_contribution_signs_cannot_be_guessed(values):
    assert witness(statements.read(document(values))) is None


def test_order_and_distinct_actual_rows_are_required():
    parsed = statements.read(document())
    table = parsed.tables[0]
    unordered = replace(table, rows=(table.rows[1], table.rows[0], *table.rows[2:]))
    # The physical row positions, rather than container order, own ordering.
    assert witness(statements.Statement((unordered,))) is not None
    assert witness(changed_row(parsed, 0, position=3)) is None
    assert witness(changed_row(parsed, 2, position=1)) is None


def test_same_coordinates_from_different_parses_cannot_be_combined():
    first, second = statements.read(document()), statements.read(document())
    left = statements.lines(first)[:3]
    right = statements.lines(second)[3:]
    assert statements.liability_deduction_witness(left + right, CONTEXT,
                                                concept='ifrs-full:CurrentLiabilities') is None
    assert statements.liability_deduction_witness(statements.lines(first) + statements.lines(second),
                        CONTEXT, concept='ifrs-full:CurrentLiabilities') is None


def test_separate_tables_or_duplicate_same_period_columns_are_ambiguous():
    assert witness(statements.read(document() + document())) is None
    assert witness(statements.read(document(second=('Dec. 31, 2025 USD', VALUES)))) is None
    assert compared(document() + document(), 20_000_000)[0] == 'FILING?'


def test_comparative_columns_keep_independent_periods_and_literal_signs():
    comparative = ('90', '(15)', '(25)', '(40)', '50', '50')
    parsed = statements.read(document(second=('Dec. 31, 2024 USD', comparative)))
    assert witness(parsed).comparand == Decimal(20_000_000)
    prior = witness(parsed, replace(CONTEXT, end=date(2024, 12, 31)))
    assert prior.comparand == Decimal(15_000_000) and prior.cell.column == 1
    assert prior.cell.lexical == '(15)'


def test_decimal_rounding_must_not_manufacture_equation_closure():
    parsed = statements.read(document(('101', '(20)', '(30)', '(50)', '51', '51')))
    with localcontext() as ctx:
        ctx.prec = 2
        assert witness(parsed) is None


def test_header_coordinate_and_line_identity_cannot_be_substituted():
    parsed = statements.read(document())
    table = parsed.tables[0]
    assert witness(statements.Statement((replace(table, headers=(replace(table.headers[0], column=1),)),))) is None
    lines = statements.lines(parsed)
    assert statements.liability_deduction_witness(lines + [lines[1]], CONTEXT,
                                                  concept='ifrs-full:CurrentLiabilities') is None


def test_unknown_scales_and_currency_do_not_gain_layout_qualification():
    assert witness(statements.read(document(scale='USD in Millions and in Thousands'))) is None
    assert witness(statements.read(document(scale='$ in Millions', header='Dec. 31, 2025'))) is None


def test_negative_equity_and_net_assets_remain_negative_without_transformation():
    parsed = statements.read(document(('40', '(20)', '(30)', '(50)', '(10)', '(10)')))
    result = witness(parsed)
    assert result is not None and result.comparand == Decimal(20_000_000)
    assert [row.cells[0].value for row in result.supporting_rows[-2:]] == [Decimal(-10_000_000)] * 2


def test_ordinary_positive_liabilities_keep_existing_literal_comparison():
    body = document(('100', '20', '30', '50', '50', '50'))
    assert witness(statements.read(body)) is None
    assert compared(body, 20_000_000)[0] == 'FILING-OK'
    assert compared(body, -20_000_000)[0] == 'FILING'


def test_selected_native_dimension_remains_unavailable_and_no_compound_is_reconciled():
    assert compared(document(), 20_000_000, segments={'issuer:Scope': 'issuer:Division'})[0] == 'FILING?'
    assert compared(document(), 20_000_000, components=[dict(accn='other', end='2025-12-31',
        unit='USD', tag='ifrs-full:CurrentLiabilities')])[0] == 'FILING?'


@pytest.mark.parametrize('concept,label,value', [
    ('us-gaap_NetIncomeLoss', 'Net loss', '(9)'),
    ('us-gaap_IncomeTaxExpenseBenefit', 'Income tax benefit', '(3)'),
    ('ifrs-full_OtherComprehensiveIncome', 'Other comprehensive loss', '(4)'),
    ('us-gaap_AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment', 'Contra-assets', '(5)'),
    ('us-gaap_PaymentsToAcquirePropertyPlantAndEquipment', 'Cash payments', '(6)'),
])
def test_unrelated_signed_cells_remain_literal_and_supply_no_witness(concept, label, value):
    body = ('<table><tr><th>USD</th><th>Dec. 31, 2025</th></tr>'
            f'<tr><td><a href="defref_{concept}">{label}</a></td><td>{value}</td></tr></table>')
    parsed = statements.read(body)
    assert parsed.tables[0].rows[0].cells[0].raw < 0
    assert witness(parsed) is None
