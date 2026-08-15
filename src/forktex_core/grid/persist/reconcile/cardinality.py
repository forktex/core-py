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

"""The relation-cardinality reconciler — declared shape → physical edge constraints.

A relation's cardinality (1:1, 1:N, N:1) becomes DB-level truth via partial unique
indexes on the ``grid_edge`` endpoints, on top of the application-level checks in
:mod:`forktex_core.grid.write.relations`. Split out from the payload-index reconciler
It is separate from the payload-index reconciler: the two operate on different physical relations
(``grid_row`` vs ``grid_edge``) and different declarations.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.database import ddl
from forktex_core.database.models import UtcDateTime
from forktex_core.grid.domain.enums import RelationShape
from forktex_core.grid.identifiers import validate_schema
from forktex_core.grid.persist import GridRelation
from forktex_core.grid.persist.reconcile.indexes import index_name, render_ddl
from forktex_core.log import get_logger

logger = get_logger(__name__)

_ENDPOINT_COLUMN = {"source": "source_row_id", "target": "target_row_id"}


def _grid_edge_for(schema: str) -> sa.Table:
    """A minimal Core ``grid_edge`` carrying only the columns these indexes touch.

    Named with an explicit ``schema`` for the same reason the payload-index
    reconciler does: the caller resolves the physical schema and passes it in.
    """
    return sa.Table(
        "grid_edge",
        sa.MetaData(),
        sa.Column("relation_id", sa.UUID(as_uuid=True)),
        sa.Column("source_row_id", sa.UUID(as_uuid=True)),
        sa.Column("target_row_id", sa.UUID(as_uuid=True)),
        sa.Column("archived_at", UtcDateTime),
        schema=schema,
    )


async def reconcile_relation_cardinality(
    session: AsyncSession,
    *,
    relation: GridRelation,
    schema: str = "forktex_grid",
) -> list[str]:
    """Materialise the partial unique indexes that enforce relation cardinality.

    The unique endpoints come from :meth:`RelationShape.unique_endpoints` — the single
    source of truth shared with the application-level cardinality check.
    """
    validate_schema(schema)
    created: list[str] = []
    for endpoint in RelationShape(relation.relation_type).unique_endpoints():
        column = _ENDPOINT_COLUMN[endpoint]
        name = index_name("gux", "edge", relation.id, column)
        edge = _grid_edge_for(schema)
        statement = ddl.CreateIndex(
            sa.Index(
                name,
                edge.c.relation_id,
                edge.c[column],
                unique=True,
                postgresql_where=edge.c.archived_at.is_(None),
            ),
            if_not_exists=True,
        )
        logger.debug("grid: cardinality index DDL", extra={"ddl": render_ddl(statement)})
        await session.execute(statement)
        created.append(name)
    await session.flush()
    return created


__all__ = ["reconcile_relation_cardinality"]
