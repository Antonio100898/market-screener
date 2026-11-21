import io
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from screener.sources.edinet import EdinetClient, EdinetError, annual_filings, xbrl_facts


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


def test_edinet_http_failure_does_not_expose_key(tmp_path):
    def reject(request):
        assert request.url.params["Subscription-Key"] == "private-test-key"
        return httpx.Response(401)

    with httpx.Client(transport=httpx.MockTransport(reject)) as http:
        client = EdinetClient("private-test-key", http=http, cache_dir=tmp_path)
        with pytest.raises(EdinetError) as error:
            client.documents_on("2026-06-19")
    assert "private-test-key" not in str(error.value)


def test_edinet_cache_replays_closed_days_and_archives_without_key(tmp_path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as bundle:
        bundle.writestr("XBRL/PublicDoc/report.xbrl", "<xbrl/>")
    today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()

    def respond(request):
        if request.url.path.endswith("documents.json"):
            return httpx.Response(200, json={"metadata": {"status": "200"},
                                             "results": [{"docID": "S100YETA"}]})
        return httpx.Response(200, content=payload.getvalue())

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        online = EdinetClient("private-test-key", http=http, cache_dir=tmp_path)
        online.documents_on("2026-06-19")
        online.documents_on(today)
        online.xbrl_archive("S100YETA")

    offline = EdinetClient(None, cache_dir=tmp_path)
    offline.api_key = None  # the Actions job exports the real key to this test
    assert offline.documents_on("2026-06-19") == [{"docID": "S100YETA"}]
    assert offline.xbrl_archive("S100YETA") == payload.getvalue()
    with pytest.raises(EdinetError, match="EDINET_API_KEY is required"):
        offline.documents_on(today)


def test_edinet_fetch_retains_annual_reports_and_counts_failures():
    from datetime import date
    from screener.sources import edinet

    class Client:
        fetched = []

        def documents_on(self, day):
            if day == "2026-06-20":
                raise EdinetError("list unavailable")
            return [{"docID": "S100YETA", "docTypeCode": "120", "xbrlFlag": "1",
                     "edinetCode": "E01772", "secCode": "67520"},
                    {"docID": "S100OTHR", "docTypeCode": "140", "xbrlFlag": "1",
                     "edinetCode": "E01772", "secCode": "67520"}]

        def xbrl_archive(self, document_id):
            self.fetched.append(document_id)

    client = Client()
    assert edinet.fetch(date(2026, 6, 19), date(2026, 6, 20), client) == 1
    assert client.fetched == ["S100YETA"]


def test_edinet_reads_the_period_end_a_correction_declares():
    from screener.sources.edinet import reported_period_end

    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as bundle:
        bundle.writestr("XBRL/PublicDoc/report.xbrl", (
            '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
            'xmlns:jpdei_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpdei/2013-08-31/jpdei_cor">'
            '<jpdei_cor:CurrentPeriodEndDateDEI contextRef="FilingDateInstant">'
            '2026-03-31</jpdei_cor:CurrentPeriodEndDateDEI></xbrli:xbrl>'))
    assert reported_period_end(data.getvalue(), "S100ZD09") == "2026-03-31"


def test_edinet_skips_withdrawn_filings():
    row = {"docID": "S100YETA", "docTypeCode": "120", "xbrlFlag": "1",
           "edinetCode": "E01772", "secCode": "67520"}
    assert annual_filings([{**row, "withdrawalStatus": "2"}, row]) == [row]
