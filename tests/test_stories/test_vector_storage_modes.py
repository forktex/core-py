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

"""STORY: VECTOR storage modes — substrate-mode contract.

Round-trips the four ``VectorConfig.storage_mode`` settings against
real Postgres + Qdrant, asserting cell shape and Qdrant-side state
per mode:

  - ``inline`` — vector lives in the row's JSONB; nothing in Qdrant.
  - ``remote`` — vector stripped from the cell, replaced with
    ``collection`` + ``point_id`` back-refs; point exists in Qdrant.
  - ``both``   — vector kept inline AND back-refs stamped; point in Qdrant.
  - ``none``   — vector stripped, no Qdrant write, no back-refs.

This isn't a consumer journey like the other story tracks; it's a
contract test that proves the four modes have the cell + Qdrant
shape we promise. Lives in ``test_stories/`` because it depends on
real Qdrant.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import forktex_core.space  # noqa: F401  side-effect: register rich VECTOR
from forktex_core.grid import Grid, TableSpec, apply_migrations
from forktex_core.space.types.vector import VECTOR_TYPE_ID
from forktex_core.vector import SearchQuery, register as register_vector

_SCHEMA = "forktex_grid"


async def _declare(session, *, namespace, slug, label, columns, is_system=False):
    return await Grid.declare(
        session,
        TableSpec.from_dicts(slug=slug, label=label, namespace=namespace, is_system=is_system, columns=columns),
    )


@pytest_asyncio.fixture
async def storage_mode_session(postgres_url, qdrant_url: str):
    fresh_schema = f"story_modes_{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(
        postgres_url.render_as_string(hide_password=False),
        execution_options={"schema_translate_map": {_SCHEMA: fresh_schema}},
    )
    await apply_migrations(engine, schema=fresh_schema)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    vector_client_name = f"story-modes-vector-{uuid.uuid4().hex[:6]}"
    register_vector(name=vector_client_name, qdrant_url=qdrant_url)
    async with maker() as session:
        yield session, vector_client_name
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_mode", ["inline", "remote", "both", "none"])
async def test_vector_storage_mode_round_trip(storage_mode_session, qdrant_collection_tracker, storage_mode):
    session, vector_client_name = storage_mode_session
    namespace = str(uuid.uuid4())
    grid = await _declare(
        session,
        namespace=namespace,
        slug="docs",
        label="Docs",
        columns=[
            {
                "key": "emb",
                "label": "Embedding",
                "type_id": VECTOR_TYPE_ID,
                "config": {"storage_mode": storage_mode, "dimensions": 4, "client_name": vector_client_name},
            }
        ],
    )

    row = await grid.create({"emb": [1.0, 0.0, 0.0, 0.0]})
    await session.commit()
    cell = row.values["emb"]
    collection_name = f"{namespace}--docs--emb"

    from forktex_core.vector import get_client

    handle = get_client(vector_client_name).collection(collection_name)

    if storage_mode == "inline":
        # Vector inline; no Qdrant side-effect.
        assert cell["vector"] == [1.0, 0.0, 0.0, 0.0]
        assert "collection" not in cell
        # `info()` raises if the collection doesn't exist — assert that.
        from forktex_core.vector import CollectionNotFoundError

        with pytest.raises(CollectionNotFoundError):
            await handle.info()

    elif storage_mode == "remote":
        # Vector stripped; back-refs stamped; Qdrant has the point.
        qdrant_collection_tracker.append((vector_client_name, collection_name))
        assert "vector" not in cell
        assert cell["collection"] == collection_name
        assert cell["point_id"] == str(row.id)
        hits = await handle.search(SearchQuery(vector=[1.0, 0.0, 0.0, 0.0]).limit(1))
        assert len(hits) == 1
        assert str(hits[0].id) == str(row.id)

    elif storage_mode == "both":
        # Vector kept AND back-refs stamped; Qdrant has the point.
        qdrant_collection_tracker.append((vector_client_name, collection_name))
        assert cell["vector"] == [1.0, 0.0, 0.0, 0.0]
        assert cell["collection"] == collection_name
        assert cell["point_id"] == str(row.id)
        hits = await handle.search(SearchQuery(vector=[1.0, 0.0, 0.0, 0.0]).limit(1))
        assert len(hits) == 1
        assert str(hits[0].id) == str(row.id)

    elif storage_mode == "none":
        # No vector kept, no back-refs, no Qdrant write.
        assert "vector" not in cell
        assert "collection" not in cell
        assert "point_id" not in cell
        from forktex_core.vector import CollectionNotFoundError

        with pytest.raises(CollectionNotFoundError):
            await handle.info()
