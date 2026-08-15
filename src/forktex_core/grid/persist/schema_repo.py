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

"""Namespace-wide schema hydration — load the whole configuration into one ``Schema``.

This is the first step of the JSON management flow: fetch every schema object in a namespace
(tables + their columns, relations, indexes, spaces) in a handful of batched queries and
assemble the in-memory :class:`Schema`. It generalises ``repos.load_table`` (single table)
to the whole namespace, resolving all ref→relation keys once instead of per column.
"""

from __future__ import annotations

import uuid
from collections import defaultdict

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.database.integrity import integrity_boundary
from forktex_core.grid.domain.binding import binding_to_json
from forktex_core.grid.domain.enums import Materialization, OnDelete, RelationShape
from forktex_core.grid.domain.schema import Schema
from forktex_core.grid.domain.spec import ColumnSpec, IndexSpec, RelationSpec, TableSpec
from forktex_core.grid.errors import BadRequestError, NotFoundError
from forktex_core.grid.identifiers import validate_key, validate_slug
from forktex_core.grid.persist import GridColumn, GridIndex, GridRelation, GridRow, GridTable
from forktex_core.grid.persist.repos import table_spec_from_orm
from forktex_core.iso import now
from forktex_core.log import get_logger

logger = get_logger(__name__)


async def hydrate(session: AsyncSession, namespace: str = "") -> Schema:
    """Load the entire namespace into a :class:`Schema` (deterministically ordered)."""
    tables = list(
        await session.scalars(
            sa.select(GridTable)
            .where(GridTable.namespace == namespace, GridTable.archived_at.is_(None))
            .order_by(GridTable.slug)
        )
    )
    relations = list(
        await session.scalars(
            sa.select(GridRelation)
            .where(GridRelation.namespace == namespace, GridRelation.archived_at.is_(None))
            .order_by(GridRelation.key)
        )
    )
    slug_by_id = {t.id: t.slug for t in tables}
    rel_key_by_id = {r.id: r.key for r in relations}
    table_ids = list(slug_by_id)

    columns = (
        list(
            await session.scalars(
                sa.select(GridColumn)
                .where(GridColumn.table_id.in_(table_ids), GridColumn.archived_at.is_(None))
                .order_by(GridColumn.display_order, GridColumn.created_at)
            )
        )
        if table_ids
        else []
    )
    cols_by_table: dict = defaultdict(list)
    for col in columns:
        cols_by_table[col.table_id].append(col)

    indexes = (
        list(
            await session.scalars(
                sa.select(GridIndex)
                .where(GridIndex.table_id.in_(table_ids), GridIndex.archived_at.is_(None))
                .order_by(GridIndex.index_kind)
            )
        )
        if table_ids
        else []
    )
    index_specs: dict[str, list[IndexSpec]] = defaultdict(list)
    for idx in indexes:
        slug = slug_by_id.get(idx.table_id)
        if slug is None:
            continue
        index_specs[slug].append(
            IndexSpec(
                column_keys=tuple(idx.column_keys),
                index_kind=idx.index_kind,
                is_unique=idx.is_unique,
            )
        )

    relation_specs: dict[str, RelationSpec] = {}
    for r in relations:
        # Skip a relation orphaned by a soft-archived endpoint (no live slug to project).
        if r.source_table_id not in slug_by_id or r.target_table_id not in slug_by_id:
            continue
        relation_specs[r.key] = RelationSpec(
            key=r.key,
            source=slug_by_id[r.source_table_id],
            target=slug_by_id[r.target_table_id],
            shape=RelationShape(str(r.relation_type)),
            through=slug_by_id.get(r.through_table_id) if r.through_table_id else None,
            on_delete=OnDelete(str(r.on_delete)),
        )

    return Schema(
        namespace=namespace,
        tables={t.slug: table_spec_from_orm(t, cols_by_table.get(t.id, []), rel_key_by_id) for t in tables},
        relations=relation_specs,
        indexes={slug: tuple(specs) for slug, specs in index_specs.items()},
    )


# "Drop" is a soft archive (is_active=False, archived_at=now) — reversible, and the physical
# reconcilers already tear down the now-orphaned index/sidecar. These primitives touch only
# the schema rows (and, for a column rename, the payload key); the caller (the reconciler)
# runs the physical reconcile tail afterwards, inside the same transaction.


def _archive(obj: object) -> None:
    obj.is_active = False  # type: ignore[attr-defined]
    obj.archived_at = now()  # type: ignore[attr-defined]


async def _table_orm(session: AsyncSession, slug: str, namespace: str) -> GridTable:
    orm = await session.scalar(
        sa.select(GridTable).where(
            GridTable.slug == slug, GridTable.namespace == namespace, GridTable.archived_at.is_(None)
        )
    )
    if orm is None:
        raise NotFoundError(f"table '{slug}' not found in namespace {namespace!r}")
    return orm


async def _column_orm(session: AsyncSession, table_id: uuid.UUID, key: str) -> GridColumn:
    orm = await session.scalar(
        sa.select(GridColumn).where(
            GridColumn.table_id == table_id, GridColumn.key == key, GridColumn.archived_at.is_(None)
        )
    )
    if orm is None:
        raise NotFoundError(f"column '{key}' not found")
    return orm


async def rename_table(session: AsyncSession, *, slug: str, new_slug: str, namespace: str = "") -> None:
    """Rename a table in place (columns/relations/rows key off its id, so nothing cascades)."""
    validate_slug(new_slug)
    orm = await _table_orm(session, slug, namespace)
    logger.info(
        "grid: renaming table",
        extra={"namespace": namespace, "table_id": str(orm.id), "slug": slug, "new_slug": new_slug},
    )
    orm.slug = new_slug
    async with integrity_boundary():
        await session.flush()


async def archive_table(session: AsyncSession, *, slug: str, namespace: str = "") -> None:
    """Soft-drop a table with everything anchored to it: its columns, indexes, rows, and every
    relation that references it (as source/target/through)."""
    orm = await _table_orm(session, slug, namespace)
    logger.info(
        "grid: archiving table and everything anchored to it",
        extra={"namespace": namespace, "table_id": str(orm.id), "slug": slug},
    )
    for column in await session.scalars(
        sa.select(GridColumn).where(GridColumn.table_id == orm.id, GridColumn.archived_at.is_(None))
    ):
        _archive(column)
    for index in await session.scalars(
        sa.select(GridIndex).where(GridIndex.table_id == orm.id, GridIndex.archived_at.is_(None))
    ):
        _archive(index)
    for relation in await session.scalars(
        sa.select(GridRelation).where(
            sa.or_(
                GridRelation.source_table_id == orm.id,
                GridRelation.target_table_id == orm.id,
                GridRelation.through_table_id == orm.id,
            ),
            GridRelation.archived_at.is_(None),
        )
    ):
        _archive(relation)
    for row in await session.scalars(
        sa.select(GridRow).where(GridRow.table_id == orm.id, GridRow.archived_at.is_(None))
    ):
        _archive(row)
    _archive(orm)
    await session.flush()


async def rename_column(session: AsyncSession, *, table_id: uuid.UUID, key: str, new_key: str) -> None:
    """Rename a column and migrate the key inside every live row's JSONB payload."""
    validate_key(new_key)
    orm = await _column_orm(session, table_id, key)
    logger.info(
        "grid: renaming column and migrating its key in every live row payload",
        extra={"table_id": str(table_id), "key": key, "new_key": new_key},
    )
    payload = GridRow.payload
    # payload = (payload - 'old') || {'new': payload->'old'}, only for rows carrying the key.
    migrated = payload.op("-")(key).op("||")(sa.func.jsonb_build_object(new_key, payload[key]))
    await session.execute(
        sa.update(GridRow)
        .where(GridRow.table_id == table_id, GridRow.archived_at.is_(None), payload.has_key(key))
        .values(payload=migrated)
    )
    orm.key = new_key
    async with integrity_boundary():
        await session.flush()


async def alter_column(session: AsyncSession, *, table_id: uuid.UUID, spec: ColumnSpec) -> None:
    """Update a column's non-type attributes. A type/materialization change (retype) needs a
    data-coercion pass and is not supported yet — it is refused here, not silently applied."""
    orm = await _column_orm(session, table_id, spec.key)
    if spec.type_id != orm.type_id or Materialization(str(orm.materialization)) is not spec.materialization:
        raise BadRequestError(f"column '{spec.key}' retype (type/materialization change) is not supported yet")
    orm.label = spec.label
    orm.is_required = spec.is_required
    orm.is_unique = spec.is_unique
    orm.default_value = spec.default_value
    orm.display_order = spec.display_order
    orm.config = dict(spec.config)
    orm.promoted_column = spec.promoted_column
    async with integrity_boundary():
        await session.flush()


async def archive_column(session: AsyncSession, *, table_id: uuid.UUID, key: str) -> None:
    """Soft-drop a column (its payload data is retained but hidden; reconcile drops its
    physical index/sidecar)."""
    logger.info("grid: archiving column", extra={"table_id": str(table_id), "key": key})
    _archive(await _column_orm(session, table_id, key))
    await session.flush()


async def archive_relation(session: AsyncSession, *, key: str, namespace: str = "") -> None:
    """Soft-drop a relation (reconcile drops its cardinality index)."""
    logger.info("grid: archiving relation", extra={"namespace": namespace, "relation_key": key})
    orm = await session.scalar(
        sa.select(GridRelation).where(
            GridRelation.key == key, GridRelation.namespace == namespace, GridRelation.archived_at.is_(None)
        )
    )
    if orm is None:
        raise NotFoundError(f"relation '{key}' not found")
    _archive(orm)
    await session.flush()


async def alter_table(session: AsyncSession, *, spec: TableSpec, namespace: str = "") -> None:
    """Update a table's non-structural attributes. Rebinding an overlay is refused (structural)."""
    orm = await _table_orm(session, spec.slug, namespace)
    if binding_to_json(spec.binding) != orm.binding:
        raise BadRequestError(f"rebinding table '{spec.slug}' is not supported (drop and recreate)")
    orm.label = spec.label
    orm.projection_predicate = dict(spec.scope_predicate) if spec.scope_predicate else None
    orm.natural_key = list(spec.natural_key) or None
    orm.is_system = spec.is_system
    await session.flush()


async def alter_relation(session: AsyncSession, *, spec: RelationSpec, namespace: str = "") -> None:
    """Update a relation's ``on_delete``. A shape change is structural — refused (drop+recreate)."""
    orm = await session.scalar(
        sa.select(GridRelation).where(
            GridRelation.key == spec.key, GridRelation.namespace == namespace, GridRelation.archived_at.is_(None)
        )
    )
    if orm is None:
        raise NotFoundError(f"relation '{spec.key}' not found")
    if str(orm.relation_type) != spec.shape.value:
        raise BadRequestError(f"relation '{spec.key}' shape change is not supported (drop and recreate)")
    orm.on_delete = spec.on_delete
    await session.flush()


async def create_index(session: AsyncSession, *, table_id: uuid.UUID, spec: IndexSpec, namespace: str = "") -> None:
    """Declare an index (a ``GridIndex`` row); the physical build is the reconcile tail."""
    session.add(
        GridIndex(
            table_id=table_id,
            namespace=namespace,
            column_keys=list(spec.column_keys),
            index_kind=spec.index_kind,
            is_unique=spec.is_unique,
        )
    )
    async with integrity_boundary():
        await session.flush()


async def archive_index(session: AsyncSession, *, table_id: uuid.UUID, spec: IndexSpec) -> None:
    """Soft-drop the declared index matching ``spec`` (by columns + kind)."""
    for orm in await session.scalars(
        sa.select(GridIndex).where(GridIndex.table_id == table_id, GridIndex.archived_at.is_(None))
    ):
        if orm.index_kind == spec.index_kind and tuple(orm.column_keys) == spec.column_keys:
            _archive(orm)
            await session.flush()
            return
    raise NotFoundError(f"index {spec.index_kind}:{','.join(spec.column_keys)} not found")


__all__ = [
    "alter_column",
    "alter_relation",
    "alter_table",
    "archive_column",
    "archive_index",
    "archive_relation",
    "archive_table",
    "create_index",
    "hydrate",
    "rename_column",
    "rename_table",
]
