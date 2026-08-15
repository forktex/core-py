# Copyright (C) 2026 FORKTEX S.R.L.
#
# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-ForkTex-Commercial
#
# This file is part of forktex-core.
#
# For commercial licensing -- including use in proprietary products, SaaS
# deployments, or any context where AGPL obligations cannot be met -- you
# MUST obtain a commercial license from FORKTEX S.R.L. (info@forktex.com).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""S3-compatible object storage connector (MinIO-first).

Thin async connector — no path conventions, no content negotiation, no
image processing. Those are interface-adapter concerns in the consuming
service (per FSD rules).

Multi-bucket services use ``register`` + ``get_client``; single-bucket
services use the module-level convenience functions (``init`` / ``upload`` /
``presign`` / etc.) which operate on the ``"default"`` client.

## Single-bucket (default client)

    import forktex_core.storage as storage

    await storage.init(url="http://minio:9000", bucket="documents",
                       access_key="key", secret_key="secret")

    await storage.upload("invoices/abc.pdf", pdf_bytes, content_type="application/pdf")
    url = await storage.presign("invoices/abc.pdf", expires_in=3600)
    data = await storage.download("invoices/abc.pdf")
    await storage.close()

## Multi-bucket (named clients)

    from forktex_core.storage import register, get_client

    register("media",      url=..., bucket="news-media", ...)
    register("messaging",  url=..., bucket="messaging", ...)
    register("data-lake",  url=..., bucket="data-lake", ...)

    media_url = await get_client("media").presign("images/hero.jpg")
    await get_client("messaging").upload("conv-1/file.pdf", data)

## Secured actor callback (presigned PUT)

    # Backend generates a short-lived callback URL — the S3 signature IS the token.
    # Actor presents the URL directly to MinIO; no separate auth needed.
    put_url = await client.presign(
        "uploads/org-abc/photo.jpg",
        expires_in=900,
        method="put_object",
        content_type="image/jpeg",   # MinIO enforces this during the PUT
    )
    # Actor: PUT {put_url} with Content-Type: image/jpeg

## Browser file upload via presigned POST

    # NOTE: "multipart/form-data" here is the HTTP encoding of the browser's
    # POST body (an ordinary <form> upload) — this is unrelated to S3's own
    # chunked "Multipart Upload" API (create_multipart_upload/upload_part/
    # complete_multipart_upload), which this module does not implement.
    post_data = await client.presign_post(
        "uploads/org-abc/doc.pdf",
        content_type="application/pdf",
        max_size_bytes=10 * 1024 * 1024,
        expires_in=600,
    )
    # post_data = {"url": "...", "fields": {...}}
    # Browser POSTs multipart/form-data using post_data["url"] + post_data["fields"]

## Public-bucket direct URLs

    # When the bucket is configured as public-read, skip presigning entirely.
    client = get_client("media")
    direct_url = client.direct_url("images/hero.jpg")
    # → "http://cdn.example.com/news-media/images/hero.jpg"

Requires: pip install forktex-core[storage]  (aioboto3)
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from pydantic import Field

from forktex_core.error import AppError, AppErrorCode
from forktex_core.log import get_logger
from forktex_core.types import BaseValueObject

if TYPE_CHECKING:
    from types import ModuleType

logger = get_logger(__name__)


class StorageError(AppError):
    """Base class for storage errors.

    An ``AppError`` so an HTTP transport renders it with a real status
    (404 for a missing object) rather than a masked 500 — see
    ``forktex_core.error``.
    """

    code = AppErrorCode.INTERNAL


class ObjectNotFoundError(StorageError):
    """Raised when a requested key does not exist."""

    code = AppErrorCode.NOT_FOUND


class ClientNotRegisteredError(StorageError):
    """Raised when ``get_client(name)`` is called for an unregistered name.

    ``INTERNAL`` — a missing ``register()`` call is a deployment/wiring
    mistake, not something the caller of an HTTP endpoint can fix.
    """

    code = AppErrorCode.INTERNAL


class StorageConfig(BaseValueObject):
    url: str
    """Internal S3 endpoint (used for uploads/downloads/deletes)."""
    bucket: str
    access_key: str = Field(repr=False)
    """Excluded from repr()/str() so it never leaks into logs/tracebacks."""
    secret_key: str = Field(repr=False)
    """Excluded from repr()/str() so it never leaks into logs/tracebacks."""
    region: str = "us-east-1"
    public_url: str | None = None
    """Public-facing endpoint for presigned URL generation. Defaults to ``url``.
    Set when the internal Docker endpoint differs from the browser-reachable URL
    (e.g. internal=``http://minio:9000``, public=``http://localhost:9100``)."""


def _get_aioboto3() -> ModuleType:
    try:
        import aioboto3

        return aioboto3
    except ImportError as exc:
        raise ImportError("Install 'forktex-core[storage]' (aioboto3) to use forktex_core.storage") from exc


class StorageClient:
    """Async S3/MinIO client scoped to a single bucket.

    Obtained via ``register(name, ...)`` + ``get_client(name)``, or constructed
    directly with a ``StorageConfig``. All methods create short-lived boto3
    clients per call — stateless, safe for concurrent async use.
    """

    def __init__(self, config: StorageConfig) -> None:
        self._config = config
        self._aioboto3 = _get_aioboto3()
        self._session = self._aioboto3.Session(
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region,
        )

    def _client(self, *, public: bool = False) -> AbstractAsyncContextManager[Any]:
        """Return an async context-manager S3 client.

        ``public=True`` uses ``config.public_url`` for presigned URL generation
        so the URL hostname is browser-reachable even when running in Docker.
        """
        public_endpoint = self._config.public_url or self._config.url
        endpoint = public_endpoint if public else self._config.url
        # aioboto3's stubs return a private ``_`` type without exposing the
        # async-context-manager protocol — at runtime it implements both
        # ``__aenter__`` and ``__aexit__``. Cast so call sites type-check.
        return cast(
            AbstractAsyncContextManager[Any],
            self._session.client("s3", endpoint_url=endpoint),
        )

    def direct_url(self, key: str) -> str:
        """Build a direct (non-presigned) URL for a public-read bucket object.

        Only use this when the bucket has a public-read policy — no auth check
        is performed here.
        """
        base = (self._config.public_url or self._config.url).rstrip("/")
        return f"{base}/{self._config.bucket}/{quote(key)}"

    async def ensure_bucket(self, *, public_read: bool = False) -> None:
        """Create the bucket if absent and optionally apply a public-read policy."""
        import json

        from botocore.exceptions import ClientError

        async with self._client() as s3:
            try:
                await s3.head_bucket(Bucket=self._config.bucket)
            except ClientError as exc:
                # Only a genuine "doesn't exist" (404/NoSuchBucket) should
                # trigger create_bucket — anything else (403 permission
                # denied, a transient network error) must surface as-is,
                # not get silently reinterpreted as "let's create it".
                code = exc.response["Error"]["Code"]
                if code not in ("404", "NoSuchBucket"):
                    logger.error(
                        "storage: bucket check failed",
                        extra={"bucket": self._config.bucket, "aws_code": code},
                    )
                    raise
                logger.info("storage: creating bucket", extra={"bucket": self._config.bucket})
                await s3.create_bucket(Bucket=self._config.bucket)

            if public_read:
                policy = json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"AWS": ["*"]},
                                "Action": ["s3:GetObject"],
                                "Resource": [f"arn:aws:s3:::{self._config.bucket}/*"],
                            }
                        ],
                    }
                )
                logger.warning(
                    "storage: applying public-read bucket policy",
                    extra={"bucket": self._config.bucket},
                )
                await s3.put_bucket_policy(Bucket=self._config.bucket, Policy=policy)

    async def upload(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload bytes to ``key``. Overwrites if the key already exists."""
        logger.debug(
            "storage: uploading object",
            extra={"bucket": self._config.bucket, "key": key, "bytes": len(data), "content_type": content_type},
        )
        async with self._client() as s3:
            await s3.put_object(
                Bucket=self._config.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )

    async def download(self, key: str) -> bytes:
        """Download and return raw bytes. Raises ``ObjectNotFoundError`` if absent."""
        from botocore.exceptions import ClientError

        try:
            async with self._client() as s3:
                response = await s3.get_object(Bucket=self._config.bucket, Key=key)
                async with response["Body"] as stream:
                    return await stream.read()
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("NoSuchKey", "404"):
                logger.info("storage: object not found", extra={"bucket": self._config.bucket, "key": key})
                raise ObjectNotFoundError(key) from exc
            # The driver message can quote request ids, ARNs and headers, so it
            # goes to the log and reaches the caller only via `__cause__`.
            logger.error(
                "storage: download failed",
                extra={"bucket": self._config.bucket, "key": key, "aws_code": code},
            )
            raise StorageError("storage download failed") from exc
        except Exception as exc:
            logger.exception("storage: download failed", extra={"bucket": self._config.bucket, "key": key})
            raise StorageError("storage download failed") from exc

    async def delete(self, key: str) -> None:
        """Delete ``key``. No-op if the key does not exist."""
        logger.info("storage: deleting object", extra={"bucket": self._config.bucket, "key": key})
        async with self._client() as s3:
            await s3.delete_object(Bucket=self._config.bucket, Key=key)

    async def exists(self, key: str) -> bool:
        """Return True if ``key`` exists in the bucket."""
        from botocore.exceptions import ClientError

        try:
            async with self._client() as s3:
                await s3.head_object(Bucket=self._config.bucket, Key=key)
                return True
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("404", "NoSuchKey"):
                return False
            logger.error(
                "storage: existence check failed",
                extra={"bucket": self._config.bucket, "key": key, "aws_code": code},
            )
            raise StorageError("storage existence check failed") from exc
        except Exception as exc:
            logger.exception("storage: existence check failed", extra={"bucket": self._config.bucket, "key": key})
            raise StorageError("storage existence check failed") from exc

    async def presign(
        self,
        key: str,
        expires_in: int = 3600,
        *,
        method: str = "get_object",
        content_type: str | None = None,
        response_content_disposition: str | None = None,
    ) -> str:
        """Generate a presigned URL for ``key``.

        The presigned URL IS the access token — present it directly to MinIO.
        No additional auth header needed. Signature embedded in the URL.

        Args:
            key: Object key.
            expires_in: URL validity in seconds (default 1 hour).
            method: ``"get_object"`` (download) or ``"put_object"`` (upload).
            content_type: When ``method="put_object"``, MinIO enforces this
                content-type during the actor's PUT request. Recommended for
                uploads so actors can't swap file types.
            response_content_disposition: Override the Content-Disposition header
                returned with a GET (e.g. ``'attachment; filename="report.pdf"'``
                to force download instead of inline display).
        """
        params: dict[str, Any] = {"Bucket": self._config.bucket, "Key": key}
        if content_type and method == "put_object":
            params["ContentType"] = content_type
        if response_content_disposition and method == "get_object":
            params["ResponseContentDisposition"] = response_content_disposition

        # Use public endpoint so the presigned URL is browser-reachable
        async with self._client(public=True) as s3:
            return await s3.generate_presigned_url(
                method,
                Params=params,
                ExpiresIn=expires_in,
            )

    async def presign_post(
        self,
        key: str,
        *,
        expires_in: int = 3600,
        content_type: str | None = None,
        max_size_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Generate a presigned POST policy for a browser file upload.

        Returns ``{"url": str, "fields": dict}`` — the browser POSTs a
        ``multipart/form-data`` request to ``url`` including all ``fields``
        as hidden form inputs. Compatible with any S3-presigned-POST client.

        This is a single-object upload — unrelated to S3's own chunked
        "Multipart Upload" API, which this module does not implement.

        Args:
            key: Object key to upload to.
            expires_in: Policy validity in seconds.
            content_type: Restrict uploads to this MIME type.
            max_size_bytes: Maximum allowed object size. None = unlimited.
        """
        conditions: list[Any] = []
        fields: dict[str, str] = {}

        if content_type:
            conditions.append({"Content-Type": content_type})
            fields["Content-Type"] = content_type

        if max_size_bytes is not None:
            conditions.append(["content-length-range", 1, max_size_bytes])

        async with self._client(public=True) as s3:
            return await s3.generate_presigned_post(
                Bucket=self._config.bucket,
                Key=key,
                Fields=fields or None,
                Conditions=conditions or None,
                ExpiresIn=expires_in,
            )


_clients: dict[str, StorageClient] = {}


def register(
    name: str,
    url: str,
    bucket: str,
    access_key: str,
    secret_key: str,
    *,
    region: str = "us-east-1",
    public_url: str | None = None,
) -> StorageClient:
    """Register a named ``StorageClient`` and return it.

    Idempotent: calling ``register`` with the same name replaces the previous
    client (useful for reconfiguration without a restart).

    Args:
        name: Logical name used with ``get_client(name)``.
              Use ``"default"`` if you only have one bucket.
    """
    cfg = StorageConfig(
        url=url,
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        region=region,
        public_url=public_url,
    )
    client = StorageClient(cfg)
    replaced = name in _clients
    _clients[name] = client
    logger.info(
        "storage: client registered",
        extra={"client": name, "bucket": bucket, "endpoint": url, "replaced": replaced},
    )
    return client


def get_client(name: str = "default") -> StorageClient:
    """Return a registered ``StorageClient`` by name.

    Raises ``ClientNotRegisteredError`` if the name has not been registered.
    """
    try:
        return _clients[name]
    except KeyError:
        registered = ", ".join(f'"{k}"' for k in _clients) or "(none)"
        raise ClientNotRegisteredError(
            f"No storage client named {name!r}. "
            f"Registered clients: {registered}. "
            f"Call storage.register({name!r}, ...) at startup."
        ) from None


def deregister(name: str = "default") -> StorageClient | None:
    """Remove ``name`` from the registry and return the dropped client (or
    ``None`` if it wasn't registered). Idempotent.

    Symmetric with ``register``: lets tests and dev tooling restore the
    registry to a known shape without hooking into ``_clients`` directly.
    The async ``close(name)`` helper now delegates here.
    """
    dropped = _clients.pop(name, None)
    if dropped is not None:
        logger.info("storage: client deregistered", extra={"client": name})
    return dropped


async def init(
    url: str,
    bucket: str,
    access_key: str,
    secret_key: str,
    *,
    region: str = "us-east-1",
    public_url: str | None = None,
) -> None:
    """Initialize the default storage client. Equivalent to
    ``register("default", ...)``.
    """
    register(
        "default",
        url,
        bucket,
        access_key,
        secret_key,
        region=region,
        public_url=public_url,
    )


async def close(name: str = "default") -> None:
    """Deregister a named client. Idempotent. Async-shape preserved for
    callers that already ``await`` it; delegates to :func:`deregister`."""
    deregister(name)


async def upload(key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
    await get_client().upload(key, data, content_type=content_type)


async def download(key: str) -> bytes:
    return await get_client().download(key)


async def delete(key: str) -> None:
    await get_client().delete(key)


async def exists(key: str) -> bool:
    return await get_client().exists(key)


async def presign(
    key: str,
    expires_in: int = 3600,
    *,
    method: str = "get_object",
    content_type: str | None = None,
    response_content_disposition: str | None = None,
) -> str:
    return await get_client().presign(
        key,
        expires_in,
        method=method,
        content_type=content_type,
        response_content_disposition=response_content_disposition,
    )


async def presign_post(
    key: str,
    *,
    expires_in: int = 3600,
    content_type: str | None = None,
    max_size_bytes: int | None = None,
) -> dict:
    return await get_client().presign_post(
        key,
        expires_in=expires_in,
        content_type=content_type,
        max_size_bytes=max_size_bytes,
    )


__all__ = [
    "ClientNotRegisteredError",
    "ObjectNotFoundError",
    "StorageClient",
    "StorageConfig",
    "StorageError",
    "close",
    "delete",
    "deregister",
    "download",
    "exists",
    "get_client",
    "init",
    "presign",
    "presign_post",
    "register",
    "upload",
]
