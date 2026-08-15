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

"""The mutation batch — schema + data as one atomic unit.

A :class:`Batch` carries an optional desired ``Schema`` (DDL) plus an ordered list of
:class:`RowOp`s (DML) spanning any number of tables. ``apply_batch`` runs the schema
reconcile then every mutation inside **one** ``atomic`` savepoint, so schema-then-data is
all-or-nothing and rows can seed a table created in the very same call.

Rows are *not* modelled declaratively (you cannot diff rows at scale): each mutation is a thin,
typed instruction dispatched to the **unchanged** :class:`RowWriter` — the single DML pipeline.
So the JSON track and the typed API share exactly one write path.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid.domain.schema import Schema
from forktex_core.grid.errors import BadRequestError
from forktex_core.grid.persist import repos
from forktex_core.grid.read.result import Row
from forktex_core.grid.write.schema import ReconcileOptions, ReconcileReport, SchemaReconciler
from forktex_core.grid.write.tx import atomic
from forktex_core.grid.write.write import RowWriter

RowOpKind = Literal["create", "create_many", "patch", "archive", "relate", "unrelate"]


class RowOp(BaseModel):
    """One DML instruction against a table (or relation) in the space's namespace."""

    model_config = ConfigDict(frozen=True)

    op: RowOpKind
    table: str = ""  # target table slug (relate/unrelate use rel_key instead)
    values: dict[str, Any] | None = None  # create / patch
    rows: list[dict[str, Any]] | None = None  # create_many
    row_id: uuid.UUID | None = None  # patch / archive
    external_ref: uuid.UUID | None = None  # create (extension link)
    rel_key: str | None = None  # relate / unrelate
    source_id: uuid.UUID | None = None  # relate / unrelate
    target_id: uuid.UUID | None = None  # relate / unrelate


class Batch(BaseModel):
    """A desired schema (optional) + an ordered batch of row mutations, applied atomically."""

    model_config = ConfigDict(frozen=True)

    namespace: str = ""
    desired: Schema | None = None
    mutations: tuple[RowOp, ...] = ()
    options: ReconcileOptions = ReconcileOptions()


class RowResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    op: RowOpKind
    table: str
    rows: list[Row] = Field(default_factory=list)


class BatchReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    reconcile: ReconcileReport | None = None
    results: tuple[RowResult, ...] = ()


def _require[T](value: T | None, op: RowOpKind, field: str) -> T:
    if value is None:
        raise BadRequestError(f"mutation '{op}' requires '{field}'")
    return value


async def apply_batch(session: AsyncSession, env: Batch) -> BatchReport:
    """Reconcile the schema (if any) then apply every mutation, in one transaction."""
    reconciler = SchemaReconciler()
    writer = RowWriter()

    # A dry-run is schema-only: report the plan, apply nothing (rows have no dry-run).
    if env.options.dry_run:
        report = None
        if env.desired is not None:
            report = await reconciler.reconcile(session, env.desired, options=env.options)
        return BatchReport(reconcile=report)

    async with atomic(session):
        report = None
        if env.desired is not None:
            report = await reconciler.reconcile(session, env.desired, options=env.options)
        results = [await _dispatch(session, writer, env.namespace, m) for m in env.mutations]

    return BatchReport(reconcile=report, results=tuple(results))


async def _dispatch(session: AsyncSession, writer: RowWriter, ns: str, m: RowOp) -> RowResult:
    if m.op in ("relate", "unrelate"):
        rel_key = _require(m.rel_key, m.op, "rel_key")
        source_id = _require(m.source_id, m.op, "source_id")
        target_id = _require(m.target_id, m.op, "target_id")
        fn = writer.relate if m.op == "relate" else writer.unrelate
        await fn(session, rel_key, source_id, target_id, ns)
        return RowResult(op=m.op, table=m.table)

    ref = await repos.load_table(session, _require(m.table, m.op, "table"), ns)
    match m.op:
        case "create":
            rows = await writer.create(session, ref, [_require(m.values, m.op, "values")], [m.external_ref])
        case "create_many":
            rows = await writer.create(session, ref, list(_require(m.rows, m.op, "rows")))
        case "patch":
            rows = [
                await writer.patch(session, ref, _require(m.row_id, m.op, "row_id"), _require(m.values, m.op, "values"))
            ]
        case "archive":
            await writer.archive(session, _require(m.row_id, m.op, "row_id"))
            rows = []
    return RowResult(op=m.op, table=m.table, rows=rows)


__all__ = ["Batch", "BatchReport", "RowOp", "RowResult", "apply_batch"]
