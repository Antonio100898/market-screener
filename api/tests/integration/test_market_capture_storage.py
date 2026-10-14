"""Genuine market bytes in disposable PostgreSQL and local immutable object storage.

No company/release publication, default database migration or live Yahoo calls.
"""
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from screener.artifacts import SqlArtifactRepository
from screener.market_captures import MarketReplay, retain_recipe, retain_response
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import evidence_artifact
from screener.sources.prices import YahooPriceProvider
from screener.storage_config import StorageSettings


pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1",
                              reason="requires local PostgreSQL and S3")


@pytest.mark.parametrize("stamp,tickers,counters", [
    ("20261004T081958687901Z", (("ABT", "USD"), ("NTES", "USD"), ("TRP", "USD"), ("MFG", "USD"), ("6752.T", "JPY")), ("JPY", "CAD")),
    ("20261004T084347905669Z", (("3028.T", "JPY"), ("6920.T", "JPY")), ()),
])
def test_genuine_capture_sql_artifacts_restart_and_offline_replay(monkeypatch, stamp, tickers, counters):
    root = Path(__file__).parents[3]
    manifest_path = root / f"tmp/verification-runtime/market-captures/manifest-{stamp}.json"
    if not manifest_path.exists():
        pytest.skip("genuine local market capture evidence is absent")
    settings = StorageSettings.from_env()
    name = f"ms_market_{uuid4().hex}"
    admin = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    url = make_url(settings.database_url).set(database=name)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url)
    objects = ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket)
    try:
        monkeypatch.setenv("SCREENER_DATABASE_URL", url.render_as_string(hide_password=False))
        command.upgrade(Config(str(root / "api/alembic.ini")), "head")
        repository = SqlArtifactRepository(engine)
        prior = (json.loads((root / "local/market-replay-2026-10-04.json").read_bytes())
                 if stamp == "20261004T081958687901Z" else None)
        manifest_bytes = manifest_path.read_bytes()
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if prior:
            assert manifest_sha256 == prior["manifest_sha256"]
        records = []
        raw_quotes = {}
        for item in json.loads(manifest_bytes)["captures"]:
            data = (manifest_path.parent / item["body_file"]).read_bytes()
            assert len(data) == item["byte_size"] and hashlib.sha256(data).hexdigest() == item["sha256"]
            if item["range"] == "1d":
                raw_quotes[item["symbol"]] = (json.loads(data)["chart"]["result"][0], datetime.fromisoformat(item["captured_at"]))
            records.append(retain_response(requested_url=item["requested_url"], response_url=item["response_url"],
                started_at=datetime.fromisoformat(item["started_at"]), captured_at=datetime.fromisoformat(item["captured_at"]),
                status_code=item["status_code"], media_type=item["media_type"], etag=item["etag"], last_modified=item["last_modified"],
                body=data, objects=objects, repository=repository))
        artifact = retain_recipe(records, objects, repository)
        assert retain_recipe(records, objects, repository).content_sha256 == artifact.content_sha256
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(evidence_artifact)) == len(records) + 1
        engine.dispose()
        # A new repository/provider process boundary reads the stored recipe, not supplied prices.
        engine = create_engine(url)
        repository = SqlArtifactRepository(engine)
        outputs = []
        for _ in range(2):
            transport = MarketReplay(artifact.content_sha256, objects, repository)
            provider = YahooPriceProvider(http=transport, clock=transport.now)
            values = {ticker: provider.history(ticker, expected_currency=currency) for ticker, currency in tickers}
            values.update({f"USD/{counter}": provider.exchange_rate_history("USD", counter) for counter in counters})
            for ticker, currency in tickers:
                raw_quote, captured_at = raw_quotes[ticker]
                assert raw_quote["meta"]["currency"] == currency
                assert values[ticker].quote == YahooPriceProvider._quote_from(raw_quote, now=captured_at, expected_currency=currency)
                assert len(values[ticker].closes) == 262
            output = json.loads(json.dumps({key: asdict(value) for key, value in values.items()}, default=str))
            if prior:
                assert output == prior["values"]
            assert len(transport.requests) == len(records)
            outputs.append(output)
        assert outputs[0] == outputs[1]
        report = {"scope": "Genuine retained bodies through SQL artifacts/S3/Yahoo owner; prerequisite only, no financial assembly or baseline-provider parity claim.",
            "manifest_sha256": manifest_sha256, "recipe_sha256": artifact.content_sha256,
            "artifact_count": len(records) + 1, "branches": len(outputs[0]), "requests_per_replay": len(records), "two_replays_equal": True,
            "prior_owner_outputs_equal": True if prior else None, "raw_quote_owner_equal": True,
            "database_scope": "disposable migrated PostgreSQL",
            "closes": {key: len(value["closes"]) for key, value in outputs[0].items()}}
        filename = "market-capture-storage-proof-2026-10-04.json" if prior else "market-capture-jpy-storage-proof-2026-10-04.json"
        (root / "local" / filename).write_text(json.dumps(report, indent=2) + "\n")
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :name'), {"name": name})
            connection.execute(text(f'DROP DATABASE "{name}"'))
        admin.dispose()
