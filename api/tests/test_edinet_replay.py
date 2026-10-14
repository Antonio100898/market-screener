"""Retained source replay repairs canonical gaps without downloading filings."""
import io
import json
import zipfile
from copy import deepcopy

import pytest

from screener import evidence, store, sync
from screener.sources.edinet_mapper import rebuild_companyfacts


def retained_report():
    document = "S100Z3O7"
    cached = {
        "cik": "E00009", "entityName": "AXYZ CO.,Ltd.",
        "facts": {"canonical": {"Assets": {"units": {"JPY": [{
            "val": "28461000000", "accn": document, "form": "JP-AR",
            "end": "2026-06-30", "filed": "2026-09-24",
        }]}}}},
        "_adapter": {
            "kind": "edinet_xbrl", "statement_basis": "canonical",
            "ticker": "1381.T", "security_code": "13810",
            "reporting_currency": "JPY", "quote_currency": "JPY",
            "security_basis": "PRIMARY_ORDINARY_SHARE",
            "reports": [{"document": document, "published": "2026-09-24",
                         "period_start": "2025-07-01", "period_end": "2026-06-30"}],
        },
    }
    # Reduced filing-shaped XML carrying verified S100Z3O7 current-year rows.
    xml = '''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:jppfs_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2025-11-01/jppfs_cor"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
      <xbrli:context id="i"><xbrli:entity><xbrli:identifier>E00009-000</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:instant>2026-06-30</xbrli:instant></xbrli:period></xbrli:context>
      <xbrli:context id="d"><xbrli:entity><xbrli:identifier>E00009-000</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-07-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:unit id="JPY"><xbrli:measure>iso4217:JPY</xbrli:measure></xbrli:unit>
      <jppfs_cor:Assets contextRef="i" unitRef="JPY">28461000000</jppfs_cor:Assets>
      <jppfs_cor:ProfitLossAttributableToOwnersOfParent contextRef="d" unitRef="JPY">2925000000</jppfs_cor:ProfitLossAttributableToOwnersOfParent>
      <jppfs_cor:DepreciationAndAmortizationOpeCF contextRef="d" unitRef="JPY">1815000000</jppfs_cor:DepreciationAndAmortizationOpeCF>
      <jppfs_cor:NetCashProvidedByUsedInOperatingActivities contextRef="d" unitRef="JPY">4885000000</jppfs_cor:NetCashProvidedByUsedInOperatingActivities>
      <jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF contextRef="d" unitRef="JPY">-1521000000</jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF>
    </xbrli:xbrl>'''
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("XBRL/PublicDoc/annual.xbrl", xml)
    return cached, archive.getvalue()


def test_replay_uses_native_rows_and_does_not_change_cached_facts():
    cached, archive = retained_report()
    before = deepcopy(cached)
    rebuilt = rebuild_companyfacts(cached, lambda document: archive)
    assert cached == before
    payment = rebuilt["facts"]["canonical"]["PaymentsToAcquirePropertyPlantAndEquipment"]["units"]["JPY"][0]
    assert payment["val"] == "1521000000"
    assert payment["_source_tag"] == "PurchaseOfPropertyPlantAndEquipmentInvCF"
    assert rebuilt["_adapter"]["reports"][0]["mapping_record"]["docTypeCode"] == "120"
    assert rebuild_companyfacts(rebuilt, lambda document: archive) == rebuilt


@pytest.mark.parametrize("problem", ["duplicate", "traversal", "unknown_form", "security", "basis", "quote"])
def test_replay_rejects_incomplete_or_conflicting_manifest_before_reading(problem):
    cached, _ = retained_report()
    if problem == "duplicate":
        cached["_adapter"]["reports"].append(deepcopy(cached["_adapter"]["reports"][0]))
    elif problem == "traversal":
        cached["_adapter"]["reports"][0]["document"] = "../S100Z3O7"
    elif problem == "unknown_form":
        cached["facts"]["canonical"]["Assets"]["units"]["JPY"][0]["form"] = "UNKNOWN"
    elif problem == "security":
        cached["_adapter"]["ticker"] = "9999.T"
    elif problem == "basis":
        cached["_adapter"]["security_basis"] = "UNKNOWN"
    else:
        cached["_adapter"].pop("quote_currency")
    with pytest.raises(ValueError):
        rebuild_companyfacts(cached, lambda document: (_ for _ in ()).throw(AssertionError("unexpected read")))


def test_replay_keeps_explicit_quote_currency_separate_from_statement_currency():
    cached, archive = retained_report()
    cached["_adapter"]["quote_currency"] = "USD"
    rebuilt = rebuild_companyfacts(cached, lambda document: archive)
    assert rebuilt["_adapter"]["reporting_currency"] == "JPY"
    assert rebuilt["_adapter"]["quote_currency"] == "USD"


def test_loader_and_process_worker_replay_the_same_local_source(tmp_path):
    cached, archive = retained_report()
    facts_path = tmp_path / "companyfacts_E00009.json"
    before = json.dumps(cached).encode()
    facts_path.write_bytes(before)
    (tmp_path / "edinet").mkdir()
    (tmp_path / "edinet" / "S100Z3O7.zip").write_bytes(archive)

    class LocalSource:
        cache_dir = tmp_path

        def company_facts(self, cik):
            raise AssertionError("unexpected source request")

    conn = store.connect(tmp_path / "test.db")
    try:
        loaded = evidence.EvidenceLoader(conn, LocalSource()).load("E00009", "1381.T")
        direct = sync._derive_evidence(loaded)
        cik, worker = sync._derive_cached_worker(("E00009", "1381.T", None, str(tmp_path)))
        assert cik == "E00009" and worker == direct
        assert direct[0] == "ok"
        assert direct[1]["owner_earnings"]["free_cash_flow"] == 3364000000
        assert facts_path.read_bytes() == before
    finally:
        conn.close()


def test_missing_archive_is_not_replaced_by_stale_canonical_data(tmp_path):
    cached, _ = retained_report()
    (tmp_path / "companyfacts_E00009.json").write_text(json.dumps(cached))
    with pytest.raises(FileNotFoundError):
        sync._derive_cached_worker(("E00009", "1381.T", None, str(tmp_path)))
