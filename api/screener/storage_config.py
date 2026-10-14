from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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


@dataclass(frozen=True)
class AutomaticIngestionSettings:
    enabled: bool = False
    plan_path: Path | None = None
    cache_dir: Path | None = None

    @classmethod
    def from_env(cls) -> "AutomaticIngestionSettings":
        enabled = os.getenv("SCREENER_AUTOMATIC_INGESTION", "0")
        if enabled not in {"0", "1"}:
            raise ValueError("SCREENER_AUTOMATIC_INGESTION must be 0 or 1")
        if enabled == "0":
            return cls()
        plan = os.getenv("SCREENER_INGESTION_PLAN")
        cache = os.getenv("SCREENER_INGESTION_CACHE")
        if not plan or not cache:
            raise ValueError("automatic ingestion requires an explicit plan and dedicated cache")
        settings = cls(True, Path(plan).resolve(), Path(cache).resolve())
        settings.validate()
        return settings

    def validate(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("automatic ingestion enabled must be boolean")
        if not self.enabled:
            return
        if not isinstance(self.plan_path, Path) or not isinstance(self.cache_dir, Path):
            raise ValueError("automatic ingestion requires an explicit plan and dedicated cache")
        original = (Path.home() / ".cache" / "graham-screener").resolve()
        cache = self.cache_dir.resolve()
        if cache == original or original in cache.parents:
            raise ValueError("automatic ingestion cache must be separate from retained legacy evidence")
