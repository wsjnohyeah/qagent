from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from minio import Minio
from pydantic import BaseModel, ConfigDict

from agentic_quant.config import ObjectStoreBackend, Settings
from agentic_quant.ids import uuid7


class RawArchiveResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw_object_id: str
    provider: str
    data_type: str
    content_sha256: str
    uri: str
    payload_bytes: int
    provider_received_at: datetime
    ingested_at: datetime
    request_metadata: dict[str, Any]


class RawArchive(Protocol):
    def store_json(
        self,
        *,
        provider: str,
        data_type: str,
        payload: dict[str, Any],
        request_metadata: dict[str, Any],
        provider_received_at: datetime,
    ) -> RawArchiveResult: ...

    def read_json(self, uri: str, *, max_bytes: int = 1_000_000) -> dict[str, Any]: ...

    def health(self) -> bool: ...


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _object_key(provider: str, data_type: str, received_at: datetime, digest: str) -> str:
    return (
        f"raw/provider={provider}/type={data_type}/date={received_at.date().isoformat()}/"
        f"{digest}.json"
    )


class FileRawArchive:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def store_json(
        self,
        *,
        provider: str,
        data_type: str,
        payload: dict[str, Any],
        request_metadata: dict[str, Any],
        provider_received_at: datetime,
    ) -> RawArchiveResult:
        content = _canonical_json(payload)
        digest = hashlib.sha256(content).hexdigest()
        key = _object_key(provider, data_type, provider_received_at, digest)
        destination = self.root / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_suffix(".tmp")
            temporary.write_bytes(content)
            temporary.replace(destination)
        return RawArchiveResult(
            raw_object_id=uuid7(),
            provider=provider,
            data_type=data_type,
            content_sha256=digest,
            uri=destination.resolve().as_uri(),
            payload_bytes=len(content),
            provider_received_at=provider_received_at,
            ingested_at=provider_received_at,
            request_metadata=request_metadata,
        )

    def health(self) -> bool:
        return self.root.exists() and self.root.is_dir()

    def read_json(self, uri: str, *, max_bytes: int = 1_000_000) -> dict[str, Any]:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise ValueError("Local raw archive only accepts file URIs")
        candidate = Path(parsed.path).resolve()
        root = self.root.resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("Raw object is outside the configured archive root")
        if candidate.stat().st_size > max_bytes:
            raise ValueError("Raw object exceeds the preview size limit")
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Raw object is not a JSON object")
        return payload


class MinioRawArchive:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Object-store endpoint must be an http(s) URL")
        self.bucket = bucket
        self.client = Minio(
            parsed.netloc,
            access_key=access_key,
            secret_key=secret_key,
            secure=parsed.scheme == "https",
        )

    def initialize(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def store_json(
        self,
        *,
        provider: str,
        data_type: str,
        payload: dict[str, Any],
        request_metadata: dict[str, Any],
        provider_received_at: datetime,
    ) -> RawArchiveResult:
        content = _canonical_json(payload)
        digest = hashlib.sha256(content).hexdigest()
        key = _object_key(provider, data_type, provider_received_at, digest)
        self.client.put_object(
            self.bucket,
            key,
            io.BytesIO(content),
            len(content),
            content_type="application/json",
        )
        return RawArchiveResult(
            raw_object_id=uuid7(),
            provider=provider,
            data_type=data_type,
            content_sha256=digest,
            uri=f"s3://{self.bucket}/{key}",
            payload_bytes=len(content),
            provider_received_at=provider_received_at,
            ingested_at=provider_received_at,
            request_metadata=request_metadata,
        )

    def health(self) -> bool:
        return bool(self.client.bucket_exists(self.bucket))

    def read_json(self, uri: str, *, max_bytes: int = 1_000_000) -> dict[str, Any]:
        parsed = urlparse(uri)
        if parsed.scheme != "s3" or parsed.netloc != self.bucket:
            raise ValueError("Raw object URI does not match the configured bucket")
        key = parsed.path.lstrip("/")
        response = self.client.get_object(self.bucket, key)
        try:
            content = response.read(max_bytes + 1)
        finally:
            response.close()
            response.release_conn()
        if len(content) > max_bytes:
            raise ValueError("Raw object exceeds the preview size limit")
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("Raw object is not a JSON object")
        return payload


def build_raw_archive(settings: Settings) -> RawArchive:
    if settings.object_store_backend == ObjectStoreBackend.LOCAL:
        return FileRawArchive(settings.object_store_root)
    if not (
        settings.object_store_endpoint
        and settings.object_store_access_key
        and settings.object_store_secret_key
    ):
        raise ValueError("S3 object-store configuration is incomplete")
    archive = MinioRawArchive(
        endpoint=settings.object_store_endpoint,
        bucket=settings.object_store_bucket,
        access_key=settings.object_store_access_key.get_secret_value(),
        secret_key=settings.object_store_secret_key.get_secret_value(),
    )
    archive.initialize()
    return archive
