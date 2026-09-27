from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from .storage_config import StorageSettings


class ObjectVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedObject:
    content_sha256: str
    object_key: str
    byte_size: int
    verified_at: datetime


def content_address(data: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(data).hexdigest()
    return digest, f"raw/sha256/{digest}"


def create_s3_client(settings: StorageSettings | None = None) -> Any:
    current = settings or StorageSettings.from_env()
    return boto3.client(
        "s3",
        endpoint_url=current.s3_endpoint_url,
        aws_access_key_id=current.s3_access_key_id,
        aws_secret_access_key=current.s3_secret_access_key,
        region_name=current.s3_region,
        config=Config(s3={"addressing_style": "path"}),
    )


class ImmutableObjectStore:
    def __init__(self, client: Any, bucket: str):
        self.client = client
        self.bucket = bucket

    def put_verified(self, data: bytes, media_type: str) -> VerifiedObject:
        digest, key = content_address(data)
        try:
            self._read_verified(key, digest, len(data))
        except ClientError as exc:
            if not _is_missing(exc):
                raise
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=data,
                    ContentType=media_type,
                    Metadata={"sha256": digest},
                    IfNoneMatch="*",
                )
            except ClientError as upload_error:
                if not _is_precondition_failed(upload_error):
                    raise

        self._read_verified(key, digest, len(data))
        return VerifiedObject(digest, key, len(data), datetime.now(timezone.utc))

    def read_verified(
        self, key: str, expected_sha256: str, expected_size: int
    ) -> bytes:
        return self._read_verified(key, expected_sha256, expected_size)

    def _read_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"]
        try:
            data = body.read()
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        if len(data) != expected_size:
            raise ObjectVerificationError(
                f"Object {key} has {len(data)} bytes; expected {expected_size}"
            )
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ObjectVerificationError(
                f"Object {key} hash does not match its content address"
            )
        return data


def _is_missing(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}


def _is_precondition_failed(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    return code in {"412", "PreconditionFailed"}
