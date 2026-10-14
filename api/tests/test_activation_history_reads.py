from datetime import date

import pytest
from fastapi.testclient import TestClient

from screener import api, company_query_api
from screener.company_queries import HistoryQuery, InvalidQuery, QueryUnavailable, RestartQuery, HistoryNotFound
from screener.figure_history import public_history_document, public_history_record, history_name_digest


@pytest.mark.parametrize('values', [dict(limit=0), dict(limit=301), dict(limit=True), dict(view='AUTO'),
    dict(metric_path=''), dict(period_kind='INSTANT', period_end=None), dict(period_kind='UNKNOWN', period_end=date(2025,12,31)),
    dict(period_kind='DURATION', period_start=date(2025,12,31), period_end=date(2024,12,31))])
def test_actual_selector_rejects_invalid_shape(values):
    with pytest.raises(InvalidQuery):
        HistoryQuery(**dict(metric_path='assets', period_kind='INSTANT', period_end=date(2025,12,31), **{} ) | values)


def test_long_names_use_fixed_identity_without_truncation():
    query = HistoryQuery('名'*10000, 'INSTANT', period_end=date(2025,12,31))
    assert len(query.fingerprint()) == len(history_name_digest(query.metric_path)) == 64
    assert query.selector()['metric_path'] == '名'*10000


@pytest.mark.parametrize('text', ['C:\\private\\filing.json', '/home/person/filing.json', 's3://bucket/a.json',
    'https://user:password@www.sec.gov/Archives/a.htm', 'https://www.sec.gov/Archives/a.htm?token=x',
    'filing token=secret', 'file:///private/a.json', 'https://example.com/a.htm', '../a.json'])
def test_documents_are_safe_as_inert_text_too(text):
    assert public_history_document(text) is None
    assert public_history_record({'reference': {'source_document': text}})['reference']['public_reference_status'] == 'UNAVAILABLE'


@pytest.mark.parametrize('text', ['annual.htm', '0000001800-26-000001', 'https://www.sec.gov/Archives/a.htm'])
def test_public_references_preserve_exact_safe_value(text):
    assert public_history_document(text) == text


@pytest.mark.parametrize('error,status', [(InvalidQuery('bad'),400),(RestartQuery('changed'),409),
    (HistoryNotFound('secret'),404),(QueryUnavailable('missing'),503)])
def test_mounted_history_domain_errors_are_no_store(monkeypatch,error,status):
    class Repository:
        def figure_history(self, **kwargs): raise error
    monkeypatch.setattr(company_query_api,'query_repository',lambda: Repository())
    response=TestClient(api.app).get('/api/v1/companies/1/figure-history',params={
        'metric_path':'assets','period_kind':'INSTANT','period_end':'2025-12-31'})
    assert response.status_code == status, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert 'secret' not in response.text


@pytest.mark.parametrize('path,params', [('figure-history',{}), ('figure-history',{'limit':301}),
    ('figure-history/dependencies',{}),('figure-history/source-events',{'source_item_id':0}),
    ('figure-history',{'unknown':'x'})])
def test_validation_errors_are_no_store_before_store_access(monkeypatch,path,params):
    monkeypatch.setattr(company_query_api,'query_repository',lambda: pytest.fail('validation accessed DB'))
    response=TestClient(api.app).get('/api/v1/companies/1/'+path,params=params)
    assert response.status_code == 422
    assert response.headers['cache-control']=='no-store'


def test_missing_repository_configuration_is503_and_no_store(monkeypatch):
    def broken(): raise ValueError('secret local DSN')
    monkeypatch.setattr(company_query_api,'query_repository',broken)
    response=TestClient(api.app).get('/api/v1/companies/1/figure-history',params={
        'metric_path':'assets','period_kind':'UNKNOWN'})
    assert response.status_code==503 and 'secret' not in response.text
    assert response.headers['cache-control']=='no-store'
