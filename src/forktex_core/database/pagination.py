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

"""One pagination shape, and the keyset machinery behind it.

Four incompatible shapes used to coexist: ``crud.PageResponse`` (offset, always
counted), ``crud.ScrollResponse`` (``limit+1``, and its ``next_cursor`` was
never populated — so it could not page past the first page), ``grid.Page``
(``rows``/``total``) and ``flow.InstancePage`` (``items``/``total``, and never
actually constructed). Field names collided three ways — ``data`` vs ``rows`` vs
``items``, ``total`` vs ``total_count`` — so nothing could be shared.

:class:`Page` is the single shape. The cursor design is grid's, because it was
the correct one: a **positional array** of the actual sort-key values plus a
tiebreaker, so it works for any sort. flow's cursor stored only
``{started_at, id}`` while its ``ORDER BY`` could be any of four columns, which
made the keyset predicate disagree with the ordering and skip or duplicate rows.

A malformed cursor raises ``BadRequestError`` rather than silently restarting
from page one — the caller asked for a specific position and should be told the
token is bad, not handed different data.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import sqlalchemy as sa
from pydantic import Field

from forktex_core.error import BadRequestError
from forktex_core.types import BaseAppModel

__all__ = [
    "Page",
    "decode_cursor",
    "encode_cursor",
    "keyset_predicate",
]


class Page[T](BaseAppModel):
    """One page of results.

    A ``BaseAppModel``, so it emits camelCase on the wire (``nextCursor``,
    ``hasMore``) — matching the error envelope rather than contradicting it, as
    the previous snake_case-only shapes did.

    ``total`` is optional and ``None`` by default because counting is a second
    full predicate evaluation: two of the four predecessors computed it
    unconditionally on every page. Ask for it explicitly when you need it.
    """

    items: list[T] = Field(default_factory=list)
    has_more: bool = False
    next_cursor: str | None = None
    total: int | None = None


def encode_cursor(values: list[Any]) -> str:
    """Encode keyset boundary ``values`` as a URL-safe base64 JSON array.

    Positional, so the caller decides what the sort keys are; the last element
    is conventionally the unique tiebreaker.
    """
    return base64.urlsafe_b64encode(json.dumps(values, default=str).encode()).decode()


def decode_cursor(cursor: str, *, expected_length: int | None = None) -> list[Any]:
    """Decode a cursor produced by :func:`encode_cursor`.

    Raises ``BadRequestError`` on anything malformed — including a base64 or
    UTF-8 error, which the predecessor implementations let escape as an
    unhandled ``binascii.Error``/``UnicodeDecodeError``.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode())
        values = json.loads(raw.decode())
    except (ValueError, TypeError) as exc:
        raise BadRequestError("invalid cursor") from exc
    if not isinstance(values, list):
        raise BadRequestError("invalid cursor")
    if expected_length is not None and len(values) != expected_length:
        raise BadRequestError("invalid cursor")
    return values


def keyset_predicate(
    # `SQLColumnExpression` rather than `ColumnElement`: it is the common base of
    # Core columns *and* ORM instrumented attributes, so callers can pass
    # `Run.started_at` directly instead of unwrapping it.
    levels: list[tuple[sa.SQLColumnExpression[Any], Any]],
    *,
    ascending: bool,
) -> sa.ColumnElement[bool]:
    """Build a keyset ("seek") predicate for a compound sort.

    ``levels`` pairs each ordering expression with the boundary value from the
    cursor, most-significant first; the final level must be a unique tiebreaker
    so the ordering is total. A ``None`` boundary value marks a NULL at that
    level, which cannot be compared with ``>``/``<`` and is therefore skipped as
    a strict-inequality candidate.

    Produces the standard lexicographic disjunction::

        (a > a0) OR (a = a0 AND b > b0) OR (a = a0 AND b = b0 AND id > id0)

    Note the expression must match the query's ``ORDER BY`` exactly, or pages
    will skip or repeat rows. Passing the ordering expressions themselves is the
    way to guarantee that.
    """
    if not levels:
        raise ValueError("keyset_predicate requires at least one level")

    def _eq(expr: sa.SQLColumnExpression[Any], value: object) -> sa.ColumnElement[bool]:
        return expr.is_(None) if value is None else (expr == value)

    disjuncts: list[sa.ColumnElement[bool]] = []
    for i, (expr, value) in enumerate(levels):
        if value is None:
            # No strict inequality is meaningful against NULL at this level.
            continue
        prefix = [_eq(e, v) for e, v in levels[:i]]
        after = expr > value if ascending else expr < value
        if i < len(levels) - 1:
            # A NULL at a non-final level sorts after non-NULL values under
            # `NULLS LAST`, so it is still "after" the boundary.
            after = sa.or_(after, expr.is_(None))
        disjuncts.append(sa.and_(*prefix, after))

    if not disjuncts:  # every level was NULL — nothing can follow deterministically
        raise BadRequestError("invalid cursor")
    return sa.or_(*disjuncts)
