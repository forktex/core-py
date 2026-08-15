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

"""Repositories — the only code that runs SQL over the schema/data ORM.

They translate between persisted ORM rows and the DB-free domain aggregates: a table
is written from a :class:`TableSpec` and read back as a hydrated :class:`Table` +
:class:`TableRef`. Every read is namespace + ``archived_at`` scoped.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.database import reflect
from forktex_core.database.integrity import integrity_boundary
from forktex_core.grid.domain.binding import Overlay, binding_from_json, binding_to_json, ownership_of
from forktex_core.grid.domain.enums import Cardinality, Materialization, Ownership, RelationShape
from forktex_core.grid.domain.spec import ColumnSpec, RelationSpec, TableSpec
from forktex_core.grid.domain.table import Table
from forktex_core.grid.errors import NotFoundError
from forktex_core.grid.persist import GridColumn, GridRelation, GridRow, GridTable
from forktex_core.grid.persist.refs import TableRef


async def reflect_column_types(session: AsyncSession, physical_relation: str) -> dict[str, str]:
    """Host column → ``udt_name``, cached on an overlay binding at bind time.

    Delegates to :func:`forktex_core.database.reflect.udt_names`; grid used to
    carry its own ``information_schema`` query (one of three copies).
    """
    return await reflect.udt_names(session, physical_relation)


def _column_to_orm(table_id: uuid.UUID, namespace: str, spec: ColumnSpec, relation_id: uuid.UUID | None) -> GridColumn:
    return GridColumn(
        table_id=table_id,
        namespace=namespace,
        key=spec.key,
        label=spec.label,
        type_id=spec.type_id,
        cardinality=spec.cardinality.value,
        materialization=spec.materialization.value,
        promoted_column=spec.promoted_column,
        derived_source=spec.derived_source,
        is_required=spec.is_required,
        is_unique=spec.is_unique,
        relation_id=relation_id,
        config=dict(spec.config),
        default_value=spec.default_value,
        display_order=spec.display_order,
    )


def column_spec_from_orm(orm: GridColumn, rel_key_by_id: Mapping[uuid.UUID, str]) -> ColumnSpec:
    """Build a ``ColumnSpec`` from a persisted column. ``rel_key_by_id`` resolves a ref
    column's relation to its key without a per-column query (shared by single-table load
    and the namespace-wide schema hydrate)."""
    relation_ref = rel_key_by_id.get(orm.relation_id) if orm.relation_id is not None else None
    return ColumnSpec(
        key=orm.key,
        label=orm.label,
        type_id=orm.type_id,
        cardinality=Cardinality(str(orm.cardinality)),
        materialization=Materialization(str(orm.materialization)),
        is_required=orm.is_required,
        is_unique=orm.is_unique,
        config=dict(orm.config or {}),
        relation_ref=relation_ref,
        promoted_column=orm.promoted_column,
        derived_source=orm.derived_source,
        default_value=orm.default_value,
        display_order=orm.display_order,
    )


def table_spec_from_orm(
    orm_table: GridTable, orm_cols: Sequence[GridColumn], rel_key_by_id: Mapping[uuid.UUID, str]
) -> TableSpec:
    """Reconstruct a ``TableSpec`` from persisted table + column rows (no session/SQL)."""
    columns = tuple(column_spec_from_orm(c, rel_key_by_id) for c in orm_cols)
    return TableSpec(
        slug=orm_table.slug,
        label=orm_table.label,
        namespace=orm_table.namespace,
        binding=binding_from_json(orm_table.binding),
        scope_predicate=orm_table.projection_predicate,
        natural_key=tuple(orm_table.natural_key or ()),
        columns=columns,
        is_system=orm_table.is_system,
    )


async def _relation_keys_for(session: AsyncSession, relation_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Map the given relation ids to their keys in one query (for ref-column hydration)."""
    ids = [rid for rid in relation_ids if rid is not None]
    if not ids:
        return {}
    rows = await session.execute(sa.select(GridRelation.id, GridRelation.key).where(GridRelation.id.in_(ids)))
    return {rid: key for rid, key in rows}


async def _resolve_relation_id(session: AsyncSession, namespace: str, key: str | None) -> uuid.UUID | None:
    if key is None:
        return None
    rel = await session.scalar(
        sa.select(GridRelation).where(
            GridRelation.key == key, GridRelation.namespace == namespace, GridRelation.archived_at.is_(None)
        )
    )
    if rel is None:
        raise NotFoundError(f"relation '{key}' not found (declare it before a ref column that projects it)")
    return rel.id


async def create_table(session: AsyncSession, spec: TableSpec) -> TableRef:
    """Persist a table + its columns from a validated spec; return the hydrated ref."""
    # Enrich an overlay binding with the reflected host column types *before* building
    # the aggregate, so the in-memory domain (used to query) casts literals to the host's
    # native types (org_id = uuid, not varchar) — matching what a later load() hydrates.
    binding = spec.binding
    if isinstance(binding, Overlay) and not binding.column_types:
        types = await reflect_column_types(session, binding.physical_relation)
        binding = binding.model_copy(update={"column_types": types})
    if binding is not spec.binding:
        spec = spec.model_copy(update={"binding": binding})
    # Constructing the aggregate enforces every invariant before any SQL.
    domain = Table.declare(spec)
    orm_table = GridTable(
        slug=spec.slug,
        label=spec.label,
        namespace=spec.namespace,
        ownership=ownership_of(binding),
        binding=binding_to_json(binding),
        projection_predicate=dict(spec.scope_predicate) if spec.scope_predicate else None,
        natural_key=list(spec.natural_key) or None,
        is_system=spec.is_system,
    )
    session.add(orm_table)
    async with integrity_boundary():
        await session.flush()

    for col in spec.columns:
        relation_id = await _resolve_relation_id(session, spec.namespace, col.relation_ref)
        session.add(_column_to_orm(orm_table.id, spec.namespace, col, relation_id))
    async with integrity_boundary():
        await session.flush()

    return TableRef(id=orm_table.id, namespace=spec.namespace, domain=domain)


async def load_table(session: AsyncSession, slug: str, namespace: str = "") -> TableRef:
    orm_table = await _get_table_orm(session, slug, namespace)
    orm_cols = await get_active_columns(session, orm_table.id)
    rel_key_by_id = await _relation_keys_for(session, [c.relation_id for c in orm_cols if c.relation_id])
    spec = table_spec_from_orm(orm_table, orm_cols, rel_key_by_id)
    return TableRef(id=orm_table.id, namespace=orm_table.namespace, domain=Table.declare(spec))


async def add_column(session: AsyncSession, ref: TableRef, spec: ColumnSpec) -> TableRef:
    """Add a column to an existing table (schema evolution); returns the fresh ref.

    The storage strategy vets the column (an overlay refuses ref/derived/promoted) and
    the ``Column`` aggregate enforces its own invariants — no ad-hoc gating here.
    """
    from forktex_core.grid.domain.table import Column

    ref.domain.storage.accept_column(spec)
    Column(spec)  # constructs → enforces column invariants (promotability, ref/derived pairing)
    relation_id = await _resolve_relation_id(session, ref.namespace, spec.relation_ref)
    session.add(_column_to_orm(ref.id, ref.namespace, spec, relation_id))
    async with integrity_boundary():
        await session.flush()
    return await load_table(session, ref.domain.spec.slug, ref.namespace)


async def _get_table_orm(session: AsyncSession, slug: str, namespace: str) -> GridTable:
    orm = await session.scalar(
        sa.select(GridTable).where(
            GridTable.slug == slug, GridTable.namespace == namespace, GridTable.archived_at.is_(None)
        )
    )
    if orm is None:
        raise NotFoundError(f"table '{slug}' not found in namespace {namespace!r}")
    return orm


async def create_relation(session: AsyncSession, spec: RelationSpec, namespace: str = "") -> GridRelation:
    source = await _get_table_orm(session, spec.source, namespace)
    target = await _get_table_orm(session, spec.target, namespace)
    through = await _get_table_orm(session, spec.through, namespace) if spec.through else None
    if source.ownership != Ownership.owned or target.ownership != Ownership.owned:
        from forktex_core.grid.errors import BadRequestError

        raise BadRequestError("relations require owned tables (link a bound entity via an extension)")
    rel = GridRelation(
        key=spec.key,
        namespace=namespace,
        source_table_id=source.id,
        target_table_id=target.id,
        through_table_id=through.id if through else None,
        relation_type=spec.shape.value,
        on_delete=spec.on_delete.value,
    )
    session.add(rel)
    async with integrity_boundary():
        await session.flush()
    return rel


async def relation_by_key(session: AsyncSession, key: str, namespace: str = "") -> GridRelation:
    rel = await session.scalar(
        sa.select(GridRelation).where(
            GridRelation.key == key, GridRelation.namespace == namespace, GridRelation.archived_at.is_(None)
        )
    )
    if rel is None:
        raise NotFoundError(f"relation '{key}' not found")
    return rel


async def get_row(session: AsyncSession, row_id: uuid.UUID) -> GridRow:
    row = await session.get(GridRow, row_id)
    if row is None or row.archived_at is not None:
        raise NotFoundError("row not found", details={"row_id": str(row_id)})
    return row


async def get_row_by_external_ref(
    session: AsyncSession, table_id: uuid.UUID, external_ref: uuid.UUID
) -> GridRow | None:
    """The live extension row of ``table_id`` linked 1:1 to a host row's PK, or ``None``."""
    return await session.scalar(
        sa.select(GridRow).where(
            GridRow.table_id == table_id,
            GridRow.external_ref == external_ref,
            GridRow.archived_at.is_(None),
        )
    )


async def get_active_columns(session: AsyncSession, table_id: uuid.UUID) -> list[GridColumn]:
    """Active (non-archived) columns of a table, ordered for display."""
    result = await session.scalars(
        sa.select(GridColumn)
        .where(GridColumn.table_id == table_id, GridColumn.archived_at.is_(None))
        .order_by(GridColumn.display_order, GridColumn.created_at)
    )
    return list(result)


def shape_of(spec: RelationSpec | GridRelation) -> RelationShape:
    return RelationShape(str(spec.relation_type if isinstance(spec, GridRelation) else spec.shape))


__all__ = [
    "create_relation",
    "create_table",
    "get_active_columns",
    "get_row",
    "get_row_by_external_ref",
    "load_table",
    "reflect_column_types",
    "relation_by_key",
    "shape_of",
]
