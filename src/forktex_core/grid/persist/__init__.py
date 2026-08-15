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

"""Persistence — the only layer that runs SQL: ORM models, migrations, repositories,
physical reconcilers, and the schema resolution they share.

The models stay anemic *by design*: behaviour lives in the domain aggregates that
repositories hydrate from these rows. `models` maps onto
`database.models.substrate_base("forktex_grid")` — its own `MetaData`, so a consumer's
`BaseDBModel.metadata.create_all()` never tries to build grid's substrate.

This layer emits **no raw SQL**. DDL that Core lacks comes from `database.ddl`, reflection
from `database.reflect`; identifiers are quoted by the dialect's preparer, never by an
f-string.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid.persist.migrations import apply_migrations
from forktex_core.grid.persist.models import (
    GridColumn,
    GridEdge,
    GridIndex,
    GridRelation,
    GridRow,
    GridSpace,
    GridTable,
)


def resolve_schema(session: AsyncSession) -> str:
    """The physical schema for raw DDL/DML (honours ``schema_translate_map``)."""
    try:
        tmap = session.get_bind().get_execution_options().get("schema_translate_map") or {}
    except Exception:
        tmap = {}
    return tmap.get("forktex_grid", "forktex_grid")


__all__ = [
    "GridColumn",
    "GridEdge",
    "GridIndex",
    "GridRelation",
    "GridRow",
    "GridSpace",
    "GridTable",
    "apply_migrations",
    "resolve_schema",
]
