"""Keep usable prior-filing rows separate from pending-filing diagnostics."""
from copy import deepcopy

import pytest

from screener.company_availability import validate_unavailable_row
from screener.company_import import DerivationError
from test_company_import import retained, _importer, _sec


@pytest.fixture
def pending_payload():
    return {
        "cik": "0000001800", "ticker": "ABT", "reporting_currency": "USD",
        "annual_eps": {"2024": 2.75}, "cash_flow": None,
        "criteria": [{"n": n, "status": "INSUFFICIENT_DATA", "value": None}
                     for n in (1, 2, 3, 4, 5, 7)],
        "data_pending": {
            "kind": "SEC_FACTS_PENDING", "filed": "2026-10-01",
            "accession": "pending-fixture-accession",
            "note": "Calculations use the last complete filing.",
        },
    }


@pytest.mark.parametrize("preserve_unavailable", [False, True])
def test_complete_pending_row_keeps_exact_payload_and_warning(
    retained, pending_payload, preserve_unavailable,
):
    _sec(*retained)
    before = deepcopy(pending_payload)
    importer, _, _, companies = _importer(
        retained, lambda bundle: ("pending_facts", pending_payload),
    )
    imported = importer.import_ticker("ABT", preserve_unavailable=preserve_unavailable)
    assert imported.data_status == "AVAILABLE"
    assert imported.unavailability_reason is None
    assert companies.calls[-1]["canonical_payload"] is pending_payload
    assert companies.calls[-1]["canonical_payload"] == before
    assert pending_payload == before
    assert companies.calls[-1]["security_basis"] == "PRIMARY_COMMON_SHARE"
    assert all(item.role != "company_unavailability_assessment" for item in imported.artifacts)


@pytest.mark.parametrize("identity", [{"cik": "0000009999"}, {"ticker": "OTHER"}])
def test_pending_full_row_identity_mismatch_is_not_an_unavailable_diagnostic(
    retained, pending_payload, identity,
):
    _sec(*retained)
    importer, _, _, companies = _importer(
        retained, lambda bundle: ("pending_facts", {**pending_payload, **identity}),
    )
    with pytest.raises(DerivationError):
        importer.import_ticker("ABT", preserve_unavailable=True)
    assert companies.calls == []


def test_pending_full_row_without_warning_is_refused(retained, pending_payload):
    _sec(*retained)
    payload = {key: value for key, value in pending_payload.items() if key != "data_pending"}
    importer, _, _, companies = _importer(retained, lambda bundle: ("pending_facts", payload))
    with pytest.raises(DerivationError):
        importer.import_ticker("ABT", preserve_unavailable=True)
    assert companies.calls == []


@pytest.mark.parametrize("payload_kind", ["none", "marker"])
def test_diagnostic_only_pending_retains_actual_inputs_and_no_financial_values(
    retained, pending_payload, payload_kind,
):
    _sec(*retained)
    payload = None if payload_kind == "none" else {"data_pending": pending_payload["data_pending"]}
    importer, _, _, companies = _importer(retained, lambda bundle: ("pending_facts", payload))
    imported = importer.import_ticker("ABT", preserve_unavailable=True)
    row = companies.calls[-1]["canonical_payload"]
    assessment, diagnostic_sha = validate_unavailable_row(row)
    assert imported.data_status == "UNAVAILABLE"
    assert assessment.reason_code == "FILING_FACTS_PENDING"
    assert row["price"] is None
    assert all(item["value"] is None for item in row["criteria"])
    assert row["annual_eps"] == {}
    assert {item.artifact.content_sha256 for item in imported.artifacts} == {
        diagnostic_sha, *assessment.input_artifact_sha256s,
    }
    with pytest.raises(DerivationError):
        importer.import_ticker("ABT")


@pytest.mark.parametrize("status", ["foreign", "error"])
def test_other_raw_statuses_do_not_gain_pending_full_row_acceptance(
    retained, pending_payload, status,
):
    _sec(*retained)
    importer, _, _, companies = _importer(retained, lambda bundle: (status, pending_payload))
    with pytest.raises(DerivationError):
        importer.import_ticker("ABT")
    assert companies.calls == []
