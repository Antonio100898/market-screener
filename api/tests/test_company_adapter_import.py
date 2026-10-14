"""Explicit company-adapter custody and admission through the existing importer."""
import hashlib
import json

import pytest

from screener.company_import import CompanyImporter, RetainedInputError
from screener.sources.ifrs_workbook import build_adidas_companyfacts
from test_company_import import retained, MemoryObjectStore, MemoryArtifactRepository, MemoryCompanyRepository
from test_ifrs_workbook import _report


def adidas_input(retained, *, quote=True):
    connection, cache = retained
    connection.execute("INSERT INTO company VALUES (?, ?, ?, ?)",
                       ("IFRS-ADIDAS-AG", "ADS.DE", "adidas AG", "XETRA"))
    directory = cache / "adapters" / "IFRS-ADIDAS-AG"
    directory.mkdir(parents=True)
    _report(directory / "cons-financial-statements-adidas-ar25.xlsx", 2025, 2024,
            current_revenue=1300, prior_revenue=1250)
    facts = build_adidas_companyfacts(directory)
    facts["_adapter"]["security_basis"] = "PRIMARY_ORDINARY_SHARE"
    if not quote:
        del facts["_adapter"]["quote_currency"]
    path = cache / "companyfacts_IFRS-ADIDAS-AG.json"
    path.write_text(json.dumps(facts), encoding="utf-8")
    return facts, path, directory


def importer(retained, *, declaration=None):
    connection, cache = retained
    objects, artifacts, companies = MemoryObjectStore(), MemoryArtifactRepository(), MemoryCompanyRepository()
    observed = []
    def derive(bundle):
        observed.append(bundle)
        return "ok", dict(ticker=bundle.ticker, cik=bundle.cik)
    return CompanyImporter(connection, cache, objects, artifacts, companies, derive,
        company_adapter_declaration=declaration), objects, companies, observed


def declaration(cache):
    source = cache / "security.html"
    source.write_bytes(b"Explicit fixture: Adidas registered ordinary share, XETRA, EUR.")
    value = dict(issuer_source="COMPANY_ADAPTER", issuer_identifier="IFRS-ADIDAS-AG",
        adapter_kind="adidas_ifrs_workbook", ticker="ADS.DE",
        security_identifier="company-adapter:IFRS-ADIDAS-AG:primary-ordinary",
        security_basis="PRIMARY_ORDINARY_SHARE", quote_currency="EUR",
        sources=[dict(path=str(source.resolve()), sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                      source_url="https://www.adidas-group.com/explicit-fixture")])
    path = cache / "security-declaration.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, value


def test_adidas_reuses_workbook_owner_and_retains_exact_generated_body(retained):
    facts, path, directory = adidas_input(retained)
    before = path.read_bytes()
    worker, objects, companies, observed = importer(retained)
    imported = worker.import_ticker("ADS.DE")
    saved, = companies.calls
    assert saved["issuer_source"] == "COMPANY_ADAPTER" and saved["issuer_identifier"] == "IFRS-ADIDAS-AG"
    assert saved["security_identifier"] == "company-adapter:IFRS-ADIDAS-AG:primary-ordinary"
    assert saved["security_basis"] == "PRIMARY_ORDINARY_SHARE" and saved["quote_currency"] == "EUR"
    bundle, = observed
    revenue = bundle.facts["facts"]["canonical"]["Revenues"]["units"]["EUR"]
    assert any(row["val"] == 1_300_000_000 and row["_source_tag"] == "Net sales"
               and row["_source_document"].endswith("ar25.xlsx") for row in revenue)
    generated = next(item.artifact for item in imported.artifacts if item.role == "canonical_company_adapter_facts")
    assert json.loads(objects.data[generated.object_key]) == bundle.facts
    assert path.read_bytes() == before
    assert {item.role for item in imported.artifacts} == {
        "retained_company_adapter_facts", "canonical_company_adapter_facts", "raw_filing"}


def test_explicit_security_declaration_fills_only_absent_quote_and_retains_sources(retained):
    _, path, _ = adidas_input(retained, quote=False)
    before = path.read_bytes()
    proof, value = declaration(retained[1])
    worker, objects, companies, observed = importer(retained, declaration=proof)
    imported = worker.import_ticker("ADS.DE")
    assert companies.calls[0]["quote_currency"] == "EUR"
    assert observed[0].facts["_adapter"]["quote_currency"] == "EUR"
    assert path.read_bytes() == before
    source = next(item.artifact for item in imported.artifacts if item.role == "company_adapter_security_source")
    assert source.content_sha256 == value["sources"][0]["sha256"]
    assert "company_adapter_security_declaration" in {item.role for item in imported.artifacts}


@pytest.mark.parametrize("tamper", ["kind", "ticker", "basis", "quote", "report_hash", "report_path", "extra_workbook", "missing_quote"])
def test_adapter_refuses_invalid_metadata_or_workbooks_before_deriving(retained, tamper):
    facts, path, directory = adidas_input(retained)
    adapter = facts["_adapter"]
    if tamper == "kind": adapter["kind"] = "other_workbook"
    elif tamper == "ticker": adapter["ticker"] = "OTHER"
    elif tamper == "basis": adapter["security_basis"] = "UNASSESSED"
    elif tamper == "quote": adapter["quote_currency"] = "USD"
    elif tamper == "report_hash": adapter["reports"][0]["sha256"] = "a" * 64
    elif tamper == "report_path": adapter["reports"][0]["document"] = "../escape.xlsx"
    elif tamper == "extra_workbook":
        _report(directory / "cons-financial-statements-adidas-ar24.xlsx", 2024, 2023,
                current_revenue=1200, prior_revenue=1100)
    else: del adapter["quote_currency"]
    path.write_text(json.dumps(facts), encoding="utf-8")
    worker, _, companies, observed = importer(retained)
    with pytest.raises(RetainedInputError):
        worker.import_ticker("ADS.DE", preserve_unavailable=True)
    assert not observed and not companies.calls


def test_unknown_text_issuer_is_not_routed_as_sec(retained):
    retained[0].execute("INSERT INTO company VALUES ('OTHER-COMPANY', 'OTHER', 'Other', 'TEST')")
    worker, _, companies, observed = importer(retained)
    with pytest.raises(RetainedInputError, match="source kind"):
        worker.import_ticker("OTHER")
    assert not companies.calls and not observed


def test_missing_adapter_workbook_keeps_honest_unavailable_identity(retained):
    _, _, directory = adidas_input(retained)
    next(directory.iterdir()).unlink()
    worker, _, companies, observed = importer(retained)
    imported = worker.import_ticker("ADS.DE", preserve_unavailable=True)
    assert imported.issuer_source == "COMPANY_ADAPTER" and imported.data_status == "UNAVAILABLE"
    assert companies.calls[0]["security_basis"] == "UNASSESSED"
    assert companies.calls[0]["quote_currency"] is None
    assert not observed


@pytest.mark.parametrize("tamper", ["source_hash", "quote", "issuer", "extra_field"])
def test_explicit_security_declaration_cannot_override_identity_or_source(retained, tamper):
    adidas_input(retained, quote=False)
    path, value = declaration(retained[1])
    if tamper == "source_hash": value["sources"][0]["sha256"] = "a" * 64
    elif tamper == "quote": value["quote_currency"] = "USD"
    elif tamper == "issuer": value["issuer_identifier"] = "OTHER"
    else: value["guessed_common"] = True
    path.write_text(json.dumps(value), encoding="utf-8")
    worker, _, companies, observed = importer(retained, declaration=path)
    with pytest.raises(RetainedInputError):
        worker.import_ticker("ADS.DE")
    assert not companies.calls and not observed
