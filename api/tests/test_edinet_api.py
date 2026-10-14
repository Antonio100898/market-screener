import json
import io
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from screener import api, store
from screener.models import PriceHistory, Quote


def test_japanese_filing_routes_use_cached_edinet_data_and_jpy_quote(tmp_path, monkeypatch):
    facts = {
        "cik": "E01772", "entityName": "Panasonic Holdings Corporation",
        "facts": {"canonical": {
            "Assets": {"units": {"JPY": [{
                "val": "10172412000000", "accn": "S100YETA", "fy": 2026,
                "fp": "FY", "form": "JP-AR", "filed": "2026-06-19",
                "end": "2026-03-31", "_canonical_adapter": "edinet_xbrl",
                "_source_namespace": "jpigp_cor", "_source_tag": "AssetsIFRS",
                "_source_unit": "JPY", "_source_document": "S100YETA",
                "_normalized_tag": "Assets",
            }]}}
        }},
        "_adapter": {"kind": "edinet_xbrl", "statement_basis": "canonical",
                     "reporting_currency": "JPY", "quote_currency": "JPY",
                     "ticker": "6752.T", "security_code": "67520",
                     "security_basis": "PRIMARY_ORDINARY_SHARE",
                     "reports": [{"document": "S100YETA", "published": "2026-06-19",
                                  "period_start": "2025-04-01", "period_end": "2026-03-31"}]},
    }
    (tmp_path / "companyfacts_E01772.json").write_text(json.dumps(facts))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as filing:
        filing.writestr("XBRL/PublicDoc/annual.xbrl", '''<xbrli:xbrl
          xmlns:xbrli="http://www.xbrl.org/2003/instance"
          xmlns:jpigp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpigp_cor">
          <xbrli:context id="balance"><xbrli:entity><xbrli:identifier>E01772-000</xbrli:identifier></xbrli:entity>
            <xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period></xbrli:context>
          <xbrli:unit id="JPY"><xbrli:measure>JPY</xbrli:measure></xbrli:unit>
          <jpigp_cor:AssetsIFRS contextRef="balance" unitRef="JPY">10172412000000</jpigp_cor:AssetsIFRS>
        </xbrli:xbrl>''')
    (tmp_path / "edinet").mkdir()
    (tmp_path / "edinet" / "S100YETA.zip").write_bytes(archive.getvalue())
    database = tmp_path / "test.db"
    conn = store.connect(database)
    store.upsert_company(conn, "E01772", "6752.T", "Panasonic Holdings Corporation")
    conn.commit()
    conn.close()

    class Edgar:
        cache_dir = tmp_path

        def cik_for(self, ticker):
            raise AssertionError("Japanese tickers must not use SEC ticker lookup")

    quote = Quote(Decimal("4287"), datetime(2026, 9, 18, tzinfo=timezone.utc), "test")
    expected = []

    class Prices:
        def history(self, ticker, expected_currency="USD"):
            expected.append((ticker, expected_currency))
            return PriceHistory(quote, ((date(2026, 9, 18), Decimal("4287")),))

    monkeypatch.setattr(api, "_edgar", Edgar())
    monkeypatch.setattr(api, "_prices", Prices())
    original_connect = store.connect
    monkeypatch.setattr(api.store, "connect", lambda: original_connect(database))
    client = TestClient(api.app)
    fundamentals = client.get("/fundamentals/6752.T")
    assert fundamentals.status_code == 200
    assert fundamentals.json()["total_assets"]["value"] == 10172412000000
    response = client.get("/screen/enterprising/6752.T")
    assert response.status_code == 200
    assert response.json()["quote"]["price"] == 4287
    assert expected == [("6752.T", "JPY")]
