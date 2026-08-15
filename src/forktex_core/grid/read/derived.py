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

"""Read-side resolution of ``derived`` columns — the third materialization.

A derived column projects a field of a related row through a single ``ref`` column
(``derived_source = "ref_key.target_field"``). Resolved after a page is fetched, in one
batched, namespace- and archived-scoped auto-join; never mutates stored data.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid.domain.enums import Cardinality, Materialization
from forktex_core.grid.persist import GridRow, repos
from forktex_core.grid.persist.refs import TableRef
from forktex_core.grid.read.result import Row


async def resolve_derived(session: AsyncSession, ref: TableRef, rows: list[Row]) -> None:
    """Fill each row's derived-column values in place (owned tables only)."""
    derived = [c for c in ref.domain.columns if c.spec.materialization is Materialization.derived]
    if not derived or not rows:
        return
    namespaces = {r.namespace for r in rows}
    for dcol in derived:
        parts = dcol.spec.derived_parts
        if parts is None:
            continue
        ref_key, target_field = parts
        ref_col = ref.domain.column(ref_key)
        if ref_col.spec.cardinality is Cardinality.many or ref_col.spec.relation_ref is None:
            continue  # to-many / unresolvable → left absent
        relation = await repos.relation_by_key(session, ref_col.spec.relation_ref, ref.namespace)

        targets: dict[str, list[Row]] = {}
        ids: dict[str, uuid.UUID] = {}
        for row in rows:
            tid = row.values.get(ref_key)
            if not tid or isinstance(tid, (list, dict)):
                continue
            try:
                ids.setdefault(str(tid), uuid.UUID(str(tid)))
            except ValueError, TypeError:
                continue
            targets.setdefault(str(tid), []).append(row)
        if not targets:
            continue
        target_rows = await session.scalars(
            sa.select(GridRow).where(
                GridRow.table_id == relation.target_table_id,
                GridRow.id.in_(list(ids.values())),
                GridRow.namespace.in_(namespaces),
                GridRow.archived_at.is_(None),
            )
        )
        by_id = {str(t.id): t.payload.get(target_field) for t in target_rows}
        for tid, group in targets.items():
            for row in group:
                row.values[dcol.key] = by_id.get(tid)


__all__ = ["resolve_derived"]
