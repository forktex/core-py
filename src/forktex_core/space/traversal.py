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

"""Cross-Grid traversal helpers for ``Bundle``.

The model: pull every active row + every active inter-row edge from a
Bundle's member Grids into an in-memory ``[graph].Graph``, then apply
the algebra (BFS / DFS / closure / cycle detection) from the graph
extra. The persisted recursive-CTE traversal in ``[grid].graph`` walks
a single Grid's edges; this module spans an arbitrary bundle.

Cost: O(N + E) load time per call, where N is the row count across
member Grids and E is the relation-row count. Snapshot semantics —
the graph is a frozen view of the Bundle at the moment of the call.
For high-cardinality Spaces, narrow the scope by passing
``entity_slugs`` (only those Grids' rows are loaded) or by post-
filtering with ``[graph].subgraph_around``.

Edges encode their source ``relation.key`` as the in-memory
``GraphEdge.kind`` (e.g., ``"contains"`` / ``"depends_on"``) so
filter-by-kind algebra works out of the box.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING

import sqlalchemy as sa

from forktex_core.graph import Graph, GraphEdge, GraphNode, edge_id
from forktex_core.grid.persist import (
    GridEdge,
    GridRelation,
    GridRow,
)

if TYPE_CHECKING:
    from forktex_core.space.bundle import Bundle


async def bundle_to_graph(
    space: Bundle,
    *,
    entity_slugs: Iterable[str] | None = None,
    include_inactive: bool = False,
) -> Graph:
    """Materialise a snapshot ``Graph`` covering this Bundle's member
    rows and the row-level edges connecting them.

    ``entity_slugs`` narrows the scope to a subset of the Bundle's
    member Grids (defaults to all). ``include_inactive`` controls
    whether soft-deleted rows / edges flow into the snapshot — by
    default they're skipped.

    Each ``GraphNode`` carries:
      - ``id``    = ``str(row.id)``
      - ``kind``  = entity slug (e.g., ``"leads"``)
      - ``name``  = ``str(row.id)`` (consumers project a friendlier
                    name from row.data themselves)
      - ``attrs`` = ``{"namespace": ..., "entity_slug": ...}``

    Each ``GraphEdge`` carries:
      - ``kind``  = ``relation.key`` (e.g., ``"contains"``)
      - ``attrs`` = ``{"relation_id": ...}``
    """
    session = space.session
    grids = list(space.grids.values())
    if entity_slugs is not None:
        wanted = set(entity_slugs)
        grids = [g for g in grids if g.slug in wanted]
    if not grids:
        return Graph.empty()

    entity_ids = [g.ref.id for g in grids]
    slug_by_entity_id = {g.ref.id: g.slug for g in grids}

    row_stmt = sa.select(GridRow).where(
        GridRow.table_id.in_(entity_ids),
        GridRow.namespace == space.namespace,
    )
    if not include_inactive:
        row_stmt = row_stmt.where(GridRow.is_active.is_(True))
    rows: list[GridRow] = list((await session.execute(row_stmt)).scalars().all())

    graph = Graph.empty()
    row_ids: set[uuid.UUID] = set()
    for row in rows:
        slug = slug_by_entity_id.get(row.table_id, "unknown")
        graph.add_node(
            GraphNode(
                id=str(row.id),
                kind=slug,
                name=str(row.id),
                attrs={"namespace": row.namespace, "entity_slug": slug},
            )
        )
        row_ids.add(row.id)

    if not row_ids:
        return graph

    # Only inter-row edges with both endpoints in scope.
    edge_stmt = (
        sa.select(GridEdge, GridRelation)
        .join(GridRelation, GridEdge.relation_id == GridRelation.id)
        .where(
            GridEdge.namespace == space.namespace,
            GridEdge.source_row_id.in_(row_ids),
            GridEdge.target_row_id.in_(row_ids),
        )
    )
    if not include_inactive:
        edge_stmt = edge_stmt.where(GridEdge.is_active.is_(True))

    rows_seen: set[str] = {n.id for n in graph.nodes}
    for edge_row, relation in (await session.execute(edge_stmt)).all():
        src = str(edge_row.source_row_id)
        dst = str(edge_row.target_row_id)
        if src not in rows_seen or dst not in rows_seen:
            continue
        attrs: dict[str, object] = {"relation_id": str(relation.id)}
        # We construct the GraphEdge directly (rather than going through
        # ``Graph.add_edge``) because attrs may contain non-string
        # values that the deterministic id helper handles correctly,
        # but the dedup rule still applies on the computed id.
        eid = edge_id(relation.key, src, dst, attrs)
        if eid in {e.id for e in graph.edges}:
            continue
        graph.edges.append(GraphEdge(id=eid, kind=relation.key, src_id=src, dst_id=dst, attrs=attrs))
    # Force a re-index next access to pick up the appended edges.
    graph._indexed = False
    return graph


__all__ = ["bundle_to_graph"]
