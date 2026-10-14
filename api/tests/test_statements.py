"""Printed cells retain their physical context before an auditor chooses a source."""
from datetime import date
from decimal import Decimal

import pytest

from screener.sources import statements


def table(rows, header="USD ($) $ in Thousands", dates=None):
    dates = dates or ["Dec. 31, 2025", "Dec. 31, 2024"]
    return f'<p>{header}</p><table><tr><th>Statement</th>' + ''.join(
        f'<th>{text}</th>' for text in dates) + '</tr>' + ''.join(
        '<tr><td>' + '</td><td>'.join(row) + '</td></tr>' for row in rows) + '</table>'


def test_sparse_cells_keep_period_positions_and_missing_is_not_zero():
    doc = table([['Income', '', '7'], ['Assets', '9', '']])
    parsed = statements.read(doc)
    assert statements.columns(parsed) == [date(2025, 12, 31), date(2024, 12, 31)]
    assert statements.lines(parsed) == [('Income', [None, Decimal(7000)]), ('Assets', [Decimal(9000), None])]
    assert statements.value_for(statements.lines(parsed), 'Income') is None
    assert statements.all_matching(statements.lines(parsed), 'Income') == []
    assert statements.value_for(statements.lines(parsed), 'Income', column=1) == ('Income', Decimal(7000))


def test_rejected_nil_and_blank_lexicals_keep_columns_and_negative_zero():
    doc = table([['Assets', '', 'nil', '(12', '-', '(0)'], ['Income', '2', '', '', '', '']],
                dates=['Dec. 31, 2025'] * 5)
    parsed = statements.read(doc)
    row = parsed.tables[0].rows[0]
    assert [cell.lexical for cell in row.cells] == ['', 'nil', '(12', '-', '(0)']
    assert [cell.status for cell in row.cells] == ['BLANK', 'NIL', 'UNPARSED', 'NIL', 'NUMERIC']
    assert parsed.tables[0].active == (0, 1, 2, 3, 4)
    assert row.cells[4].raw.is_signed() and row.cells[4].value.is_signed()
    assert statements.lines(parsed)[0][1] == [None, None, None, None, Decimal(0)]


@pytest.mark.parametrize('token', ['(12', '12)', '(-12)', '(+12)', '1,23', '12oops', '$$12'])
def test_malformed_printed_number_is_retained_and_unparsed(token):
    cell = statements.read(table([['Assets', token, '1']])).tables[0].rows[0].cells[0]
    assert cell.lexical == token and cell.status == 'UNPARSED'
    assert cell.raw is None and cell.value is None


def test_symbol_does_not_invent_iso_currency_or_collapse_foreign_column():
    doc = table([['Assets', '¥ (2)', '$ 3']], header='¥ in Thousands, $ in Millions',
                dates=['Dec. 31, 2025 CNY (¥)', 'Dec. 31, 2025 USD ($)'])
    row = statements.read(doc).tables[0].rows[0]
    assert [cell.currency for cell in row.cells] == ['CNY', 'USD']
    assert [cell.value for cell in row.cells] == [Decimal(-2000), Decimal(3000000)]
    unknown = statements.read(table([['Assets', '¥ 2', '3']], header='¥ in Thousands')).tables[0]
    assert all(header.currency is None for header in unknown.headers)
    assert unknown.rows[0].cells[0].raw == Decimal(2)
    conflict = statements.read(table([['Assets', '2', '3']], header='USD and CAD in Thousands')).tables[0]
    assert all(header.currency is None for header in conflict.headers)


def test_eps_counts_and_money_have_separate_scales_and_count_cannot_be_eps():
    doc = table([
        ['<a onclick="defref_us-gaap_IncomeLossFromContinuingOperationsPerDilutedShare">Diluted</a>', '(.09)', '(.33)'],
        ['<a onclick="defref_us-gaap_WeightedAverageNumberOfDilutedSharesOutstanding">Diluted</a>', '100', '90'],
        ['Income to shareholders', '7', '6']], header='USD ($) $ in Millions, shares in Thousands')
    parsed = statements.read(doc)
    assert [row.measurement for row in parsed.tables[0].rows] == ['PER_SHARE', 'SHARES', 'MONEY']
    assert [row.cells[0].value for row in parsed.tables[0].rows] == [Decimal('-.09'), Decimal(100000), Decimal(7000000)]
    assert statements.all_matching(statements.lines(parsed), 'Diluted', money_only=True) == [('Diluted', Decimal('-.09'))]
    assert statements.elements(parsed)['us-gaap_IncomeLossFromContinuingOperationsPerDilutedShare'] == [Decimal('-.09'), Decimal('-.33')]


def test_cent_per_share_and_untagged_section_units():
    doc = table([['Earnings per share (cents)', '', ''], ['Diluted', '(9)', '4'],
                 ['Weighted average number of shares', '', ''], ['Basic', '300', '200']],
                header='GBP (£) £ in Millions, shares in Thousands')
    parsed = statements.read(doc)
    assert parsed.tables[0].rows[1].section == 'Earnings per share (cents)'
    assert parsed.tables[0].rows[1].cells[0].value == Decimal('-.09')
    assert parsed.tables[0].rows[3].cells[0].value == Decimal(300000)


def test_explicit_cent_parenthesis_and_unicode_minus_are_literal_units_and_signs():
    parsed = statements.read(table([['Earnings per ordinary share (in cents)', '−9', '4']]))
    cell = parsed.tables[0].rows[0].cells[0]
    assert cell.lexical == '−9' and cell.raw == Decimal(-9) and cell.value == Decimal('-.09')


def test_repeated_qname_retains_section_basis_and_legacy_projection_is_unknown():
    doc = table([['Ordinary equity shares', '', ''],
                 ['<a onclick="defref_us-gaap_EarningsPerShareDiluted">Diluted</a>', '(.01)', '(.02)'],
                 ['Per ADS information', '', ''],
                 ['<a onclick="defref_us-gaap_EarningsPerShareDiluted">Diluted</a>', '(2)', '(4)']])
    parsed = statements.read(doc)
    rows = [row for row in parsed.tables[0].rows if row.concept]
    assert [row.basis for row in rows] == ['ORDINARY', 'RECEIPT']
    assert [row.position for row in rows] == [2, 4]
    assert [row.cells[0].value for row in rows] == [Decimal('-.01'), Decimal(-2)]
    assert statements.elements(parsed)['us-gaap_EarningsPerShareDiluted'] == [None, None]


def test_rowspan_period_band_and_ambiguous_body_colspan():
    doc = '''<table><tr><th rowspan="2">USD ($) $ in Thousands</th><th colspan="2">12 Months Ended</th></tr>
    <tr><th>Dec. 31, 2025</th><th>Dec. 31, 2024</th></tr>
    <tr><td>Assets</td><td>10</td><td>8</td></tr>
    <tr><td>Income</td><td colspan="2">7</td></tr></table>'''
    parsed = statements.read(doc)
    assert statements.columns(parsed) == [date(2025, 12, 31), date(2024, 12, 31)]
    assert all('12 Months Ended' in header.text for header in parsed.tables[0].headers)
    assert parsed.tables[0].rows[1].cells[0].raw == Decimal(7)
    assert parsed.tables[0].rows[1].cells[0].column_span == 2
    assert statements.value_for(statements.lines(parsed), 'Income') is None


def test_wide_label_column_retains_actual_physical_numeric_coordinates():
    doc = '''<table><tr><th colspan="2" rowspan="2">USD ($)</th><th colspan="2">12 Months Ended</th></tr>
    <tr><th>Dec. 31, 2025</th><th>Dec. 31, 2024</th></tr>
    <tr><td colspan="2">Income</td><td>10</td><td>8</td></tr></table>'''
    parsed = statements.read(doc)
    assert statements.columns(parsed) == [date(2025, 12, 31), date(2024, 12, 31)]
    assert parsed.tables[0].rows[0].cells[0].physical_column == 2
    assert statements.lines(parsed) == [('Income', [Decimal(10), Decimal(8)])]


def test_explicit_caption_area_keeps_reference_cells_without_creating_a_period():
    doc = '''<table><tr><th colspan="2" rowspan="2">USD ($)</th><th colspan="2">12 Months Ended</th></tr>
    <tr><th>Dec. 31, 2025</th><th>Dec. 31, 2024</th></tr>
    <tr><td>Income</td><td><sup>[1]</sup></td><td>10</td><td>8</td></tr></table>'''
    parsed = statements.read(doc)
    assert parsed.tables[0].rows[0].label_cells == ('Income', '[1]')
    assert parsed.tables[0].rows[0].cells[0].physical_column == 2
    assert statements.columns(parsed) == [date(2025, 12, 31), date(2024, 12, 31)]


def test_explicit_year_band_is_retained_without_guessing_a_balance_sheet_date():
    doc = table([['Assets', '2', '3']], dates=['2025', '2024'])
    parsed = statements.read(doc)
    assert [header.year for header in parsed.tables[0].headers] == [2025, 2024]
    assert all(header.end is None for header in parsed.tables[0].headers)
    assert statements.columns(parsed) == []


def test_explicit_thead_and_row_header_have_distinct_roles():
    doc = '''<table><thead><tr><td>USD ($)</td><td>Dec. 31, 2025</td></tr></thead>
    <tr><th scope="row">Assets</th><td>7</td></tr></table>'''
    parsed = statements.read(doc)
    assert statements.columns(parsed) == [date(2025, 12, 31)]
    assert statements.lines(parsed) == [('Assets', [Decimal(7)])]


def test_readable_iso_and_month_header_dates_do_not_need_a_comma():
    parsed = statements.read(table([['Assets', '2', '3']], dates=['2025-12-31', 'December 31 2024']))
    assert statements.columns(parsed) == [date(2025, 12, 31), date(2024, 12, 31)]


def test_unsupported_explicit_money_or_share_scale_stays_unknown():
    doc = table([['Assets', '2', '3'], ['Weighted average shares', '4', '5']],
                header='INR (₨) ₨ in Crores, shares in Lakhs')
    parsed = statements.read(doc)
    assert parsed.tables[0].money_scale is None
    assert all(cell.value is None for row in parsed.tables[0].rows for cell in row.cells)


def test_unknown_bare_diluted_and_conflicting_scale_do_not_supply_a_number():
    parsed = statements.read(table([['Diluted', '3', '2']], header='$ in Thousands'))
    assert parsed.tables[0].rows[0].measurement == 'UNKNOWN'
    assert statements.value_for(statements.lines(parsed), 'Diluted') is None
    conflict = statements.read(table([['Assets', '2', '3']], header='$ in Thousands, £ in Millions'))
    assert conflict.tables[0].money_scale is None
    assert all(cell.value is None for cell in conflict.tables[0].rows[0].cells)


def test_nested_definition_tables_keep_table_identity_and_literal_sign():
    doc = '<script>USD in Millions</script>' + table([
        ['Net loss', '$ 12', '$ (8)']], header='USD ($) $ in Thousands') + '<table><tr><td>Definition</td><td>credit</td></tr></table>'
    parsed = statements.read(doc)
    assert [item.position for item in parsed.tables] == [0, 1]
    assert statements.lines(parsed) == [('Net loss', [Decimal(12000), Decimal(-8000)])]
    assert parsed.tables[0].rows[0].cells[1].raw == Decimal(-8)


def test_later_table_without_local_declaration_has_no_inherited_currency():
    doc = '<p>USD in Millions</p><table><tr><th>2025-12-31</th></tr><tr><td>Assets</td><td>2</td></tr></table>'
    doc += '<table><tr><th>2025-12-31</th></tr><tr><td>Assets</td><td>3</td></tr></table>'
    parsed = statements.read(doc)
    assert parsed.tables[1].rows[0].cells[0].currency is None
    assert parsed.tables[1].rows[0].cells[0].value != Decimal(3000000)


def test_conflicting_explicit_per_share_scales_remain_unknown():
    parsed = statements.read(table([['Earnings per share (in cents)', '9', '7']],
                                  header='GBP', dates=['2025-12-31 (dollars per share)', '2024-12-31']))
    assert parsed.tables[0].rows[0].cells[0].value is None
    assert parsed.tables[0].rows[0].cells[1].value == Decimal('.07')


def test_explicit_date_range_keeps_both_reported_period_endpoints():
    parsed = statements.read(table([['Income', '9', '7']], header='USD',
                                  dates=['Jan. 1, 2025 to Mar. 12, 2025 Predecessor',
                                         'Mar. 13, 2025 through Dec. 31, 2025 Successor']))
    first, second = parsed.tables[0].headers
    assert (first.start, first.end) == (date(2025, 1, 1), date(2025, 3, 12))
    assert (second.start, second.end) == (date(2025, 3, 13), date(2025, 12, 31))
