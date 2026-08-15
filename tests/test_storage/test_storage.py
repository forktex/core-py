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

"""Integration tests for forktex_core.storage — requires MinIO container."""

from __future__ import annotations

import pytest
import pytest_asyncio

pytest.importorskip("aioboto3", reason="aioboto3 not installed")

from forktex_core.storage import (
    ClientNotRegisteredError,
    ObjectNotFoundError,
    StorageClient,
    get_client,
    register,
)


@pytest_asyncio.fixture
async def client(minio_config: dict) -> StorageClient:
    c = register(
        "test",
        url=minio_config["url"],
        bucket=minio_config["bucket"],
        access_key=minio_config["access_key"],
        secret_key=minio_config["secret_key"],
    )
    return c


@pytest.mark.asyncio
async def test_upload_and_download(client: StorageClient):
    data = b"hello storage world"
    await client.upload("test/hello.txt", data, content_type="text/plain")
    result = await client.download("test/hello.txt")
    assert result == data


@pytest.mark.asyncio
async def test_exists_true_and_false(client: StorageClient):
    await client.upload("test/exists.txt", b"data")
    assert await client.exists("test/exists.txt") is True
    assert await client.exists("test/no-such-key.txt") is False


@pytest.mark.asyncio
async def test_delete(client: StorageClient):
    await client.upload("test/to-delete.txt", b"bye")
    await client.delete("test/to-delete.txt")
    assert await client.exists("test/to-delete.txt") is False


@pytest.mark.asyncio
async def test_delete_missing_key_is_a_noop(client: StorageClient):
    """delete() on a key that never existed must not raise."""
    await client.delete("test/never-existed-xyz-abc.txt")


@pytest.mark.asyncio
async def test_upload_overwrites_existing_key(client: StorageClient):
    await client.upload("test/overwrite.txt", b"original")
    await client.upload("test/overwrite.txt", b"replaced")
    assert await client.download("test/overwrite.txt") == b"replaced"


@pytest.mark.asyncio
async def test_upload_download_key_with_special_characters(client: StorageClient):
    key = "test/special chars #1 (v2).txt"
    data = b"special key data"
    await client.upload(key, data)
    assert await client.download(key) == data
    assert await client.exists(key) is True


@pytest.mark.asyncio
async def test_download_missing_raises(client: StorageClient):
    with pytest.raises(ObjectNotFoundError):
        await client.download("test/definitely-missing-xyz-abc.txt")


@pytest.mark.asyncio
async def test_presign_get_returns_url(client: StorageClient):
    await client.upload("test/presign-target.txt", b"presigned")
    url = await client.presign("test/presign-target.txt", expires_in=300)
    assert url.startswith("http")
    assert "test-bucket" in url or "presign-target" in url


@pytest.mark.asyncio
async def test_presign_put_returns_url(client: StorageClient):
    url = await client.presign(
        "test/upload-target.txt",
        expires_in=300,
        method="put_object",
        content_type="text/plain",
    )
    assert url.startswith("http")


@pytest.mark.asyncio
async def test_presign_post_returns_policy(client: StorageClient):
    result = await client.presign_post(
        "test/multipart-target.txt",
        expires_in=300,
        content_type="text/plain",
        max_size_bytes=1024 * 1024,
    )
    assert "url" in result
    assert "fields" in result


@pytest.mark.asyncio
async def test_multi_client_registry(minio_config: dict):
    c1 = register(
        "multi-a",
        url=minio_config["url"],
        bucket=minio_config["bucket"],
        access_key=minio_config["access_key"],
        secret_key=minio_config["secret_key"],
    )
    c2 = register(
        "multi-b",
        url=minio_config["url"],
        bucket=minio_config["bucket"],
        access_key=minio_config["access_key"],
        secret_key=minio_config["secret_key"],
    )
    assert get_client("multi-a") is c1
    assert get_client("multi-b") is c2
    assert c1 is not c2


@pytest.mark.asyncio
async def test_get_unregistered_client_raises():
    with pytest.raises(ClientNotRegisteredError):
        get_client("not-registered-xyz-abc")


@pytest.mark.asyncio
async def test_direct_url(client: StorageClient):
    url = client.direct_url("images/hero.jpg")
    assert "hero.jpg" in url
    assert "test-bucket" in url


@pytest.mark.asyncio
async def test_direct_url_encodes_special_characters_in_key():
    """A raw '#'/space in the key must not be able to truncate the URL or
    change which fragment/query the browser resolves it as."""
    from forktex_core.storage import StorageClient, StorageConfig

    c = StorageClient(
        StorageConfig(
            url="http://minio:9000",
            bucket="test-bucket",
            access_key="x",
            secret_key="y",
            public_url="http://cdn.example.com",
        )
    )
    url = c.direct_url("images/hero #1.jpg")
    assert url == "http://cdn.example.com/test-bucket/images/hero%20%231.jpg"


def test_storage_config_masks_secrets_in_repr():
    """access_key/secret_key must never appear in repr()/str() — they're
    plaintext credentials that could otherwise leak into logs/tracebacks."""
    from forktex_core.storage import StorageConfig

    c = StorageConfig(url="http://minio:9000", bucket="b", access_key="AKIA_SECRET", secret_key="SK_SECRET")
    assert "AKIA_SECRET" not in repr(c)
    assert "SK_SECRET" not in repr(c)
    assert "AKIA_SECRET" not in str(c)
    assert "SK_SECRET" not in str(c)
    # ... but the real values are still there for actual use and serialization.
    assert c.access_key == "AKIA_SECRET"
    assert c.secret_key == "SK_SECRET"
    assert c.model_dump()["access_key"] == "AKIA_SECRET"
    assert c.model_dump()["secret_key"] == "SK_SECRET"


@pytest.mark.asyncio
async def test_ensure_bucket_creates_when_absent(minio_config: dict):
    import uuid

    from forktex_core.storage import StorageClient, StorageConfig

    fresh_bucket = f"ensure-bucket-test-{uuid.uuid4().hex[:8]}"
    c = StorageClient(
        StorageConfig(
            url=minio_config["url"],
            bucket=fresh_bucket,
            access_key=minio_config["access_key"],
            secret_key=minio_config["secret_key"],
        )
    )
    await c.ensure_bucket()
    # Bucket now usable — upload proves it exists.
    await c.upload("probe.txt", b"data")
    assert await c.download("probe.txt") == b"data"


@pytest.mark.asyncio
async def test_ensure_bucket_idempotent_when_already_exists(client: StorageClient):
    await client.ensure_bucket()  # bucket from the `client` fixture already exists
    await client.ensure_bucket()  # must not raise the second time either


@pytest.mark.asyncio
async def test_ensure_bucket_public_read_sets_policy(minio_config: dict):
    import uuid

    from forktex_core.storage import StorageClient, StorageConfig

    fresh_bucket = f"ensure-bucket-public-{uuid.uuid4().hex[:8]}"
    c = StorageClient(
        StorageConfig(
            url=minio_config["url"],
            bucket=fresh_bucket,
            access_key=minio_config["access_key"],
            secret_key=minio_config["secret_key"],
        )
    )
    await c.ensure_bucket(public_read=True)

    async with c._client() as s3:
        policy = await s3.get_bucket_policy(Bucket=fresh_bucket)
        assert "s3:GetObject" in policy["Policy"]
