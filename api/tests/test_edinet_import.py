import json
from datetime import date

from screener import store, sync


def bundle(doc, value, published):
    return {
        "cik": "E01772", "entityName": "Panasonic Holdings Corporation",
        "facts": {"canonical": {"Assets": {"units": {"JPY": [{
            "accn": doc, "val": value, "end": "2026-03-31",
        }]}}}},
        "_adapter": {
            "kind": "edinet_xbrl", "statement_basis": "canonical",
            "reporting_currency": "JPY", "quote_currency": "JPY",
            "ticker": "6752.T", "reports": [{"document": doc, "published": published}],
        },
    }


def test_reimport_replaces_same_filing_and_keeps_other_years():
    first = bundle("S100A001", "100", "2025-06-19")
    second = bundle("S100YETA", "200", "2026-06-19")
    merged = sync._merge_edinet_facts(first, second)
    assert [entry["val"] for entry in merged["facts"]["canonical"]["Assets"]["units"]["JPY"]] == [
        "100", "200"]
    refreshed = sync._merge_edinet_facts(merged, bundle("S100YETA", "201", "2026-06-19"))
    assert [entry["val"] for entry in refreshed["facts"]["canonical"]["Assets"]["units"]["JPY"]] == [
        "100", "201"]
    assert [report["document"] for report in refreshed["_adapter"]["reports"]] == [
        "S100A001", "S100YETA"]


def test_import_uses_verified_listing_and_can_repeat_from_cached_archive(tmp_path, monkeypatch):
    class Client:
        calls = 0

        def documents_on(self, day):
            assert day == "2026-06-19"
            return [{"docID": "S100YETA", "edinetCode": "E01772",
                     "secCode": "67520", "docTypeCode": "120", "xbrlFlag": "1",
                     "submitDateTime": "2026-06-19 10:48"}]

        def xbrl_archive(self, doc):
            self.calls += 1
            return b"filing archive"

    monkeypatch.setattr(sync, "build_edinet_companyfacts",
                        lambda record, archive, ticker: bundle(record["docID"], "200", "2026-06-19"))
    monkeypatch.setattr(sync, "_derive_evidence",
                        lambda evidence: ("ok", {"cik": evidence.cik, "ticker": evidence.ticker,
                                                 "criteria": [], "verdict": "INDETERMINATE"}))
    conn = store.connect(tmp_path / "screener.db")
    client = Client()
    listings = {"6752": {"name": "Panasonic Holdings Corporation",
                         "industry": "Electric Appliances"}}
    for _ in range(2):
        assert sync.import_edinet(
            conn, date(2026, 6, 19), date(2026, 6, 19),
            client=client, listings=listings, only_code="6752",
            cache_dir=tmp_path, progress=lambda *args: None) == 1
    assert client.calls == 1
    row = conn.execute("SELECT ticker, name, listed, exchange, incorporation FROM company "
                       "WHERE cik = 'E01772'").fetchone()
    assert tuple(row) == ("6752.T", "Panasonic Holdings Corporation", "external", "TSE",
                          "M0|Japan")
    saved = json.loads((tmp_path / "companyfacts_E01772.json").read_text())
    assert len(saved["facts"]["canonical"]["Assets"]["units"]["JPY"]) == 1
    assert len(store.dashboard_rows(conn)) == 1


def test_one_unsupported_edinet_report_does_not_hide_other_listed_companies(
        tmp_path, monkeypatch):
    class Client:
        def documents_on(self, day):
            return [
                {"docID": doc, "edinetCode": entity, "secCode": code,
                 "docTypeCode": "120", "xbrlFlag": "1",
                 "submitDateTime": "2026-06-19 10:48"}
                for doc, entity, code in [
                    ("S100A001", "E00001", "11110"),
                    ("S100YETA", "E01772", "67520"),
                ]
            ]

        def xbrl_archive(self, doc):
            return b"archive"

    def map_filing(record, archive, *, ticker):
        if record["docID"] == "S100A001":
            raise ValueError("no coherent statement currency")
        return bundle(record["docID"], "200", "2026-06-19")

    monkeypatch.setattr(sync, "build_edinet_companyfacts", map_filing)
    monkeypatch.setattr(sync, "_derive_evidence",
                        lambda evidence: ("ok", {"cik": evidence.cik, "ticker": evidence.ticker}))
    messages = []
    conn = store.connect(tmp_path / "screener.db")
    count = sync.import_edinet(
        conn, date(2026, 6, 19), date(2026, 6, 19), client=Client(),
        listings={"1111": {"name": "Unsupported", "industry": "Other"},
                  "6752": {"name": "Panasonic", "industry": "Electric Appliances"}},
        cache_dir=tmp_path, progress=lambda message, *args: messages.append(message))
    assert count == 1
    assert any("S100A001 skipped" in message for message in messages)
    assert [row["ticker"] for row in store.dashboard_rows(conn)] == ["6752.T"]


def correction(doc, corrects, value, published):
    data = bundle(doc, value, published)
    data["_adapter"]["reports"][0]["corrects"] = corrects
    return data


def test_correction_replaces_the_report_it_corrects_and_survives_reimport():
    def values(data):
        return [entry["val"] for entry in data["facts"]["canonical"]["Assets"]["units"]["JPY"]]

    data = sync._merge_edinet_facts(bundle("S100A001", "100", "2025-06-19"),
                                    bundle("S100YETA", "200", "2026-06-19"))
    data = sync._merge_edinet_facts(data, correction("S100ZD09", "S100YETA", "190", "2026-11-10"))
    assert values(data) == ["100", "190"]
    data = sync._merge_edinet_facts(data, bundle("S100YETA", "200", "2026-06-19"))
    assert values(data) == ["100", "190"]
    data = sync._merge_edinet_facts(data, correction("S100ZE00", "S100YETA", "185", "2026-11-20"))
    data = sync._merge_edinet_facts(data, correction("S100ZD09", "S100YETA", "190", "2026-11-10"))
    assert values(data) == ["100", "185"]
    assert [report["document"] for report in data["_adapter"]["reports"]] == [
        "S100A001", "S100ZE00"]


def test_rebuild_merges_each_company_once_from_retained_filings(tmp_path, monkeypatch):
    days = {
        "2025-06-19": [("S100A001", "E01772", "67520", "120", None)],
        "2026-06-19": [("S100YETA", "E01772", "67520", "120", None),
                       ("S100B002", "E00001", "11110", "120", None)],  # not listed
        "2026-11-10": [("S100ZD09", "E01772", "67520", "130", "S100YETA")],
    }

    class Client:
        def documents_on(self, day):
            return [{"docID": doc, "edinetCode": entity, "secCode": code,
                     "docTypeCode": kind, "xbrlFlag": "1", "parentDocID": parent,
                     "submitDateTime": f"{day} 10:48"}
                    for doc, entity, code, kind, parent in days.get(day, [])]

        def xbrl_archive(self, doc):
            raise AssertionError("a rebuild reads retained archives only")

    values = {"S100A001": "100", "S100YETA": "200", "S100ZD09": "190"}

    def map_filing(record, archive, *, ticker):
        assert archive == record["docID"].encode()
        published = record["submitDateTime"][:10]
        if record["docTypeCode"] == "130":
            return correction(record["docID"], record["parentDocID"],
                              values[record["docID"]], published)
        return bundle(record["docID"], values[record["docID"]], published)

    derived = []
    monkeypatch.setattr(sync, "build_edinet_companyfacts", map_filing)
    monkeypatch.setattr(sync, "_derive_evidence", lambda evidence: (
        derived.append(evidence.cik)
        or ("ok", {"cik": evidence.cik, "ticker": evidence.ticker,
                   "criteria": [], "verdict": "INDETERMINATE"})))
    (tmp_path / "edinet").mkdir()
    for doc in values:
        (tmp_path / "edinet" / f"{doc}.zip").write_bytes(doc.encode())
    conn = store.connect(tmp_path / "screener.db")
    assert sync.rebuild_edinet(
        conn, date(2025, 6, 19), date(2026, 11, 10), client=Client(),
        listings={"6752": {"name": "Panasonic", "industry": "Electric Appliances"}},
        cache_dir=tmp_path, workers=1, progress=lambda *args: None) == 1
    assert derived == ["E01772"]
    saved = json.loads((tmp_path / "companyfacts_E01772.json").read_text())
    assert [entry["val"] for entry in saved["facts"]["canonical"]["Assets"]["units"]["JPY"]] == [
        "100", "190"]
    assert conn.execute("SELECT last_filing FROM company WHERE cik = 'E01772'"
                        ).fetchone()[0] == "2026-11-10"
    assert [row["ticker"] for row in store.dashboard_rows(conn)] == ["6752.T"]


def test_history_keeps_only_the_latest_statement_scope():
    parent_only = bundle("S100A001", "100", "2020-06-19")
    parent_only["_adapter"]["reports"][0].update(scope="standalone", period_end="2020-03-31")
    group = bundle("S100YETA", "900", "2021-06-19")
    group["_adapter"]["reports"][0].update(scope="consolidated", period_end="2021-03-31")
    late_fix = correction("S100ZD09", "S100A001", "101", "2026-11-10")
    late_fix["_adapter"]["reports"][0].update(scope="standalone", period_end="2020-03-31")

    data = sync._merge_edinet_facts(sync._merge_edinet_facts(parent_only, group), late_fix)

    assert [entry["val"] for entry in data["facts"]["canonical"]["Assets"]["units"]["JPY"]] == ["900"]
    assert [report["document"] for report in data["_adapter"]["reports"]] == ["S100YETA"]
    assert data["_adapter"]["statement_scope"] == "consolidated"
