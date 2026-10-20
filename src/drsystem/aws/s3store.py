"""S3-backed ObjectStore with the same conditional-write semantics as the local store."""

from __future__ import annotations

from typing import Any

from ..storage.base import NotFound, PreconditionFailed, StoredObject, StoreUnavailable


class S3ObjectStore:
    def __init__(self, bucket: str, region: str, client: Any | None = None) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.region = region
        self.s3 = client or boto3.client(
            "s3", region_name=region, config=Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 2})
        )

    def _translate(self, exc: Exception, key: str) -> Exception:
        from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

        if isinstance(exc, ClientError):
            code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"NoSuchKey", "404", "NotFound"}:
                return NotFound(key)
            if code in {"PreconditionFailed", "ConditionalRequestConflict"} or status in {409, 412}:
                return PreconditionFailed(f"{key}: {code}")
            if status and status >= 500:
                return StoreUnavailable(f"S3 {self.region}: {code}")
        if isinstance(exc, (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError)):
            return StoreUnavailable(f"S3 {self.region}: {type(exc).__name__}")
        return exc

    def get(self, key: str) -> StoredObject:
        try:
            r = self.s3.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - translated below
            raise self._translate(exc, key) from exc
        # S3 user metadata travels as HTTP headers; store keys with hyphens, expose with underscores.
        meta = {k.replace("-", "_"): v for k, v in r.get("Metadata", {}).items()}
        if r.get("ReplicationStatus"):
            meta["replication_status"] = r["ReplicationStatus"]
        return StoredObject(key, r["Body"].read(), r["ETag"].strip('"'), r["LastModified"].timestamp(), meta)

    def put(
        self,
        key: str,
        data: bytes,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
        metadata: dict[str, str] | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": data,
            "Metadata": {k.replace("_", "-"): v for k, v in {**(metadata or {}), "written_region": self.region}.items()},
        }
        if if_match is not None:
            kwargs["IfMatch"] = f'"{if_match}"'
        if if_none_match:
            kwargs["IfNoneMatch"] = "*"
        try:
            r = self.s3.put_object(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, key) from exc
        return r["ETag"].strip('"')

    def list(self, prefix: str = "") -> list[str]:
        keys: list[str] = []
        try:
            for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
                keys.extend(o["Key"] for o in page.get("Contents", []))
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, prefix) from exc
        return sorted(keys)

    def delete(self, key: str) -> None:
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, key) from exc
