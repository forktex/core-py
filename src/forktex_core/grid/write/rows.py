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

"""Write-side row mechanics reused by :class:`~forktex_core.grid.write.write.RowWriter`.

These are the proven leaf operations the single write pipeline composes — the
column lifecycle hooks, the ``ref`` ⇄ ``grid_edge`` projection, and the two-pass
``on_delete`` deletion planner. Each is a small, isolated function; the pipeline
(create / patch / archive) sequences them inside one savepoint.

``ref`` columns store the target row's UUID in the payload, but payload is *not*
the only representation of a relationship: :func:`_sync_ref_edges` keeps
``grid_edge`` in step with the payload ref (validating the target exists, is live,
is in the relation's target table, and shares the namespace). That single edge
projection is what the graph, cardinality, and ``on_delete`` machinery act on — so
referential integrity holds regardless of which API a consumer uses.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core import iso
from forktex_core.grid.domain.enums import FieldType, OnDelete
from forktex_core.grid.domain.fieldtypes import WriteContext, get_field_type
from forktex_core.grid.errors import BadRequestError
from forktex_core.grid.persist import GridColumn, GridEdge, GridRelation, GridRow, GridTable
from forktex_core.grid.persist.repos import get_active_columns
from forktex_core.grid.write.relations import relate_rows
from forktex_core.types import JsonValue


async def fire_hooks(
    session: AsyncSession,
    *,
    columns: list[GridColumn],
    table_slug: str,
    row: GridRow,
    before: dict[str, Any] | None,
    archived: bool,
    changed: set[str] | None = None,
) -> None:
    """Fire per-column lifecycle hooks for the (just-staged) row mutation.

    Default hooks are no-ops; handlers that need write-time side effects (VECTOR
    embed, FILE upload) act here, inside the transaction. ``changed`` limits
    firing to the columns this mutation actually touched (all present, if None).
    """
    before = before or {}
    for col in columns:
        if changed is not None and col.key not in changed:
            continue
        if col.key not in row.payload:
            continue
        handler = get_field_type(col.type_id)
        config = handler.validate_config(col.config)
        ctx = WriteContext(
            session=session,
            namespace=row.namespace,
            table_id=row.table_id,
            table_slug=table_slug,
            column_key=col.key,
            row_id=row.id,
            before_value=before.get(col.key),
            after_value=None if archived else row.payload.get(col.key),
        )
        if archived:
            await handler.on_rows_archived([ctx], config=config)
        else:
            await handler.on_rows_written([ctx], config=config)


async def fire_hooks_batch(
    session: AsyncSession,
    *,
    columns: list[GridColumn],
    table_slug: str,
    rows: list[GridRow],
    archived: bool,
) -> None:
    """Fire lifecycle hooks once per column over the whole batch of rows.

    This is the throughput win the batch-shaped hook API was built for: one
    ``on_rows_written`` call carrying N contexts (e.g. a single embedding request
    for N vector cells) instead of N single-row calls.
    """
    for col in columns:
        handler = get_field_type(col.type_id)
        config = handler.validate_config(col.config)
        ctxs = [
            WriteContext(
                session=session,
                namespace=row.namespace,
                table_id=row.table_id,
                table_slug=table_slug,
                column_key=col.key,
                row_id=row.id,
                before_value=None,
                after_value=None if archived else row.payload.get(col.key),
            )
            for row in rows
            if col.key in row.payload
        ]
        if not ctxs:
            continue
        if archived:
            await handler.on_rows_archived(ctxs, config=config)
        else:
            await handler.on_rows_written(ctxs, config=config)


def _payload_ref_targets(raw: JsonValue) -> list[uuid.UUID]:
    """The set of target UUIDs a ref cell points at (one value or a list)."""
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    out: list[uuid.UUID] = []
    for v in values:
        if v is None:
            continue
        out.append(v if isinstance(v, uuid.UUID) else uuid.UUID(str(v)))
    return out


async def sync_ref_edges(
    session: AsyncSession,
    *,
    row: GridRow,
    columns: list[GridColumn],
    changed: set[str] | None,
) -> None:
    """Make ``grid_edge`` match the row's ``ref`` payload values.

    For each ref column (touched by this write), validate every target row
    (exists, live, right table, same namespace) then add/remove edges so the edge
    set equals the payload. This is what keeps ``on_delete`` / graph / cardinality
    honest — payload is the source of truth, edges the projection.
    """
    for col in columns:
        if col.type_id != FieldType.ref.value or col.relation_id is None:
            continue
        if changed is not None and col.key not in changed:
            continue
        relation = await session.get(GridRelation, col.relation_id)
        if relation is None:
            continue

        desired = _payload_ref_targets(row.payload.get(col.key))
        targets: dict[uuid.UUID, GridRow] = {}
        for tid in desired:
            target = await session.get(GridRow, tid)
            if target is None or target.archived_at is not None:
                raise BadRequestError(
                    f"ref '{col.key}' points at a row that does not exist",
                    details={"column": col.key, "target_row_id": str(tid)},
                )
            if target.table_id != relation.target_table_id:
                raise BadRequestError(
                    f"ref '{col.key}' points at a row in the wrong table",
                    details={"column": col.key, "target_row_id": str(tid)},
                )
            if target.namespace != row.namespace:
                raise BadRequestError(
                    f"ref '{col.key}' points at a row in a different namespace",
                    details={"column": col.key, "target_row_id": str(tid)},
                )
            targets[tid] = target

        existing = {
            edge.target_row_id: edge
            for edge in await session.scalars(
                sa.select(GridEdge).where(
                    GridEdge.relation_id == relation.id,
                    GridEdge.source_row_id == row.id,
                    GridEdge.archived_at.is_(None),
                )
            )
        }
        # Remove edges no longer referenced, then add new ones (this order lets a
        # re-pointed one_to_one/one_to_many pass its cardinality check).
        for tid, edge in existing.items():
            if tid not in targets:
                await session.delete(edge)
        await session.flush()
        for tid, target in targets.items():
            if tid not in existing:
                await relate_rows(session, relation=relation, source_row=row, target_row=target)


async def archive_row(session: AsyncSession, *, row: GridRow) -> GridRow:
    """Soft-delete a row, applying each relation's ``on_delete`` policy.

    The deletion planner runs in two passes so the whole archive is atomic: a
    read-only **plan** pass walks the cascade closure and validates every
    ``restrict`` (and every ``set_null`` on a required ref) *before* anything is
    mutated — so a violation raises with no partial write — then an **apply** pass
    drops edges, clears set-null refs, and flips rows to archived. Cycle-safe via
    the plan's visited set.
    """
    if row.archived_at is not None:
        return row

    plan = _ArchivePlan()
    await _plan_archive(session, row, plan)

    for edge in plan.edges_to_delete.values():
        await session.delete(edge)
    for relation, source_id in plan.refs_to_clear:
        await _clear_ref(session, relation, source_id)
    await session.flush()

    now = iso.now()
    for target in plan.rows_to_archive:
        real_columns = await get_active_columns(session, target.table_id)
        table = await session.get(GridTable, target.table_id)
        before = dict(target.payload)
        target.is_active = False
        target.archived_at = now
        await session.flush()
        await fire_hooks(
            session,
            columns=real_columns,
            table_slug=table.slug if table else "",
            row=target,
            before=before,
            archived=True,
        )
    return row


class _ArchivePlan:
    """The mutation set computed by the read-only planning pass."""

    def __init__(self) -> None:
        self.rows_to_archive: list[GridRow] = []
        self.edges_to_delete: dict[uuid.UUID, GridEdge] = {}
        self.refs_to_clear: list[tuple[GridRelation, uuid.UUID]] = []
        self._seen: set[uuid.UUID] = set()

    def mark(self, row: GridRow) -> bool:
        """Record a row for archival; returns False if already planned (cycle)."""
        if row.id in self._seen:
            return False
        self._seen.add(row.id)
        self.rows_to_archive.append(row)
        return True


async def _plan_archive(session: AsyncSession, row: GridRow, plan: _ArchivePlan) -> None:
    """Read-only: collect the archive closure + validate restrict/required refs.

    Raises ``BadRequestError`` (before any mutation) if a ``restrict`` relation
    references the row, or a ``set_null`` would null a *required* ref.
    """
    if not plan.mark(row):
        return

    # Relations where this row is a TARGET (something references it).
    inbound_relations = list(
        await session.scalars(
            sa.select(GridRelation).where(
                GridRelation.target_table_id == row.table_id, GridRelation.archived_at.is_(None)
            )
        )
    )
    for relation in inbound_relations:
        edges = list(
            await session.scalars(
                sa.select(GridEdge).where(
                    GridEdge.relation_id == relation.id,
                    GridEdge.target_row_id == row.id,
                    GridEdge.archived_at.is_(None),
                )
            )
        )
        if not edges:
            continue
        if relation.on_delete == OnDelete.restrict:
            raise BadRequestError(
                f"cannot archive row: referenced by relation '{relation.key}' (on_delete=restrict)",
                details={"relation_key": relation.key, "row_id": str(row.id)},
            )
        if relation.on_delete == OnDelete.set_null:
            column = await _ref_column_for_relation(session, relation)
            if column is not None and column.is_required:
                raise BadRequestError(
                    f"cannot archive row: relation '{relation.key}' would null required ref '{column.key}'",
                    details={"relation_key": relation.key, "column": column.key, "row_id": str(row.id)},
                )
        for edge in edges:
            if relation.on_delete == OnDelete.cascade:
                source = await session.get(GridRow, edge.source_row_id)
                if source is not None and source.archived_at is None:
                    await _plan_archive(session, source, plan)  # source's own edges get collected there
                else:
                    plan.edges_to_delete[edge.id] = edge
            elif relation.on_delete == OnDelete.set_null:
                plan.refs_to_clear.append((relation, edge.source_row_id))
                plan.edges_to_delete[edge.id] = edge

    # This row's own outgoing edges (it as SOURCE) are removed unconditionally.
    for edge in await session.scalars(
        sa.select(GridEdge).where(GridEdge.source_row_id == row.id, GridEdge.archived_at.is_(None))
    ):
        plan.edges_to_delete[edge.id] = edge


async def _ref_column_for_relation(session: AsyncSession, relation: GridRelation) -> GridColumn | None:
    return await session.scalar(
        sa.select(GridColumn).where(
            GridColumn.table_id == relation.source_table_id,
            GridColumn.relation_id == relation.id,
            GridColumn.archived_at.is_(None),
        )
    )


async def _clear_ref(session: AsyncSession, relation: GridRelation, source_row_id: uuid.UUID) -> None:
    """Null out the (optional) ref column on the source row projecting this relation.

    The planner has already guaranteed the column is not required, so writing
    ``None`` yields a valid payload.
    """
    column = await _ref_column_for_relation(session, relation)
    if column is None:
        return
    source = await session.get(GridRow, source_row_id)
    if source is not None and column.key in source.payload:
        source.payload = {**source.payload, column.key: None}


__all__ = ["archive_row", "fire_hooks", "fire_hooks_batch", "sync_ref_edges"]
