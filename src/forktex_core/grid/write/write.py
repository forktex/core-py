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

"""The single row-write pipeline — one implementation for create, create_many and patch.

Normalization is driven by the column's :class:`MaterializationStrategy` (``accepts_write``
/ ``store``) and its :class:`FieldTypeHandler` — no branching on ``materialization``. The
per-row referential-edge sync, promoted dual-write, and lifecycle hooks reuse the proven
leaf helpers (they operate on the shared ORM); the savepoint/atomicity is
:func:`forktex_core.grid.write.tx.atomic`. One method family, batchable; single-row is a batch of one.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid.domain.enums import Cardinality
from forktex_core.grid.domain.table import Column, Table
from forktex_core.grid.errors import BadRequestError
from forktex_core.grid.persist import GridRow, reconcile, repos
from forktex_core.grid.persist.refs import TableRef
from forktex_core.grid.read.result import Row
from forktex_core.grid.write.relations import list_related as _list_related
from forktex_core.grid.write.relations import relate_rows as _relate_rows
from forktex_core.grid.write.relations import unrelate_rows as _unrelate_rows
from forktex_core.grid.write.rows import archive_row as _archive_row
from forktex_core.grid.write.rows import fire_hooks, fire_hooks_batch, sync_ref_edges
from forktex_core.grid.write.tx import atomic
from forktex_core.types import JsonValue


def normalize(table: Table, values: dict[str, Any], *, enforce_required: bool) -> dict[str, Any]:
    """Validate + canonicalize a write into a payload dict, via the column strategies."""
    by_key = {c.key: c for c in table.columns}
    unknown = set(values) - set(by_key)
    if unknown:
        raise BadRequestError(f"unknown columns: {', '.join(sorted(unknown))}")
    payload: dict[str, Any] = {}
    for col in table.columns:
        if col.key in values:
            if not col.value.accepts_write():
                raise BadRequestError(f"column '{col.key}' is read-only")
            payload_value = _normalize_cell(col, values[col.key])
            col.value.store(payload, col.key, payload_value)
        elif enforce_required:
            if col.spec.default_value is not None:
                col.value.store(payload, col.key, col.spec.default_value)
            elif col.spec.is_required:
                raise BadRequestError(f"column '{col.key}' is required")
    return payload


def _normalize_cell(col: Column, value: object) -> JsonValue:
    cfg = col.handler.validate_config(col.spec.config)
    try:
        if col.spec.cardinality is Cardinality.many:
            if value is None:
                return None
            if not isinstance(value, list):
                raise ValueError("expected a list of values")
            return [col.handler.normalize(v, config=cfg) for v in value]
        return None if value is None else col.handler.normalize(value, config=cfg)
    except ValueError as exc:
        raise BadRequestError(f"column '{col.key}': {exc}") from exc


class RowWriter:
    """One write pipeline for create / create_many / patch (+ archive/relate helpers)."""

    async def create(
        self,
        session: AsyncSession,
        ref: TableRef,
        rows: list[dict[str, Any]],
        external_refs: list[uuid.UUID | None] | None = None,
    ) -> list[Row]:
        ref.domain.storage.ensure_writable()
        if not rows:
            return []
        cols = await repos.get_active_columns(session, ref.id)
        slug = ref.domain.spec.slug
        objs: list[GridRow] = []
        async with atomic(session):
            for i, values in enumerate(rows):
                payload = normalize(ref.domain, values, enforce_required=True)
                external_ref = external_refs[i] if external_refs is not None else None
                obj = GridRow(table_id=ref.id, namespace=ref.namespace, payload=payload, external_ref=external_ref)
                session.add(obj)
                objs.append(obj)
            await session.flush()
            for obj in objs:
                await sync_ref_edges(session, row=obj, columns=cols, changed=None)
            await reconcile.sync_promoted(session, ref, objs)
            await fire_hooks_batch(session, columns=cols, table_slug=slug, rows=objs, archived=False)
        return [Row(id=o.id, namespace=o.namespace, values=dict(o.payload)) for o in objs]

    async def patch(self, session: AsyncSession, ref: TableRef, row_id: uuid.UUID, values: dict[str, Any]) -> Row:
        ref.domain.storage.ensure_writable()
        row = await repos.get_row(session, row_id)
        cols = await repos.get_active_columns(session, ref.id)
        before = dict(row.payload)
        partial = normalize(ref.domain, values, enforce_required=False)
        async with atomic(session):
            row.payload = {**row.payload, **partial}
            await session.flush()
            await sync_ref_edges(session, row=row, columns=cols, changed=set(partial))
            await reconcile.sync_promoted(session, ref, [row])
            await fire_hooks(
                session,
                columns=cols,
                table_slug=ref.domain.spec.slug,
                row=row,
                before=before,
                archived=False,
                changed=set(partial),
            )
        return Row(id=row.id, namespace=row.namespace, values=dict(row.payload))

    async def archive(self, session: AsyncSession, row_id: uuid.UUID) -> None:
        await _archive_row(session, row=await repos.get_row(session, row_id))

    async def relate(
        self, session: AsyncSession, rel_key: str, source_id: uuid.UUID, target_id: uuid.UUID, namespace: str = ""
    ) -> None:
        relation = await repos.relation_by_key(session, rel_key, namespace)
        await _relate_rows(
            session,
            relation=relation,
            source_row=await repos.get_row(session, source_id),
            target_row=await repos.get_row(session, target_id),
        )

    async def unrelate(
        self, session: AsyncSession, rel_key: str, source_id: uuid.UUID, target_id: uuid.UUID, namespace: str = ""
    ) -> None:
        relation = await repos.relation_by_key(session, rel_key, namespace)
        await _unrelate_rows(
            session,
            relation=relation,
            source_row=await repos.get_row(session, source_id),
            target_row=await repos.get_row(session, target_id),
        )

    async def related(
        self, session: AsyncSession, rel_key: str, source_id: uuid.UUID, namespace: str = ""
    ) -> list[Row]:
        relation = await repos.relation_by_key(session, rel_key, namespace)
        rows = await _list_related(session, relation=relation, source_row=await repos.get_row(session, source_id))
        return [Row(id=r.id, namespace=r.namespace, values=dict(r.payload)) for r in rows]


__all__ = ["RowWriter", "normalize"]
