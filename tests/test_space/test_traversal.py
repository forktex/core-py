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

"""Cross-Grid traversal: Bundle.to_graph + Bundle.traverse."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import forktex_core.space  # noqa: F401  ensure rich handlers registered
from forktex_core.grid import (
    FieldType,
    Grid,
    OnDelete,
    TableSpec,
    apply_migrations,
)
from forktex_core.grid.domain.enums import RelationShape
from forktex_core.grid.persist import GridEdge, GridRelation
from forktex_core.graph import bfs, transitive_closure
from forktex_core.space import Bundle

_SCHEMA = "forktex_grid"


@pytest_asyncio.fixture
async def trv_session(postgres_url_str: str, fresh_schema: str):
    engine = create_async_engine(
        postgres_url_str,
        execution_options={"schema_translate_map": {_SCHEMA: fresh_schema}},
    )
    await apply_migrations(engine, schema=fresh_schema)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed_two_grids_with_edges(session: AsyncSession):
    """Two member Grids:
        leads (3 rows: a, b, c)
        notes (2 rows: x, y)
    Relations:
        leads -> notes (key="has_note") with edges  a→x, b→y
        leads -> leads (key="parent_of")           a→b, b→c
    Returns (space, leads_grid, notes_grid, ids_dict).
    """
    ns = str(uuid.uuid4())
    leads = await Grid.declare(
        session,
        TableSpec.from_dicts(
            slug="leads",
            label="Leads",
            namespace=ns,
            columns=[{"key": "title", "label": "Title", "type_id": FieldType.text.value}],
        ),
    )
    notes = await Grid.declare(
        session,
        TableSpec.from_dicts(
            slug="notes",
            label="Notes",
            namespace=ns,
            columns=[{"key": "body", "label": "Body", "type_id": FieldType.text.value}],
        ),
    )
    space = await Bundle.declare(session, namespace=ns, slug="bundle", members=[leads, notes])
    await session.flush()

    # Rows
    a = await leads.create({"title": "A"})
    b = await leads.create({"title": "B"})
    c = await leads.create({"title": "C"})
    x = await notes.create({"body": "X"})
    y = await notes.create({"body": "Y"})

    # Cross-Grid relation: leads → notes. on_delete=set_null so archiving an
    # endpoint just drops the edges (there are no ref columns to clear here).
    rel_has_note = GridRelation(
        namespace=ns,
        source_table_id=leads.ref.id,
        target_table_id=notes.ref.id,
        key="has_note",
        relation_type=RelationShape.one_to_many,
        on_delete=OnDelete.set_null,
    )
    # Same-Grid relation: leads → leads (parent of)
    rel_parent = GridRelation(
        namespace=ns,
        source_table_id=leads.ref.id,
        target_table_id=leads.ref.id,
        key="parent_of",
        relation_type=RelationShape.one_to_many,
        on_delete=OnDelete.set_null,
    )
    session.add(rel_has_note)
    session.add(rel_parent)
    await session.flush()

    session.add_all(
        [
            GridEdge(namespace=ns, relation_id=rel_has_note.id, source_row_id=a.id, target_row_id=x.id),
            GridEdge(namespace=ns, relation_id=rel_has_note.id, source_row_id=b.id, target_row_id=y.id),
            GridEdge(namespace=ns, relation_id=rel_parent.id, source_row_id=a.id, target_row_id=b.id),
            GridEdge(namespace=ns, relation_id=rel_parent.id, source_row_id=b.id, target_row_id=c.id),
        ]
    )
    await session.flush()
    return space, leads, notes, {"a": a.id, "b": b.id, "c": c.id, "x": x.id, "y": y.id}


@pytest.mark.asyncio
async def test_to_graph_includes_all_member_rows_and_edges(trv_session: AsyncSession):
    space, _, _, ids = await _seed_two_grids_with_edges(trv_session)
    graph = await space.to_graph()

    expected_node_ids = {str(v) for v in ids.values()}
    assert {n.id for n in graph.nodes} == expected_node_ids

    edge_kinds = sorted(e.kind for e in graph.edges)
    assert edge_kinds == ["has_note", "has_note", "parent_of", "parent_of"]


@pytest.mark.asyncio
async def test_to_graph_filters_by_entity_slugs(trv_session: AsyncSession):
    space, _, _, ids = await _seed_two_grids_with_edges(trv_session)
    leads_only = await space.to_graph(entity_slugs=["leads"])

    expected_lead_ids = {str(ids["a"]), str(ids["b"]), str(ids["c"])}
    assert {n.id for n in leads_only.nodes} == expected_lead_ids
    # Cross-Grid edges (has_note) must drop because their note endpoints
    # are out of scope.
    edge_kinds = sorted(e.kind for e in leads_only.edges)
    assert edge_kinds == ["parent_of", "parent_of"]


@pytest.mark.asyncio
async def test_traverse_returns_reachable_subgraph(trv_session: AsyncSession):
    space, _, _, ids = await _seed_two_grids_with_edges(trv_session)
    sub = await space.traverse(ids["a"], max_depth=1, direction="out")

    # From a: reach b (parent_of) and x (has_note) at depth 1.
    assert {n.id for n in sub.nodes} == {str(ids["a"]), str(ids["b"]), str(ids["x"])}


@pytest.mark.asyncio
async def test_traverse_filters_by_edge_kind(trv_session: AsyncSession):
    space, _, _, ids = await _seed_two_grids_with_edges(trv_session)
    sub = await space.traverse(ids["a"], max_depth=3, direction="out", edge_kind="parent_of")

    # parent_of only: a → b → c chain.
    assert {n.id for n in sub.nodes} == {str(ids["a"]), str(ids["b"]), str(ids["c"])}


@pytest.mark.asyncio
async def test_algebra_works_over_snapshot(trv_session: AsyncSession):
    space, _, _, ids = await _seed_two_grids_with_edges(trv_session)
    graph = await space.to_graph()

    closure = transitive_closure(graph, str(ids["a"]))
    # From a (out direction default): a → b (parent_of) → c (parent_of) and y (has_note);
    # plus a → x directly (has_note).
    assert closure == {
        str(ids["a"]),
        str(ids["b"]),
        str(ids["c"]),
        str(ids["x"]),
        str(ids["y"]),
    }

    # BFS from a should hit b before c.
    order = bfs(graph, str(ids["a"]))
    assert order.index(str(ids["b"])) < order.index(str(ids["c"]))


@pytest.mark.asyncio
async def test_to_graph_skips_inactive_by_default(trv_session: AsyncSession):
    space, leads, _, ids = await _seed_two_grids_with_edges(trv_session)
    await leads.archive(ids["b"])
    await trv_session.flush()

    graph = await space.to_graph()
    assert str(ids["b"]) not in {n.id for n in graph.nodes}

    full = await space.to_graph(include_inactive=True)
    assert str(ids["b"]) in {n.id for n in full.nodes}
