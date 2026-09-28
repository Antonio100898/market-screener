import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from screener import api, store
from screener.artifacts import SqlArtifactRepository
from screener.company_import import CompanyImporter, _read_only_sqlite
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import create_postgres_engine
from screener.shared_companies import SharedCompanyRepository
from screener.storage_config import StorageSettings


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_STORAGE_INTEGRATION") != "1",
    reason="set RUN_STORAGE_INTEGRATION=1 with local PostgreSQL and S3 running",
)


_EXPORT_ENRICHED_FIELDS = {
    "annual_ratios",
    "asset_quality",
    "context_notes",
    "criteria",
    "verdict",
}


def test_real_three_company_import_is_read_through_fastapi(monkeypatch):
    base_settings = StorageSettings.from_env()
    database_name = f"ms_company_api_{uuid4().hex}"
    admin_engine = create_postgres_engine(base_settings).execution_options(
        isolation_level="AUTOCOMMIT"
    )
    database_url = make_url(base_settings.database_url).set(database=database_name)
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    import_engine = None
    api_repository = None
    sqlite_connection = None
    try:
        monkeypatch.setenv(
            "SCREENER_DATABASE_URL",
            database_url.render_as_string(hide_password=False),
        )
        command.upgrade(Config("alembic.ini"), "head")
        settings = StorageSettings.from_env()
        import_engine = create_engine(database_url)
        sqlite_connection = _read_only_sqlite(store.DEFAULT_DB)
        importer = CompanyImporter(
            sqlite_connection,
            store.DEFAULT_DB.parent,
            ImmutableObjectStore(
                create_s3_client(settings),
                settings.s3_bucket,
            ),
            SqlArtifactRepository(import_engine),
            SharedCompanyRepository(import_engine),
        )
        imported = importer.import_tickers(["ABT", "NTES", "6752.T"])

        sqlite_payloads = {
            row["ticker"]: json.loads(row["data"])
            for row in sqlite_connection.execute(
                """SELECT c.ticker, s.data
                   FROM company c JOIN snapshot s USING (cik)
                   WHERE c.ticker IN ('ABT', 'NTES', '6752.T')"""
            )
        }
        dashboard_rows = _dashboard_rows(sqlite_payloads)
        api._shared_company_repository.cache_clear()
        client = TestClient(api.app)
        responses = {}

        for ticker in ("ABT", "NTES", "6752.T"):
            response = client.get(f"/companies/{ticker.lower()}")
            assert response.status_code == 200, response.text
            body = response.json()
            responses[ticker] = body
            assert body["canonical_payload"] == sqlite_payloads[ticker]
            _assert_dashboard_canonical_fields(
                body["canonical_payload"],
                dashboard_rows.get(ticker),
            )

        by_ticker = {item.ticker: item for item in imported}
        for ticker, body in responses.items():
            assert body["revision"]["snapshot_id"] == (
                by_ticker[ticker].stored.snapshot_id
            )
            assert body["revision"]["payload_sha256"] == (
                by_ticker[ticker].stored.payload_sha256
            )

        ntes = responses["NTES"]
        assert ntes["security"]["source_accession"] == (
            "0001104659-26-043468"
        )
        assert ntes["security"]["receipt_ratio"] == "5"
        assert {
            item["role"]: item["content_sha256"] for item in ntes["artifacts"]
        } == {
            "official_api_facts": (
                "03ed93bf459aaa2aa101c99a9c39e89213e80f6d13a908c8fc72f7c4ac6b755c"
            ),
            "dimensioned_facts": (
                "af730296ec376df2b356ab8b13a0f5c444db54b690789991cc9dda3219be1541"
            ),
            "structured_cover_identity": (
                "6fe18bb3506dacb7907095d00967143d913ddcd39645b78b92407edaf0565152"
            ),
        }

        panasonic = responses["6752.T"]
        assert panasonic["security"]["quote_currency"] == "JPY"
        assert panasonic["security"]["security_basis"] == (
            "PRIMARY_ORDINARY_SHARE"
        )
        assert panasonic["security"]["receipt_ratio"] is None
        assert {
            item["role"]: item["content_sha256"]
            for item in panasonic["artifacts"]
        }["raw_filing"] == (
            "95cc16e5afa261db468633743e8e46cf0be0b827b6fee5c4d625d87327cbc35f"
        )

        api_repository = api._shared_company_repository()
        print(
            json.dumps(
                {
                    ticker: {
                        "status": 200,
                        "revision": body["revision"],
                        "issuer": body["issuer"],
                        "security": body["security"],
                        "artifacts": body["artifacts"],
                        "sqlite_payload_equal": True,
                        "dashboard_canonical_fields_equal": (
                            ticker in dashboard_rows
                        ),
                    }
                    for ticker, body in responses.items()
                },
                sort_keys=True,
            )
        )
    finally:
        if sqlite_connection is not None:
            sqlite_connection.close()
        if api_repository is not None:
            api_repository.engine.dispose()
        api._shared_company_repository.cache_clear()
        if import_engine is not None:
            import_engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin_engine.dispose()


def _dashboard_rows(sqlite_payloads):
    path = Path(api.__file__).parent / "static" / "dashboard.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return {
            row["ticker"]: row
            for row in json.load(handle)["rows"]
            if row.get("ticker") in sqlite_payloads
        }


def _assert_dashboard_canonical_fields(canonical_payload, dashboard_row):
    if dashboard_row is None:
        return
    fields = (
        canonical_payload.keys()
        & dashboard_row.keys()
        - _EXPORT_ENRICHED_FIELDS
    )
    assert fields
    assert {field: canonical_payload[field] for field in fields} == {
        field: dashboard_row[field] for field in fields
    }
