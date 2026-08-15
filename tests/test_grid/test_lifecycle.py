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

"""Grid 4.0 lifecycle guarantees driven through the curated API.

Ports the highest-value 3.0 behaviours the conformance suite didn't already cover:
the deletion planner's ``restrict`` / ``set_null`` policies, write atomicity (a
failed ref write leaves no orphan), migration idempotency, and namespace isolation.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from forktex_core.grid import (
    ColumnSpec,
    Grid,
    OnDelete,
    RelationShape,
    RelationSpec,
    TableSpec,
    apply_migrations,
    declare_relation,
)


def _cols(*specs: tuple[str, str]) -> tuple[ColumnSpec, ...]:
    return tuple(ColumnSpec(key=k, label=k.title(), type_id=t) for k, t in specs)


async def _parent_child(session: AsyncSession, on_delete: OnDelete, *, required: bool) -> tuple[Grid, Grid]:
    parent = await Grid.declare(session, TableSpec(slug="parent", label="Parent", columns=_cols(("name", "text"))))
    child = await Grid.declare(session, TableSpec(slug="child", label="Child", columns=_cols(("name", "text"))))
    await declare_relation(
        session,
        RelationSpec(
            key="parent", source="child", target="parent", shape=RelationShape.many_to_one, on_delete=on_delete
        ),
    )
    await child.add_column(
        ColumnSpec(key="parent", label="Parent", type_id="ref", relation_ref="parent", is_required=required)
    )
    return parent, child


# ── deletion planner: the two policies conformance didn't cover ────────────────


async def test_on_delete_restrict_blocks(session: AsyncSession) -> None:
    parent, child = await _parent_child(session, OnDelete.restrict, required=True)
    p = await parent.create({"name": "P"})
    await child.create({"name": "C", "parent": str(p.id)})
    with pytest.raises(Exception):  # noqa: B017 — BadRequestError family
        await parent.archive(p.id)
    assert [r.values["name"] for r in (await parent.query()).rows] == ["P"]  # still there


async def test_on_delete_set_null_clears_ref(session: AsyncSession) -> None:
    parent, child = await _parent_child(session, OnDelete.set_null, required=False)
    p = await parent.create({"name": "P"})
    c = await child.create({"name": "C", "parent": str(p.id)})
    await parent.archive(p.id)  # allowed: nulls the optional ref
    row = await child.get(c.id)
    assert row.values.get("parent") is None
    assert (await parent.query()).rows == []


# ── write atomicity: a failed ref sync leaves no orphan row ────────────────────


async def test_failed_ref_write_is_atomic(session: AsyncSession) -> None:
    parent, child = await _parent_child(session, OnDelete.restrict, required=False)
    before = len((await child.query()).rows)
    with pytest.raises(Exception):  # noqa: B017 — ref points at a non-existent row
        await child.create({"name": "orphan", "parent": str(uuid.uuid4())})
    assert len((await child.query()).rows) == before  # savepoint rolled the INSERT back


# ── migration idempotency + baseline shape ─────────────────────────────────────


async def test_migrations_idempotent(grid_engine: AsyncEngine, grid_schema: str) -> None:
    # Re-applying is a no-op: each version is recorded exactly once, and no
    # extra tables appear.
    await apply_migrations(grid_engine, schema=grid_schema)
    async with grid_engine.connect() as conn:
        versions = list(
            await conn.scalars(sa.text(f'SELECT version FROM "{grid_schema}".schema_version ORDER BY version'))
        )
        tables = await conn.scalar(
            sa.text("SELECT count(*) FROM information_schema.tables WHERE table_schema = :s"),
            {"s": grid_schema},
        )
    # Asserted as an invariant rather than a hardcoded list, so adding a
    # migration doesn't break the idempotency test it has nothing to do with.
    assert versions, "no migrations recorded"
    assert versions == sorted(set(versions)), f"a version was applied twice: {versions}"
    assert versions[0] == 1, f"baseline missing: {versions}"
    # 7 grid tables (space, table, relation, column, index, row, edge) + schema_version.
    assert tables == 8


# ── namespace isolation: same slug, different tenants, no leak ──────────────────


async def test_namespace_isolation(session: AsyncSession) -> None:
    a = await Grid.declare(
        session, TableSpec(slug="doc", label="Doc", namespace="tenant-a", columns=_cols(("t", "text")))
    )
    b = await Grid.declare(
        session, TableSpec(slug="doc", label="Doc", namespace="tenant-b", columns=_cols(("t", "text")))
    )
    await a.create({"t": "secret-a"})
    await b.create({"t": "secret-b"})
    assert [r.values["t"] for r in (await a.query()).rows] == ["secret-a"]
    assert [r.values["t"] for r in (await b.query()).rows] == ["secret-b"]
