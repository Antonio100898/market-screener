import io
import json
import sqlite3
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from screener import company_import
from screener.artifacts import EvidenceArtifact
from screener.company_import import (
    BulkImportSummary,
    CompanyImporter,
    DerivationError,
    ImportFailure,
    RetainedInputError,
    main,
)
from screener.object_store import VerifiedObject, content_address
from screener.shared_companies import StoredCompany
from screener.sources import cover


class MemoryObjectStore:
    def __init__(self):
        self.data = {}
        self.reads = []

    def put_verified(self, data, media_type):
        del media_type
        digest, key = content_address(data)
        self.data.setdefault(key, data)
        return VerifiedObject(digest, key, len(data), datetime.now(timezone.utc))

    def read_verified(self, key, expected_sha256, expected_size):
        data = self.data[key]
        assert content_address(data)[0] == expected_sha256
        assert len(data) == expected_size
        self.reads.append(key)
        return data


class MemoryArtifactRepository:
    def __init__(self):
        self.rows = {}

    def add_verified(self, verified, media_type):
        row = EvidenceArtifact(
            verified.content_sha256,
            verified.object_key,
            verified.byte_size,
            media_type,
            datetime.now(timezone.utc),
            verified.verified_at,
        )
        return self.rows.setdefault(verified.content_sha256, row)


class MemoryCompanyRepository:
    def __init__(self):
        self.calls = []
        self.rows = {}
        self.security_ids = {}

    def store_and_select(self, **values):
        values["artifacts"] = tuple(values["artifacts"])
        self.calls.append(values)
        security_id = self.security_ids.setdefault(
            values["security_identifier"], len(self.security_ids) + 1
        )
        identity = (
            values["security_identifier"],
            json.dumps(values["canonical_payload"], sort_keys=True),
            values["source_accession"],
            str(values["receipt_ratio"]),
            values["artifacts"],
        )
        created = identity not in self.rows
        if created:
            self.rows[identity] = StoredCompany(
                issuer_id=security_id,
                security_id=security_id,
                snapshot_id=len(self.rows) + 1,
                payload_sha256=f"{len(self.rows) + 1:064x}",
                engine_revision=values["engine_revision"],
            )
        return self.rows[identity] if created else replace(self.rows[identity], created=False)


class FailSecondCompanyOnce(MemoryCompanyRepository):
    def __init__(self):
        super().__init__()
        self.failed = False

    def store_and_select(self, **values):
        if len(self.calls) == 1 and not self.failed:
            self.failed = True
            raise RuntimeError("interrupted")
        return super().store_and_select(**values)


@pytest.fixture
def retained(tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE company (
            cik TEXT PRIMARY KEY,
            ticker TEXT,
            name TEXT,
            exchange TEXT
        );
        CREATE TABLE security_cover (
            cik TEXT NOT NULL,
            symbol TEXT NOT NULL,
            accn TEXT NOT NULL,
            title TEXT NOT NULL,
            ratio TEXT,
            PRIMARY KEY (cik, symbol)
        );
        CREATE TABLE snapshot (
            cik TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            data TEXT
        );
        """
    )
    yield connection, tmp_path
    connection.close()


def _sec(
    connection,
    cache,
    ticker="ABT",
    cik="0000001800",
    *,
    title=None,
    ratio=None,
    with_cover=True,
):
    title = title or "Common Shares, Without Par Value"
    connection.execute(
        "INSERT INTO company (cik, ticker, name, exchange) VALUES (?, ?, ?, ?)",
        (cik, ticker, f"{ticker} issuer", "NYSE"),
    )
    if with_cover:
        connection.execute(
            """INSERT INTO security_cover (cik, symbol, accn, title, ratio)
               VALUES (?, ?, ?, ?, ?)""",
            (cik, ticker, f"{ticker}-accession", title, ratio),
        )
    (cache / f"companyfacts_{cik}.json").write_text(
        json.dumps({"cik": int(cik), "entityName": f"{ticker} issuer", "facts": {}})
    )


def _ticker_map(cache, *pairs):
    (cache / "company_tickers.json").write_text(
        json.dumps(
            {
                str(index): {"cik_str": int(cik), "ticker": ticker, "title": ticker}
                for index, (cik, ticker) in enumerate(pairs)
            }
        )
    )


def _raw_cover(cache, accession, title, symbol, report_number=2):
    raw = (
        "<table><tr><td>Title of 12(b) Security</td>"
        f"<td>{title}</td></tr><tr><td>Trading Symbol</td>"
        f"<td>{symbol}</td></tr></table>"
    ).encode()
    path = cover.report_cache_path(cache, accession, report_number)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    return raw


def _raw_cover_primary(cache, accession, document, text):
    raw = text.encode()
    path = cover.primary_document_cache_path(cache, accession, document)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _edinet(connection, cache, documents=("S100OLD", "S100NEW")):
    connection.execute(
        "INSERT INTO company (cik, ticker, name, exchange) VALUES (?, ?, ?, ?)",
        ("E01772", "6752.T", "Panasonic Holdings Corporation", "TSE"),
    )
    reports = [
        {"document": document, "published": f"202{index + 5}-06-19"}
        for index, document in enumerate(documents)
    ]
    facts = {
        "cik": "E01772",
        "entityName": "Panasonic Holdings Corporation",
        "facts": {"canonical": {}},
        "_adapter": {
            "kind": "edinet_xbrl",
            "statement_basis": "canonical",
            "reporting_currency": "JPY",
            "quote_currency": "JPY",
            "ticker": "6752.T",
            "security_code": "67520",
            "security_basis": "PRIMARY_ORDINARY_SHARE",
            "reports": reports,
        },
    }
    (cache / "companyfacts_E01772.json").write_text(json.dumps(facts))
    (cache / "edinet").mkdir()
    for document in documents:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("report.xbrl", document)
        (cache / "edinet" / f"{document}.zip").write_bytes(output.getvalue())


def _importer(retained, derive, companies=None):
    connection, cache = retained
    objects = MemoryObjectStore()
    artifacts = MemoryArtifactRepository()
    companies = companies or MemoryCompanyRepository()
    return (
        CompanyImporter(connection, cache, objects, artifacts, companies, derive),
        objects,
        artifacts,
        companies,
    )


def test_sec_import_uses_verified_facts_dimensioned_facts_and_cover(retained):
    connection, cache = retained
    _sec(connection, cache)
    (cache / "dimensioned_0000001800.json").write_text(
        json.dumps({"facts": {"us-gaap": {}}})
    )

    def derive(bundle):
        assert bundle.ticker == "ABT"
        assert bundle.dimensioned == {"facts": {"us-gaap": {}}}
        assert bundle.receipt["accn"] == "ABT-accession"
        return "ok", {"ticker": "ABT", "source": bundle.receipt["accn"]}

    importer, objects, _, companies = _importer(retained, derive)
    result = importer.import_ticker("abt")

    assert result.security_identifier == "sec:0000001800:primary-listed-security"
    assert [item.role for item in result.artifacts] == [
        "official_api_facts",
        "dimensioned_facts",
        "structured_cover_identity",
    ]
    assert len(objects.reads) == 3
    assert companies.calls[0]["security_basis"] == "PRIMARY_COMMON_SHARE"


def test_sec_import_links_exact_raw_cover_and_reuses_it_idempotently(retained):
    connection, cache = retained
    _sec(connection, cache)
    raw = _raw_cover(
        cache,
        "ABT-accession",
        "Common Shares, Without Par Value",
        "ABT",
    )
    importer, objects, artifacts, companies = _importer(
        retained, lambda bundle: ("ok", {"ticker": bundle.ticker})
    )

    first = importer.import_ticker("ABT")
    second = importer.import_ticker("ABT")

    assert [item.role for item in first.artifacts] == [
        "official_api_facts",
        "raw_cover_filing",
        "structured_cover_identity",
    ]
    raw_artifact = next(
        item.artifact for item in first.artifacts if item.role == "raw_cover_filing"
    )
    assert objects.data[raw_artifact.object_key] == raw
    assert first.stored.snapshot_id == second.stored.snapshot_id
    assert len(artifacts.rows) == 3
    assert len(companies.rows) == 1


def test_sec_ads_keeps_exact_cover_accession_and_ratio(retained):
    connection, cache = retained
    _sec(
        connection,
        cache,
        ticker="NTES",
        cik="0001110646",
        title="American Depositary Shares, each representing five ordinary shares",
        ratio="5",
    )
    importer, _, _, companies = _importer(
        retained,
        lambda bundle: ("ok", {"ticker": bundle.ticker, "receipt": bundle.receipt}),
    )

    importer.import_ticker("NTES")

    saved = companies.calls[0]
    assert saved["source_accession"] == "NTES-accession"
    assert saved["receipt_ratio"] == "5"
    assert saved["security_basis"] == "PRIMARY_DEPOSITARY_RECEIPT"


def test_sec_ads_links_unique_matching_primary_document(retained):
    connection, cache = retained
    _sec(
        connection,
        cache,
        ticker="FMS",
        cik="0001333141",
        title="American Depositary Shares",
        ratio="0.5",
    )
    raw = _raw_cover_primary(
        cache,
        "FMS-accession",
        "fms-20251231x20f.htm",
        "Where ADSs are held, two ADSs represent one ordinary share.",
    )
    importer, objects, _, _ = _importer(
        retained, lambda bundle: ("ok", {"ticker": bundle.ticker})
    )

    result = importer.import_ticker("FMS")

    primary = next(
        item for item in result.artifacts
        if item.role == "raw_cover_primary_document"
    )
    assert objects.data[primary.artifact.object_key] == raw


def test_sec_ads_does_not_link_conflicting_primary_document(retained):
    connection, cache = retained
    _sec(
        connection,
        cache,
        ticker="WAVE",
        cik="0001846715",
        title="American Depositary Shares",
        ratio="8",
    )
    _raw_cover_primary(
        cache,
        "WAVE-accession",
        "wave.htm",
        "Each ADS represents eight common shares. Historical ADSs each "
        "represented one common share.",
    )
    importer, _, _, _ = _importer(
        retained, lambda bundle: ("ok", {"ticker": bundle.ticker})
    )

    result = importer.import_ticker("WAVE")

    assert "raw_cover_primary_document" not in [
        item.role for item in result.artifacts
    ]


def test_sec_ads_without_ratio_does_not_use_ticker_mapping_fallback(retained):
    connection, cache = retained
    _sec(
        connection,
        cache,
        ticker="NTES",
        cik="0001110646",
        title="American Depositary Shares",
    )
    _ticker_map(cache, ("0001110646", "NTES"))
    importer, _, artifacts, companies = _importer(
        retained, lambda bundle: ("ok", {"ticker": bundle.ticker})
    )

    with pytest.raises(RetainedInputError, match="depositary receipt ratio is missing"):
        importer.import_ticker("NTES")

    assert len(artifacts.rows) == 2
    assert companies.calls == []


def test_sec_without_cover_uses_verified_exact_official_ticker_mapping(retained):
    connection, cache = retained
    _sec(connection, cache, with_cover=False)
    _ticker_map(cache, ("0000001800", "ABT"))

    def derive(bundle):
        assert bundle.receipt is None
        return "ok", {"ticker": bundle.ticker}

    importer, objects, _, companies = _importer(retained, derive)
    result = importer.import_ticker("ABT")

    assert [item.role for item in result.artifacts] == [
        "official_api_facts",
        "official_sec_ticker_mapping",
    ]
    assert len(objects.reads) == 2
    saved = companies.calls[0]
    assert saved["security_title"] is None
    assert saved["source_accession"] is None
    assert saved["receipt_ratio"] is None
    assert saved["security_basis"] == "SEC_TICKER_MAPPING_ONLY"


@pytest.mark.parametrize("missing_field", ["title", "accn"])
def test_incomplete_sec_cover_uses_verified_exact_ticker_mapping(
    retained, missing_field
):
    connection, cache = retained
    _sec(connection, cache)
    connection.execute(
        f"UPDATE security_cover SET {missing_field} = '' WHERE cik = ?",
        ("0000001800",),
    )
    _ticker_map(cache, ("0000001800", "ABT"))

    expected_receipt = {
        "cik": "0000001800",
        "symbol": "ABT",
        "accn": "" if missing_field == "accn" else "ABT-accession",
        "title": "" if missing_field == "title" else "Common Shares, Without Par Value",
        "ratio": None,
    }

    def derive(bundle):
        assert bundle.receipt == expected_receipt
        return "ok", {"ticker": bundle.ticker, "receipt": bundle.receipt}

    importer, _, _, companies = _importer(retained, derive)
    result = importer.import_ticker("ABT")

    assert [item.role for item in result.artifacts] == [
        "official_api_facts",
        "official_sec_ticker_mapping",
    ]
    assert companies.calls[0]["security_title"] is None
    assert companies.calls[0]["source_accession"] is None
    assert companies.calls[0]["security_basis"] == "SEC_TICKER_MAPPING_ONLY"
    assert companies.calls[0]["canonical_payload"]["receipt"] == expected_receipt


@pytest.mark.parametrize("missing_field", ["title", "accn"])
def test_incomplete_sec_cover_rejects_ticker_map_mismatch(retained, missing_field):
    connection, cache = retained
    _sec(connection, cache)
    connection.execute(
        f"UPDATE security_cover SET {missing_field} = '' WHERE cik = ?",
        ("0000001800",),
    )
    _ticker_map(cache, ("0000001801", "ABT"))

    importer, _, _, companies = _importer(
        retained, lambda bundle: ("ok", {"ticker": bundle.ticker})
    )
    with pytest.raises(
        RetainedInputError, match="does not map 0000001800 to ABT"
    ):
        importer.import_ticker("ABT")
    assert companies.calls == []


def test_incomplete_foreign_cover_still_uses_derivation_as_support_gate(retained):
    connection, cache = retained
    _sec(connection, cache)
    connection.execute(
        "UPDATE security_cover SET title = '' WHERE cik = ?",
        ("0000001800",),
    )
    _ticker_map(cache, ("0000001800", "ABT"))

    def derive(bundle):
        assert bundle.receipt["accn"] == "ABT-accession"
        assert bundle.receipt["title"] == ""
        return "foreign", None

    importer, _, _, companies = _importer(retained, derive)
    with pytest.raises(DerivationError, match="derived as foreign"):
        importer.import_ticker("ABT")
    assert companies.calls == []


@pytest.mark.parametrize(
    "mapping, message",
    [
        (None, "missing retained input"),
        ((("0000001801", "ABT"),), "does not map 0000001800 to ABT"),
        ((("0000001800", "OTHER"),), "does not map 0000001800 to ABT"),
    ],
)
def test_sec_without_cover_rejects_missing_or_mismatched_ticker_map(
    retained, mapping, message
):
    connection, cache = retained
    _sec(connection, cache, with_cover=False)
    if mapping is not None:
        _ticker_map(cache, *mapping)

    importer, _, _, companies = _importer(
        retained, lambda bundle: ("ok", {"ticker": bundle.ticker})
    )
    with pytest.raises(RetainedInputError, match=message):
        importer.import_ticker("ABT")
    assert companies.calls == []


def test_sec_without_cover_keeps_foreign_derivation_excluded(retained):
    connection, cache = retained
    _sec(connection, cache, with_cover=False)
    _ticker_map(cache, ("0000001800", "ABT"))
    importer, _, artifacts, companies = _importer(
        retained, lambda bundle: ("foreign", None) if bundle.receipt is None else ("ok", {})
    )

    with pytest.raises(DerivationError, match="derived as foreign"):
        importer.import_ticker("ABT")

    assert len(artifacts.rows) == 2
    assert companies.calls == []


def test_edinet_import_verifies_canonical_facts_and_every_manifest_zip(retained):
    connection, cache = retained
    _edinet(connection, cache)

    def derive(bundle):
        assert bundle.cik == "E01772"
        assert bundle.receipt is None
        return "ok", {"ticker": bundle.ticker, "currency": "JPY"}

    importer, objects, _, companies = _importer(retained, derive)
    result = importer.import_ticker("6752.T")

    assert result.security_identifier == "edinet:E01772:primary-ordinary"
    assert [item.role for item in result.artifacts] == [
        "canonical_edinet_facts",
        "raw_filing",
        "raw_filing",
    ]
    assert len(objects.reads) == 3
    saved = companies.calls[0]
    assert saved["quote_currency"] == "JPY"
    assert saved["security_basis"] == "PRIMARY_ORDINARY_SHARE"
    assert saved["source_accession"] == "S100NEW"


def test_missing_retained_input_is_visible_and_does_not_derive(retained):
    connection, cache = retained
    _sec(connection, cache)
    (cache / "companyfacts_0000001800.json").unlink()
    called = False

    def derive(_bundle):
        nonlocal called
        called = True
        return "ok", {}

    importer, _, _, companies = _importer(retained, derive)
    with pytest.raises(RetainedInputError, match="missing retained input"):
        importer.import_ticker("ABT")

    assert called is False
    assert companies.calls == []


def test_failed_derivation_does_not_store_or_select_snapshot(retained):
    connection, cache = retained
    _sec(connection, cache)
    importer, _, artifacts, companies = _importer(
        retained, lambda _bundle: ("foreign", None)
    )

    with pytest.raises(DerivationError, match="derived as foreign"):
        importer.import_ticker("ABT")

    assert len(artifacts.rows) == 2
    assert companies.calls == []


def test_identical_retry_reuses_artifacts_and_snapshot(retained):
    connection, cache = retained
    _sec(connection, cache)
    importer, _, artifacts, companies = _importer(
        retained, lambda bundle: ("ok", {"ticker": bundle.ticker})
    )

    first = importer.import_ticker("ABT")
    second = importer.import_ticker("ABT")

    assert first.stored.snapshot_id == second.stored.snapshot_id
    assert len(artifacts.rows) == 2
    assert len(companies.rows) == 1


def test_rerun_after_second_company_interrupt_keeps_first_and_completes_batch(retained):
    connection, cache = retained
    _sec(connection, cache)
    _sec(
        connection,
        cache,
        ticker="NTES",
        cik="0001110646",
        title="American Depositary Shares, each representing five ordinary shares",
        ratio="5",
    )
    companies = FailSecondCompanyOnce()
    importer, _, artifacts, _ = _importer(
        retained,
        lambda bundle: ("ok", {"ticker": bundle.ticker}),
        companies,
    )

    with pytest.raises(RuntimeError, match="interrupted"):
        importer.import_tickers(["ABT", "NTES"])
    completed = importer.import_tickers(["ABT", "NTES"])

    assert [result.ticker for result in completed] == ["ABT", "NTES"]
    assert len(companies.rows) == 2
    assert len(artifacts.rows) == 4


def test_bulk_uses_dashboard_eligibility_stable_order_and_continues_failures(retained):
    connection, cache = retained
    for ticker, cik in (
        ("ZZZ", "0000000003"),
        ("BAD", "0000000002"),
        ("AAA", "0000000001"),
        ("SKIP", "0000000004"),
    ):
        _sec(connection, cache, ticker=ticker, cik=cik)
        connection.execute(
            "INSERT INTO snapshot (cik, status, data) VALUES (?, ?, ?)",
            (cik, "foreign" if ticker == "SKIP" else "ok", "{}"),
        )

    def derive(bundle):
        if bundle.ticker == "BAD":
            raise RuntimeError("bad retained evidence")
        return "ok", {"ticker": bundle.ticker}

    importer, _, _, companies = _importer(retained, derive)
    first = importer.import_dashboard()
    second = importer.import_dashboard()

    assert first == BulkImportSummary(
        imported=("AAA", "ZZZ"),
        reused=(),
        failed=(ImportFailure("BAD", "bad retained evidence"),),
    )
    assert second.imported == ()
    assert second.reused == ("AAA", "ZZZ")
    assert second.failed == first.failed
    assert [call["ticker"] for call in companies.calls] == [
        "AAA",
        "ZZZ",
        "AAA",
        "ZZZ",
    ]


def test_cli_requires_exactly_one_selection_mode():
    with pytest.raises(SystemExit) as no_selection:
        main([])
    with pytest.raises(SystemExit) as both:
        main(["--all-dashboard", "ABT"])

    assert no_selection.value.code == 2
    assert both.value.code == 2


def test_bulk_cli_prints_summary_and_exits_nonzero_on_any_failure(
    monkeypatch, capsys, tmp_path
):
    summary = BulkImportSummary(
        imported=("AAA",),
        reused=("ZZZ",),
        failed=(ImportFailure("BAD", "missing facts"),),
    )

    class Resource:
        def close(self):
            pass

        def dispose(self):
            pass

    class Importer:
        def __init__(self, *_args):
            pass

        def import_dashboard(self):
            return summary

    monkeypatch.setattr(
        company_import.StorageSettings,
        "from_env",
        lambda: type("Settings", (), {"s3_bucket": "bucket"})(),
    )
    monkeypatch.setattr(company_import, "_read_only_sqlite", lambda _path: Resource())
    monkeypatch.setattr(company_import, "create_postgres_engine", lambda _settings: Resource())
    monkeypatch.setattr(company_import, "create_s3_client", lambda _settings: object())
    monkeypatch.setattr(company_import, "ImmutableObjectStore", lambda *_args: object())
    monkeypatch.setattr(company_import, "SqlArtifactRepository", lambda _engine: object())
    monkeypatch.setattr(company_import, "SharedCompanyRepository", lambda _engine: object())
    monkeypatch.setattr(company_import, "CompanyImporter", Importer)

    assert main(["--all-dashboard", "--sqlite", str(tmp_path / "store.db")]) == 1
    assert json.loads(capsys.readouterr().out) == summary.as_dict()
