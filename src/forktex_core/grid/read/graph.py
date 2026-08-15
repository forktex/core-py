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

"""Relation-graph traversal over ``grid_edge``.

``neighbors`` / ``traverse`` / ``subgraph`` walk the materialised edges between
rows. Traversal is a batched breadth-first walk (one query per depth level over
the whole frontier), which keeps it schema-translate-safe (pure ORM, no raw
DDL) and computes a correct shortest-path depth for diamond/multi-path graphs.
``Direction`` selects edge orientation; ``relation_keys`` filters by relation.
"""

from __future__ import annotations

import enum
import uuid

import sqlalchemy as sa
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid.persist import GridEdge, GridRelation
from forktex_core.types import BaseValueObject


class Direction(enum.StrEnum):
    outbound = "outbound"  # follow source → target
    inbound = "inbound"  # follow target → source
    both = "both"


class GraphEdge(BaseValueObject):
    """A single traversed relation edge.

    Distinct from the unrelated ``forktex_core.graph.models.GraphEdge`` —
    same name, different module, different fields; never imported together.
    """

    relation_id: uuid.UUID
    relation_key: str
    source_row_id: uuid.UUID
    target_row_id: uuid.UUID


class TraversalResult(BaseValueObject):
    """Reachable nodes (with shortest-path depth) + the edges traversed."""

    depth: dict[uuid.UUID, int] = Field(default_factory=dict)
    edges: list[GraphEdge] = Field(default_factory=list)

    @property
    def nodes(self) -> set[uuid.UUID]:
        return set(self.depth)


async def _edges_from(
    session: AsyncSession,
    *,
    frontier: set[uuid.UUID],
    namespace: str,
    direction: Direction,
    relation_keys: list[str] | None,
) -> list[GraphEdge]:
    stmt = (
        sa.select(
            GridEdge.relation_id,
            GridRelation.key,
            GridEdge.source_row_id,
            GridEdge.target_row_id,
        )
        .join(GridRelation, GridRelation.id == GridEdge.relation_id)
        .where(GridEdge.namespace == namespace, GridEdge.archived_at.is_(None))
    )
    ids = list(frontier)
    if direction == Direction.outbound:
        stmt = stmt.where(GridEdge.source_row_id.in_(ids))
    elif direction == Direction.inbound:
        stmt = stmt.where(GridEdge.target_row_id.in_(ids))
    else:
        stmt = stmt.where(sa.or_(GridEdge.source_row_id.in_(ids), GridEdge.target_row_id.in_(ids)))
    if relation_keys:
        stmt = stmt.where(GridRelation.key.in_(relation_keys))
    rows = await session.execute(stmt)
    return [GraphEdge(relation_id=r[0], relation_key=r[1], source_row_id=r[2], target_row_id=r[3]) for r in rows]


def _other_endpoints(edge: GraphEdge, frontier: set[uuid.UUID], direction: Direction) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    if direction in (Direction.outbound, Direction.both) and edge.source_row_id in frontier:
        out.append(edge.target_row_id)
    if direction in (Direction.inbound, Direction.both) and edge.target_row_id in frontier:
        out.append(edge.source_row_id)
    return out


async def neighbors(
    session: AsyncSession,
    *,
    row_id: uuid.UUID,
    namespace: str,
    direction: Direction = Direction.both,
    relation_keys: list[str] | None = None,
) -> list[uuid.UUID]:
    """Directly-adjacent rows of ``row_id`` under ``direction``."""
    edges = await _edges_from(
        session, frontier={row_id}, namespace=namespace, direction=direction, relation_keys=relation_keys
    )
    seen: dict[uuid.UUID, None] = {}
    for edge in edges:
        for node in _other_endpoints(edge, {row_id}, direction):
            if node != row_id:  # a self-loop is not its own neighbour
                seen.setdefault(node, None)
    return list(seen)


async def traverse(
    session: AsyncSession,
    *,
    start_row_id: uuid.UUID,
    namespace: str,
    direction: Direction = Direction.both,
    max_depth: int = 3,
    relation_keys: list[str] | None = None,
) -> TraversalResult:
    """Breadth-first walk from ``start_row_id`` up to ``max_depth`` hops.

    Records each reachable node's shortest-path depth and every traversed edge.
    Cycle-safe (visited set) and correct for diamonds (first-visit depth wins).
    """
    result = TraversalResult(depth={start_row_id: 0})
    frontier = {start_row_id}
    seen_edges: set[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = set()
    for current_depth in range(max_depth):
        if not frontier:
            break
        edges = await _edges_from(
            session, frontier=frontier, namespace=namespace, direction=direction, relation_keys=relation_keys
        )
        next_frontier: set[uuid.UUID] = set()
        for edge in edges:
            key = (edge.relation_id, edge.source_row_id, edge.target_row_id)
            if key not in seen_edges:
                seen_edges.add(key)
                result.edges.append(edge)
            for node in _other_endpoints(edge, frontier, direction):
                if node not in result.depth:
                    result.depth[node] = current_depth + 1
                    next_frontier.add(node)
        frontier = next_frontier
    return result


async def subgraph(
    session: AsyncSession,
    *,
    row_ids: set[uuid.UUID],
    namespace: str,
    relation_keys: list[str] | None = None,
) -> list[GraphEdge]:
    """All edges whose BOTH endpoints lie within ``row_ids`` (the induced subgraph)."""
    if not row_ids:
        return []
    edges = await _edges_from(
        session, frontier=row_ids, namespace=namespace, direction=Direction.both, relation_keys=relation_keys
    )
    return [e for e in edges if e.source_row_id in row_ids and e.target_row_id in row_ids]


__all__ = ["Direction", "GraphEdge", "TraversalResult", "neighbors", "subgraph", "traverse"]
