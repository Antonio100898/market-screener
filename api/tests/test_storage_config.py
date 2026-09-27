import pytest

from screener.storage_config import StorageSettings


def test_storage_settings_use_local_defaults(monkeypatch):
    for name in (
        "SCREENER_DATABASE_URL",
        "SCREENER_S3_ENDPOINT_URL",
        "SCREENER_S3_ACCESS_KEY_ID",
        "SCREENER_S3_SECRET_ACCESS_KEY",
        "SCREENER_S3_BUCKET",
        "SCREENER_S3_REGION",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = StorageSettings.from_env()

    assert settings.database_url == (
        "postgresql+psycopg://market_screener:market_screener_local"
        "@127.0.0.1:55432/market_screener"
    )
    assert settings.s3_endpoint_url == "http://127.0.0.1:8333"
    assert settings.s3_bucket == "market-screener-evidence"


def test_storage_settings_reject_a_second_database_owner(monkeypatch):
    monkeypatch.setenv("SCREENER_DATABASE_URL", "postgresql://localhost/wrong-driver")

    with pytest.raises(ValueError, match=r"postgresql\+psycopg"):
        StorageSettings.from_env()


def test_storage_settings_read_environment(monkeypatch):
    monkeypatch.setenv(
        "SCREENER_DATABASE_URL", "postgresql+psycopg://user:pass@db/app"
    )
    monkeypatch.setenv("SCREENER_S3_ENDPOINT_URL", "https://objects.example")
    monkeypatch.setenv("SCREENER_S3_ACCESS_KEY_ID", "access")
    monkeypatch.setenv("SCREENER_S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("SCREENER_S3_BUCKET", "evidence")
    monkeypatch.setenv("SCREENER_S3_REGION", "eu-west-1")

    settings = StorageSettings.from_env()

    assert settings.database_url == "postgresql+psycopg://user:pass@db/app"
    assert settings.s3_endpoint_url == "https://objects.example"
    assert settings.s3_access_key_id == "access"
    assert settings.s3_secret_access_key == "secret"
    assert settings.s3_bucket == "evidence"
    assert settings.s3_region == "eu-west-1"
