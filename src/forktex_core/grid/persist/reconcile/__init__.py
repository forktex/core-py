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

"""Physical reconcilers — declared intent → real Postgres indexes / sidecars / edge
constraints. The relation-cardinality reconciler lives here rather than in the indexing
module: the two operate on different physical relations (``grid_edge`` vs ``grid_row``) and
on different declarations. Both are driven from a ``TableRef``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid.persist import GridColumn, GridRelation, GridRow, GridTable, resolve_schema
from forktex_core.grid.persist.reconcile.cardinality import reconcile_relation_cardinality as _reconcile_cardinality
from forktex_core.grid.persist.reconcile.indexes import reconcile_table_indexes as _reconcile_indexes
from forktex_core.grid.persist.reconcile.sidecar import reconcile_table_promoted as _reconcile_promoted
from forktex_core.grid.persist.reconcile.sidecar import sidecar_table_name
from forktex_core.grid.persist.reconcile.sidecar import sync_promoted as _sync_promoted
from forktex_core.grid.persist.refs import TableRef
from forktex_core.log import get_logger

logger = get_logger(__name__)


async def _orm_table(session: AsyncSession, ref: TableRef) -> GridTable:
    orm = await session.get(GridTable, ref.id)
    assert orm is not None  # a live TableRef always has its row
    return orm


async def _active_columns(session: AsyncSession, ref: TableRef) -> list[GridColumn]:
    return list(
        await session.scalars(
            sa.select(GridColumn).where(GridColumn.table_id == ref.id, GridColumn.archived_at.is_(None))
        )
    )


async def reconcile_table(session: AsyncSession, ref: TableRef) -> None:
    """Materialise all physical structures for a table: indexes + promoted sidecar."""
    schema = resolve_schema(session)
    orm = await _orm_table(session, ref)
    logger.debug(
        "grid: reconciling table",
        extra={"schema": schema, "table_id": str(ref.id), "namespace": ref.namespace},
    )
    await _reconcile_indexes(session, table=orm, schema=schema)
    await _reconcile_promoted(session, table=orm, schema=schema)


async def reconcile_relation(session: AsyncSession, relation: GridRelation) -> None:
    schema = resolve_schema(session)
    logger.debug(
        "grid: reconciling relation cardinality",
        extra={"schema": schema, "relation_key": relation.key, "relation_type": str(relation.relation_type)},
    )
    await _reconcile_cardinality(session, relation=relation, schema=schema)


async def sync_promoted(session: AsyncSession, ref: TableRef, rows: Sequence[GridRow]) -> None:
    """Dual-write promoted values of ``rows`` into the sidecar (no-op if none/unreconciled)."""
    columns = await _active_columns(session, ref)
    orm = await _orm_table(session, ref)
    await _sync_promoted(session, table=orm, rows=rows, columns=columns)


__all__ = ["reconcile_relation", "reconcile_table", "sidecar_table_name", "sync_promoted"]
