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

"""End-to-end Bundle facade tests against testcontainers postgres."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forktex_core.grid import FieldType, Grid, TableSpec, apply_migrations
from forktex_core.grid.persist import GridTable
from forktex_core.error import AlreadyExistsError, NotFoundError
from forktex_core.space import Bundle, BundleConfig, SyncSourceConfig, VectorDefaults

_SCHEMA = "forktex_grid"


@pytest_asyncio.fixture
async def space_session(postgres_url_str: str, fresh_schema: str):
    engine = create_async_engine(
        postgres_url_str,
        execution_options={"schema_translate_map": {_SCHEMA: fresh_schema}},
    )
    await apply_migrations(engine, schema=fresh_schema)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _new_grid(session: AsyncSession, namespace: str, slug: str) -> Grid:
    return await Grid.declare(
        session,
        TableSpec.from_dicts(
            slug=slug,
            label=slug.title(),
            namespace=namespace,
            columns=[{"key": "title", "label": "Title", "type_id": FieldType.text.value}],
        ),
    )


async def _space_id(session: AsyncSession, grid: Grid) -> uuid.UUID | None:
    """The member table's ``space_id`` FK, read from the catalog row."""
    table = await session.get(GridTable, grid.ref.id)
    assert table is not None
    return table.space_id


@pytest.mark.asyncio
async def test_declare_space_creates_record_and_attaches_grids(space_session: AsyncSession):
    ns = str(uuid.uuid4())
    leads = await _new_grid(space_session, ns, "leads")
    notes = await _new_grid(space_session, ns, "notes")

    space = await Bundle.declare(
        space_session,
        namespace=ns,
        slug="sales",
        label="Sales Workspace",
        config=BundleConfig(vector=VectorDefaults(model="bge-small", dimensions=384)),
        sync_sources=[SyncSourceConfig(kind="hubspot", options={"portal_id": 42})],
        members=[leads, notes],
    )
    await space_session.commit()

    assert space.slug == "sales"
    assert {g.slug for g in space.grids.values()} == {"leads", "notes"}
    assert space.record.label == "Sales Workspace"

    # Both Grids' entities now point at this Bundle.
    assert await _space_id(space_session, leads) == space.record.id
    assert await _space_id(space_session, notes) == space.record.id


@pytest.mark.asyncio
async def test_declare_duplicate_slug_raises(space_session: AsyncSession):
    ns = str(uuid.uuid4())
    await Bundle.declare(space_session, namespace=ns, slug="dup", members=[])
    with pytest.raises(AlreadyExistsError):
        await Bundle.declare(space_session, namespace=ns, slug="dup", members=[])


@pytest.mark.asyncio
async def test_bind_loads_existing_space_and_members(space_session: AsyncSession):
    ns = str(uuid.uuid4())
    leads = await _new_grid(space_session, ns, "leads")
    declared = await Bundle.declare(
        space_session,
        namespace=ns,
        slug="sales",
        config=BundleConfig(vector=VectorDefaults(model="bge-small")),
        sync_sources=[SyncSourceConfig(kind="hubspot")],
        members=[leads],
    )
    await space_session.commit()

    # Drop in-process state, rebind from DB.
    bound = await Bundle.bind(space_session, namespace=ns, slug="sales")
    assert bound.record.id == declared.record.id
    assert bound.config.vector.model == "bge-small"
    assert len(bound.sync_sources) == 1
    assert bound.sync_sources[0].kind == "hubspot"
    assert "leads" in bound.grids


@pytest.mark.asyncio
async def test_bind_missing_raises_not_found(space_session: AsyncSession):
    with pytest.raises(NotFoundError):
        await Bundle.bind(space_session, namespace=str(uuid.uuid4()), slug="ghost")


@pytest.mark.asyncio
async def test_attach_and_detach_round_trip(space_session: AsyncSession):
    ns = str(uuid.uuid4())
    leads = await _new_grid(space_session, ns, "leads")
    space = await Bundle.declare(space_session, namespace=ns, slug="sales", members=[])
    await space_session.commit()

    # Initially standalone.
    assert await _space_id(space_session, leads) is None

    await space.attach(leads)
    assert await _space_id(space_session, leads) == space.record.id
    assert space.grid("leads") is leads

    # Idempotent re-attach.
    await space.attach(leads)
    assert await _space_id(space_session, leads) == space.record.id

    await space.detach("leads")
    assert await _space_id(space_session, leads) is None
    with pytest.raises(KeyError):
        space.grid("leads")


@pytest.mark.asyncio
async def test_grid_unknown_member_raises_keyerror(space_session: AsyncSession):
    ns = str(uuid.uuid4())
    space = await Bundle.declare(space_session, namespace=ns, slug="sales", members=[])
    with pytest.raises(KeyError):
        space.grid("missing")


@pytest.mark.asyncio
async def test_list_grids_reads_fresh_from_db(space_session: AsyncSession):
    ns = str(uuid.uuid4())
    leads = await _new_grid(space_session, ns, "leads")
    space = await Bundle.declare(space_session, namespace=ns, slug="sales", members=[leads])
    await space_session.commit()

    grids = await space.list_grids()
    assert [g.slug for g in grids] == ["leads"]
