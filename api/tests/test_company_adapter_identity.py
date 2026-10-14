"""The new source kind admits only the existing explicit Adidas identity."""
import pytest

from screener.company_availability import LOCAL_KIND, load_assessment
from screener.shared_companies import _issuer_values
from screener.source_observations import SourceItemIdentity, _source_item_identity
from test_shared_companies import repository, _company, _add_artifact


def test_explicit_adapter_identity_and_existing_sources_remain_distinct():
    assert _issuer_values("COMPANY_ADAPTER", "IFRS-ADIDAS-AG") == {
        "source_system": "COMPANY_ADAPTER", "source_identifier": "IFRS-ADIDAS-AG"}
    assert _issuer_values("SEC", "74046")["source_identifier"] == "0000074046"
    assert _issuer_values("EDINET", "E00009")["source_identifier"] == "E00009"
    item = SourceItemIdentity("COMPANY_ADAPTER", "filing", "adidas-ar25", "IFRS-ADIDAS-AG")
    assert _source_item_identity(item) == item


@pytest.mark.parametrize("identifier", [None, "OTHER", "74046", "E00009"])
def test_company_adapter_source_cannot_be_a_generic_issuer(identifier):
    with pytest.raises(ValueError):
        _source_item_identity(SourceItemIdentity("COMPANY_ADAPTER", "filing", "x", identifier))
    if identifier is not None:
        with pytest.raises(ValueError):
            _issuer_values("COMPANY_ADAPTER", identifier)


def adapter_company():
    return _company(issuer_source="COMPANY_ADAPTER", issuer_identifier="IFRS-ADIDAS-AG",
        security_identifier="company-adapter:IFRS-ADIDAS-AG:primary-ordinary", ticker="ADS.DE",
        exchange_code="XETRA", quote_currency="EUR", security_basis="PRIMARY_ORDINARY_SHARE",
        canonical_payload=dict(ticker="ADS.DE", cik="IFRS-ADIDAS-AG"))


def test_shared_adapter_snapshot_requires_exact_security_and_original_workbook(repository):
    from screener.shared_companies import SnapshotArtifact
    for digest in ("a" * 64, "b" * 64):
        _add_artifact(repository, digest)
    inputs = (SnapshotArtifact("a" * 64, "raw_filing"),
              SnapshotArtifact("b" * 64, "canonical_company_adapter_facts"))
    value = adapter_company()
    assert repository.store_and_select(**value, artifacts=inputs).created
    for key, bad in (('ticker', 'OTHER'), ('security_basis', 'UNASSESSED'),
                     ('quote_currency', 'USD'), ('security_identifier', 'sec:fake')):
        with pytest.raises(ValueError, match="company adapter"):
            repository.store_and_select(**{**value, key: bad}, artifacts=inputs)
    with pytest.raises(ValueError, match="workbook"):
        repository.store_and_select(**value, artifacts=inputs[1:])


def test_adapter_unavailable_assessment_stays_internal_and_exact():
    values = dict(kind=LOCAL_KIND, issuer_source="COMPANY_ADAPTER", issuer_identifier="IFRS-ADIDAS-AG",
        ticker="ADS.DE", reason_code="LOCAL_INPUT_MISSING", reason="Missing retained workbook, remote not checked.",
        assessed_at="2026-10-13T00:00:00Z", input_artifact_sha256s=[], remote_observation_ids=[])
    assert load_assessment(values).issuer_source == "COMPANY_ADAPTER"
    with pytest.raises(ValueError):
        load_assessment({**values, "ticker": "OTHER"})
