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

"""Grid 4.0 conformance — the curated ``Grid`` API driven end-to-end on real Postgres.

Proves the layered/strategy architecture behaves: owned CRUD + query (filter/sort/
offset/cursor/total), promoted dual-write, the ONE compiler over a bound overlay, and
relations — all through the small public surface.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid import (
    BrowseMode,
    ColumnSpec,
    Grid,
    Materialization,
    Overlay,
    ReadOnlyStorage,
    RelationShape,
    RelationSpec,
    TableSpec,
    declare_relation,
)
from forktex_core.grid.persist.reconcile import sidecar_table_name


def _cols(*specs: tuple[str, str]) -> tuple[ColumnSpec, ...]:
    return tuple(ColumnSpec(key=k, label=k.title(), type_id=t) for k, t in specs)


async def _people(session: AsyncSession, ns: str = "") -> Grid:
    return await Grid.declare(
        session,
        TableSpec(slug="people", label="People", namespace=ns, columns=_cols(("name", "text"), ("age", "integer"))),
    )


# ── owned CRUD + query ───────────────────────────────────────────────────────


async def test_declare_create_and_query(session: AsyncSession) -> None:
    g = await _people(session)
    a = await g.create({"name": "Ann", "age": 30})
    await g.create_many([{"name": "Bob", "age": 25}, {"name": "Cid", "age": 40}])

    assert (await g.get(a.id)).values == {"name": "Ann", "age": 30}
    page = await g.query(
        filter={"column": "age", "op": "gte", "value": 30}, sort=[{"column": "name"}], include_total=True
    )
    assert [r.values["name"] for r in page.rows] == ["Ann", "Cid"]
    assert page.total == 2


async def test_patch_and_archive(session: AsyncSession) -> None:
    g = await _people(session)
    r = await g.create({"name": "Ann", "age": 30})
    await g.patch(r.id, {"age": 31})
    assert (await g.get(r.id)).values["age"] == 31
    await g.archive(r.id)
    assert (await g.query()).rows == []


async def test_required_and_unknown_column_rejected(session: AsyncSession) -> None:
    g = await Grid.declare(
        session,
        TableSpec(slug="t", label="T", columns=(ColumnSpec(key="name", label="N", type_id="text", is_required=True),)),
    )
    with pytest.raises(Exception):  # noqa: B017 — BadRequestError family
        await g.create({})
    with pytest.raises(Exception):  # noqa: B017
        await g.create({"name": "x", "nope": 1})


async def test_cursor_pagination(session: AsyncSession) -> None:
    g = await _people(session)
    await g.create_many([{"name": n, "age": i} for i, n in enumerate(["a", "b", "c", "d", "e"])])
    p1 = await g.query(sort=[{"column": "name"}], mode=BrowseMode.cursor, limit=2)
    assert [r.values["name"] for r in p1.rows] == ["a", "b"] and p1.next_cursor
    p2 = await g.query(sort=[{"column": "name"}], mode=BrowseMode.cursor, limit=2, cursor=p1.next_cursor)
    assert [r.values["name"] for r in p2.rows] == ["c", "d"]


# ── promoted ─────────────────────────────────────────────────────────────────


async def test_promoted_dual_write_and_query(session: AsyncSession, grid_schema: str) -> None:
    g = await Grid.declare(
        session,
        TableSpec(
            slug="acct",
            label="Acct",
            columns=(
                ColumnSpec(key="balance", label="Balance", type_id="integer", materialization=Materialization.promoted),
            ),
        ),
    )
    await g.reconcile()
    r = await g.create({"balance": 100})
    # payload query works...
    assert [
        x.values["balance"] for x in (await g.query(filter={"column": "balance", "op": "gte", "value": 50})).rows
    ] == [100]
    # ...and the native sidecar mirrors it.
    side = sidecar_table_name(g.ref.id)
    mirrored = await session.scalar(
        sa.text(f'SELECT balance FROM "{grid_schema}"."{side}" WHERE row_id = :r'), {"r": r.id}
    )
    assert mirrored == 100


# ── the ONE compiler over a bound overlay ────────────────────────────────────


async def test_bound_overlay_shares_the_compiler(session: AsyncSession, grid_schema: str) -> None:
    org = uuid.uuid4()
    await session.execute(
        sa.text(f'CREATE TABLE "{grid_schema}".host_co (id uuid PRIMARY KEY, org_id uuid, name text, score integer)')
    )
    for name, score in [("Acme", 10), ("Beta", 20), ("Acme Labs", 30)]:
        await session.execute(
            sa.text(f'INSERT INTO "{grid_schema}".host_co VALUES (:i, :o, :n, :s)'),
            {"i": uuid.uuid4(), "o": org, "n": name, "s": score},
        )
    g = await Grid.declare(
        session,
        TableSpec(
            slug="company",
            label="Company",
            namespace=str(org),
            binding=Overlay(physical_relation=f"{grid_schema}.host_co", namespace_column="org_id"),
            columns=_cols(("name", "text"), ("score", "integer")),
        ),
    )
    assert not g.writable
    with pytest.raises(ReadOnlyStorage):
        await g.create({"name": "x"})
    # filter (LIKE) + sort + between all go through the same compiler as owned tables
    like = await g.query(filter={"column": "name", "op": "icontains", "value": "acme"}, sort=[{"column": "score"}])
    assert [r.values["name"] for r in like.rows] == ["Acme", "Acme Labs"]
    btw = await g.query(filter={"column": "score", "op": "between", "value": [15, 30]}, include_total=True)
    assert btw.total == 2


# ── relations ────────────────────────────────────────────────────────────────


async def test_relations_relate_and_list(session: AsyncSession) -> None:
    await Grid.declare(session, TableSpec(slug="child", label="Child", columns=_cols(("name", "text"))))
    parent = await Grid.declare(session, TableSpec(slug="parent", label="Parent", columns=_cols(("name", "text"))))
    child = await Grid.open(session, slug="child")
    await declare_relation(
        session, RelationSpec(key="parent", source="child", target="parent", shape=RelationShape.many_to_one)
    )
    p = await parent.create({"name": "P"})
    c = await child.create({"name": "C"})
    await child.relate("parent", c.id, p.id)
    assert [r.values["name"] for r in await child.related("parent", c.id)] == ["P"]


# ── derived (the third materialization) + schema evolution (add_column) ────────


async def test_derived_resolves_through_a_ref(session: AsyncSession) -> None:
    company = await Grid.declare(session, TableSpec(slug="company", label="Company", columns=_cols(("tier", "text"))))
    deal = await Grid.declare(session, TableSpec(slug="deal", label="Deal", columns=_cols(("name", "text"))))
    await declare_relation(
        session, RelationSpec(key="company", source="deal", target="company", shape=RelationShape.many_to_one)
    )
    # Schema evolution: add the ref + derived columns after the relation exists.
    await deal.add_column(ColumnSpec(key="company", label="Company", type_id="ref", relation_ref="company"))
    await deal.add_column(
        ColumnSpec(
            key="co_tier",
            label="Company Tier",
            type_id="text",
            materialization=Materialization.derived,
            derived_source="company.tier",
        )
    )
    co = await company.create({"tier": "gold"})
    d = await deal.create({"name": "D1", "company": str(co.id)})

    row = next(r for r in (await deal.query()).rows if r.id == d.id)
    assert row.values["co_tier"] == "gold"  # resolved read-side via the ref auto-join


# ── on_delete (cascade) via the deletion planner ──────────────────────────────


async def test_on_delete_cascade(session: AsyncSession) -> None:
    from forktex_core.grid import OnDelete

    parent = await Grid.declare(session, TableSpec(slug="parent", label="Parent", columns=_cols(("name", "text"))))
    child = await Grid.declare(session, TableSpec(slug="child", label="Child", columns=_cols(("name", "text"))))
    await declare_relation(
        session,
        RelationSpec(
            key="parent", source="child", target="parent", shape=RelationShape.many_to_one, on_delete=OnDelete.cascade
        ),
    )
    await child.add_column(ColumnSpec(key="parent", label="Parent", type_id="ref", relation_ref="parent"))
    p = await parent.create({"name": "P"})
    await child.create({"name": "C", "parent": str(p.id)})
    await parent.archive(p.id)  # cascade archives the child
    assert (await child.query()).rows == []


# ── introspection / graph / numbering ─────────────────────────────────────────


async def test_describe_and_numbering(session: AsyncSession) -> None:
    g = await _people(session)
    info = await g.describe()
    assert info.slug == "people" and g.writable
    assert {c.key for c in info.columns} == {"name", "age"}
    assert (await g.next_number("invoice")) == 1
    assert (await g.next_number("invoice")) == 2  # strictly gapless


async def test_grid_schema_evolution_methods(session: AsyncSession) -> None:
    g = await _people(session)
    row = await g.create({"name": "Ann", "age": 30})

    await g.alter_column(ColumnSpec(key="name", label="Full name", type_id="text", is_required=True))
    await g.rename_column("age", "years")
    await g.drop_column("name")

    keys = {c.key for c in (await g.describe()).columns}
    assert keys == {"years"}
    # rename migrated the payload value
    assert (await g.get(row.id)).values.get("years") == 30


async def test_traverse_over_relations(session: AsyncSession) -> None:
    await Grid.declare(session, TableSpec(slug="node", label="Node", columns=_cols(("name", "text"))))
    node = await Grid.open(session, slug="node")
    await Grid.declare(session, TableSpec(slug="link", label="Link"))
    await declare_relation(
        session,
        RelationSpec(key="link", source="node", target="node", shape=RelationShape.many_to_many, through="link"),
    )
    n = [await node.create({"name": f"n{i}"}) for i in range(3)]
    await node.relate("link", n[0].id, n[1].id)
    await node.relate("link", n[1].id, n[2].id)
    depth = await node.traverse(n[0].id, direction="outbound", depth=3)
    assert depth[n[0].id] == 0 and depth[n[1].id] == 1 and depth[n[2].id] == 2
