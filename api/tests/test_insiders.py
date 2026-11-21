import io
import zipfile
from datetime import date

from screener import store
from screener.sources import edinet, insiders

ISSUER = "0000000001"


def _tsv(header: str, rows: list[str]) -> str:
    return "\n".join([header, *rows]) + "\n"


def _quarter(tmp_path, name, submissions, owners, trans=(), holdings=()):
    path = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("SUBMISSION.tsv", _tsv(
            "ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERCIK\tNO_SECURITIES_OWNED",
            submissions))
        z.writestr("REPORTINGOWNER.tsv", _tsv(
            "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP\tRPTOWNER_TITLE",
            owners))
        z.writestr("NONDERIV_TRANS.tsv", _tsv(
            "ACCESSION_NUMBER\tNONDERIV_TRANS_SK\tSECURITY_TITLE\tTRANS_DATE\t"
            "SHRS_OWND_FOLWNG_TRANS\tDIRECT_INDIRECT_OWNERSHIP\tNATURE_OF_OWNERSHIP", trans))
        z.writestr("NONDERIV_HOLDING.tsv", _tsv(
            "ACCESSION_NUMBER\tNONDERIV_HOLDING_SK\tSECURITY_TITLE\t"
            "SHRS_OWND_FOLWNG_TRANS\tDIRECT_INDIRECT_OWNERSHIP\tNATURE_OF_OWNERSHIP", holdings))
    return path


def test_latest_filing_wins_and_ten_percent_owner_is_not_management(tmp_path):
    old = _quarter(tmp_path, "2023q1",
                   [f"a-1\t10-JAN-2023\t4\t{ISSUER}\t"],
                   ["a-1\tP1\tDOE JANE\tDirector,Officer\tCEO"],
                   trans=["a-1\t1\tCommon Stock\t09-JAN-2023\t500\tD\t"])
    new = _quarter(tmp_path, "2025q1",
                   [f"b-1\t10-FEB-2025\t4\t{ISSUER}\t",
                    f"b-2\t11-FEB-2025\t4\t{ISSUER}\t",
                    f"b-3\t12-FEB-2025\t4\t{ISSUER}\t"],
                   ["b-1\tP1\tDOE JANE\tDirector,Officer\tCEO",
                    "b-2\tP2\tBIG FUND LP\tTenPercentOwner\t",
                    "b-3\tP1\tDOE JANE\tDirector,Officer\tCEO"],
                   # the later transaction in a filing states the holding after it
                   trans=["b-1\t7\tCommon Stock\t08-FEB-2025\t900\tD\t",
                          "b-1\t8\tCommon Stock\t09-FEB-2025\t800\tD\t",
                          "b-1\t9\tSeries A Preferred Stock\t09-FEB-2025\t5\tD\t",
                          "b-2\t10\tCommon Stock\t09-FEB-2025\t9999\tD\t"],
                   holdings=["b-1\t1\tCommon Stock\t200\tI\tBy trust"])
    latest = {}
    for path in (old, new):
        insiders.fold(path, {ISSUER}, latest)
    people = insiders.management(latest)
    assert [p["name"] for p in people] == ["DOE JANE"]
    jane = people[0]
    # b-3 reports no shares (an option grant), so b-1 still owns the holding
    assert (jane["shares"], jane["indirect_shares"], jane["accn"]) == (1000, 200, "b-1")
    assert (jane["filed"], jane["last_filed"], jane["title"]) == ("2025-02-10", "2025-02-12", "CEO")


def _person(name, shares, filed, accn, source="SEC"):
    return {"source": source, "person": name, "name": name, "title": "Director",
            "shares": shares, "indirect_shares": 0, "filed": filed, "accn": accn,
            "form": "4", "last_filed": filed}


def test_stale_person_is_listed_apart_and_unknown_is_not_zero():
    out = insiders.summary([
        _person("A", 1000, "2026-01-01", "x-1"),
        _person("B", None, "2026-02-01", "x-2"),
        _person("C", 5000, "2023-01-01", "x-3"),   # over two years before today
        _person("D", 1000, "2026-01-01", "x-1"),   # same joint filing as A
    ], shares_outstanding=10_000, today=date(2026, 11, 21))
    assert [p["name"] for p in out["people"]] == ["A", "D", "B"]
    assert [p["name"] for p in out["possibly_former"]] == ["C"]
    assert out["people"][2]["shares"] is None
    assert (out["total_shares"], out["unknown_holdings"]) == (1000, 1)
    assert out["percent_of_outstanding"] == 10.0


def test_no_known_holding_has_no_total_and_no_people_is_unknown():
    out = insiders.summary([_person("B", None, "2026-02-01", "x-2")], 10_000,
                           today=date(2026, 11, 21))
    assert out["total_shares"] is None and out["percent_of_outstanding"] is None
    assert insiders.summary([], 10_000) is None


def test_edinet_directors_each_count_and_are_never_stale():
    people = [_person(n, 100, "2015-06-30", "S100AAAA", source="EDINET") for n in "AB"]
    out = insiders.summary(people, 1000, today=date(2026, 11, 21))
    assert out["possibly_former"] == [] and out["total_shares"] == 200


def _archive(facts: str) -> bytes:
    instance = f"""<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-11-01/jpcrp_cor">
      {facts}</xbrli:xbrl>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("XBRL/PublicDoc/report.xbrl", instance)
    return buffer.getvalue()


def test_edinet_directors_read_current_board_not_proposal():
    facts = """
      <jpcrp_cor:NameInformationAboutDirectorsAndCorporateAuditors contextRef="F_YamadaMember">山田　太郎</jpcrp_cor:NameInformationAboutDirectorsAndCorporateAuditors>
      <jpcrp_cor:OfficialTitleOrPositionInformationAboutDirectorsAndCorporateAuditors contextRef="F_YamadaMember">代表取締役
        社長</jpcrp_cor:OfficialTitleOrPositionInformationAboutDirectorsAndCorporateAuditors>
      <jpcrp_cor:NumberOfSharesHeldOrdinarySharesInformationAboutDirectorsAndCorporateAuditors contextRef="F_YamadaMember" unitRef="shares" decimals="0">49700</jpcrp_cor:NumberOfSharesHeldOrdinarySharesInformationAboutDirectorsAndCorporateAuditors>
      <jpcrp_cor:NumberOfSharesHeldOrdinarySharesInformationAboutDirectorsAndCorporateAuditorsProposal contextRef="F_YamadaMember" unitRef="shares" decimals="0">1</jpcrp_cor:NumberOfSharesHeldOrdinarySharesInformationAboutDirectorsAndCorporateAuditorsProposal>
      <jpcrp_cor:NameInformationAboutDirectorsAndCorporateAuditors contextRef="F_SatoMember">佐藤　花子</jpcrp_cor:NameInformationAboutDirectorsAndCorporateAuditors>
      <jpcrp_cor:NumberOfSharesHeldOrdinarySharesInformationAboutDirectorsAndCorporateAuditors contextRef="F" unitRef="shares" decimals="0">49700</jpcrp_cor:NumberOfSharesHeldOrdinarySharesInformationAboutDirectorsAndCorporateAuditors>
    """
    archive = _archive(facts)
    assert edinet.directors(archive, "S100TEST") == [
        {"name": "山田 太郎", "title": "代表取締役 社長", "shares": 49700.0},
        {"name": "佐藤 花子", "title": None, "shares": 0.0},   # dash; total proves zero
    ]
    unconfirmed = _archive(facts.replace('contextRef="F" unitRef="shares" decimals="0">49700',
                                         'contextRef="F" unitRef="shares" decimals="0">50000'))
    assert edinet.directors(unconfirmed, "S100TEST")[1]["shares"] is None


def test_insiders_command_uses_the_correction_of_the_latest_report(tmp_path, monkeypatch):
    import json
    from screener import sync

    cache = tmp_path / "cache"
    (cache / "edinet").mkdir(parents=True)
    member = ('<jpcrp_cor:NameInformationAboutDirectorsAndCorporateAuditors contextRef="F_AMember">'
              'A</jpcrp_cor:NameInformationAboutDirectorsAndCorporateAuditors>'
              '<jpcrp_cor:NumberOfSharesHeldOrdinarySharesInformationAboutDirectorsAndCorporateAuditors'
              ' contextRef="F_AMember" unitRef="shares">{n}</jpcrp_cor:'
              'NumberOfSharesHeldOrdinarySharesInformationAboutDirectorsAndCorporateAuditors>')
    for doc, n in (("S100OLD1", 1), ("S100CORR", 300)):
        (cache / "edinet" / f"{doc}.zip").write_bytes(_archive(member.format(n=n)))
    # the import keeps the latest version of each report; S100OLD1 is the prior year
    (cache / "companyfacts_E00001.json").write_text(json.dumps({"_adapter": {"reports": [
        {"period_end": "2025-03-31", "published": "2025-06-30", "document": "S100OLD1",
         "corrects": None},
        {"period_end": "2026-03-31", "published": "2026-08-01", "document": "S100CORR",
         "corrects": "S100ORIG"}]}}))
    conn = store.connect(tmp_path / "db.sqlite")
    store.upsert_company(conn, "E00001", "1111.T", "Test KK")
    store.put_snapshot(conn, "E00001", "ok", {"cik": "E00001", "shares": 3000})

    class Edgar:
        user_agent, cache_dir = "test", cache
    monkeypatch.setattr(sync, "EdgarClient", Edgar)
    monkeypatch.setattr(sync.insider_data, "quarter_urls", lambda _: {})
    sync.insiders(conn, progress=lambda *a: None)

    row = next(r for r in store.dashboard_rows(conn) if r["cik"] == "E00001")
    held = row["management_holdings"]
    assert held["people"][0] == {
        "name": "A", "title": None, "shares": 300.0, "indirect_shares": None,
        "filed": "2026-08-01", "accn": "S100CORR", "form": "Annual report correction",
        "last_filed": "2026-08-01"}
    assert held["percent_of_outstanding"] == 10.0


def test_shared_indirect_block_counts_once_and_overlap_withholds_percent():
    a = _person("A", 1_100, "2026-05-05", "x-1") | {"indirect_shares": 1_000}
    b = _person("B", 1_050, "2026-05-05", "x-2") | {"indirect_shares": 1_000}
    out = insiders.summary([a, b], 2_000, today=date(2026, 11, 21))
    assert out["total_shares"] == 1_150 and out["percent_of_outstanding"] == 57.5
    out = insiders.summary([a, b], 1_000, today=date(2026, 11, 21))
    assert out["percent_of_outstanding"] is None and out["percent_withheld"] is True
