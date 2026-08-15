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

"""Generic async CRUD utilities for SQLAlchemy.

Provides paginated queries (page-based and cursor-based), single-record
lookups, and creation helpers with conflict detection.
"""

from collections.abc import Callable
from math import ceil
from typing import Any

from pydantic import AliasChoices, Field
from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from forktex_core.database.integrity import integrity_boundary
from forktex_core.database.pagination import Page, decode_cursor, encode_cursor, keyset_predicate
from forktex_core.error import BadRequestError, ConflictError
from forktex_core.log import get_logger

logger = get_logger(__name__)


class PageResponse[T](Page[T]):
    """Offset-pagination response: :class:`Page` plus page-number metadata.

    A ``Page`` subclass rather than a parallel model — it used to be one of four
    disagreeing page shapes. ``data`` and ``total_count`` are kept as the accepted
    and serialised names, because that is what offset-paginating callers already
    read; ``items`` and ``total`` are the canonical Python names shared with every
    other page in the library.

    Prefer :class:`forktex_core.database.pagination.Page` directly for new code;
    this shape exists for the offset metadata (``current_page`` / ``total_pages``),
    which keyset pagination has no equivalent of.
    """

    items: list[T] = Field(
        default_factory=list,
        validation_alias=AliasChoices("items", "data"),
        serialization_alias="data",
    )
    total: int | None = Field(
        default=None,
        validation_alias=AliasChoices("total", "total_count", "totalCount"),
        serialization_alias="totalCount",
    )
    limit: int
    current_page: int | None = None
    total_pages: int | None = None

    @property
    def data(self) -> list[T]:
        """Alias of :attr:`items`."""
        return self.items

    @property
    def total_count(self) -> int | None:
        """Alias of :attr:`total`."""
        return self.total

    def apply_to_page_data[U](self, map_func: Callable[[T], U]) -> PageResponse[U]:
        return PageResponse[U](
            items=[map_func(item) for item in self.items],
            has_more=self.has_more,
            limit=self.limit,
            total=self.total,
            current_page=self.current_page,
            total_pages=self.total_pages,
        )


class ScrollResponse[T](Page[T]):
    """Keyset ("scroll") pagination response. See :func:`paginate_scroll`.

    Structurally :class:`Page` with the echoed ``limit`` added — it was a verbatim
    re-declaration of the same four fields before. ``data`` stays the accepted and
    serialised name, as in :class:`PageResponse`.
    """

    items: list[T] = Field(
        default_factory=list,
        validation_alias=AliasChoices("items", "data"),
        serialization_alias="data",
    )
    limit: int

    @property
    def data(self) -> list[T]:
        """Alias of :attr:`items`."""
        return self.items

    def apply_to_scroll_data[U](self, map_func: Callable[[T], U]) -> ScrollResponse[U]:
        return ScrollResponse[U](
            items=[map_func(item) for item in self.items],
            limit=self.limit,
            has_more=self.has_more,
            next_cursor=self.next_cursor,
        )


async def get[T](
    session: AsyncSession,
    model: type[T],
    value: object,
    *,
    key: str = "id",
    options: list | None = None,
) -> T | None:
    """Retrieve a single record by column value (default: primary key)."""
    if not hasattr(model, key):
        raise AttributeError(f"{model.__name__} has no attribute '{key}'")

    stmt = select(model).where(getattr(model, key) == value)
    if options:
        stmt = stmt.options(*options)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def find_one_by[T](
    session: AsyncSession,
    model: type[T],
    **filters: object,
) -> T | None:
    """Find a single record matching all keyword filters."""
    stmt = select(model).filter_by(**filters)
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def list_all[T](
    session: AsyncSession,
    model: type[T],
    *,
    options: list | None = None,
) -> list[T]:
    """Return all records of a model (use sparingly on large tables)."""
    stmt = select(model)
    if options:
        stmt = stmt.options(*options)
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def create[T](
    session: AsyncSession,
    model: type[T],
    **values: object,
) -> T:
    """Create a new record.

    Constraint violations are translated by
    :func:`forktex_core.database.integrity.integrity_boundary`: a unique
    violation raises ``AlreadyExistsError``, other violations (FK, NOT NULL,
    CHECK) raise ``BadRequestError``. This previously mapped *every*
    ``IntegrityError`` to ``ConflictError`` and embedded the raw driver message
    — which could quote user data — in the user-facing error.
    """
    obj = model(**values)
    session.add(obj)
    async with integrity_boundary():
        await session.flush()
    await session.refresh(obj)
    return obj


async def paginate[T](
    session: AsyncSession,
    model: type[T],
    page: int = 1,
    page_size: int = 100,
    conditions: list[ColumnElement] | None = None,
    order_by: list[ColumnElement] | None = None,
    joins: list[InstrumentedAttribute] | None = None,
    options: list | None = None,
) -> PageResponse[T]:
    """Page-based pagination with total count."""
    stmt = select(model)

    if options:
        stmt = stmt.options(*options)
    if joins:
        for join in joins:
            stmt = stmt.outerjoin(join)
    if conditions:
        stmt = stmt.where(*conditions)
    if order_by:
        stmt = stmt.order_by(*order_by)

    return await _paginate_query(session, stmt, page, page_size)


async def paginate_scroll[T](
    session: AsyncSession,
    model: type[T],
    limit: int = 20,
    conditions: list[ColumnElement] | None = None,
    order_by: list[ColumnElement] | None = None,
    joins: list[InstrumentedAttribute] | None = None,
    options: list | None = None,
    *,
    keyset: list[ColumnElement] | None = None,
    cursor: str | None = None,
    ascending: bool = True,
) -> ScrollResponse[T]:
    """Keyset ("scroll") pagination. Fetches ``limit+1`` to detect ``has_more``.

    Pass ``keyset`` — the ordering expressions, most-significant first, ending
    in a unique tiebreaker — to get a working ``next_cursor``, then hand that
    back as ``cursor`` for the following page. ``keyset`` must match
    ``order_by`` or pages will skip or repeat rows.

    Without ``keyset`` this degrades to "first page only": that was the previous
    behaviour, which accepted no cursor and never populated ``next_cursor``
    despite being documented as cursor-based, so it could never advance.
    """
    stmt = select(model)

    if options:
        stmt = stmt.options(*options)
    if joins:
        for join in joins:
            stmt = stmt.outerjoin(join)
    if conditions:
        stmt = stmt.where(*conditions)
    if keyset and cursor is not None:
        boundary = decode_cursor(cursor, expected_length=len(keyset))
        stmt = stmt.where(keyset_predicate(list(zip(keyset, boundary, strict=True)), ascending=ascending))
    if order_by:
        stmt = stmt.order_by(*order_by)

    return await _paginate_scroll_query(session, stmt, limit, keyset=keyset)


async def _paginate_query[T](
    session: AsyncSession,
    query: Select[tuple[T]] | Select[Any],
    page: int = 1,
    page_size: int = 100,
) -> PageResponse[T]:
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 10

    total_count_stmt = select(func.count()).select_from(query.subquery())
    total_count = (await session.execute(total_count_stmt)).scalar_one()

    offset = (page - 1) * page_size
    paginated_stmt = query.offset(offset).limit(page_size)
    result = await session.execute(paginated_stmt)
    data = list(result.scalars().all())

    has_more = (page * page_size) < total_count
    total_pages = ceil(total_count / page_size) if page_size else 1

    return PageResponse(
        items=data,
        has_more=has_more,
        limit=page_size,
        total=total_count,
        current_page=page,
        total_pages=total_pages,
    )


async def _paginate_scroll_query[T](
    session: AsyncSession,
    query: Select[tuple[T]] | Select[Any],
    limit: int = 20,
    *,
    keyset: list[ColumnElement] | None = None,
) -> ScrollResponse[T]:
    if limit < 1:
        limit = 20

    scroll_stmt = query.limit(limit + 1)
    result = await session.execute(scroll_stmt)
    data = list(result.scalars().all())

    has_more = len(data) > limit
    if has_more:
        data = data[:limit]

    next_cursor: str | None = None
    if has_more and data and keyset:
        # The boundary is the last row's value for each ordering expression, so
        # the next page resumes exactly where this one stopped.
        last = data[-1]
        next_cursor = encode_cursor([_value_of(last, expr) for expr in keyset])

    return ScrollResponse(
        items=data,
        has_more=has_more,
        limit=limit,
        next_cursor=next_cursor,
    )


def _value_of(row: object, expr: ColumnElement) -> object:
    """Read the value an ordering expression selected, off a fetched row.

    Works for a mapped attribute (``Model.col``), which is what keyset ordering
    uses in practice; a computed expression has no attribute to read and is
    rejected rather than silently producing a cursor that cannot resume.
    """
    key = getattr(expr, "key", None) or getattr(expr, "name", None)
    if key is None or not hasattr(row, str(key)):
        raise BadRequestError("keyset ordering must use mapped columns so a cursor can be built")
    return getattr(row, str(key))


__all__ = [
    "ConflictError",
    "PageResponse",
    "ScrollResponse",
    "create",
    "find_one_by",
    "get",
    "list_all",
    "paginate",
    "paginate_scroll",
]
