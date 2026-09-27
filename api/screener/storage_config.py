from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class StorageSettings:
    database_url: str
    s3_endpoint_url: str | None
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_bucket: str
    s3_region: str

    @classmethod
    def from_env(cls) -> "StorageSettings":
        settings = cls(
            database_url=os.getenv(
                "SCREENER_DATABASE_URL",
                "postgresql+psycopg://market_screener:market_screener_local"
                "@127.0.0.1:55432/market_screener",
            ),
            s3_endpoint_url=os.getenv(
                "SCREENER_S3_ENDPOINT_URL", "http://127.0.0.1:8333"
            ) or None,
            s3_access_key_id=os.getenv(
                "SCREENER_S3_ACCESS_KEY_ID", "market_screener"
            ),
            s3_secret_access_key=os.getenv(
                "SCREENER_S3_SECRET_ACCESS_KEY", "market_screener_local"
            ),
            s3_bucket=os.getenv(
                "SCREENER_S3_BUCKET", "market-screener-evidence"
            ),
            s3_region=os.getenv("SCREENER_S3_REGION", "us-east-1"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("SCREENER_DATABASE_URL must use postgresql+psycopg://")
        for name, value in (
            ("SCREENER_S3_ACCESS_KEY_ID", self.s3_access_key_id),
            ("SCREENER_S3_SECRET_ACCESS_KEY", self.s3_secret_access_key),
            ("SCREENER_S3_BUCKET", self.s3_bucket),
            ("SCREENER_S3_REGION", self.s3_region),
        ):
            if not value:
                raise ValueError(f"{name} must not be empty")
