"""Native primary input consumption through existing financial selection owners."""
from decimal import Decimal
from dataclasses import replace
import copy
import pickle
import hashlib
import json
from datetime import date, datetime, timezone
import pytest

from screener import normalize
from helpers import dur, tagdata
from screener import evidence, sync
from screener.models import AdmittedPrimaryInput, UnavailablePrimaryInput, CapturedSource
from screener.sources import sec_json
from test_primary_input_targets import plan
from test_native_primary_inputs import CIK, fixture, mapping, bind


def admitted_view(*, unavailable=False, source_changes=None, document_changes=None,
                  native_text=None, unary_factor_text=None, replacement_text=None, **kwargs):
    declaration, payloads, tree = plan(**kwargs)
    if declaration.target_origin.unit == 'USD/shares':
        body = payloads.pop(declaration.original.source.artifact_sha256)
        body = body.replace(b'<x:unit id="u"><x:measure>iso:USD</x:measure></x:unit>',
            b'<x:unit id="u"><x:divide><x:unitNumerator><x:measure>iso:USD</x:measure></x:unitNumerator>'
            b'<x:unitDenominator><x:measure>x:shares</x:measure></x:unitDenominator></x:divide></x:unit>')
        request = bind(body, replace(declaration.original,
            unit_denominator=('{http://www.xbrl.org/2003/instance}shares',)))
        declaration = replace(mapping(body,request), target_origin=declaration.target_origin)
        payloads[request.source.artifact_sha256] = body
    if native_text is not None:
        body = payloads.pop(declaration.original.source.artifact_sha256)
        body = body.replace(b'>2.06<', b'>' + native_text.encode() + b'<')
        request = bind(body,declaration.original)
        declaration = replace(mapping(body,request),target_origin=declaration.target_origin)
        payloads[request.source.artifact_sha256] = body
    if unary_factor_text is not None:
        declaration = replace(declaration,unary_factor_text=unary_factor_text)
    if replacement_text is not None:
        body = payloads[declaration.original.source.artifact_sha256]
        replacement_body = body.replace(b'>2.06<', b'>' + replacement_text.encode() + b'<')
        replacement = bind(replacement_body,declaration.original)
        declaration = replace(declaration,replacement=replacement)
        payloads[replacement.source.artifact_sha256] = replacement_body
    if source_changes:
        declaration = replace(declaration, original=replace(declaration.original,
            source=replace(declaration.original.source, **source_changes)))
    if document_changes:
        documents = tuple(document.model_copy(update={'reference':
            document.reference.model_copy(update=document_changes)}) for document in declaration.documents)
        declaration = replace(declaration, documents=documents)
    if unavailable:
        del payloads[declaration.original.source.artifact_sha256]
    outcome = evidence.admit_primary_inputs(CIK, (declaration,), payloads)[0]
    assert isinstance(outcome, UnavailablePrimaryInput if unavailable else AdmittedPrimaryInput)
    return tree, outcome, normalize.prepare_primary_inputs(tree, (outcome,))


def gaap(view):
    return normalize._with_fiscal_calendar(view['facts']['us-gaap'], cik=CIK)


def test_native_period_occurrence_keeps_bound_original_replacement_and_basis_without_cf_relabel():
    _, outcome, view = admitted_view(replacement_text='3.06')
    taxonomy = gaap(view)
    periods = normalize._per_share_periods(taxonomy)
    native = [row[2] for rows in periods.occurrences.values() for row in rows if row[2]['primary_input'] is outcome]
    assert len(native) == 1
    source = native[0]
    assert source['origin'] is None
    assert source['primary_input'].mapping.target_origin is outcome.mapping.target_origin
    assert source['primary_input'].original is outcome.original
    assert source['primary_input'].replacement is outcome.replacement
    assert source['share_basis'] is outcome.mapping.share_basis


@pytest.mark.parametrize('classed_present', [False, True])
def test_class_reconciliation_retains_private_support_with_original_ttm_missingness(classed_present):
    from screener.models import Fact, Provenance
    _, unavailable, _ = admitted_view(unavailable=True)
    assert unavailable.canonical_tag == 'EarningsPerShareDiluted'
    assert unavailable.target_origin.end == '2020-12-31'
    fact = Fact(Decimal('1'), Provenance(
        'EarningsPerShareDiluted', 'us-gaap:EarningsPerShareDiluted', 2019,
        '10-K', 'synthetic2019', date(2020, 3, 1),
        date(2019, 12, 31), date(2019, 1, 1), unit='USD/shares'))
    support = {'records': (), 'qualification': 'NOT_ASSESSED'}
    series = normalize._SelectedAnnualSeries(
        {2019: fact}, unavailable={2020: unavailable}, decision_support={2019: support})
    result = normalize._prefer_reconciling_classed_eps(
        {}, series, {2019: fact} if classed_present else {})
    assert dict(result) == {2019: fact}
    assert result[2019] is fact
    assert result.decision_support[2019] is support
    if classed_present:
        assert result.unavailable == {}
        assert normalize._ttm_eps({}, result) == (Decimal('1'), (fact,))
    else:
        assert result is series
        assert result.unavailable[2020] is unavailable
        assert normalize._ttm_eps({}, result) == (None, ())


def test_magnitude_discard_stays_private_support_and_does_not_create_fallback_ancestry():
    _, outcome, view = admitted_view(native_text='100001')
    series = normalize._annual_eps(gaap(view))
    assert 2020 not in series and 2020 not in getattr(series, 'unavailable', {})
    support = series.decision_support[2020]
    dropped = [row['fact'] for row in support['records'] if row['phase'] == 'magnitude-discard']
    assert len(dropped) == 1 and dropped[0].primary_input is outcome
    fallback = normalize._fact('synthetic fallback', 'NetIncomeLoss', annual(-1), 2020)
    retained = normalize._copy_series({2020:fallback}, series)
    assert retained[2020] is fallback and fallback.primary_input is None and fallback.operation_inputs == ()
    assert retained.decision_support[2020] is support
    assert sync._source(fallback) == sync._source(retained[2020])


def unavailable_taxonomy(tags, target_tag, *, index=0, unit='USD'):
    raw = json.dumps({'cik':int(CIK), 'facts':{'us-gaap':{
        tag:tagdata(unit, rows) for tag,rows in tags.items()}}}).encode()
    source = CapturedSource('company_facts', hashlib.sha256(raw).hexdigest(),
        'https://data.sec.gov/api/xbrl/companyfacts/CIK0001066923.json', 1,2,3,'EXACT_LEXICAL')
    tree = sec_json.decode_company_facts(raw, source=source)
    target = tree['facts']['us-gaap'][target_tag]['units'][unit][index][sec_json.ORIGIN_KEY]
    body, request = fixture('not-a-number')
    declaration = replace(mapping(body, request), canonical_tag=target_tag, target_origin=target)
    outcome = evidence.admit_primary_inputs(CIK, (declaration,),
        {source.artifact_sha256:raw, request.source.artifact_sha256:body})[0]
    assert isinstance(outcome, UnavailablePrimaryInput)
    return gaap(normalize.prepare_primary_inputs(tree, (outcome,))), outcome


def annual(value, *, filed='2025-02-20', accession='current'):
    return dur('2024-01-01', '2024-12-31', value, accn=accession, filed=filed)


def test_existing_latest_filed_winner_ignores_older_same_period():
    taxo = {'EarningsPerShareDiluted': tagdata('USD/shares', [
        annual('1.00', filed='2025-01-20', accession='old'), annual('2.00')])}
    selected = normalize._annual_series(taxo, 'EarningsPerShareDiluted')
    assert selected[2024].value == Decimal('2.00')
    assert selected[2024].provenance.accession == 'current'


def test_existing_dollar_scope_priority_outranks_longer_equal_recency_history():
    taxo = {'NetIncomeLoss': tagdata('USD', [annual('100')]),
            'ProfitLoss': tagdata('USD', [annual('200'), dur('2023-01-01', '2023-12-31', 50,
                                                           filed='2024-02-20', accn='older')])}
    result = normalize._annual_dollar_series(taxo, ('NetIncomeLoss', 'ProfitLoss'))
    assert result[2024].value == 100 and result[2023].value == 50


def test_existing_dollar_recency_can_change_winning_scope():
    taxo = {'NetIncomeLoss': tagdata('USD', [dur('2023-01-01', '2023-12-31', 100,
                                               filed='2024-02-20', accn='old')]),
            'ProfitLoss': tagdata('USD', [annual('200')])}
    assert normalize._annual_dollar_series(taxo, ('NetIncomeLoss', 'ProfitLoss'))[2024].value == 200


def test_existing_union_priority_is_per_year_before_other_tag_fallback():
    taxo = {'WeightedAverageNumberOfDilutedSharesOutstanding': tagdata('shares', [annual('10')]),
            'WeightedAverageNumberOfSharesOutstandingBasic': tagdata('shares', [annual('20', filed='2025-03-20')])}
    result = normalize._annual_union(taxo, ('WeightedAverageNumberOfDilutedSharesOutstanding',
                                          'WeightedAverageNumberOfSharesOutstandingBasic'), unit=('shares',))
    assert result[2024].value == 10


def test_existing_cutoff_reselects_only_then_available_filing():
    taxo = {'EarningsPerShareDiluted': tagdata('USD/shares', [
        annual('1.00', filed='2025-01-20', accession='old'), annual('2.00')])}
    cut = normalize._filed_by(taxo, '2025-02-01')
    assert normalize._annual_series(cut, 'EarningsPerShareDiluted')[2024].value == 1
    assert normalize._annual_series(taxo, 'EarningsPerShareDiluted')[2024].value == 2


def test_existing_derived_eps_uses_actual_selected_income_and_count():
    taxo = {'NetIncomeLoss': tagdata('USD', [annual('100')]),
            'WeightedAverageNumberOfDilutedSharesOutstanding': tagdata('shares', [annual('10')])}
    income = normalize._annual_dollar_series(taxo, ('NetIncomeLoss',))
    result = normalize._derived_annual_eps(taxo, {}, {}, income, {})
    assert result[2024].value == 10 and result[2024].provenance.accession == 'current'
    inputs = result[2024].derived_eps_inputs
    assert inputs.income is income[2024] and inputs.count.value == 10
    assert inputs.preferred is None and inputs.absent_preferred_reason


def test_exact_native_view_changes_only_target99_and_retains_original_tree():
    tree, outcome, view = admitted_view()
    before = copy.deepcopy(tree)
    entries = view['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares']
    assert entries[98]['val'] == tree['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares'][98]['val']
    assert entries[99]['val'] == Decimal('2.06')
    selected = normalize._annual_series(gaap(view), 'EarningsPerShareDiluted')[2020]
    assert selected.value == Decimal('2.06') and selected.primary_input is outcome
    assert selected.exact_origin is None and selected.exact_unassessed_reason
    assert selected.provenance.document == outcome.original.request.source.source_document
    assert tree == before and view.original_company_facts is tree
    assert normalize.prepare_primary_inputs(view, (outcome,)) is view
    sec_json.verify_origin(outcome.mapping.target_origin, tree)
    assert pickle.loads(pickle.dumps(view)) == view


@pytest.mark.parametrize('field,value', [('lexical_text','2.330'), ('accession','other'),
    ('pointer','/facts/us-gaap/EarningsPerShareDiluted/units/USD~1shares/98/val')])
def test_view_rejects_changed_complete_origin(field, value):
    tree, outcome, _ = admitted_view()
    changed = replace(outcome, mapping=replace(outcome.mapping,
        target_origin=replace(outcome.mapping.target_origin, **{field:value})))
    with pytest.raises(ValueError):
        normalize.prepare_primary_inputs(tree, (changed,))


def test_unavailable_winner_cannot_be_filled_from_equal98_or_older_same_tag():
    _, outcome, view = admitted_view(unavailable=True)
    selected = normalize._annual_eps(gaap(view))
    assert 2020 not in selected
    assert selected.unavailable[2020] is outcome
    assert normalize._ttm_eps(gaap(view), selected) == (None, ())


def test_cutoff_does_not_apply_later_unavailable_declaration():
    _, outcome, view = admitted_view(unavailable=True)
    cutoff = normalize._filed_by(gaap(view), '2023-03-01')
    assert normalize._annual_eps(cutoff)[2020].value == Decimal('2.33')
    assert not getattr(normalize._annual_eps(cutoff), 'unavailable', {})
    assert normalize._annual_eps(gaap(view)).unavailable[2020] is outcome


def test_pending_removal_retains_owned_context_and_reselects_remaining_entries():
    tree, outcome, view = admitted_view(unavailable=True)
    removed = sync._without_filing(view, ('2023-03-22', '0001213900-23-022083'))
    assert removed.original_company_facts is tree and removed.primary_inputs == (outcome,)
    assert normalize.prepare_primary_inputs(removed) is removed
    assert normalize._annual_eps(gaap(removed))[2020].value == Decimal('2.33')


def test_actual_evidence_caller_prepares_once_for_strict_and_default(monkeypatch):
    tree, outcome, _ = admitted_view()
    observed = []
    def derive(cik, ticker, facts, **kwargs):
        observed.append((facts, kwargs['assume_absent_zero']))
        assert normalize._annual_series(gaap(facts), 'EarningsPerShareDiluted')[2020].value == Decimal('2.06')
        return sync.DeriveResult('ok', {})
    monkeypatch.setattr(sync, '_derive_result', derive)
    sync._derive_evidence_result(evidence.EvidenceBundle(CIK, 'SYNTH', tree, None, None,
        original_company_facts=tree, primary_inputs=(outcome,)))
    assert [mode for _,mode in observed] == [False, True]
    assert observed[0][0] is observed[1][0]


def test_empty_input_returns_original_root():
    tree = {'facts': {}}
    assert normalize.prepare_primary_inputs(tree) is tree


@pytest.mark.parametrize('mismatch',['period','unit','role'])
def test_independently_admitted_mismatched_native_context_is_unsupported(mismatch):
    declaration, payloads, tree = plan()
    body = payloads.pop(declaration.original.source.artifact_sha256)
    request = declaration.original
    if mismatch != 'unit':
        body = body.replace(b'<x:unit id="u"><x:measure>iso:USD</x:measure></x:unit>',
            b'<x:unit id="u"><x:divide><x:unitNumerator><x:measure>iso:USD</x:measure></x:unitNumerator>'
            b'<x:unitDenominator><x:measure>x:shares</x:measure></x:unitDenominator></x:divide></x:unit>')
        request = replace(request,unit_denominator=('{http://www.xbrl.org/2003/instance}shares',))
    if mismatch == 'period':
        body = body.replace(b'2020-01-01',b'2019-01-01').replace(b'2020-12-31',b'2019-12-31')
        request = replace(request,context=replace(request.context,start=date(2019,1,1),end=date(2019,12,31)))
    request = bind(body,request)
    declaration = replace(mapping(body,request),target_origin=declaration.target_origin,
        canonical_tag='NetIncomeLoss' if mismatch == 'role' else declaration.canonical_tag)
    payloads[request.source.artifact_sha256] = body
    outcome = evidence.admit_primary_inputs(CIK,(declaration,),payloads)[0]
    assert isinstance(outcome,AdmittedPrimaryInput)
    view = normalize.prepare_primary_inputs(tree,(outcome,))
    selected = normalize._annual_series(gaap(view),'EarningsPerShareDiluted')
    assert 2020 not in selected and selected.unavailable[2020].reason
    assert view.primary_inputs == (outcome,)


def test_vintage_before_actual_native_disclosure_keeps_exact_original_cf_entry():
    tree,outcome,view=admitted_view()
    cut=normalize._filed_by(gaap(view),'2023-03-23')
    assert normalize._annual_series(cut,'EarningsPerShareDiluted')[2020].value == Decimal('2.33')
    after=normalize._filed_by(gaap(view),'2023-03-24')
    assert normalize._annual_series(after,'EarningsPerShareDiluted')[2020].value == Decimal('2.06')
    assert outcome.original.request.source.filed == date(2023,3,24)


def test_vintage_before_missing_native_disclosure_cannot_keep_future_barrier():
    _,outcome,view=admitted_view(unavailable=True)
    cut=normalize._filed_by(gaap(view),'2023-03-23')
    assert normalize._annual_series(cut,'EarningsPerShareDiluted')[2020].value == Decimal('2.33')
    assert outcome.original.source.filed == date(2023,3,24)


@pytest.mark.parametrize('source_changes', [
    {'filed':date(2023,3,26)}, {'annual_filed':date(2023,3,26)}])
def test_source_and_wrapper_each_delay_the_actual_correction(source_changes):
    _,_,view=admitted_view(source_changes=source_changes)
    assert normalize._annual_series(normalize._filed_by(gaap(view),'2023-03-25'),
        'EarningsPerShareDiluted')[2020].value == Decimal('2.33')
    assert normalize._annual_series(normalize._filed_by(gaap(view),'2023-03-26'),
        'EarningsPerShareDiluted')[2020].value == Decimal('2.06')


def test_explanation_later_than_native_source_and_wrapper_delays_correction():
    _,_,view=admitted_view(document_changes={'source_document':'https://www.sec.gov/Archives/edgar/data/1066923/000121390023022083/correction.htm',
        'source_published_at':datetime(2023,3,27,tzinfo=timezone.utc)})
    assert normalize._annual_series(normalize._filed_by(gaap(view),'2023-03-26'),
        'EarningsPerShareDiluted')[2020].value == Decimal('2.33')
    assert normalize._annual_series(normalize._filed_by(gaap(view),'2023-03-27'),
        'EarningsPerShareDiluted')[2020].value == Decimal('2.06')


def test_unqualified_separate_explanation_withholds_current_but_retains_association():
    _,outcome,view=admitted_view(document_changes={'source_document':'https://www.sec.gov/Archives/edgar/data/1066923/000121390023022083/correction.htm'})
    result=normalize._annual_series(gaap(view),'EarningsPerShareDiluted')
    assert result.unavailable[2020].reason == 'NATIVE_PRIMARY_DISCLOSURE_UNASSESSED'
    assert view.primary_inputs == (outcome,)
    assert normalize._annual_series(normalize._filed_by(gaap(view),'2023-03-23'),
        'EarningsPerShareDiluted')[2020].value == Decimal('2.33')


@pytest.mark.parametrize('wrapper',[False,True])
def test_exact_removed_source_or_wrapper_restores_only_original_target(wrapper):
    _,outcome,view=admitted_view(source_changes={'annual_accession':'0001213900-23-099999'})
    source=outcome.original.request.source
    filing=(source.annual_filed.isoformat(),source.annual_accession) if wrapper else (
        source.filed.isoformat(),source.accession)
    filtered=sync._without_filing(view,filing)
    selected=normalize._annual_series(gaap(filtered),'EarningsPerShareDiluted')[2020]
    assert selected.value == Decimal('2.33') and selected.primary_input is None
    assert filtered.primary_inputs == (outcome,)


def test_unrelated_removed_filing_does_not_restore_corrected_target():
    _,_,view=admitted_view()
    selected=normalize._annual_series(gaap(sync._without_filing(view,('2023-03-24','unrelated'))),
        'EarningsPerShareDiluted')[2020]
    assert selected.value == Decimal('2.06') and selected.primary_input is not None


def test_missing_assets_keep_actual_balance_date_and_stale_floor_at_snapshot_caller():
    def instant(value,end='2024-12-31'):
        entry=annual(value); entry.pop('start'); entry['end']=end
        return entry
    taxo,_=unavailable_taxonomy({'Assets':[instant(200)],
        'Goodwill':[instant(5,'2020-12-31')],
        'IntangibleAssetsNetExcludingGoodwill':[instant(7)]},'Assets')
    snapshot=normalize.build_snapshot('SYNTH',CIK,{'facts':{'us-gaap':taxo}})
    assert snapshot.total_assets is None and snapshot.goodwill is None
    assert snapshot.balance_sheet_date == date(2024,12,31)
    assert snapshot.intangibles.value == 7


@pytest.mark.parametrize('native,factor,expected',[
    ('0.123456789012345678901234567890123456789','-1','-0.123456789012345678901234567890123456789'),
    ('0.000','-1','-0.000'), ('0.000','2','0.000')])
def test_unary_applies_once_with_exact_digits_and_signed_zero(native,factor,expected):
    tree,outcome,view=admitted_view(native_text=native,unary_factor_text=factor)
    entry=view['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares'][99]
    assert entry['val'].as_tuple() == Decimal(expected).as_tuple()
    assert normalize.prepare_primary_inputs(view,(outcome,)) is view
    assert entry['val'].as_tuple() == Decimal(expected).as_tuple()
    assert tree['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares'][99]['val'] != entry['val']


def test_replacement_uses_exact_replacement_operand_and_private_association():
    _,outcome,view=admitted_view(replacement_text='1.5020600')
    selected=normalize._annual_series(gaap(view),'EarningsPerShareDiluted')[2020]
    assert selected.value.as_tuple() == Decimal('1.5020600').as_tuple()
    assert selected.primary_input is outcome and outcome.original.value == Decimal('2.06')
    assert outcome.replacement.lexical_text == '1.5020600'
    assert 'primary_input' not in sync._source(selected)


def test_selected_current_liability_cannot_be_assumed_zero_in_capital():
    def instant(value):
        entry=annual(value); entry.pop('start'); return entry
    taxo,_=unavailable_taxonomy({'Assets':[instant(100)],'LiabilitiesCurrent':[instant(9)]},
        'LiabilitiesCurrent')
    assumptions=set(); notes=[]
    assert normalize._capital_fact(taxo,date(2024,12,31),False,notes,
        assume_absent_all_zero=True,assumed_zero=assumptions) is None
    assert 'current_liabilities' not in assumptions and any('withheld' in note for note in notes)


def test_authoritative_intangible_total_ignores_unused_unknown_finite_part():
    def instant(value):
        entry=annual(value); entry.pop('start'); return entry
    taxo,_=unavailable_taxonomy({'IntangibleAssetsNetExcludingGoodwill':[instant(50)],
        'FiniteLivedIntangibleAssetsNet':[instant(99)],
        'IndefiniteLivedIntangibleAssetsExcludingGoodwill':[instant(4)]},'FiniteLivedIntangibleAssetsNet')
    assert normalize._intangibles(taxo,None).value == 50


def test_actual_sum_with_unknown_intangible_part_cannot_use_partial_amount():
    def instant(value):
        entry=annual(value); entry.pop('start'); return entry
    taxo,_=unavailable_taxonomy({'FiniteLivedIntangibleAssetsNet':[instant(99)],
        'IndefiniteLivedIntangibleAssetsExcludingGoodwill':[instant(4)]},'FiniteLivedIntangibleAssetsNet')
    assert normalize._intangibles(taxo,None) is None


def test_receipt_keeps_selected_unknown_year_and_private_operand_identity():
    _,_,view=admitted_view(unavailable=True)
    series=normalize._annual_series(gaap(view),'EarningsPerShareDiluted')
    parts={'annual_eps':series}
    normalize._restate_onto_receipt(parts,Decimal(5),'synthetic-cover')
    assert parts['annual_eps'].unavailable == series.unavailable
    _,outcome,view=admitted_view()
    selected=normalize._annual_series(gaap(view),'EarningsPerShareDiluted')[2020]
    parts={'annual_eps':{2020:selected}}
    normalize._restate_onto_receipt(parts,Decimal(5),'synthetic-cover')
    assert parts['annual_eps'][2020].primary_input is outcome
    assert parts['annual_eps'][2020].operation_inputs == (selected,)


@pytest.mark.parametrize('assume',[False,True])
def test_aggregate_unknown_preserves_reported_parts_and_blocks_grade_ev_and_history(assume):
    from screener.screens.enterprising import settled_debt,_c3_debt
    from screener.models import Status
    def instant(value):
        entry=annual(value); entry.pop('start'); return entry
    taxo,outcome=unavailable_taxonomy({'Assets':[instant(200)],'AssetsCurrent':[instant(150)],
        'Liabilities':[instant(100)], 'LiabilitiesCurrent':[instant(100)],
        'DebtLongtermAndShorttermCombinedAmount':[instant(999)],
        'LongTermDebtNoncurrent':[instant(40)],'DebtCurrent':[instant(5)]},
        'DebtLongtermAndShorttermCombinedAmount')
    snapshot=normalize.build_snapshot('SYNTH',CIK,{'facts':{'us-gaap':taxo}},assume_absent_zero=assume)
    assert snapshot.long_term_debt.value == 40 and snapshot.short_term_debt.value == 5
    assert snapshot.debt_unavailable == (outcome,)
    amount,inputs,reasons=settled_debt(snapshot)
    assert amount is None and inputs == (snapshot.long_term_debt,snapshot.short_term_debt) and reasons
    assert _c3_debt(snapshot).status == Status.INSUFFICIENT_DATA
    assert sync._debt_to_equity(snapshot) is None
    assert _c3_debt(replace(snapshot,current_assets=replace(snapshot.current_assets,value=Decimal(90)))).status == Status.FAIL
    historical=normalize.annual_ratios(taxo,{}, {}, {})
    assert 'debt_to_equity' not in historical.get(2024,{})


def test_ordinary_missing_total_still_uses_known_parts_and_no_private_barrier():
    from screener.screens.enterprising import settled_debt
    def instant(value):
        entry=annual(value); entry.pop('start'); return entry
    tree={'facts':{'us-gaap':{tag:tagdata('USD',[instant(value)]) for tag,value in (
        ('Assets',200),('AssetsCurrent',150),('Liabilities',100),('LiabilitiesCurrent',100),
        ('LongTermDebtNoncurrent',40),('DebtCurrent',5))}}}
    snapshot=normalize.build_snapshot('SYNTH',CIK,tree)
    assert settled_debt(snapshot)[0] == 45 and not snapshot.debt_unavailable


def test_conclusively_newer_complete_debt_parts_survive_old_unknown_rollup():
    from screener.screens.enterprising import settled_debt
    def instant(value,end='2024-12-31'):
        entry=annual(value); entry.pop('start'); entry['end']=end; return entry
    taxo,_=unavailable_taxonomy({'Assets':[instant(200)],'AssetsCurrent':[instant(150)],
        'Liabilities':[instant(100)],'LiabilitiesCurrent':[instant(100)],
        'DebtLongtermAndShorttermCombinedAmount':[instant(999,'2024-03-31')],
        'LongTermDebtNoncurrent':[instant(40)],'DebtCurrent':[instant(5)]},
        'DebtLongtermAndShorttermCombinedAmount')
    snapshot=normalize.build_snapshot('SYNTH',CIK,{'facts':{'us-gaap':taxo}})
    assert settled_debt(snapshot)[0] == 45 and not snapshot.debt_unavailable


def test_derived_eps_keeps_object_identical_actual_count_operand(monkeypatch):
    taxo={'WeightedAverageNumberOfDilutedSharesOutstanding':tagdata('shares',[annual(10)])}
    counts=normalize._annual_share_counts(taxo,{})
    monkeypatch.setattr(normalize,'_annual_share_counts',lambda *args,**kwargs:counts)
    income={2024:normalize._fact('income','NetIncomeLoss',annual(100),2024)}
    derived=normalize._derived_annual_eps(taxo,{}, {},income,{})
    assert derived[2024].derived_eps_inputs.count is counts[2024]
    assert derived[2024].derived_eps_inputs.income is income[2024]


@pytest.mark.parametrize('assume',[False,True])
def test_unknown_goodwill_never_becomes_zero_in_tangible_operating_assets(assume):
    def instant(value):
        entry=annual(value); entry.pop('start'); return entry
    taxo,_=unavailable_taxonomy({'Assets':[instant(100)],'LiabilitiesCurrent':[instant(20)],
        'DebtCurrent':[instant(0)],'CashAndCashEquivalentsAtCarryingValue':[instant(0)],
        'ShortTermInvestments':[instant(0)],'Goodwill':[instant(9)],
        'IntangibleAssetsNetExcludingGoodwill':[instant(0)],
        'LongTermInvestments':[instant(0)]},'Goodwill')
    assumptions=set(); notes=[]
    assert normalize._net_tangible_operating_assets_fact(taxo,date(2024,12,31),notes,
        assume_absent_all_zero=assume,assumed_zero=assumptions) is None
    assert 'goodwill' not in assumptions
    assert any('withheld' in note for note in notes)


def test_unknown_operating_income_never_becomes_assumed_zero_in_annual_returns():
    assets=annual(100); assets.pop('start')
    taxo,_=unavailable_taxonomy({'OperatingIncomeLoss':[annual(9)],'Assets':[assets]},'OperatingIncomeLoss')
    operating=normalize._annual_operating_income(taxo)
    result=normalize._annual_operating_returns(taxo,operating,years=1,
        assume_absent_zero=True,fallback_end=date(2024,12,31))
    assert result[2024].operating_income is None and result[2024].nopat is None
    assert 'operating_income' not in result[2024].assumed_zero
    assert any('withheld' in note for note in result[2024].caveats)


def test_unavailable_subtraction_operand_is_not_optional_absence():
    def instant(value):
        entry=annual(value); entry.pop('start'); return entry
    taxo,_=unavailable_taxonomy({'GoodwillGross':[instant(100)],
        'GoodwillImpairedAccumulatedImpairmentLoss':[instant(9)]},
        'GoodwillImpairedAccumulatedImpairmentLoss')
    assert normalize._derived_instant(taxo,'Goodwill','GoodwillGross',
        'GoodwillImpairedAccumulatedImpairmentLoss',None,subtrahend_optional=True) is None
    assert normalize._goodwill_and_intangibles(taxo,None)[0] is None


def test_missing_long_debt_lease_note_cannot_say_no_debt_reported():
    def instant(value):
        entry=annual(value); entry.pop('start'); return entry
    taxo,_=unavailable_taxonomy({'Assets':[instant(200)],'AssetsCurrent':[instant(150)],
        'Liabilities':[instant(100)],'LiabilitiesCurrent':[instant(100)],
        'LongTermDebtNoncurrent':[instant(99)],'DebtCurrent':[instant(5)],
        'OperatingLeaseLiability':[instant(70)]},'LongTermDebtNoncurrent')
    snapshot=normalize.build_snapshot('SYNTH',CIK,{'facts':{'us-gaap':taxo}})
    lease=[note['text'] for note in snapshot.context_notes if note['kind']=='Lease obligations']
    assert lease and 'long-term debt unavailable' in lease[0]
    assert 'no long-term debt reported' not in lease[0]


def test_selected_unavailable_dollar_scope_blocks_other_tag_but_not_other_role():
    taxo, outcome = unavailable_taxonomy({'NetIncomeLoss':[annual(100)],
        'ProfitLoss':[annual(200)], 'Revenues':[annual(300)]}, 'NetIncomeLoss')
    income = normalize._annual_net_income(taxo)
    assert 2024 not in income and income.unavailable[2024] is outcome
    assert normalize._annual_revenue(taxo)[2024].value == 300


def test_valid_current_dollar_scope_beats_older_unavailable_scope():
    taxo, _ = unavailable_taxonomy({'NetIncomeLoss':[dur('2023-01-01','2023-12-31',100,
        filed='2024-02-20',accn='old')], 'ProfitLoss':[annual(200)]}, 'NetIncomeLoss')
    assert normalize._annual_net_income(taxo)[2024].value == 200


@pytest.mark.parametrize('amount',[0, 1, 9000000, -9000000])
def test_unknown_revenue_never_uses_rejected_magnitude(amount):
    taxo, outcome = unavailable_taxonomy({'Revenues':[annual(amount)],
        'RevenueFromContractWithCustomerExcludingAssessedTax':[annual(90)]}, 'Revenues')
    result = normalize._annual_revenue(taxo)
    assert 2024 not in result and result.unavailable[2024] is outcome


def test_distant_unavailable_revenue_cannot_block_current_top_line():
    taxo, _ = unavailable_taxonomy({'Revenues':[dur('2020-01-01','2020-12-31',9000000,
        filed='2021-04-01',accn='old')],
        'RevenueFromContractWithCustomerExcludingAssessedTax':[annual(90)]}, 'Revenues')
    assert normalize._annual_revenue(taxo)[2024].value == 90


@pytest.mark.parametrize('amount',[0, 1, 9000000, -9000000])
def test_unknown_debt_never_uses_rejected_magnitude(amount):
    one = annual(amount); one.pop('start')
    two = annual(90); two.pop('start')
    taxo, _ = unavailable_taxonomy({'LongTermDebtNoncurrent':[one], 'LongTermDebt':[two]},
        'LongTermDebtNoncurrent')
    assert normalize._latest_instant_across(taxo,'Debt',('LongTermDebtNoncurrent','LongTermDebt'),
        largest_wins=True) is None


def test_selected_instant_withholds_but_losing_same_tag_does_not():
    old = annual(7,filed='2025-01-01',accession='old'); old.pop('start')
    current = annual(9); current.pop('start')
    selected,_ = unavailable_taxonomy({'LongTermDebt':[old,current]}, 'LongTermDebt',index=1)
    losing,_ = unavailable_taxonomy({'LongTermDebt':[old,current]}, 'LongTermDebt',index=0)
    assert normalize._latest_instant(selected,'Debt',('LongTermDebt',)) is None
    assert normalize._latest_instant(losing,'Debt',('LongTermDebt',)).value == 9


def test_derived_eps_cannot_fill_withheld_reported_eps_or_count_or_preferred():
    taxo, _ = unavailable_taxonomy({'EarningsPerShareDiluted':[annual(99)],
        'NetIncomeLoss':[annual(100)],
        'WeightedAverageNumberOfDilutedSharesOutstanding':[annual(10)]},
        'EarningsPerShareDiluted',unit='USD/shares')
    # Separate units do not fabricate a denominator from this synthetic EPS taxonomy.
    reported = normalize._annual_eps(taxo)
    income = {2024:normalize._fact('income','NetIncomeLoss',annual(100),2024)}
    assert normalize._derived_annual_eps(taxo,{},reported,income,{}) == {}
    counts_taxo,_ = unavailable_taxonomy({
        'WeightedAverageNumberOfDilutedSharesOutstanding':[annual(10)],
        'WeightedAverageNumberOfSharesOutstandingBasic':[annual(20)]},
        'WeightedAverageNumberOfDilutedSharesOutstanding',unit='shares')
    assert 2024 not in normalize._annual_share_counts(counts_taxo,{})
    assert normalize._derived_annual_eps(counts_taxo,{},{},income,{}) == {}


@pytest.mark.parametrize('number',[1000000,1000000.0,Decimal('1000000')])
def test_decimal_current_and_historical_evidence_keeps_actual_floor(number):
    entry = annual(number); entry.pop('start')
    facts = {'us-gaap':{'LongTermDebtNoncurrent':tagdata('USD',[entry])}}
    assert 'debt' not in normalize._absent_zero_candidates(facts)
    investments = {'us-gaap':{'LongTermInvestments':tagdata('USD',[entry])}}
    assert 'noncurrent_investments' not in normalize._historical_absent_zero_candidates(
        investments,date(2024,12,31))


@pytest.mark.parametrize('number',[1,1.0,Decimal('1')])
def test_decimal_unfamiliar_payout_is_unknown(number):
    assert normalize._dividend({'CustomDividendPayment':tagdata('USD',[annual(number)])},
        date(2024,12,31)) == (None,None)

def selected_missing_eps_snapshot(*, assume=False):
    rows=[annual(7)] + [dur(f'{year}-01-01',f'{year}-12-31',1,
        accn='current',filed='2025-02-20') for year in range(2019,2024)]
    taxo,outcome=unavailable_taxonomy({'EarningsPerShareDiluted':rows},
        'EarningsPerShareDiluted',unit='USD/shares')
    asset=annual(200); asset.pop('start'); taxo['Assets']=tagdata('USD',[asset])
    snap=normalize.build_snapshot('SYNTH',CIK,{'facts':{'us-gaap':taxo}},
        assume_absent_zero=assume)
    assert snap.annual_eps.unavailable == {2024:outcome}
    return snap


@pytest.mark.parametrize('assume',[False,True])
def test_actual_row_profiles_keep_unavailable_latest_year(monkeypatch,assume):
    from screener import profiles
    from screener.screens import enterprising
    from screener.models import Status
    snap=selected_missing_eps_snapshot(assume=assume)
    monkeypatch.setattr(sync,'build_snapshot',lambda *a,**k:snap)
    result=sync._derive_result(CIK,'SYNTH',{'facts':{'us-gaap':{}}},assume_absent_zero=assume)
    row=result.row
    assert row['ch13']['latest_fy']==2024 and row['ch13']['avg_recent'] is None
    assert 'eps' not in row['sources'] and set(row['annual_eps'])==set(map(str,range(2019,2024)))
    row.update(price=10,bvps=10,first_filed='2019-01-01')
    assert profiles._pe3(row) is None
    assert profiles._enterprising(row,profiles.PROFILE_OPERATING)['growth_modern_4fy']=={
        'status':'INSUFFICIENT_DATA','label':'Modern 4-FY EPS comparison','base_fy':2020,'latest_fy':2024}
    assert profiles._defensive(row,profiles.PROFILE_OPERATING)['tests']['stability_10y']=='INSUFFICIENT_DATA'
    assert enterprising._c4_earnings_stability(snap).status==Status.INSUFFICIENT_DATA
    assert enterprising._eps_growth(snap) is None


def test_empty_numeric_known_selected_year_is_safe_and_has_no_new_public_fields():
    from screener import ch13,profiles
    assert ch13.eps_stats({}) is None
    stats=ch13.eps_stats({},latest_fy=2024)
    assert stats['latest_fy']==2024 and stats['avg_recent'] is None and stats['ten_year_present']==0
    assert set(stats)==set(ch13.eps_stats({2024:Decimal('1')}))
    row={'ch13':stats,'annual_eps':{},'price':10,'bvps':10}
    assert profiles._pe3(row) is None
    assert profiles._enterprising(row,profiles.PROFILE_OPERATING)['growth_modern_4fy']['status']=='INSUFFICIENT_DATA'
    assert profiles._defensive(row,profiles.PROFILE_OPERATING)['tests']['stability_10y']=='INSUFFICIENT_DATA'


def test_typed_profile_latest_label_cannot_slide_numeric_window_back():
    from screener import profiles
    row={'annual_eps':{'2022':1,'2023':1,'2024':1},'price':10,'ch13':{'latest_fy':2023}}
    assert profiles._pe3(row)==10
    for untyped in (True,2025.0,'2025'):
        row['ch13']['latest_fy']=untyped
        assert profiles._pe3(row)==10
    row['ch13']['latest_fy']=2025
    assert profiles._pe3(row) is None


@pytest.mark.parametrize('stale',[False,True])
def test_selected_unavailable_actual_period_keeps_staleness_and_loss_precedence(stale):
    from screener.models import Status
    from screener.screens.enterprising import _c4_earnings_stability
    snap=selected_missing_eps_snapshot()
    snap.annual_eps[2023]=replace(snap.annual_eps[2023],value=Decimal('-1'))
    snap=replace(snap,balance_sheet_date=date(2027 if stale else 2024,12,31))
    assert _c4_earnings_stability(snap).status==(Status.INSUFFICIENT_DATA if stale else Status.FAIL)


def test_unknown_base_eps_withholds_growth_but_outside_window_does_not():
    from screener.screens.enterprising import _eps_growth
    snap=selected_missing_eps_snapshot()
    outcome=snap.annual_eps.unavailable[2024]
    numeric={**dict(snap.annual_eps),2024:replace(snap.annual_eps[2023],value=Decimal('2'))}
    series=normalize._SelectedAnnualSeries(numeric,{2019:outcome})
    assert _eps_growth(replace(snap,annual_eps=series)) is None
    series=normalize._SelectedAnnualSeries(numeric,{2010:outcome})
    assert _eps_growth(replace(snap,annual_eps=series)).latest_fiscal_year==2024


def test_common_owner_unknown_keeps_reason_and_known_direct_ignores_unused_claim():
    taxo,outcome=unavailable_taxonomy({'NetIncomeLossAvailableToCommonStockholdersBasic':[annual(9)],
        'NetIncomeLoss':[annual(100)]},'NetIncomeLossAvailableToCommonStockholdersBasic')
    result=normalize._common_owner_earnings(taxo)
    assert 2024 not in result and result.unavailable[2024] is outcome
    taxo,outcome=unavailable_taxonomy({'NetIncomeLossAvailableToCommonStockholdersBasic':[annual(90)],
        'NetIncomeLoss':[annual(100)],'PreferredStockDividendsAndOtherAdjustments':[annual(9)]},
        'PreferredStockDividendsAndOtherAdjustments')
    assert normalize._common_owner_earnings(taxo)[2024].value==90
    taxo.pop('NetIncomeLossAvailableToCommonStockholdersBasic')
    result=normalize._common_owner_earnings(taxo)
    assert 2024 not in result and result.unavailable[2024] is outcome


@pytest.mark.parametrize('claim',[None,0,9])
def test_common_owner_no_primary_optional_claim_keeps_original_arithmetic(claim):
    taxo={'NetIncomeLoss':tagdata('USD',[annual(100)])}
    if claim is not None:
        taxo['PreferredStockDividendsAndOtherAdjustments']=tagdata('USD',[annual(claim)])
    result=normalize._common_owner_earnings(taxo)
    assert type(result) is dict and result[2024].value==100-(claim or 0)


def test_private_unary_extreme_exponent_keeps_exact_coefficient():
    _,_,view=admitted_view(native_text='1.23456789012345678901234567890123456789',
        unary_factor_text='-1e1000000')
    entry=view['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares'][99]
    assert entry['val'].as_tuple()==Decimal('-1.23456789012345678901234567890123456789e1000000').as_tuple()


def optional_operand_taxonomy(target):
    def instant(value):
        item=annual(value);item.pop('start');return item
    values={'Assets':[instant(200)],'LiabilitiesCurrent':[instant(100)],
        'DebtCurrent':[instant(10)],'CashAndCashEquivalentsAtCarryingValue':[instant(20)],
        'ShortTermInvestments':[instant(5)],'Goodwill':[instant(0)],
        'FiniteLivedIntangibleAssetsNet':[instant(0)],'LongTermInvestments':[instant(0)]}
    if target=='RestrictedCash':
        values.pop('CashAndCashEquivalentsAtCarryingValue')
        values.update(CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents=[instant(20)],
            RestrictedCash=[instant(3)])
    else:
        values.update(OperatingLeaseRightOfUseAsset=[instant(20)],
            OperatingLeaseLiabilityCurrent=[instant(3)])
        if target=='OperatingLeaseRightOfUseAsset':
            old=instant(5);old.update(end='2023-12-31',accn='old',filed='2024-02-20')
            values[target].append(old)
    return unavailable_taxonomy(values,target)[0]


@pytest.mark.parametrize('target',['RestrictedCash','OperatingLeaseLiabilityCurrent',
                                   'OperatingLeaseRightOfUseAsset'])
def test_actual_unknown_optional_operands_cannot_be_zero(target):
    taxo=optional_operand_taxonomy(target); notes=[]; assumed=set()
    if target=='RestrictedCash':
        result=normalize._capital_fact(taxo,date(2024,12,31),True,notes,
            assume_absent_all_zero=True,assumed_zero=assumed)
    else:
        result=normalize._net_tangible_operating_assets_fact(taxo,date(2024,12,31),notes,
            assume_absent_all_zero=True,assumed_zero=assumed,
            lease_neutral=target=='OperatingLeaseRightOfUseAsset')
    assert result is None and assumed==set()
    assert any('withheld' in note for note in notes)


def test_actual_admitted_ifrs_target_keeps_statement_family():
 from types import SimpleNamespace
 raw=json.dumps({'cik':int(CIK),'facts':{'ifrs-full':{'Assets':{'units':{'USD':[{
  'val':200,'end':'2020-12-31','form':'20-F','accn':'old','filed':'2021-04-15'}]}}}}}).encode()
 cf=CapturedSource('company_facts',hashlib.sha256(raw).hexdigest(),
 'https://data.sec.gov/api/xbrl/companyfacts/CIK0001066923.json',1,2,3,'EXACT_LEXICAL')
 tree=sec_json.decode_company_facts(raw,source=cf)
 target=tree['facts']['ifrs-full']['Assets']['units']['USD'][0][sec_json.ORIGIN_KEY]
 body,req=fixture('150')
 body=body.replace(b'<x:startDate>2020-01-01</x:startDate><x:endDate>2020-12-31</x:endDate>',
 b'<x:instant>2020-12-31</x:instant>')
 req=bind(body,replace(req,context=replace(req.context,start=None,end=None,instant=date(2020,12,31))))
 decl=replace(mapping(body,req),canonical_tag='Assets',target_origin=target)
 outcome=evidence.admit_primary_inputs(CIK,(decl,),{cf.artifact_sha256:raw,req.source.artifact_sha256:body})[0]
 assert isinstance(outcome,AdmittedPrimaryInput)
 view=normalize.prepare_primary_inputs(tree,(outcome,))
 fact=normalize._latest_instant(normalize._ifrs_as_us_gaap(view['facts']['ifrs-full']),
 'Assets',('Assets',))
 assert fact.value==150 and fact.primary_input is outcome
 snap=SimpleNamespace(total_assets=fact,total_liabilities=None,current_assets=None,current_liabilities=None)
 assert sync._statement_source_namespace(snap)=='ifrs-full'


def historical_instant(value):
 e=annual(value);e.pop('start');return e

def historical_claim_taxonomy(target):
 values={'Assets':[historical_instant(200)],'Liabilities':[historical_instant(100)],'AssetsCurrent':[historical_instant(150)],
 'LiabilitiesCurrent':[historical_instant(50)],'StockholdersEquity':[historical_instant(100)],
 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest':[historical_instant(120)],
 'MinorityInterest':[historical_instant(20)],'PreferredStockValue':[historical_instant(10)],
 'WeightedAverageNumberOfDilutedSharesOutstanding':[annual(10)],'NetIncomeLoss':[annual(9)]}
 taxo,_=unavailable_taxonomy(values,target)
 taxo['WeightedAverageNumberOfDilutedSharesOutstanding']=tagdata('shares',[annual(10)])
 return taxo

def historical_claim_ratios(taxo):
 return normalize.annual_ratios(taxo,normalize._annual_net_income(taxo),{},{},years=1)

def test_unknown_preferred_cannot_be_zero_in_historical_common_equity():
 row=historical_claim_ratios(historical_claim_taxonomy('PreferredStockValue'))[2024]
 assert 'bvps' not in row and 'return_on_book' not in row and 'ncavps' not in row

def test_unknown_parent_equity_cannot_be_replaced_by_group_identity():
 row=historical_claim_ratios(historical_claim_taxonomy('StockholdersEquity'))[2024]
 assert 'bvps' not in row and 'return_on_book' not in row

def test_unknown_minority_cannot_be_zero_for_group_equity_fallback():
 taxo=historical_claim_taxonomy('MinorityInterest');taxo.pop('StockholdersEquity')
 row=historical_claim_ratios(taxo)[2024]
 assert 'bvps' not in row and 'return_on_book' not in row and 'ncavps' not in row

@pytest.mark.parametrize('unused_target',['MinorityInterest',
    'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'])
def test_historical_known_direct_parent_ignores_unused_uncertain_equity(unused_target):
    taxo=historical_claim_taxonomy(unused_target)
    row=historical_claim_ratios(taxo)[2024]
    assert row['bvps']==9 and row['return_on_book']==10
    assert row['current_ratio']==3
    if unused_target=='MinorityInterest':
        assert 'ncavps' not in row


def test_historical_unknown_claim_keeps_independent_measurable_fields_and_reason():
    row=historical_claim_ratios(historical_claim_taxonomy('PreferredStockValue'))[2024]
    assert row['current_ratio']==3
    assert row['unavailable_inputs'] and any('PreferredStockValue' in reason for reason in row['unavailable_inputs'])


def test_historical_known_zero_and_ordinary_missing_claim_keep_existing_arithmetic():
    values={'Assets':[historical_instant(200)],'Liabilities':[historical_instant(100)],
        'StockholdersEquity':[historical_instant(100)],'AssetsCurrent':[historical_instant(150)],
        'LiabilitiesCurrent':[historical_instant(50)],'NetIncomeLoss':[annual(9)]}
    for zero in (False,True):
        taxo={tag:tagdata('USD',entries) for tag,entries in values.items()}
        taxo['WeightedAverageNumberOfDilutedSharesOutstanding']=tagdata('shares',[annual(10)])
        if zero:
            taxo['PreferredStockValue']=tagdata('USD',[historical_instant(0)])
            taxo['MinorityInterest']=tagdata('USD',[historical_instant(0)])
        row=historical_claim_ratios(taxo)[2024]
        assert row['bvps']==10 and row['ncavps']==5 and row['return_on_book']==9
        assert 'unavailable_inputs' not in row

def test_actual_native_fact_public_source_and_repr_do_not_leak_private_carriers():
    _,outcome,view=admitted_view()
    fact=normalize._annual_eps(gaap(view))[2020]
    assert fact.primary_input is outcome
    public=sync._source(fact)
    assert public['tag']==fact.provenance.tag and public['accn']==outcome.original.request.source.accession
    text=json.dumps(public)
    for key in ('primary_input','target_origin','captured_source','operation_inputs','derived_eps_inputs'):
        assert key not in text and key not in repr(fact)
    restored=pickle.loads(pickle.dumps(fact))
    assert restored==fact and restored.primary_input==outcome


def test_absent_private_fields_keep_legacy_fact_identity_and_repr():
    legacy=normalize._fact('income','NetIncomeLoss',annual(100),2024)
    assert legacy.primary_input is None and legacy.derived_eps_inputs is None and legacy.operation_inputs==()
    private=replace(legacy,operation_inputs=[legacy])
    assert private==legacy and repr(private)==repr(legacy)
    assert private.operation_inputs==(legacy,)


def test_native_count_not_rewritten_by_adjacent_cf_scale_heuristic():
 tag='WeightedAverageNumberOfDilutedSharesOutstanding'
 rows=[dur(f'{y}-01-01',f'{y}-12-31',v,accn=f'cf-{y}',filed='2023-03-20')
       for y,v in [(2019,1000),(2020,1),(2021,1000)]]
 raw=json.dumps({'cik':int(CIK),'facts':{'us-gaap':{tag:{'units':{'shares':rows}}}}}).encode()
 cf=CapturedSource('company_facts',hashlib.sha256(raw).hexdigest(),
 'https://data.sec.gov/api/xbrl/companyfacts/CIK0001066923.json',1,2,3,'EXACT_LEXICAL')
 tree=sec_json.decode_company_facts(raw,source=cf)
 target=tree['facts']['us-gaap'][tag]['units']['shares'][1][sec_json.ORIGIN_KEY]
 body,req=fixture('1')
 body=body.replace(b'<x:measure>iso:USD</x:measure>',b'<x:measure>x:shares</x:measure>')
 req=bind(body,replace(req,unit_numerator=('{http://www.xbrl.org/2003/instance}shares',)))
 decl=replace(mapping(body,req),canonical_tag=tag,target_origin=target)
 outcome=evidence.admit_primary_inputs(CIK,(decl,),{cf.artifact_sha256:raw,req.source.artifact_sha256:body})[0]
 assert isinstance(outcome,AdmittedPrimaryInput)
 view=normalize.prepare_primary_inputs(tree,(outcome,))
 taxo=normalize._with_fiscal_calendar(view['facts']['us-gaap'])
 # Separate synthetic selected maps activate the legacy owner; gaap has no
 # filing-local income/EPS identity able to authorize this count correction.
 one=normalize._fact('income','NetIncomeLoss',dur('2020-01-01','2020-12-31',1),2020)
 counts=normalize._annual_share_counts(taxo,{}, {2020:one}, {2020:one})
 assert counts[2020].primary_input is outcome
 assert counts[2020].value==1


def owner_partial_case(target):
 def rows(amount):
  return [annual(amount),dur('2023-01-01','2023-12-31',amount,
   accn='older',filed='2024-02-20')]
 values={'NetIncomeLossAvailableToCommonStockholdersBasic':rows(100),
 'DepreciationDepletionAndAmortization':rows(10),
 'PaymentsToAcquirePropertyPlantAndEquipment':rows(5),
 'NetCashProvidedByUsedInOperatingActivities':rows(50)}
 if target not in values:
  values[target]=rows(20)
 taxo,_=unavailable_taxonomy(values,target)
 counts={year:normalize._fact('count','WeightedAverageNumberOfDilutedSharesOutstanding',
  dur(f'{year}-01-01',f'{year}-12-31',10,accn='current',filed='2025-02-20'),year)
  for year in (2023,2024)}
 return taxo,counts


@pytest.mark.parametrize('target',['NetIncomeLossAvailableToCommonStockholdersBasic',
 'DepreciationDepletionAndAmortization','PaymentsToAcquirePropertyPlantAndEquipment'])
def test_partial_current_owner_keeps_independent_fcf_and_older_history(target):
 taxo,counts=owner_partial_case(target)
 owner=normalize._owner_earnings(taxo,{},None,counts)
 assert owner is not None and owner.fiscal_year==2024
 assert owner.all_capex_floor is None
 assert owner.free_cash_flow is None if target=='PaymentsToAcquirePropertyPlantAndEquipment' else owner.free_cash_flow.value==45
 assert owner.annual[2023].all_capex_floor.value==105
 assert 2024 in owner.annual and owner.annual[2024].all_capex_floor is None
 assert any(target in reason for reason in owner.caveats)
 snap=replace(selected_missing_eps_snapshot(),owner_earnings=owner)
 row=sync._owner_earnings_row(snap)
 assert row['fiscal_year']==2024 and row['all_capex_floor'] is None
 assert row['annual_per_share']['2023']['all_capex_floor']==105
 assert row['annual_per_share']['2024']['all_capex_floor'] is None


@pytest.mark.parametrize('target', ['DepreciationDepletionAndAmortization',
                                  'PaymentsToAcquirePropertyPlantAndEquipment'])
def test_partial_owner_maintenance_is_independent_known_income(target):
    taxo, counts = owner_partial_case(target)
    owner = normalize._owner_earnings(taxo, {}, None, counts)
    assert owner.maintenance_estimate.value == 100
    assert owner.annual[2024].maintenance_estimate.value == 100
    assert owner.all_capex_floor is None
    assert owner.annual[2024].reported_earnings.value == 100
    residual = owner.annual[2024].other_operating_cash_flow_adjustments
    if target.startswith('Depreciation'):
        assert residual is None
    else:
        assert residual.value == -60


def test_authoritative_combined_da_ignores_unknown_additive_part():
    taxo, counts = owner_partial_case('Depreciation')
    owner = normalize._owner_earnings(taxo, {}, None, counts)
    assert owner.all_capex_floor.value == 105
    assert owner.annual[2024].depreciation_and_amortisation.value == 10


def test_required_unknown_da_part_cannot_be_partial_sum():
    values = {'NetIncomeLossAvailableToCommonStockholdersBasic': [annual(100)],
              'Depreciation': [annual(10)], 'AmortizationOfIntangibleAssets': [annual(20)],
              'PaymentsToAcquirePropertyPlantAndEquipment': [annual(5)],
              'NetCashProvidedByUsedInOperatingActivities': [annual(50)]}
    taxo, _ = unavailable_taxonomy(values, 'AmortizationOfIntangibleAssets')
    count = normalize._fact('count', 'WeightedAverageNumberOfDilutedSharesOutstanding', annual(10), 2024)
    owner = normalize._owner_earnings(taxo, {}, None, {2024: count})
    assert owner.fiscal_year == 2024 and owner.all_capex_floor is None
    assert owner.maintenance_estimate.value == 100 and owner.free_cash_flow.value == 45
    assert owner.annual[2024].depreciation_and_amortisation is None


@pytest.mark.parametrize('amount', [0, 10])
def test_no_primary_owner_known_zero_and_known_amount_keep_original_formula(amount):
    values = {'NetIncomeLossAvailableToCommonStockholdersBasic': [annual(100)],
              'DepreciationDepletionAndAmortization': [annual(amount)],
              'PaymentsToAcquirePropertyPlantAndEquipment': [annual(5)],
              'NetCashProvidedByUsedInOperatingActivities': [annual(50)]}
    taxo = {tag: tagdata('USD', rows) for tag, rows in values.items()}
    owner = normalize._owner_earnings(taxo, {}, None)
    assert owner.all_capex_floor.value == 95 + amount
    assert owner.maintenance_estimate.value == 100 and owner.free_cash_flow.value == 45
    del taxo['DepreciationDepletionAndAmortization']
    assert normalize._owner_earnings(taxo, {}, None) is None


from screener.models import Status

def instant(value):
    result = annual(value)
    result.pop('start')
    return result


def primary_claim_snapshot(target, *, assumed=False, direct_liabilities=True, parent=True, group=True,
             liquidation=None, temporary=False):
    tags = {'Assets': [instant(200)], 'LiabilitiesAndStockholdersEquity': [instant(200)],
            'AssetsCurrent': [instant(150)], 'LiabilitiesCurrent': [instant(50)],
            'Goodwill': [instant(0)], 'IntangibleAssetsNetExcludingGoodwill': [instant(0)],
            'MinorityInterest': [instant(20)], 'PreferredStockValue': [instant(0 if temporary else 10)],
            'NetIncomeLoss': [annual(900000)], 'Revenues': [annual(10000000)]}
    if direct_liabilities:
        tags['Liabilities'] = [instant(100)]
    if parent:
        tags['StockholdersEquity'] = [instant(100)]
    if group:
        tags['StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'] = [instant(120)]
    if liquidation is not None:
        tags['PreferredStockLiquidationPreferenceValue'] = [instant(liquidation)]
    if temporary:
        tags['TemporaryEquityCarryingAmountAttributableToParent'] = [instant(10)]
    taxo, outcome = unavailable_taxonomy(tags, target)
    taxo['CommonStockSharesOutstanding'] = tagdata('shares', [instant(10)])
    snap = normalize.build_snapshot('SYNTH', CIK, {'facts': {'us-gaap': taxo}},
                                    assume_absent_zero=assumed)
    return snap, taxo, outcome


def primary_claim_row(name, snap, taxo, outcome, monkeypatch):
    from screener.models import Quote, Status
    from screener.screens.enterprising import _c7_tangible_asset_valuation
    quote = Quote(Decimal(1), datetime(2025, 3, 1, tzinfo=timezone.utc), 'synthetic')
    c7 = _c7_tangible_asset_valuation(snap, quote)
    # Reuse this exact successful Snapshot in the actual row owner; no second build.
    monkeypatch.setattr(sync, 'build_snapshot', lambda *args, **kwargs: snap)
    result = sync._derive_result(CIK, 'SYNTH', {'facts': {'us-gaap': taxo}})
    assert result.snapshot is snap and result.status == 'ok'
    return c7, result.row


@pytest.mark.parametrize('assumed', [False, True])
@pytest.mark.parametrize('target,options', [
    ('PreferredStockValue', {}),
    ('MinorityInterest', {}),
    ('TemporaryEquityCarryingAmountAttributableToParent', {'temporary': True}),
])
def test_selected_unavailable_senior_claim_is_not_zero_in_current_screen_or_row(monkeypatch, assumed, target, options):
    snap, taxo, outcome = primary_claim_snapshot(target, assumed=assumed, **options)
    c7, row = primary_claim_row(f'{target}:{assumed}', snap, taxo, outcome, monkeypatch)
    assert c7.status is Status.INSUFFICIENT_DATA
    assert row['bvps'] is None and row['tbvps'] is None and row['ncavps'] is None
    assert row['profitability']['on_book'] is None and row['profitability']['on_net_tangible_assets'] is None


def test_selected_unavailable_group_equity_cannot_choose_parent_fallback_current_liabilities(monkeypatch):
    target = 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'
    snap, taxo, outcome = primary_claim_snapshot(target, direct_liabilities=False)
    c7, row = primary_claim_row(target, snap, taxo, outcome, monkeypatch)
    assert snap.total_liabilities is None
    assert c7.status is Status.INSUFFICIENT_DATA and row['bvps'] is None


def test_authoritative_liquidation_preference_ignores_unused_carried_preferred(monkeypatch):
    snap, taxo, outcome = primary_claim_snapshot('PreferredStockValue', liquidation=5)
    c7, row = primary_claim_row('unused-preferred-competitor', snap, taxo, outcome, monkeypatch)
    assert snap.preferred_stock.value == 5
    assert c7.status is Status.PASS and row['bvps'] == 7.5


def test_direct_valid_liabilities_ignore_unused_unavailable_parent_equity(monkeypatch):
    snap, taxo, outcome = primary_claim_snapshot('StockholdersEquity')
    c7, row = primary_claim_row('unused-parent-equity', snap, taxo, outcome, monkeypatch)
    assert snap.total_liabilities.value == 100
    assert c7.status is Status.PASS and row['bvps'] == 7


def test_valid_group_equity_identity_ignores_unused_unavailable_parent_equity(monkeypatch):
    snap, taxo, outcome = primary_claim_snapshot('StockholdersEquity', direct_liabilities=False)
    c7, row = primary_claim_row('valid-group-identity', snap, taxo, outcome, monkeypatch)
    assert snap.total_liabilities.value == 80 and snap.noncontrolling_interest.value == 20
    assert c7.status is Status.PASS and row['bvps'] == 9


def test_parent_only_identity_ignores_unused_noncontrolling_deduction(monkeypatch):
    snap, taxo, outcome = primary_claim_snapshot('MinorityInterest', direct_liabilities=False, group=False)
    c7, row = primary_claim_row('parent-only-identity', snap, taxo, outcome, monkeypatch)
    assert snap.total_liabilities.value == 100 and snap.noncontrolling_interest is None
    assert c7.status is Status.PASS and row['bvps'] == 9



def optional_owner_source(target, *, primary_wc=True, alternate_wc=False):
    tags = {
        'NetIncomeLoss': [annual(100)],
        'DepreciationDepletionAndAmortization': [annual(10)],
        'PaymentsToAcquirePropertyPlantAndEquipment': [annual(20)],
        'NetCashProvidedByUsedInOperatingActivities': [annual(150)],
        'Revenues': [annual(200)],
        'ShareBasedCompensation': [annual(7)],
        'PaymentsToAcquireBusinessesNetOfCashAcquired': [annual(11)],
        'PaymentsToDevelopSoftware': [annual(3)],
        'PaymentsToAcquireIntangibleAssets': [annual(4)],
    }
    if primary_wc:
        tags['IncreaseDecreaseInOperatingCapital'] = [annual(5)]
    if alternate_wc:
        tags['IncreaseDecreaseInOperatingAssetsAndLiabilities'] = [annual(-50)]
    if target is None:
        taxo = {tag: tagdata('USD', rows) for tag, rows in tags.items()}
        taxo = normalize._with_fiscal_calendar(taxo, cik=CIK)
        outcome = None
    else:
        taxo, outcome = unavailable_taxonomy(tags, target)
    taxo['WeightedAverageNumberOfDilutedSharesOutstanding'] = tagdata('shares', [annual(10)])
    counts = normalize._annual_union(taxo, ('WeightedAverageNumberOfDilutedSharesOutstanding',), unit=('shares',))
    oe = normalize._owner_earnings(taxo, {}, None, annual_share_counts=counts, cik=CIK)
    assert oe is not None
    return taxo, outcome, oe


def test_unknown_sbc_withholds_only_adjusted_sbc_fields_and_preserves_base_fcf():
    _, outcome, oe = optional_owner_source('ShareBasedCompensation')
    assert oe.free_cash_flow.value == 130
    assert oe.free_cash_flow_after_stock_compensation is None
    assert oe.stock_compensation_to_free_cash_flow is None
    # The established residual owns unseparated compensation; it is not a reported zero.
    assert oe.annual[2024].other_operating_cash_flow_adjustments.value == 45


def test_unknown_acquisition_withholds_only_adjusted_acquisition_fields_and_preserves_base_fcf():
    _, outcome, oe = optional_owner_source('PaymentsToAcquireBusinessesNetOfCashAcquired')
    assert oe.free_cash_flow.value == 130
    assert oe.free_cash_flow_after_acquisitions is None
    assert oe.acquisitions_to_free_cash_flow is None


def test_unknown_working_capital_without_lower_alias_preserves_base_fcf_and_unseparated_residual():
    _, outcome, oe = optional_owner_source('IncreaseDecreaseInOperatingCapital')
    assert oe.free_cash_flow.value == 130
    assert oe.working_capital_cash_effect is None
    assert oe.operating_cash_flow_before_working_capital is None
    assert oe.annual[2024].other_operating_cash_flow_adjustments.value == 33


def test_selected_unknown_working_capital_cannot_fall_through_to_lower_alias():
    _, outcome, oe = optional_owner_source('IncreaseDecreaseInOperatingCapital', alternate_wc=True)
    assert oe.free_cash_flow.value == 130
    assert oe.working_capital_cash_effect is None
    assert oe.operating_cash_flow_before_working_capital is None
    assert oe.annual[2024].working_capital_cash_effect is None


def test_known_working_capital_primary_outranks_unused_lower_alias():
    _, outcome, oe = optional_owner_source(None, alternate_wc=True)
    assert oe.free_cash_flow.value == 130
    assert oe.working_capital_cash_effect.value == -5
    assert oe.operating_cash_flow_before_working_capital.value == 155


def test_absent_primary_working_capital_keeps_existing_lower_alias():
    _, outcome, oe = optional_owner_source(None, primary_wc=False, alternate_wc=True)
    assert oe.free_cash_flow.value == 130
    assert oe.working_capital_cash_effect.value == -50
    assert oe.operating_cash_flow_before_working_capital.value == 200


def test_selected_unknown_capitalized_intangible_component_is_not_omitted_from_required_sum():
    _, outcome, oe = optional_owner_source('PaymentsToDevelopSoftware')
    assert oe.free_cash_flow.value == 130
    assert oe.capitalized_intangible_investment is None
    assert oe.expanded_free_cash_flow is None
    assert oe.annual[2024].capitalized_intangible_investment is None
    assert oe.annual[2024].expanded_free_cash_flow is None


def primary_claim_note_row(monkeypatch, target):
    def instant(value):
        item = annual(value)
        item.pop('start')
        return item
    tags = {'Assets': [instant(200)], 'Liabilities': [instant(100)],
        'AssetsCurrent': [instant(150)], 'LiabilitiesCurrent': [instant(50)],
        'LiabilitiesAndStockholdersEquity': [instant(200)],
        'StockholdersEquity': [instant(100)],
        'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest': [instant(120)],
        'Goodwill': [instant(100)], 'IntangibleAssetsNetExcludingGoodwill': [instant(0)],
        'PreferredStockValue': [instant(10)], 'MinorityInterest': [instant(20)],
        'NetIncomeLoss': [annual(90)], 'Revenues': [annual(200)]}
    taxo, outcome = unavailable_taxonomy(tags, target)
    taxo['CommonStockSharesOutstanding'] = tagdata('shares', [instant(10)])
    snap = normalize.build_snapshot('SYNTH', CIK, {'facts': {'us-gaap': taxo}})
    monkeypatch.setattr(sync, 'build_snapshot', lambda *args, **kwargs: snap)
    result = sync._derive_result(snap.cik, snap.ticker, {'facts': {'us-gaap': taxo}})
    assert result.snapshot is snap and result.status == 'ok'
    row = result.row
    from screener import profiles
    note = profiles.acquisition_book_note(row)
    return row, note


def test_selected_unknown_preferred_does_not_assert_numeric_common_equity_acquisition_note(monkeypatch):
    row, note = primary_claim_note_row(monkeypatch, 'PreferredStockValue')
    assert row['preferred_stock'] is None
    assert note is None


def test_unused_unknown_parent_equity_keeps_known_claim_acquisition_note(monkeypatch):
    row, note = primary_claim_note_row(monkeypatch, 'StockholdersEquity')
    assert row['total_liabilities'] == 100
    assert row['asset_quality']['common_equity'] == 70
    assert note is not None


@pytest.mark.parametrize('total_newer', [False, True])
def test_current_redeemable_total_and_unknown_parts_keep_existing_rollup_priority(total_newer):
    total = 'RedeemableNoncontrollingInterestEquityCarryingAmount'
    part = 'RedeemableNoncontrollingInterestEquityOtherCarryingAmount'
    values = {total: [instant(10)], part: [instant(3)]}
    if not total_newer:
        values[total][0]['end'] = '2024-06-30'
    taxo, outcome = unavailable_taxonomy(values, part)
    selected = normalize._InstantReads()
    _, nci, _ = normalize._senior_claims(taxo, date(2024, 1, 1), None, None,
                                       False, False, _selection=selected)
    if total_newer:
        assert nci.value == 10 and not selected.unavailable
    else:
        assert nci is None and tuple(item.unavailable for item in selected.unavailable) == (outcome,)


def test_current_unknown_redeemable_total_can_use_conclusively_newer_complete_parts():
    total = 'RedeemableNoncontrollingInterestEquityCarryingAmount'
    part = 'RedeemableNoncontrollingInterestEquityOtherCarryingAmount'
    values = {total: [instant(10)], part: [instant(3)]}
    values[total][0]['end'] = '2024-06-30'
    taxo, _ = unavailable_taxonomy(values, total)
    selected = normalize._InstantReads()
    _, nci, _ = normalize._senior_claims(taxo, date(2024, 1, 1), None, None,
                                       False, False, _selection=selected)
    assert nci.value == 3 and not selected.unavailable


def test_private_claim_tuple_is_immutable_and_default_legacy_snapshot_available():
    snap, _, outcome = primary_claim_snapshot('PreferredStockValue')
    assert not snap.current_senior_claims_available()
    assert snap.senior_claims_unavailable == (outcome,)
    copied = replace(snap, senior_claims_unavailable=[outcome])
    assert copied.senior_claims_unavailable == (outcome,) and copied == snap
    legacy = replace(snap, senior_claims_unavailable=())
    assert legacy.current_senior_claims_available()
    assert 'senior_claims_unavailable' not in repr(copied)
    assert pickle.loads(pickle.dumps(copied)).senior_claims_unavailable == (outcome,)


def test_unknown_lower_working_capital_alias_does_not_block_known_primary():
    tags = {'IncreaseDecreaseInOperatingCapital': [annual(5)],
            'IncreaseDecreaseInOperatingAssetsAndLiabilities': [annual(-50)]}
    taxo, _ = unavailable_taxonomy(tags, 'IncreaseDecreaseInOperatingAssetsAndLiabilities')
    result = normalize._annual_working_capital_cash_effect(taxo)
    assert result[2024].value == -5 and not getattr(result, 'unavailable', {})


def test_unknown_only_working_capital_alternate_retains_exact_unavailable():
    tag = 'IncreaseDecreaseInOperatingAssetsAndLiabilities'
    taxo, outcome = unavailable_taxonomy({tag: [annual(-50)]}, tag)
    result = normalize._annual_working_capital_cash_effect(taxo)
    assert not result and result.unavailable[2024] is outcome


@pytest.mark.parametrize('known', [True, False])
def test_profile_reuses_explicit_qualified_equity_and_preserves_legacy_absent_key(known):
    from screener import profiles
    row = {'total_assets': 200, 'total_liabilities': 100, 'preferred_stock': None,
           'noncontrolling_interest': 20, 'goodwill': 100}
    legacy = profiles.acquisition_book_note(row)
    assert legacy is not None
    row['asset_quality'] = {'common_equity': 70 if known else None}
    assert (profiles.acquisition_book_note(row) is not None) is known
