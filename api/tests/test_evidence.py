"""Evidence assembly and invalidation — the boundary shared by every pipeline."""
import hashlib
import json
import zipfile
from datetime import date

import pytest

from screener import evidence, store
from screener.sources import dera, inline_xbrl


def test_dera_candidate_is_the_immediately_preceding_closed_quarter():
    """A missing newest zip is already a harmless 404 in ``download``; holding
    the cursor back an extra quarter instead hides datasets that SEC did publish."""
    assert dera.latest_published(date(2026, 1, 1)) == dera.Quarter(2025, 4)
    assert dera.latest_published(date(2026, 4, 1)) == dera.Quarter(2026, 1)
    assert dera.latest_published(date(2026, 9, 8)) == dera.Quarter(2026, 2)


class EdgarStub:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir

    def company_facts(self, cik):  # pragma: no cover - supplied facts should win
        raise AssertionError("unexpected network fetch")


def _write_manifest(tmp_path, cik="0000000001", accession="0000000001-26-000001"):
    directory = inline_xbrl.accession_directory(tmp_path, accession)
    directory.mkdir(parents=True)
    documents = {
        "index": "index.json",
        "instance": "annual_htm.xml",
        "filing_summary": "FilingSummary.xml",
        "presentation": "issuer_pre.xml",
        "schema": "issuer.xsd",
        "primary_document": "annual.htm",
    }
    records = {}
    base = ("https://www.sec.gov/Archives/edgar/data/"
            f"{int(cik)}/{accession.replace('-', '')}/")
    for role, document in documents.items():
        raw = f"{role}-bytes".encode()
        (directory / document).write_bytes(raw)
        records[role] = {
            "document": document,
            "url": base + document,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        }
    manifest = {
        "schema": inline_xbrl.MANIFEST_SCHEMA,
        "parser_contract_revision": inline_xbrl.PARSER_CONTRACT_REVISION,
        "relationship": "direct_annual",
        "cik": cik,
        "entity_name": "Example PLC",
        "annual": {
            "accession": accession,
            "form": "20-F",
            "filed": "2026-03-01",
            "report_date": "2025-12-31",
            "document": "annual.htm",
        },
        "source": {
            "accession": accession,
            "form": "20-F",
            "filed": "2026-03-01",
            "document": "annual.htm",
        },
        "files": records,
    }
    path = inline_xbrl.current_manifest_path(tmp_path, cik)
    path.parent.mkdir(parents=True)
    path.write_bytes(inline_xbrl.manifest_bytes(manifest))
    return manifest, path


def _current_facts(accession="0000000001-26-000001"):
    return {"cik": "0000000001", "facts": {"dei": {"EntityPublicFloat": {
        "units": {"USD": [{
            "val": 1, "end": "2025-12-31", "accn": accession,
            "form": "20-F", "filed": "2026-03-01",
        }]},
    }}}}


def test_loader_always_adds_dimensioned_and_cover_evidence(tmp_path):
    cik, ticker = "0000000001", "ADR"
    facts = {"facts": {"us-gaap": {}}}
    dimensioned = {"facts": {"us-gaap": {"EarningsPerShareDiluted": {}}}}
    (tmp_path / f"dimensioned_{cik}.json").write_text(json.dumps(dimensioned))
    conn = store.connect(tmp_path / "store.db")
    store.set_cover(conn, cik, [{
        "symbol": ticker,
        "title": "American Depositary Shares, each representing 13 Ordinary Shares",
        "ratio": 13,
    }], "accn-1")

    bundle = evidence.EvidenceLoader(conn, EdgarStub(tmp_path)).load(cik, ticker, facts)

    assert bundle.facts is facts
    assert bundle.dimensioned == dimensioned
    assert bundle.receipt["ratio"] == "13"
    assert bundle.receipt["accn"] == "accn-1"


def test_loader_merges_only_a_verified_current_manifest(tmp_path, monkeypatch):
    manifest, _ = _write_manifest(tmp_path)
    supplemental = {
        "cik": "0000000001",
        "facts": {"ifrs-full": {"Assets": {"units": {"USD": [{
            "val": "10", "end": "2025-12-31",
            "accn": manifest["annual"]["accession"], "form": "20-F",
            "filed": "2026-03-01", "_source_context_id": "instant",
        }]}}}},
        "_inline_xbrl": {"source_accession": manifest["annual"]["accession"]},
    }
    monkeypatch.setattr(inline_xbrl, "parse_inline_xbrl", lambda *args: supplemental)

    bundle = evidence.EvidenceLoader(
        store.connect(tmp_path / "store.db"), EdgarStub(tmp_path)
    ).load("0000000001", "TEST", _current_facts())

    assert bundle.supplement_state == "applied"
    assert bundle.facts["facts"]["ifrs-full"]["Assets"]["units"]["USD"][0][
        "_source_context_id"
    ] == "instant"


def test_loader_ignores_a_stale_manifest_with_explicit_state(tmp_path, monkeypatch):
    _write_manifest(tmp_path)
    monkeypatch.setattr(
        inline_xbrl, "parse_inline_xbrl",
        lambda *args: pytest.fail("stale manifest must not be parsed"),
    )
    facts = _current_facts("0000000001-26-000002")

    bundle = evidence.EvidenceLoader(
        store.connect(tmp_path / "store.db"), EdgarStub(tmp_path)
    ).load("0000000001", "TEST", facts)

    assert bundle.facts is facts
    assert bundle.supplement_state == "stale"


def test_loader_fails_closed_for_missing_or_changed_retained_bytes(tmp_path, monkeypatch):
    manifest, _ = _write_manifest(tmp_path)
    monkeypatch.setattr(inline_xbrl, "parse_inline_xbrl", lambda *args: {})
    directory = inline_xbrl.accession_directory(tmp_path, manifest["annual"]["accession"])
    (directory / manifest["files"]["instance"]["document"]).write_bytes(b"changed")

    with pytest.raises(ValueError, match="file verification failed: instance"):
        evidence.EvidenceLoader(
            store.connect(tmp_path / "store.db"), EdgarStub(tmp_path)
        ).load("0000000001", "TEST", _current_facts())


def test_loader_rejects_an_incorporated_source_relationship(tmp_path):
    manifest, path = _write_manifest(tmp_path)
    manifest["source"]["accession"] = "0000000001-26-000099"
    path.write_bytes(inline_xbrl.manifest_bytes(manifest))

    with pytest.raises(
        inline_xbrl.UnsupportedInlineXbrlRelationship,
        match="incorporated_filing_relationship",
    ):
        evidence.EvidenceLoader(
            store.connect(tmp_path / "store.db"), EdgarStub(tmp_path)
        ).load("0000000001", "TEST", _current_facts())


def _prior_security(title="Class A Ordinary Shares, par value $0.001 per share"):
    return {
        "symbol": "OLD",
        "title": title,
        "ratio": None,
        "accn": "annual-1",
    }


def test_later_sec_filing_proves_same_class_ticker_continuity():
    filing = (
        "Securities offered: Class A Ordinary Shares, par value $0.001 per share. "
        "Our Class A Ordinary Shares are listed on the Nasdaq Capital Market "
        "under the symbol “NEW”."
    )

    resolved = evidence.continuity_security(
        [_prior_security()], filing, "NEW", "Nasdaq"
    )

    assert resolved == {
        **_prior_security(),
        "previous_symbol": "OLD",
        "symbol": "NEW",
        "exchange": "Nasdaq",
        "basis_accn": "annual-1",
    }


def test_short_listing_phrase_requires_the_exact_full_annual_title_in_same_filing():
    filing = (
        "Our Class A Ordinary Shares are listed on the Nasdaq Capital Market "
        "under the symbol NEW."
    )

    assert evidence.continuity_security(
        [_prior_security()], filing, "NEW", "Nasdaq"
    ) is None


def test_later_sec_filing_rejects_ambiguous_or_changed_classes():
    ambiguous = (
        "Ordinary Shares, par value $0.001 per share. "
        "Ordinary Shares, no par value. Our Ordinary Shares are listed on "
        "the Nasdaq Capital Market under the symbol NEW."
    )
    priors = [
        _prior_security("Ordinary Shares, par value $0.001 per share"),
        {**_prior_security("Ordinary Shares, no par value"), "symbol": "OLD2"},
    ]
    changed = (
        "Class B Ordinary Shares, par value $0.001 per share. "
        "Our Class B Ordinary Shares are listed on the Nasdaq Capital Market "
        "under the symbol NEW."
    )

    assert evidence.continuity_security(priors, ambiguous, "NEW", "Nasdaq") is None
    assert evidence.continuity_security(
        [_prior_security()], changed, "NEW", "Nasdaq"
    ) is None


def test_later_sec_filing_rejects_changed_exchange_and_otc_alias():
    filing = (
        "Class A Ordinary Shares, par value $0.001 per share. "
        "Our Class A Ordinary Shares are listed on the Nasdaq Capital Market "
        "under the symbol NEW."
    )

    assert evidence.continuity_security(
        [_prior_security()], filing, "NEW", "NYSE"
    ) is None
    assert evidence.continuity_security(
        [_prior_security()], filing, "NEW", "OTC"
    ) is None


def test_unbound_later_filing_prose_cannot_replace_the_annual_depositary_ratio():
    prior = _prior_security("American Depositary Shares")
    prior["ratio"] = "2"
    base = (
        "American Depositary Shares. Our American Depositary Shares are listed "
        "on the New York Stock Exchange under the symbol ADR."
    )

    kept = evidence.continuity_security([prior], base, "ADR", "NYSE")
    unrelated = evidence.continuity_security(
        [prior], base + " Each ADS represents five ordinary shares.", "ADR", "NYSE"
    )

    assert kept["ratio"] == "2"
    assert unrelated["ratio"] == "2"


def test_cover_storage_retains_annual_and_later_observations(tmp_path):
    conn = store.connect(tmp_path / "store.db")
    prior = _prior_security()
    store.set_cover(conn, "0000000001", [prior], "annual-1", "2025-03-01")
    later = {
        **prior,
        "previous_symbol": "OLD",
        "symbol": "NEW",
        "exchange": "Nasdaq",
        "basis_accn": "annual-1",
    }
    store.set_cover_continuity(
        conn, "0000000001", later, "later-1", "2025-07-01"
    )

    observations = conn.execute(
        """SELECT accn, symbol, basis_accn FROM security_cover_observation
           WHERE cik = ? ORDER BY accn""",
        ("0000000001",),
    ).fetchall()

    assert [tuple(row) for row in observations] == [
        ("annual-1", "OLD", "annual-1"),
        ("later-1", "NEW", "annual-1"),
    ]
    assert store.cover_for(conn, "0000000001", "OLD") is None
    assert store.cover_for(conn, "0000000001", "NEW")["accn"] == "later-1"
    _, selected = evidence.EvidenceLoader(conn, EdgarStub(tmp_path)).identity(
        "0000000001", "NEW"
    )
    assert selected["basis_accn"] == "annual-1"


def test_loader_uses_stored_symbol_when_current_sec_mapping_is_absent(tmp_path):
    cik, ticker = "0000000001", "OLD"
    facts = {"facts": {"us-gaap": {}}}
    conn = store.connect(tmp_path / "store.db")
    store.upsert_company(conn, cik, ticker, "Formerly listed")
    store.set_cover(conn, cik, [{
        "symbol": ticker,
        "title": "Class A common stock",
        "ratio": None,
    }], "accn-1")

    bundle = evidence.EvidenceLoader(conn, EdgarStub(tmp_path)).load(cik, None, facts)

    assert bundle.ticker == ticker
    assert bundle.receipt["symbol"] == ticker
    assert bundle.receipt["title"] == "Class A common stock"


def test_loader_reinterprets_a_stored_cover_title_without_refetching(tmp_path):
    """A parser repair applies to immutable evidence already in SQLite. AMBO's
    title always said 20 ordinary shares per ADS; only the old grammar missed it."""
    cik, ticker = "0000000001", "AMBO"
    facts = {"facts": {"us-gaap": {}}}
    conn = store.connect(tmp_path / "store.db")
    store.set_cover(conn, cik, [{
        "symbol": ticker,
        "title": ("American depositary shares (one American depositary share "
                  "representing twenty Class A Ordinary Shares)"),
        "ratio": None,
    }], "accn-1")

    bundle = evidence.EvidenceLoader(conn, EdgarStub(tmp_path)).load(cik, ticker, facts)

    assert bundle.receipt["ratio"] == "20"


def test_store_prefers_the_priced_equity_when_cover_rows_share_a_symbol(tmp_path):
    conn = store.connect(tmp_path / "store.db")
    store.set_cover(conn, "0000000001", [
        {"symbol": "LX", "title": "Class A ordinary shares*", "ratio": None},
        {"symbol": "LX", "title": "American Depositary Shares, each representing "
         "2 Class A ordinary shares", "ratio": 2},
    ], "accn-1")

    saved = store.cover_for(conn, "0000000001", "LX")
    assert saved["ratio"] == "2"
    assert saved["title"].startswith("American Depositary")


def test_loader_selects_exact_ticker_from_a_filed_compound_symbol(tmp_path):
    cik, ticker = "0000000001", "SUZ"
    page = """
      <table>
        <tr><td>Title of 12(b) Security</td><td>American Depositary Shares</td></tr>
        <tr><td>Trading Symbol</td><td>SUZB3/SUZ</td></tr>
        <tr><td>Title of 12(b) Security</td><td>6.000% Notes due 2029</td></tr>
        <tr><td>Trading Symbol</td><td>SUZ/29</td></tr>
      </table>
    """
    conn = store.connect(tmp_path / "store.db")
    from screener.sources import cover

    store.set_cover(conn, cik, cover.securities(page), "accn-1")

    _, receipt = evidence.EvidenceLoader(conn, EdgarStub(tmp_path)).identity(cik, ticker)
    assert receipt["symbol"] == "SUZB3/SUZ"
    assert receipt["title"] == "American Depositary Shares"


def test_loader_rejects_ambiguous_compound_symbol_components(tmp_path):
    conn = store.connect(tmp_path / "store.db")
    store.set_cover(conn, "0000000001", [
        {"symbol": "LOCAL1/ADS", "title": "American Depositary Shares", "ratio": 1},
        {"symbol": "LOCAL2/ADS", "title": "Class A Common Shares", "ratio": None},
    ], "accn-1")

    _, receipt = evidence.EvidenceLoader(conn, EdgarStub(tmp_path)).identity(
        "0000000001", "ADS"
    )
    assert receipt is None


def test_loader_rejects_stale_noncommon_or_unlisted_cover_collisions(tmp_path):
    conn = store.connect(tmp_path / "store.db")
    # Insert directly to reproduce rows damaged by the pre-fix parser; the fixed
    # set_cover() would no longer persist either collision over the priced class.
    conn.execute(
        "INSERT INTO security_cover (cik, symbol, accn, title, read_at) VALUES (?, ?, ?, ?, ?)",
        ("0000000001", "HON", "a1", "2.800% Senior Notes due 2030", store._now()),
    )
    conn.execute(
        "INSERT INTO security_cover (cik, symbol, accn, title, read_at) VALUES (?, ?, ?, ?, ?)",
        ("0000000002", "LX", "a2", "Class A ordinary shares*", store._now()),
    )

    loader = evidence.EvidenceLoader(conn, EdgarStub(tmp_path))
    assert loader.identity("0000000001", "HON")[1] is None
    assert loader.identity("0000000002", "LX")[1] is None


def test_changed_cover_invalidates_current_snapshot_until_recomputed(tmp_path):
    cik = "0000000001"
    conn = store.connect(tmp_path / "store.db")
    store.put_snapshot(conn, cik, "ok", {"ticker": "ADR"})
    assert store.needs_recompute(conn) == []

    security = {"symbol": "ADR", "title": "ADS, each representing 2 Ordinary Shares",
                "ratio": 2}
    store.set_cover(conn, cik, [security], "accn-1")
    assert store.needs_recompute(conn) == [cik]

    store.put_snapshot(conn, cik, "ok", {"ticker": "ADR", "receipt_ratio": 2})
    assert store.needs_recompute(conn) == []
    # Reading the identical cover again is idempotent and must not dirty the row.
    store.set_cover(conn, cik, [security], "accn-1")
    assert store.needs_recompute(conn) == []


def test_dera_merge_reports_only_real_evidence_changes(tmp_path):
    cik = "0000000001"
    harvested = {cik: {"facts": {"us-gaap": {"Assets": {"units": {"USD": [{
        "end": "2026-03-31", "val": 10, "accn": "a", "form": "10-Q",
        "filed": "2026-05-01", "fy": 0, "fp": "FY", "segments": "",
    }]}}}}}}
    quarter = dera.Quarter(2026, 1)

    assert dera.merge_into_sidecars(harvested, tmp_path, quarter) == {cik}
    assert dera.merge_into_sidecars(harvested, tmp_path, quarter) == set()


def test_dera_recognizes_ifrs_version_as_standard_and_keeps_per_share_fact(tmp_path):
    archive = tmp_path / "2026q1.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(
            "sub.txt",
            "adsh\tcik\tform\tfiled\nifrs-1\t1\t20-F\t20260325\n",
        )
        z.writestr(
            "num.txt",
            "adsh\ttag\tversion\tcoreg\tvalue\tddate\tqtrs\tuom\tsegments\n"
            "ifrs-1\tBasicEarningsLossPerShare\tifrs/2024\t\t1.25\t"
            "20251231\t4\tUSD\tClassesOfShareCapital=ClassA;\n",
        )

    harvested = dera.harvest(
        archive, {"0000000001"}, frozenset({"BasicEarningsLossPerShare"}),
        frozenset({"BasicEarningsLossPerShare"}),
    )

    entries = harvested["0000000001"]["facts"]["ifrs-full"] \
        ["BasicEarningsLossPerShare"]["units"]["USD/shares"]
    assert entries[0]["val"] == 1.25
    assert entries[0]["segments"] == "ClassesOfShareCapital=ClassA;"


def test_dera_keeps_allowlisted_small_statement_extension_below_materiality_floor(tmp_path):
    archive = tmp_path / "2025q1.zip"
    tag = "DepreciationAndAmortizationOfPropertyPlantAndEquipmentAndComputerPrograms"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(
            "sub.txt",
            "adsh\tcik\tform\tfiled\nfusb-k24\t717806\t10-K\t20250314\n",
        )
        z.writestr(
            "num.txt",
            "adsh\ttag\tversion\tcoreg\tvalue\tddate\tqtrs\tuom\tsegments\n"
            f"fusb-k24\t{tag}\tfusb/2024\t\t1581000\t20231231\t4\tUSD\t\n"
            "fusb-k24\tUnrelatedSmallExtension\tfusb/2024\t\t2000000\t"
            "20231231\t4\tUSD\t\n",
        )

    harvested = dera.harvest(
        archive, {"0000717806"}, frozenset({tag}),
    )["0000717806"]["facts"]

    assert harvested["ext:fusb/2024"][tag]["units"]["USD"][0]["val"] == 1_581_000
    assert "UnrelatedSmallExtension" not in harvested["ext:fusb/2024"]
