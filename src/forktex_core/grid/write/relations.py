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

"""Relating rows — materialised edges with enforced cardinality.

``relate_rows`` validates the endpoints belong to the relation's tables and
share a namespace, then enforces cardinality at the application layer:

- ``one_to_one`` — neither endpoint may already participate in this relation.
- ``one_to_many`` — each target may belong to at most one source.
- ``many_to_one`` — each source may reference at most one target (the canonical
  foreign key: many rows → one target).
- ``many_to_many`` — only the (relation, source, target) triple is unique.

This layer gives friendly, typed errors; DB-level partial unique indexes on
``grid_edge`` (emitted by :func:`~forktex_core.grid.persist.reconcile.cardinality.reconcile_relation_cardinality`)
enforce the same cardinality under concurrency.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.database.integrity import integrity_boundary
from forktex_core.error import AlreadyExistsError, BadRequestError, NotFoundError
from forktex_core.grid.domain.enums import RelationShape
from forktex_core.grid.persist import GridEdge, GridRelation, GridRow


async def relate_rows(
    session: AsyncSession,
    *,
    relation: GridRelation,
    source_row: GridRow,
    target_row: GridRow,
    payload: dict[str, Any] | None = None,
) -> GridEdge:
    _validate_endpoints(relation, source_row, target_row)
    await _enforce_cardinality(session, relation, source_row, target_row)
    edge = GridEdge(
        relation_id=relation.id,
        # Stamp the edge with the rows' namespace (validated equal above), not the
        # relation's — otherwise an edge between rows in a namespace other than the
        # relation's would be invisible to namespace-scoped graph traversal.
        namespace=source_row.namespace,
        source_row_id=source_row.id,
        target_row_id=target_row.id,
        payload=payload or {},
    )
    session.add(edge)
    async with integrity_boundary():
        await session.flush()
    return edge


async def unrelate_rows(
    session: AsyncSession,
    *,
    relation: GridRelation,
    source_row: GridRow,
    target_row: GridRow,
) -> None:
    edge = await session.scalar(
        sa.select(GridEdge).where(
            GridEdge.relation_id == relation.id,
            GridEdge.source_row_id == source_row.id,
            GridEdge.target_row_id == target_row.id,
            GridEdge.archived_at.is_(None),
        )
    )
    if edge is None:
        raise NotFoundError("Edge not found")
    await session.delete(edge)
    await session.flush()


async def list_related(
    session: AsyncSession,
    *,
    relation: GridRelation,
    source_row: GridRow,
) -> list[GridRow]:
    """Target rows linked to ``source_row`` under ``relation``."""
    stmt = (
        sa.select(GridRow)
        .join(GridEdge, GridEdge.target_row_id == GridRow.id)
        .where(
            GridEdge.relation_id == relation.id,
            GridEdge.source_row_id == source_row.id,
            GridEdge.archived_at.is_(None),
            GridRow.archived_at.is_(None),
        )
        .order_by(GridRow.created_at)
    )
    return list(await session.scalars(stmt))


def _validate_endpoints(relation: GridRelation, source_row: GridRow, target_row: GridRow) -> None:
    if source_row.table_id != relation.source_table_id:
        raise BadRequestError("source_row does not belong to the relation's source table")
    if target_row.table_id != relation.target_table_id:
        raise BadRequestError("target_row does not belong to the relation's target table")
    if source_row.namespace != target_row.namespace:
        raise BadRequestError("cannot relate rows across namespaces")


async def _enforce_cardinality(
    session: AsyncSession,
    relation: GridRelation,
    source_row: GridRow,
    target_row: GridRow,
) -> None:
    if await _count_edges(session, relation.id, source_row_id=source_row.id, target_row_id=target_row.id):
        raise AlreadyExistsError("rows are already related")

    if relation.relation_type == RelationShape.many_to_many:
        return
    if relation.relation_type == RelationShape.one_to_one:
        if await _count_edges(session, relation.id, source_row_id=source_row.id):
            raise BadRequestError("source already has a related row (one_to_one)")
        if await _count_edges(session, relation.id, target_row_id=target_row.id):
            raise BadRequestError("target already has a related row (one_to_one)")
    elif relation.relation_type == RelationShape.one_to_many:
        # one source → many targets; each target belongs to at most one source.
        if await _count_edges(session, relation.id, target_row_id=target_row.id):
            raise BadRequestError("target already belongs to a source (one_to_many)")
    elif relation.relation_type == RelationShape.many_to_one:
        # many sources → one target; each source references at most one target.
        if await _count_edges(session, relation.id, source_row_id=source_row.id):
            raise BadRequestError("source already references a target (many_to_one)")


async def _count_edges(
    session: AsyncSession,
    relation_id: uuid.UUID,
    *,
    source_row_id: uuid.UUID | None = None,
    target_row_id: uuid.UUID | None = None,
) -> int:
    stmt = (
        sa.select(sa.func.count())
        .select_from(GridEdge)
        .where(GridEdge.relation_id == relation_id, GridEdge.archived_at.is_(None))
    )
    if source_row_id is not None:
        stmt = stmt.where(GridEdge.source_row_id == source_row_id)
    if target_row_id is not None:
        stmt = stmt.where(GridEdge.target_row_id == target_row_id)
    return await session.scalar(stmt) or 0


__all__ = ["list_related", "relate_rows", "unrelate_rows"]
