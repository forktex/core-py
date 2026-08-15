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

"""Integration tests for the rich VECTOR handler in [space]."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import forktex_core.space  # noqa: F401  side-effect: register rich VECTOR handler
from forktex_core.error import BadRequestError
from forktex_core.grid import Grid, TableSpec, apply_migrations
from forktex_core.grid.domain.fieldtypes import get_field_type
from forktex_core.space.types.vector import VECTOR_TYPE_ID, RichVectorType, VectorConfig
from forktex_core.vector import register

_SCHEMA = "forktex_grid"


async def _declare(session, *, namespace, slug, label, columns):
    return await Grid.declare(
        session, TableSpec.from_dicts(slug=slug, label=label, namespace=namespace, columns=columns)
    )


def _vector_column(**config):
    return {"key": "emb", "label": "Embedding", "type_id": VECTOR_TYPE_ID, "config": config}


@pytest.fixture(scope="module")
def vector_client_name(qdrant_url: str) -> str:
    name = "test-vector-handler"
    register(name=name, qdrant_url=qdrant_url)
    return name


@pytest_asyncio.fixture
async def vec_session(postgres_url_str: str, fresh_schema: str):
    engine = create_async_engine(
        postgres_url_str,
        execution_options={"schema_translate_map": {_SCHEMA: fresh_schema}},
    )
    await apply_migrations(engine, schema=fresh_schema)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
    await engine.dispose()


# ── Unit tests (no DB / no Qdrant) ───────────────────────────────────────


def test_rich_vector_handler_is_registered():
    handler = get_field_type(VECTOR_TYPE_ID)
    assert isinstance(handler, RichVectorType)


def test_normalize_list_to_descriptor():
    handler = RichVectorType()
    config = handler.validate_config({"storage_mode": "inline", "dimensions": 4})
    out = handler.normalize([1.0, 2.0, 3.0, 4.0], config=config)
    assert out["vector"] == [1.0, 2.0, 3.0, 4.0]
    assert out["dimensions"] == 4


def test_normalize_keeps_inline_vector_in_remote_mode_for_hook_consumption():
    """``normalize`` always preserves the vector so on_rows_written can
    upsert it; the hook strips the inline copy after the upsert lands."""
    handler = RichVectorType()
    config = handler.validate_config({"storage_mode": "remote", "dimensions": 4})
    out = handler.normalize([1.0, 2.0, 3.0, 4.0], config=config)
    assert out["vector"] == [1.0, 2.0, 3.0, 4.0]


def test_normalize_keeps_inline_vector_in_both_mode():
    handler = RichVectorType()
    config = handler.validate_config({"storage_mode": "both", "dimensions": 4})
    out = handler.normalize([1.0, 2.0, 3.0, 4.0], config=config)
    assert out["vector"] == [1.0, 2.0, 3.0, 4.0]


def test_normalize_dimension_mismatch_raises():
    handler = RichVectorType()
    config = handler.validate_config({"storage_mode": "inline", "dimensions": 4})
    with pytest.raises(BadRequestError):
        handler.normalize([1.0, 2.0], config=config)


def test_normalize_descriptor_with_only_point_id_passes():
    handler = RichVectorType()
    config = handler.validate_config({"storage_mode": "remote"})
    out = handler.normalize({"point_id": "row-uuid", "collection": "c"}, config=config)
    assert out == {"point_id": "row-uuid", "collection": "c"}


def test_normalize_empty_descriptor_raises():
    handler = RichVectorType()
    config = handler.validate_config({"storage_mode": "remote"})
    with pytest.raises(BadRequestError):
        handler.normalize({}, config=config)


def test_normalize_wrong_type_raises():
    handler = RichVectorType()
    config = handler.validate_config({"storage_mode": "inline"})
    with pytest.raises(BadRequestError):
        handler.normalize("hello", config=config)


def test_normalize_none_returns_none():
    handler = RichVectorType()
    config = handler.validate_config({"storage_mode": "inline"})
    assert handler.normalize(None, config=config) is None


def test_invalid_storage_mode_rejected():
    with pytest.raises(Exception):
        VectorConfig(storage_mode="diagonal")  # type: ignore[arg-type]


# ── End-to-end tests against testcontainers Qdrant ─────────────────────────


@pytest.mark.asyncio
async def test_remote_mode_upserts_to_qdrant(vec_session: AsyncSession, vector_client_name: str):
    from forktex_core.vector import SearchQuery, get_client

    ns = str(uuid.uuid4())
    grid = await _declare(
        vec_session,
        namespace=ns,
        slug="docs",
        label="Docs",
        columns=[
            _vector_column(
                storage_mode="remote",
                dimensions=4,
                model="test-model",
                client_name=vector_client_name,
            )
        ],
    )

    row = await grid.create({"emb": [1.0, 0.0, 0.0, 0.0]})
    await vec_session.commit()

    # Cell-level descriptor: vector stripped (remote mode); model carried.
    assert row.values["emb"]["model"] == "test-model"
    assert "vector" not in row.values["emb"]

    client = get_client(vector_client_name)
    collection_name = f"{ns}--docs--emb"
    handle = client.collection(collection_name)
    info = await handle.info()
    assert info.vectors_count == 1

    hits = await handle.search(SearchQuery(vector=[1.0, 0.0, 0.0, 0.0]).limit(5))
    assert len(hits) == 1
    assert str(hits[0].id) == str(row.id)
    assert hits[0].payload["row_id"] == str(row.id)

    # Cleanup the collection so Qdrant doesn't accumulate.
    await handle.delete()


@pytest.mark.asyncio
async def test_inline_mode_keeps_vector_in_payload_no_qdrant_writes(vec_session: AsyncSession, vector_client_name: str):
    from forktex_core.vector import get_client

    ns = str(uuid.uuid4())
    grid = await _declare(
        vec_session,
        namespace=ns,
        slug="docs",
        label="Docs",
        columns=[_vector_column(storage_mode="inline", dimensions=3, client_name=vector_client_name)],
    )

    row = await grid.create({"emb": [0.5, 0.5, 0.0]})
    await vec_session.commit()

    assert row.values["emb"]["vector"] == [0.5, 0.5, 0.0]

    client = get_client(vector_client_name)
    collections = await client.list_collections(prefix=f"{ns}--")
    assert collections == []  # No Qdrant write happened in inline mode.


@pytest.mark.asyncio
async def test_archive_deletes_remote_point(vec_session: AsyncSession, vector_client_name: str):
    from forktex_core.vector import SearchQuery, get_client

    ns = str(uuid.uuid4())
    grid = await _declare(
        vec_session,
        namespace=ns,
        slug="docs",
        label="Docs",
        columns=[_vector_column(storage_mode="remote", dimensions=4, client_name=vector_client_name)],
    )

    row = await grid.create({"emb": [1.0, 0.0, 0.0, 0.0]})
    await vec_session.commit()

    client = get_client(vector_client_name)
    handle = client.collection(f"{ns}--docs--emb")
    pre = await handle.search(SearchQuery(vector=[1.0, 0.0, 0.0, 0.0]).limit(5))
    assert len(pre) == 1

    await grid.archive(row.id)
    await vec_session.commit()

    post = await handle.search(SearchQuery(vector=[1.0, 0.0, 0.0, 0.0]).limit(5))
    assert post == []

    await handle.delete()


@pytest.mark.asyncio
async def test_archive_with_delete_on_archive_false_keeps_point(vec_session: AsyncSession, vector_client_name: str):
    from forktex_core.vector import SearchQuery, get_client

    ns = str(uuid.uuid4())
    grid = await _declare(
        vec_session,
        namespace=ns,
        slug="docs",
        label="Docs",
        columns=[
            _vector_column(
                storage_mode="remote",
                dimensions=4,
                client_name=vector_client_name,
                delete_on_archive=False,
            )
        ],
    )

    row = await grid.create({"emb": [1.0, 0.0, 0.0, 0.0]})
    await vec_session.commit()

    await grid.archive(row.id)
    await vec_session.commit()

    client = get_client(vector_client_name)
    handle = client.collection(f"{ns}--docs--emb")
    hits = await handle.search(SearchQuery(vector=[1.0, 0.0, 0.0, 0.0]).limit(5))
    # Point preserved despite archive.
    assert any(str(h.id) == str(row.id) for h in hits)

    await handle.delete()
