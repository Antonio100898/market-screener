import io
import zipfile
from decimal import Decimal

import httpx
import pytest

from screener.sources.edinet import (
    EdinetClient,
    EdinetError,
    annual_filings,
    validate_xbrl_archive,
    xbrl_facts,
)


def archive(document: str) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as bundle:
        bundle.writestr("XBRL/PublicDoc/report.xbrl", document)
    return data.getvalue()


PANASONIC_FACTS = """<xbrli:xbrl
    xmlns:xbrli="http://www.xbrl.org/2003/instance"
    xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:jpigp="http://disclosure.edinet-fsa.go.jp/taxonomy/jpigp/2025-11-01/jpigp_cor">
  <xbrli:context id="CurrentYearDuration">
    <xbrli:entity><xbrli:identifier scheme="edinet">E01772</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate>
      <xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearDuration_NonConsolidatedMember">
    <xbrli:entity><xbrli:identifier scheme="edinet">E01772</xbrli:identifier>
      <xbrli:segment><xbrldi:explicitMember dimension="ConsolidationAxis">
        NonConsolidatedMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate>
      <xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:unit id="JPY"><xbrli:measure>iso4217:JPY</xbrli:measure></xbrli:unit>
  <xbrli:unit id="JPYPerShares"><xbrli:divide><xbrli:unitNumerator>
    <xbrli:measure>iso4217:JPY</xbrli:measure></xbrli:unitNumerator>
    <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure>
    </xbrli:unitDenominator></xbrli:divide></xbrli:unit>
  <jpigp:RevenueIFRSSummaryOfBusinessResults contextRef="CurrentYearDuration"
    unitRef="JPY">8048722000000</jpigp:RevenueIFRSSummaryOfBusinessResults>
  <jpigp:EarningsPerShare contextRef="CurrentYearDuration" unitRef="JPYPerShares">
    81.17</jpigp:EarningsPerShare>
  <jpigp:RevenueIFRSSummaryOfBusinessResults
    contextRef="CurrentYearDuration_NonConsolidatedMember" unitRef="JPY">1
  </jpigp:RevenueIFRSSummaryOfBusinessResults>
  <jpigp:Missing contextRef="CurrentYearDuration" unitRef="JPY" xsi:nil="true" />
</xbrli:xbrl>"""


def test_edinet_filters_annual_reports_and_keeps_filing_identity():
    records = [
        {"docID": "S100YETA", "edinetCode": "E01772", "secCode": "67520",
         "docTypeCode": "120", "xbrlFlag": "1"},
        {"docID": "S100YETB", "edinetCode": "E01772", "secCode": "67520",
         "docTypeCode": "130", "xbrlFlag": "1"},
        {"docID": "S100YEL1", "edinetCode": "E01772", "secCode": "67520",
         "docTypeCode": "135", "xbrlFlag": "0"},
    ]
    assert annual_filings(records) == records[:2]


def test_edinet_keeps_exact_unit_period_and_dimension():
    facts = xbrl_facts(archive(PANASONIC_FACTS), "S100YETA")
    assert len(facts) == 3
    assert facts[0]["tag"] == "RevenueIFRSSummaryOfBusinessResults"
    assert facts[0]["value"] == Decimal("8048722000000")
    assert (facts[0]["unit"], facts[0]["start"], facts[0]["end"]) == (
        "JPY", "2025-04-01", "2026-03-31")
    assert facts[1]["unit"] == "JPY/shares"
    assert facts[2]["dimensions"] == [
        {"axis": "ConsolidationAxis", "member": "NonConsolidatedMember"}]
    assert facts[0]["document_id"] == "S100YETA"
    assert facts[0]["entity"] == "E01772"


def test_edinet_http_failure_does_not_expose_key():
    def reject(request):
        assert request.url.params["Subscription-Key"] == "private-test-key"
        return httpx.Response(401)

    with httpx.Client(transport=httpx.MockTransport(reject)) as http:
        client = EdinetClient("private-test-key", http=http)
        with pytest.raises(EdinetError) as error:
            client.documents_on("2026-06-19")
    assert "private-test-key" not in str(error.value)


def test_archive_validation_rejects_a_corrupt_non_xbrl_member():
    data = io.BytesIO()
    bad_payload = b"corrupt member payload"
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr("XBRL/PublicDoc/report.xbrl", PANASONIC_FACTS)
        bundle.writestr("XBRL/PublicDoc/attachment.pdf", bad_payload)
    corrupt = bytearray(data.getvalue())
    position = corrupt.index(bad_payload)
    corrupt[position] ^= 1

    with pytest.raises(EdinetError, match="corrupt archive member"):
        validate_xbrl_archive(bytes(corrupt), "S100YETA")
