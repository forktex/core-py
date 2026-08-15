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

"""Integration tests for the rich FILE handler in [space].

Strategy: install the rich handler (forktex_core.space import side-effect),
declare a Grid with a FILE field whose config points at a registered
storage client, write rows whose cells carry a real MinIO key, archive
rows, verify the blob is gone.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import forktex_core.space  # noqa: F401  side-effect: register rich FILE handler
from forktex_core.error import BadRequestError
from forktex_core.grid import Grid, TableSpec, apply_migrations
from forktex_core.grid.domain.fieldtypes import get_field_type
from forktex_core.grid.persist import GridRow
from forktex_core.space.types.file import FILE_TYPE_ID, RichFileType
from forktex_core.storage import StorageClient, register

_SCHEMA = "forktex_grid"


async def _declare(session, *, namespace, slug, label, columns):
    return await Grid.declare(
        session, TableSpec.from_dicts(slug=slug, label=label, namespace=namespace, columns=columns)
    )


def _file_column(**config):
    return {"key": "attachment", "label": "Attachment", "type_id": FILE_TYPE_ID, "config": config}


@pytest.fixture(scope="module")
def storage_client_name(minio_config: dict) -> str:
    """Register a storage client for the rich FILE handler to find."""
    name = "test-file-handler"
    register(
        name=name,
        url=minio_config["url"],
        bucket=minio_config["bucket"],
        access_key=minio_config["access_key"],
        secret_key=minio_config["secret_key"],
    )
    return name


@pytest_asyncio.fixture
async def file_session(postgres_url_str: str, fresh_schema: str):
    engine = create_async_engine(
        postgres_url_str,
        execution_options={"schema_translate_map": {_SCHEMA: fresh_schema}},
    )
    await apply_migrations(engine, schema=fresh_schema)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
    await engine.dispose()


def test_rich_file_handler_is_registered():
    handler = get_field_type(FILE_TYPE_ID)
    assert isinstance(handler, RichFileType)


def test_normalize_string_to_descriptor():
    handler = RichFileType()
    config = handler.validate_config({})
    out = handler.normalize("uploads/abc", config=config)
    assert out == {"storage_key": "uploads/abc"}


def test_normalize_dict_passes_through():
    handler = RichFileType()
    config = handler.validate_config({})
    descriptor = {"storage_key": "k", "filename": "x.pdf", "content_type": "application/pdf", "size": 1024}
    out = handler.normalize(descriptor, config=config)
    assert out == descriptor


def test_normalize_missing_storage_key_raises():
    handler = RichFileType()
    config = handler.validate_config({})
    with pytest.raises(BadRequestError):
        handler.normalize({"filename": "x"}, config=config)


def test_normalize_wrong_type_raises():
    handler = RichFileType()
    config = handler.validate_config({})
    with pytest.raises(BadRequestError):
        handler.normalize(123, config=config)


def test_normalize_none_returns_none():
    handler = RichFileType()
    config = handler.validate_config({})
    assert handler.normalize(None, config=config) is None


@pytest.mark.asyncio
async def test_on_row_archive_deletes_blob(file_session: AsyncSession, minio_config: dict, storage_client_name: str):
    """End-to-end: upload via [storage], wire the FILE field, archive
    the row → blob gone."""
    from forktex_core.storage import get_client

    client: StorageClient = get_client(storage_client_name)
    storage_key = f"file-handler-test/{uuid.uuid4()}.txt"
    await client.upload(storage_key, b"hello world", content_type="text/plain")
    assert await client.exists(storage_key) is True

    ns = str(uuid.uuid4())
    grid = await _declare(
        file_session,
        namespace=ns,
        slug="docs",
        label="Docs",
        columns=[_file_column(client_name=storage_client_name, delete_on_archive=True)],
    )
    row = await grid.create(
        {
            "attachment": {
                "storage_key": storage_key,
                "filename": "hello.txt",
                "content_type": "text/plain",
                "size": 11,
            }
        }
    )
    await file_session.commit()
    assert row.values["attachment"]["storage_key"] == storage_key

    await grid.archive(row.id)
    await file_session.commit()

    assert await client.exists(storage_key) is False


@pytest.mark.asyncio
async def test_on_row_archive_respects_delete_on_archive_false(
    file_session: AsyncSession, minio_config: dict, storage_client_name: str
):
    """delete_on_archive=False keeps the blob even after row archive."""
    from forktex_core.storage import get_client

    client = get_client(storage_client_name)
    storage_key = f"file-handler-test/keep-{uuid.uuid4()}.txt"
    await client.upload(storage_key, b"keep me", content_type="text/plain")

    ns = str(uuid.uuid4())
    grid = await _declare(
        file_session,
        namespace=ns,
        slug="docs",
        label="Docs",
        columns=[_file_column(client_name=storage_client_name, delete_on_archive=False)],
    )
    row = await grid.create({"attachment": {"storage_key": storage_key}})
    await file_session.commit()

    await grid.archive(row.id)
    await file_session.commit()

    assert await client.exists(storage_key) is True
    # Cleanup the blob we kept on purpose so the bucket stays tidy.
    await client.delete(storage_key)


@pytest.mark.asyncio
async def test_on_row_archive_tolerates_missing_blob(file_session: AsyncSession, storage_client_name: str):
    """Already-deleted blob doesn't break the archive."""
    ns = str(uuid.uuid4())
    grid = await _declare(
        file_session,
        namespace=ns,
        slug="docs",
        label="Docs",
        columns=[_file_column(client_name=storage_client_name)],
    )
    row = await grid.create({"attachment": {"storage_key": "ghost/key/that/never/existed.txt"}})
    await file_session.commit()
    await grid.archive(row.id)  # must not raise despite the blob already being gone
    await file_session.commit()

    orm_row = await file_session.get(GridRow, row.id)
    assert orm_row is not None
    assert orm_row.is_active is False
    assert orm_row.archived_at is not None
