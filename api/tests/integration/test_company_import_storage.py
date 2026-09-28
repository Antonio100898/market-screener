import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from screener import store
from screener.artifacts import SqlArtifactRepository
from screener.company_import import (
    CompanyImporter,
    _canonical_json,
    _read_only_sqlite,
    main,
)
from screener.evidence import EvidenceLoader
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import (
    company_snapshot,
    company_snapshot_artifact,
    create_postgres_engine,
    current_company_snapshot,
    evidence_artifact,
    issuer,
    priced_security,
)
from screener.shared_companies import SharedCompanyRepository
from screener.sources import cover, dera, inline_xbrl
from screener.storage_config import StorageSettings


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_STORAGE_INTEGRATION") != "1",
    reason="set RUN_STORAGE_INTEGRATION=1 with local PostgreSQL and S3 running",
)


def test_real_retained_sec_and_edinet_import_restart_retry_and_readback(
    monkeypatch, tmp_path
):
    base_settings = StorageSettings.from_env()
    database_name = f"ms_import_{uuid4().hex}"
    admin_engine = create_postgres_engine(base_settings).execution_options(
        isolation_level="AUTOCOMMIT"
    )
    database_url = make_url(base_settings.database_url).set(database=database_name)
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    engine = None
    sqlite_connection = None
    try:
        monkeypatch.setenv(
            "SCREENER_DATABASE_URL",
            database_url.render_as_string(hide_password=False),
        )
        command.upgrade(Config("alembic.ini"), "head")
        settings = StorageSettings.from_env()
        engine = create_engine(database_url)
        objects = ImmutableObjectStore(
            create_s3_client(settings),
            settings.s3_bucket,
        )
        sqlite_connection = _read_only_sqlite(store.DEFAULT_DB)
        cache = tmp_path / "retained"
        _copy_company_cache(store.DEFAULT_DB.parent, cache)
        raw_cover = _write_cover_fixture(sqlite_connection, cache)
        raw_primary = _write_primary_fixture(sqlite_connection, cache)
        repository = SharedCompanyRepository(engine)
        interrupted = CompanyImporter(
            sqlite_connection,
            cache,
            objects,
            SqlArtifactRepository(engine),
            repository,
            derive=lambda _bundle: (_ for _ in ()).throw(
                RuntimeError("forced interruption after evidence retention")
            ),
        )
        with pytest.raises(RuntimeError, match="forced interruption"):
            interrupted.import_ticker("AERO")
        assert _counts(engine) == (8, 0, 0, 0, 0, 0)

        importer = CompanyImporter(
            sqlite_connection,
            cache,
            objects,
            SqlArtifactRepository(engine),
            repository,
        )

        first_only = importer.import_tickers(["ABT"])
        assert _counts(engine) == (12, 1, 4, 1, 1, 1)

        tickers = ["ABT", "NTES", "AERO", "CNI", "6752.T", "7974.T", "CATO"]
        completed = importer.import_tickers(tickers)
        complete_counts = _counts(engine)
        assert main(
            [
                "--sqlite",
                str(store.DEFAULT_DB),
                "--cache-dir",
                str(cache),
                "ABT",
                "NTES",
                "AERO",
                "CNI",
                "6752.T",
                "7974.T",
                "CATO",
            ]
        ) == 0

        expected_snapshot_ids = {
            item.ticker: item.stored.snapshot_id for item in completed
        }
        engine.dispose()
        engine = None
        admin_engine.dispose()
        subprocess.run(
            ["docker", "compose", "restart", "postgres", "seaweedfs"],
            cwd=Path(__file__).parents[3],
            check=True,
            timeout=120,
        )
        subprocess.run(
            [
                "docker", "compose", "up", "-d", "--wait",
                "postgres", "seaweedfs",
            ],
            cwd=Path(__file__).parents[3],
            check=True,
            timeout=120,
        )
        admin_engine = create_postgres_engine(base_settings).execution_options(
            isolation_level="AUTOCOMMIT"
        )
        engine = create_engine(database_url)
        settings = StorageSettings.from_env()
        objects = ImmutableObjectStore(
            create_s3_client(settings),
            settings.s3_bucket,
        )
        repository = SharedCompanyRepository(engine)
        restarted_importer = CompanyImporter(
            sqlite_connection,
            cache,
            objects,
            SqlArtifactRepository(engine),
            repository,
        )
        completed = restarted_importer.import_tickers(tickers)
        assert {
            item.ticker: item.stored.snapshot_id for item in completed
        } == expected_snapshot_ids

        assert first_only[0].stored.snapshot_id == completed[0].stored.snapshot_id
        assert _counts(engine) == complete_counts == (35, 7, 36, 7, 7, 7)
        assert [
            repository.current(item.stored.security_id).snapshot_id
            for item in completed
        ] == [item.stored.snapshot_id for item in completed]

        sqlite_payloads = {
            row["ticker"]: json.loads(row["data"])
            for row in sqlite_connection.execute(
                """SELECT c.ticker, s.data
                   FROM company c JOIN snapshot s USING (cik)
                   WHERE c.ticker IN
                       ('ABT', 'NTES', 'AERO', 'CNI', '6752.T', '7974.T', 'CATO')"""
            )
        }
        for imported in completed:
            stored = repository.current(imported.stored.security_id)
            assert _canonical_json(stored.canonical_payload) == _canonical_json(
                sqlite_payloads[imported.ticker]
            )
            for retained in imported.artifacts:
                artifact = retained.artifact
                assert objects.read_verified(
                    artifact.object_key,
                    artifact.content_sha256,
                    artifact.byte_size,
                ) == _expected_bytes(
                    sqlite_connection,
                    cache,
                    imported.ticker,
                    retained.role,
                    artifact.content_sha256,
                )

        abt_raw = next(
            retained.artifact
            for retained in completed[0].artifacts
            if retained.role == "raw_cover_filing"
        )
        assert objects.read_verified(
            abt_raw.object_key,
            abt_raw.content_sha256,
            abt_raw.byte_size,
        ) == raw_cover
        ntes_raw_primary = next(
            retained.artifact
            for retained in completed[1].artifacts
            if retained.role == "raw_cover_primary_document"
        )
        assert objects.read_verified(
            ntes_raw_primary.object_key,
            ntes_raw_primary.content_sha256,
            ntes_raw_primary.byte_size,
        ) == raw_primary

        by_ticker = {
            item.ticker: repository.current(item.stored.security_id)
            for item in completed
        }
        imported_by_ticker = {item.ticker: item for item in completed}
        print(
            json.dumps(
                {
                    "counts": complete_counts,
                    "companies": {
                        imported.ticker: {
                            "snapshot_id": imported.stored.snapshot_id,
                            "snapshot_sha256": by_ticker[
                                imported.ticker
                            ].snapshot_sha256,
                            "payload_sha256": imported.stored.payload_sha256,
                            "artifacts": [
                                [item.role, item.artifact.content_sha256]
                                for item in imported.artifacts
                            ],
                        }
                        for imported in completed
                    },
                },
                sort_keys=True,
            )
        )
        assert by_ticker["NTES"].source_accession == "0001104659-26-043468"
        assert str(by_ticker["NTES"].receipt_ratio) == "5"
        assert (
            imported_by_ticker["AERO"].issuer_source,
            imported_by_ticker["AERO"].issuer_identifier,
            imported_by_ticker["AERO"].security_identifier,
            by_ticker["AERO"].engine_revision,
            by_ticker["AERO"].exchange_code,
            by_ticker["AERO"].quote_currency,
            by_ticker["AERO"].source_accession,
            by_ticker["AERO"].security_basis,
            by_ticker["AERO"].receipt_ratio,
        ) == (
            "SEC",
            "0001561861",
            "sec:0001561861:primary-listed-security",
            187,
            "NYSE",
            "USD",
            "0001193125-26-197494",
            "PRIMARY_DEPOSITARY_RECEIPT",
            10,
        )
        assert (
            imported_by_ticker["CNI"].issuer_source,
            imported_by_ticker["CNI"].issuer_identifier,
            imported_by_ticker["CNI"].security_identifier,
            by_ticker["CNI"].engine_revision,
            by_ticker["CNI"].exchange_code,
            by_ticker["CNI"].quote_currency,
            by_ticker["CNI"].source_accession,
            by_ticker["CNI"].security_basis,
        ) == (
            "SEC",
            "0000016868",
            "sec:0000016868:primary-listed-security",
            187,
            "NYSE",
            "USD",
            "0001104659-26-010352",
            "PRIMARY_COMMON_SHARE",
        )
        assert by_ticker["CNI"].canonical_payload["reporting_currency"] == "CAD"
        assert by_ticker["CNI"].canonical_payload["sources"]["total_assets"] == {
            "tag": "us-gaap:Assets",
            "form": "6-K",
            "accn": "0000016868-26-000011",
            "end": "2025-12-31",
            "filed": "2026-02-04",
            "annual_accn": "0001104659-26-010352",
            "annual_form": "40-F",
            "annual_filed": "2026-02-04",
            "unit": "CAD",
            "document": "cni-20251231.htm",
            "canonical_tag": "Assets",
        }
        for ticker, entity, document in (
            ("6752.T", "E01772", "S100YETA"),
            ("7974.T", "E02367", "S100Y9NX"),
        ):
            assert (
                imported_by_ticker[ticker].issuer_source,
                imported_by_ticker[ticker].issuer_identifier,
                imported_by_ticker[ticker].security_identifier,
                by_ticker[ticker].engine_revision,
                by_ticker[ticker].exchange_code,
                by_ticker[ticker].quote_currency,
                by_ticker[ticker].source_accession,
                by_ticker[ticker].security_basis,
            ) == (
                "EDINET",
                entity,
                f"edinet:{entity}:primary-ordinary",
                187,
                "TSE",
                "JPY",
                document,
                "PRIMARY_ORDINARY_SHARE",
            )
        assert by_ticker["CATO"].security_title is None
        assert by_ticker["CATO"].source_accession is None
        assert by_ticker["CATO"].receipt_ratio is None
        assert by_ticker["CATO"].security_basis == "SEC_TICKER_MAPPING_ONLY"
        assert by_ticker["CATO"].canonical_payload["receipt"] == {
            "cik": "0000018255",
            "symbol": "CATO",
            "accn": "0000018255-26-000004",
            "title": "",
            "ratio": None,
        }
        cato = next(item for item in completed if item.ticker == "CATO")
        assert cato.stored.payload_sha256 == (
            "4d757a5939529317b050aa734c31e98330e101afe7a3596bf34d189e0363dd35"
        )
        assert by_ticker["CATO"].snapshot_sha256 == (
            "1091a2374e0fa6ac9e993e12bbda32cf3a50c823d22620bd41f85b9beee73646"
        )
        assert [
            (item.role, item.artifact.content_sha256) for item in cato.artifacts
        ] == [
            (
                "official_api_facts",
                "27fc6f288357a9cbe9f901c2ac6128d8da5dd214e606bb6d8b99051520ecf758",
            ),
            (
                "dimensioned_facts",
                "ab5d13787d491937b3837179d2c578b28fddde9ea91c07df60c8f6418e0b4dc4",
            ),
            (
                "official_sec_ticker_mapping",
                "91ed71e82bdb9d5306ed644b45311a040427678ca5944a81a1afc1c789145cad",
            ),
        ]
        assert [item.role for item in completed[-1].artifacts] == [
            "official_api_facts",
            "dimensioned_facts",
            "official_sec_ticker_mapping",
        ]
        assert {item.role for item in imported_by_ticker["AERO"].artifacts} == {
            "official_api_facts",
            "sec_inline_current_manifest",
            "sec_inline_direct_annual_filing_summary",
            "sec_inline_direct_annual_index",
            "sec_inline_direct_annual_instance",
            "sec_inline_direct_annual_presentation",
            "sec_inline_direct_annual_primary_document",
            "sec_inline_direct_annual_schema",
            "structured_cover_identity",
        }
        assert {item.role for item in imported_by_ticker["CNI"].artifacts} == {
            "official_api_facts",
            "dimensioned_facts",
            "sec_inline_current_manifest",
            "sec_inline_annual_wrapper_index",
            "sec_inline_annual_wrapper_primary_document",
            "sec_inline_incorporated_source_filing_summary",
            "sec_inline_incorporated_source_index",
            "sec_inline_incorporated_source_instance",
            "sec_inline_incorporated_source_presentation",
            "sec_inline_incorporated_source_primary_document",
            "sec_inline_incorporated_source_schema",
            "structured_cover_identity",
        }
        for ticker in ("6752.T", "7974.T"):
            assert [item.role for item in imported_by_ticker[ticker].artifacts] == [
                "canonical_edinet_facts",
                "raw_filing",
            ]
        aero_artifacts = {
            item.role: item.artifact.content_sha256
            for item in imported_by_ticker["AERO"].artifacts
        }
        assert (
            aero_artifacts["sec_inline_direct_annual_presentation"]
            == aero_artifacts["sec_inline_direct_annual_schema"]
            == "30f70090d1860858457af1d381235756fc3109bdb8a87f1c50274e2810217479"
        )
        assert len(repository.artifacts(by_ticker["AERO"].snapshot_id)) == 9
        assert {
            ticker: (
                by_ticker[ticker].snapshot_sha256,
                next(
                    item for item in completed if item.ticker == ticker
                ).stored.payload_sha256,
            )
            for ticker in ("ABT", "NTES", "AERO", "CNI", "6752.T", "7974.T")
        } == {
            "ABT": (
                "8e7d61bd1441d916f1d2d11e5b1242955195b56014ace29ec504d07f73fd440e",
                "3f65756336803204f7c55725494b42437ec0af2e104d4a674796ea381d50170f",
            ),
            "NTES": (
                "9dfbda5041ce740c5a824e625a4e7fdb1ffc73fa1453e612c567b3bf1b64d953",
                "5d6501bf32b5a30a6c08b16f879425351f1a7c4e8cade72650ef0cd9d7b63496",
            ),
            "AERO": (
                "d9997d75d0cfdcedbc74536d118405ca20c2a84a180419111bad3de9f6a174d9",
                "db7c02a7c1664ae47f39d4d15368a54eb25df76544486c862aa28a0dbcf0abad",
            ),
            "CNI": (
                "6de480b8c1e02bbe705907644b441aff8ee24fa935ffae965154e76f775bc1fa",
                "f2ef3efb6f7622df3a67ca1b349d9179174d8ab277af04c2d16c7cd80fd1ded6",
            ),
            "6752.T": (
                "c0200925aecc5f50f49801535f4b985941b74d01675c792d505f3b613e7c5f86",
                "5593ade3e5bad5f63ee630a5dc02c80c81ee09b1534fef269e56ff75cfb4330d",
            ),
            "7974.T": (
                "032408cdb7db6dc6ebd236bea5aa6c3ffb493672ce2ffdd6691315526230de0e",
                "616dc4fd05158c59570bcca7273390c0197c7e55a123041a8339b8843ec29a04",
            ),
        }
    finally:
        if sqlite_connection is not None:
            sqlite_connection.close()
        if engine is not None:
            engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin_engine.dispose()


def _counts(engine):
    with engine.connect() as connection:
        return tuple(
            connection.scalar(select(func.count()).select_from(table))
            for table in (
                evidence_artifact,
                company_snapshot,
                company_snapshot_artifact,
                current_company_snapshot,
                issuer,
                priced_security,
            )
        )


def _copy_company_cache(source, destination):
    destination.mkdir()
    ciks = (
        "0000001800",
        "0001110646",
        "0001561861",
        "0000016868",
        "E01772",
        "E02367",
        "0000018255",
    )
    for cik in ciks:
        facts_path = source / f"companyfacts_{cik}.json"
        shutil.copy2(facts_path, destination / facts_path.name)
        sidecar = dera.sidecar_path(source, cik)
        if sidecar.exists():
            shutil.copy2(sidecar, dera.sidecar_path(destination, cik))
        if cik.isdigit():
            facts = json.loads(facts_path.read_bytes())
            for artifact in inline_xbrl.current_retained_artifacts(
                source, cik, facts,
            ):
                target = destination / artifact.path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(artifact.path, target)
    shutil.copy2(source / "company_tickers.json", destination / "company_tickers.json")
    (destination / "edinet").mkdir()
    for cik in ("E01772", "E02367"):
        edinet = json.loads((destination / f"companyfacts_{cik}.json").read_bytes())
        for report in edinet["_adapter"]["reports"]:
            document = report["document"]
            shutil.copy2(
                source / "edinet" / f"{document}.zip",
                destination / "edinet" / f"{document}.zip",
            )


def _write_cover_fixture(connection, cache):
    receipt = EvidenceLoader(
        connection,
        SimpleNamespace(cache_dir=cache),
    ).identity("0000001800", "ABT")[1]
    assert receipt is not None
    raw = (
        "<table><tr><td>Title of 12(b) Security</td>"
        f"<td>{receipt['title']}</td></tr><tr><td>Trading Symbol</td>"
        f"<td>{receipt['symbol']}</td></tr></table>"
    ).encode()
    path = cover.report_cache_path(cache, receipt["accn"], 2)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    return raw


def _write_primary_fixture(connection, cache):
    receipt = EvidenceLoader(
        connection,
        SimpleNamespace(cache_dir=cache),
    ).identity("0001110646", "NTES")[1]
    assert receipt is not None
    raw = b"Each American Depositary Share represents five ordinary shares."
    path = cover.primary_document_cache_path(
        cache, receipt["accn"], "ntes-primary.htm"
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    return raw


def _expected_bytes(connection, cache, ticker, role, digest):
    row = connection.execute(
        "SELECT cik, ticker FROM company WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    cik = row["cik"]
    candidates = []
    if role == "official_api_facts":
        candidates = [(cache / f"companyfacts_{cik}.json").read_bytes()]
    elif role == "dimensioned_facts":
        candidates = [dera.sidecar_path(cache, cik).read_bytes()]
    elif role == "structured_cover_identity":
        receipt = EvidenceLoader(
            connection,
            type("RetainedCache", (), {"cache_dir": cache})(),
        ).identity(cik, ticker)[1]
        candidates = [_canonical_json(receipt)]
    elif role == "raw_cover_filing":
        receipt = EvidenceLoader(
            connection,
            type("RetainedCache", (), {"cache_dir": cache})(),
        ).identity(cik, ticker)[1]
        candidates = [
            path.read_bytes()
            for report in cover.COVER_REPORTS
            if (path := cover.report_cache_path(cache, receipt["accn"], report)).exists()
            and any(
                security["symbol"] == receipt["symbol"]
                and security["title"] == receipt["title"]
                for security in cover.securities(
                    path.read_bytes().decode("utf-8", errors="replace")
                )
            )
        ]
    elif role == "raw_cover_primary_document":
        receipt = EvidenceLoader(
            connection,
            type("RetainedCache", (), {"cache_dir": cache})(),
        ).identity(cik, ticker)[1]
        candidates = [
            path.read_bytes()
            for path in cover.primary_document_cache_dir(
                cache, receipt["accn"]
            ).glob("*")
            if path.is_file()
        ]
    elif role == "official_sec_ticker_mapping":
        candidates = [(cache / "company_tickers.json").read_bytes()]
    elif role.startswith("sec_inline_"):
        facts = json.loads((cache / f"companyfacts_{cik}.json").read_bytes())
        candidates = [
            artifact.payload
            for artifact in inline_xbrl.current_retained_artifacts(
                cache, cik, facts,
            )
            if artifact.role == role
        ]
    elif role == "canonical_edinet_facts":
        candidates = [(cache / f"companyfacts_{cik}.json").read_bytes()]
    elif role == "raw_filing":
        facts = json.loads((cache / f"companyfacts_{cik}.json").read_bytes())
        candidates = [
            (cache / "edinet" / f"{report['document']}.zip").read_bytes()
            for report in facts["_adapter"]["reports"]
        ]
    return next(data for data in candidates if _sha256(data) == digest)


def _sha256(data):
    import hashlib

    return hashlib.sha256(data).hexdigest()
