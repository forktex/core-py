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

"""The ONE query pipeline — filter / sort / paginate, shared by owned and bound.

Two near-identical compilers (owned ``payload->>`` vs bound host-column) would be the naive shape. Here
the AST→SQL walker (:func:`compile_filter` / :func:`compile_order`) is written once and
parameterized by a :class:`QuerySource` — the seam that differs between storages:
``PayloadSource`` (owned ``grid_row`` rows) and ``HostSource`` (a host-table overlay).
The source is chosen by a dict keyed on ``table.storage.kind`` — no ``if ownership``.
"""

from __future__ import annotations

import abc
import base64
import json
import uuid
from collections.abc import Mapping
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.database.filters import compile_filter as _db_compile_filter
from forktex_core.database.integrity import read_boundary
from forktex_core.grid.domain.enums import BrowseMode
from forktex_core.grid.domain.fieldtypes import FilterOp, effective_capabilities
from forktex_core.grid.domain.table import Column, Table
from forktex_core.grid.errors import BadRequestError
from forktex_core.grid.persist import GridRow
from forktex_core.grid.persist.refs import TableRef
from forktex_core.grid.read.derived import resolve_derived as _resolve_derived
from forktex_core.grid.read.filters import FilterNode, SortDirection, SortKey, parse_filter
from forktex_core.grid.read.result import Page, Row
from forktex_core.types import JsonValue

MAX_LIMIT = 1000
MAX_OFFSET = 100_000

# Postgres udt_name → SQLAlchemy type, for casting overlay comparison literals.
_UDT_TO_SA: dict[str, sa.types.TypeEngine[Any]] = {
    "uuid": pg.UUID(as_uuid=False),
    "int2": sa.SmallInteger(),
    "int4": sa.Integer(),
    "int8": sa.BigInteger(),
    "numeric": sa.Numeric(),
    "float4": sa.Float(),
    "float8": sa.Float(),
    "bool": sa.Boolean(),
    "timestamptz": pg.TIMESTAMP(timezone=True),
    "timestamp": sa.TIMESTAMP(),
    "date": sa.Date(),
    "time": sa.Time(),
    "text": sa.Text(),
    "varchar": sa.Text(),
    "bpchar": sa.Text(),
    "citext": sa.Text(),
}


def _pg_text(value: JsonValue) -> str:
    return "true" if value is True else "false" if value is False else str(value)


class QuerySource(abc.ABC):
    supports_cursor: bool

    def __init__(self, ref: TableRef, namespace: str) -> None:
        self.ref = ref
        self.table: Table = ref.domain
        self.namespace = namespace

    @abc.abstractmethod
    def base_query(self) -> sa.Select[Any]: ...
    @abc.abstractmethod
    def count_query(self) -> sa.Select[Any]: ...
    @abc.abstractmethod
    async def fetch(self, session: AsyncSession, stmt: sa.Select[Any]) -> list[Row]: ...
    @abc.abstractmethod
    def raw_expr(self, col: Column) -> sa.ColumnElement[Any]: ...
    @abc.abstractmethod
    def typed_expr(self, col: Column) -> sa.ColumnElement[Any]: ...
    @abc.abstractmethod
    def operand(self, col: Column, raw: object) -> sa.ColumnElement[Any]: ...
    @abc.abstractmethod
    def id_col(self) -> sa.ColumnElement[Any]: ...

    def like_lhs(self, col: Column) -> sa.ColumnElement[Any]:
        return sa.cast(self.raw_expr(col), sa.Text)

    def order_expr(self, col: Column, *, desc: bool) -> sa.ColumnElement[Any]:
        e = self.typed_expr(col)
        return (e.desc() if desc else e.asc()).nulls_last()


class PayloadSource(QuerySource):
    """Owned tables: rows in ``grid_row.payload``."""

    supports_cursor = True

    def _scope(self) -> list[sa.ColumnElement[bool]]:
        return [GridRow.table_id == self.ref.id, GridRow.namespace == self.namespace, GridRow.archived_at.is_(None)]

    def base_query(self) -> sa.Select[Any]:
        return sa.select(GridRow).where(*self._scope())

    def count_query(self) -> sa.Select[Any]:
        return sa.select(sa.func.count()).select_from(GridRow).where(*self._scope())

    async def fetch(self, session: AsyncSession, stmt: sa.Select[Any]) -> list[Row]:
        rows = await session.scalars(stmt)
        return [Row(id=r.id, namespace=r.namespace, values=dict(r.payload)) for r in rows]

    def raw_expr(self, col: Column) -> sa.ColumnElement[Any]:
        return GridRow.payload[col.key].astext

    def typed_expr(self, col: Column) -> sa.ColumnElement[Any]:
        return col.handler.sql_cast(self.raw_expr(col))

    def operand(self, col: Column, raw: object) -> sa.ColumnElement[Any]:
        cfg = col.handler.validate_config(dict(col.spec.config))
        return col.handler.sql_cast(sa.literal(_pg_text(col.handler.normalize(raw, config=cfg))))

    def id_col(self) -> sa.ColumnElement[Any]:
        return cast("sa.ColumnElement[Any]", GridRow.id)


class HostSource(QuerySource):
    """Bound overlay: read straight from a host physical table (read-only)."""

    supports_cursor = False

    def __init__(self, ref: TableRef, namespace: str) -> None:
        super().__init__(ref, namespace)
        b = ref.domain.storage.binding  # type: ignore[attr-defined]
        parts = b.physical_relation.split(".")
        self._schema = parts[0] if len(parts) == 2 else None
        self._pk = b.primary_key
        self._ns_col = b.namespace_column
        self._types: Mapping[str, str] = b.column_types
        self._key_to_col = {c.key: b.column_map.get(c.key, c.key) for c in ref.domain.columns}
        names = {self._pk, *self._key_to_col.values()} | ({self._ns_col} if self._ns_col else set())
        self._host = sa.table(parts[-1], *(sa.column(n) for n in names), schema=self._schema)

    def _scope(self) -> list[sa.ColumnElement[bool]]:
        if self._ns_col is None:
            return []
        return [self._host.c[self._ns_col] == self._typed_literal(self.namespace, self._types.get(self._ns_col))]

    def _typed_literal(self, text: str, udt: str | None) -> sa.ColumnElement[Any]:
        target = _UDT_TO_SA.get(udt) if udt else None
        lit = sa.literal(text)
        return sa.cast(lit, target) if target is not None else lit

    def base_query(self) -> sa.Select[Any]:
        cols = [self._host.c[self._pk].label("id")] + [
            self._host.c[self._key_to_col[c.key]].label(c.key) for c in self.table.columns
        ]
        return sa.select(*cols).where(*self._scope())

    def count_query(self) -> sa.Select[Any]:
        return sa.select(sa.func.count()).select_from(self._host).where(*self._scope())

    async def fetch(self, session: AsyncSession, stmt: sa.Select[Any]) -> list[Row]:
        res = await session.execute(stmt)
        return [
            Row(id=m["id"], namespace=self.namespace, values={c.key: m[c.key] for c in self.table.columns})
            for m in res.mappings()
        ]

    def _col(self, col: Column) -> sa.ColumnElement[Any]:
        return self._host.c[self._key_to_col[col.key]]

    def raw_expr(self, col: Column) -> sa.ColumnElement[Any]:
        return self._col(col)

    def typed_expr(self, col: Column) -> sa.ColumnElement[Any]:
        return self._col(col)

    def operand(self, col: Column, raw: object) -> sa.ColumnElement[Any]:
        udt = self._types.get(self._key_to_col[col.key])
        cfg = col.handler.validate_config(dict(col.spec.config))
        text = _pg_text(col.handler.normalize(raw, config=cfg))
        target = _UDT_TO_SA.get(udt) if udt else None
        return sa.cast(sa.literal(text), target if target is not None else col.handler.promoted_type(config=cfg))

    def id_col(self) -> sa.ColumnElement[Any]:
        return self._host.c[self._pk]


_SOURCES: dict[str, type[QuerySource]] = {"owned": PayloadSource, "overlay": HostSource}


class _GridFilterSource:
    """Adapt grid's :class:`QuerySource` to ``database.filters.FilterSource``.

    The two differ only in their unit of address: ``FilterSource`` resolves a
    column *name* (so the shared walker needs no knowledge of grid's schema
    model), while ``QuerySource`` works on a resolved grid ``Column`` because it
    needs the field-type handler to cast. This adapter is that resolution step,
    plus grid's per-column capability gate.
    """

    def __init__(self, table: Table, src: QuerySource) -> None:
        self._table = table
        self._src = src

    def check(self, column: str, op: FilterOp) -> None:
        col = self._table.column(column)
        caps = effective_capabilities(col.handler, None)
        if not caps.filterable:
            raise BadRequestError(f"column '{col.key}' is not filterable")
        if op not in caps.filter_ops:
            raise BadRequestError(f"operator '{op}' is not supported for column '{col.key}'")

    def raw_expr(self, column: str) -> sa.ColumnElement[Any]:
        return self._src.raw_expr(self._table.column(column))

    def typed_expr(self, column: str) -> sa.ColumnElement[Any]:
        return self._src.typed_expr(self._table.column(column))

    def operand(self, column: str, value: object) -> sa.ColumnElement[Any]:
        return self._src.operand(self._table.column(column), value)

    def like_lhs(self, column: str) -> sa.ColumnElement[Any]:
        return self._src.like_lhs(self._table.column(column))


def compile_filter(node: FilterNode, table: Table, src: QuerySource) -> sa.ColumnElement[bool]:
    """Compile a filter AST against a grid table.

    The AST walker, the operator→SQL mapping, LIKE escaping and the safety
    guards all live in ``database.filters`` now — grid supplies only how a
    column name resolves to an expression.
    """
    return _db_compile_filter(node, _GridFilterSource(table, src))


def compile_order(
    sort: list[SortKey] | None, table: Table, src: QuerySource, *, require_uniform: bool
) -> tuple[list[sa.ColumnElement[Any]], bool]:
    exprs: list[sa.ColumnElement[Any]] = []
    directions: set[SortDirection] = set()
    for key in sort or []:
        col = table.column(key.column)
        if not effective_capabilities(col.handler, None).sortable:
            raise BadRequestError(f"column '{col.key}' is not sortable")
        directions.add(key.direction)
        exprs.append(src.order_expr(col, desc=key.direction is SortDirection.desc))
    if require_uniform and len(directions) > 1:
        raise BadRequestError("cursor/keyset sorting requires a uniform direction")
    ascending = SortDirection.desc not in directions
    exprs.append(src.id_col().asc() if ascending else src.id_col().desc())
    return exprs, ascending


def _decode_cursor(cursor: str) -> list[Any]:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except ValueError, json.JSONDecodeError:
        raise BadRequestError("invalid cursor") from None


def _encode_cursor(row: Row, sort_keys: list[str]) -> str:
    payload = [row.values.get(k) for k in sort_keys] + [str(row.id)]
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _keyset_predicate(
    sort_keys: list[str], table: Table, src: QuerySource, boundary: list[Any], ascending: bool
) -> sa.ColumnElement[bool]:
    levels: list[tuple[sa.ColumnElement[Any], Any, bool]] = []
    try:
        for i, key in enumerate(sort_keys):
            col = table.column(key)
            raw = boundary[i]
            if raw is None:
                levels.append((src.typed_expr(col), None, True))
            else:
                levels.append((src.typed_expr(col), src.operand(col, raw), False))
        levels.append((src.id_col(), sa.literal(uuid.UUID(str(boundary[-1]))), False))
    except (ValueError, TypeError) as exc:
        raise BadRequestError("invalid cursor") from exc

    def _eq(expr: sa.ColumnElement[Any], value: object, is_null: bool) -> sa.ColumnElement[bool]:
        return expr.is_(None) if is_null else (expr == value)

    ors: list[sa.ColumnElement[bool]] = []
    for i, (expr, value, is_null) in enumerate(levels):
        if is_null:
            continue
        prefix = [_eq(*levels[j]) for j in range(i)]
        after = expr > value if ascending else expr < value
        if expr is not src.id_col():
            after = sa.or_(after, expr.is_(None))
        ors.append(sa.and_(*prefix, after))
    return sa.or_(*ors)


async def run_query(
    session: AsyncSession,
    ref: TableRef,
    *,
    filter: FilterNode | None = None,
    sort: list[SortKey] | None = None,
    mode: BrowseMode = BrowseMode.page,
    limit: int = 50,
    offset: int = 0,
    cursor: str | None = None,
    include_total: bool = False,
    namespace: str | None = None,
) -> Page:
    ns = ref.namespace if namespace is None else namespace
    src = _SOURCES[ref.kind](ref, ns)
    table = ref.domain

    async with read_boundary():
        preds: list[sa.ColumnElement[bool]] = []
        if table.spec.scope_predicate:
            preds.append(compile_filter(parse_filter(dict(table.spec.scope_predicate)), table, src))
        if filter is not None:
            preds.append(compile_filter(filter, table, src))
        order, ascending = compile_order(sort, table, src, require_uniform=mode is BrowseMode.cursor)
        limit = max(1, min(limit, MAX_LIMIT))

        if mode is BrowseMode.cursor:
            if not src.supports_cursor:
                raise BadRequestError("cursor pagination is not supported for overlay tables; use offset")
            sort_keys = [k.column for k in sort or []]
            if cursor is not None:
                boundary = _decode_cursor(cursor)
                if not isinstance(boundary, list) or len(boundary) != len(sort_keys) + 1:
                    raise BadRequestError("invalid cursor")
                preds.append(_keyset_predicate(sort_keys, table, src, boundary, ascending))
            stmt = src.base_query().where(*preds).order_by(*order).limit(limit + 1)
            rows = await src.fetch(session, stmt)
            next_cursor = None
            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]
                next_cursor = _encode_cursor(rows[-1], sort_keys)
            await _resolve_derived(session, ref, rows)
            total = await _count(session, src, preds) if include_total else None
            # `has_more` was computed here all along and then thrown away, because
            # grid's own page shape had no field for it.
            return Page(items=rows, has_more=has_more, next_cursor=next_cursor, total=total)

        if not 0 <= offset <= MAX_OFFSET:
            raise BadRequestError(f"offset must be between 0 and {MAX_OFFSET}")
        # `limit + 1`, same as the cursor branch: `len(rows) == limit` would report
        # `has_more` on an exactly-full final page, and `total` is optional so it
        # cannot be relied on to answer the question.
        stmt = src.base_query().where(*preds).order_by(*order).limit(limit + 1).offset(offset)
        rows = await src.fetch(session, stmt)
        has_more = len(rows) > limit
        rows = rows[:limit]
        await _resolve_derived(session, ref, rows)
        total = await _count(session, src, preds) if include_total else None
        return Page(items=rows, has_more=has_more, total=total)


async def _count(session: AsyncSession, src: QuerySource, preds: list[sa.ColumnElement[bool]]) -> int:
    return await session.scalar(src.count_query().where(*preds)) or 0


__all__ = ["HostSource", "PayloadSource", "QuerySource", "compile_filter", "compile_order", "run_query"]
