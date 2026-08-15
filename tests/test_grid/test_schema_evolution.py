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

"""Destructive schema primitives — rename / drop / alter as soft-archive + payload migration.

These are the persist-layer building blocks the reconciler drives. Verified directly against a
live Postgres: renames migrate the JSONB payload key, drops soft-archive out of the hydrated
schema, and a retype is refused (deferred capability).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid import ColumnSpec, Grid, RelationShape, RelationSpec, TableSpec, declare_relation
from forktex_core.grid.errors import BadRequestError
from forktex_core.grid.domain.enums import Materialization
from forktex_core.grid.persist import schema_repo

NS = "acme"


async def _people(session: AsyncSession) -> Grid:
    return await Grid.declare(
        session,
        TableSpec(
            slug="people",
            label="People",
            namespace=NS,
            columns=(
                ColumnSpec(key="name", label="Name", type_id="text"),
                ColumnSpec(key="age", label="Age", type_id="integer"),
            ),
        ),
    )


async def test_rename_column_migrates_payload(session: AsyncSession) -> None:
    g = await _people(session)
    row = await g.create({"name": "Ann", "age": 30})

    await schema_repo.rename_column(session, table_id=g.ref.id, key="age", new_key="years")
    cat = await schema_repo.hydrate(session, NS)

    keys = {c.key for c in cat.tables["people"].columns}
    assert keys == {"name", "years"}
    # the stored value moved from the old key to the new one
    refetched = await Grid.open(session, slug="people", namespace=NS)
    got = await refetched.get(row.id)
    assert got.values.get("years") == 30 and "age" not in got.values


async def test_rename_table(session: AsyncSession) -> None:
    await _people(session)
    await schema_repo.rename_table(session, slug="people", new_slug="humans", namespace=NS)
    cat = await schema_repo.hydrate(session, NS)
    assert set(cat.tables) == {"humans"}


async def test_alter_column_updates_attributes(session: AsyncSession) -> None:
    g = await _people(session)
    await schema_repo.alter_column(
        session,
        table_id=g.ref.id,
        spec=ColumnSpec(key="name", label="Full name", type_id="text", is_required=True),
    )
    cat = await schema_repo.hydrate(session, NS)
    name = next(c for c in cat.tables["people"].columns if c.key == "name")
    assert name.label == "Full name" and name.is_required


async def test_alter_column_refuses_retype(session: AsyncSession) -> None:
    g = await _people(session)
    with pytest.raises(BadRequestError, match="retype"):
        await schema_repo.alter_column(
            session,
            table_id=g.ref.id,
            spec=ColumnSpec(key="age", label="Age", type_id="text"),  # integer -> text
        )
    with pytest.raises(BadRequestError, match="retype"):
        await schema_repo.alter_column(
            session,
            table_id=g.ref.id,
            spec=ColumnSpec(key="age", label="Age", type_id="integer", materialization=Materialization.promoted),
        )


async def test_archive_column_drops_it_from_catalog(session: AsyncSession) -> None:
    g = await _people(session)
    await schema_repo.archive_column(session, table_id=g.ref.id, key="age")
    cat = await schema_repo.hydrate(session, NS)
    assert {c.key for c in cat.tables["people"].columns} == {"name"}


async def test_archive_table_removes_table_and_its_relations(session: AsyncSession) -> None:
    await _people(session)
    await Grid.declare(
        session,
        TableSpec(
            slug="company",
            label="Company",
            namespace=NS,
            columns=(ColumnSpec(key="name", label="Name", type_id="text"),),
        ),
    )
    await declare_relation(
        session,
        RelationSpec(key="employer", source="people", target="company", shape=RelationShape.many_to_one),
        namespace=NS,
    )

    await schema_repo.archive_table(session, slug="company", namespace=NS)
    cat = await schema_repo.hydrate(session, NS)

    assert set(cat.tables) == {"people"}
    assert cat.relations == {}  # the relation referencing 'company' was archived too


async def test_archive_relation(session: AsyncSession) -> None:
    await _people(session)
    await Grid.declare(
        session,
        TableSpec(
            slug="company",
            label="Company",
            namespace=NS,
            columns=(ColumnSpec(key="name", label="Name", type_id="text"),),
        ),
    )
    await declare_relation(
        session,
        RelationSpec(key="employer", source="people", target="company", shape=RelationShape.many_to_one),
        namespace=NS,
    )
    await schema_repo.archive_relation(session, key="employer", namespace=NS)
    cat = await schema_repo.hydrate(session, NS)
    assert cat.relations == {} and set(cat.tables) == {"people", "company"}
