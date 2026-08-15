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

"""The filter AST and sort spec — one typed query vocabulary for the library.

Two parallel implementations of this used to exist: a serialisable
``FilterSpec``/``CompoundFilter`` DSL in ``database.query`` that had **no
consumers at all**, and this AST in ``grid.read.filters``, which is the one
actually used and tested. The dead one is gone; this is the survivor, promoted
so ``flow`` and any other consumer can share it instead of hand-building
``WHERE`` clauses by string concatenation.

The split of responsibility:

- **Here**: the operator vocabulary (:class:`FilterOp`), the AST nodes, the
  JSON-boundary adapter (:func:`parse_filter`), the sort spec, and the
  schema-agnostic walker (:func:`compile_filter`) with its operator→SQL mapping,
  LIKE escaping and safety guards.
- **In each consumer**: how a column *name* becomes a SQL expression. grid
  extracts from a JSONB payload and casts per field type; a plain ORM consumer
  just resolves an attribute. That difference is the :class:`FilterSource`
  protocol below, so the walker is written once.

Callers outside Python (HTTP bodies, stored specs) pass dicts;
:func:`parse_filter` / :meth:`SortKey.parse` are the boundary adapters that
validate structure, operators and nesting depth once, up front.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable

import sqlalchemy as sa

from forktex_core.error import BadRequestError
from forktex_core.types import BaseValueObject, BaseWireValueObject

#: Guard against a pathological nesting depth in a caller-supplied filter.
MAX_FILTER_DEPTH = 50

#: Guard against an unbounded ``IN`` list.
MAX_IN_ITEMS = 1000


class FilterOp(StrEnum):
    """The vocabulary of filter operators a column may support.

    Stored only inside filter ASTs (never as a column value), so member name
    and value may differ where Python keywords intrude.
    """

    eq = "eq"
    ne = "ne"
    lt = "lt"
    lte = "lte"
    gt = "gt"
    gte = "gte"
    in_ = "in"
    not_in = "not_in"
    contains = "contains"
    icontains = "icontains"
    starts_with = "startswith"
    ends_with = "endswith"
    between = "between"
    is_null = "is_null"
    fuzzy = "fuzzy"


#: Operators that compile to a ``LIKE``/``ILIKE`` pattern match.
LIKE_OPS = frozenset({FilterOp.contains, FilterOp.icontains, FilterOp.starts_with, FilterOp.ends_with})


class SortDirection(StrEnum):
    asc = "asc"
    desc = "desc"


class SortKey(BaseWireValueObject):
    column: str
    direction: SortDirection = SortDirection.asc

    @classmethod
    def parse(cls, obj: SortKey | Mapping[str, str]) -> SortKey:
        if isinstance(obj, SortKey):
            return obj
        try:
            direction = SortDirection(str(obj.get("direction", "asc")).lower())
        except ValueError:
            raise BadRequestError(f"invalid sort direction {obj.get('direction')!r}") from None
        return cls(column=obj["column"], direction=direction)


class Comparison(BaseValueObject):
    column: str
    op: FilterOp
    value: Any = None


class And(BaseValueObject):
    __match_args__: ClassVar[tuple[str, ...]] = ("children",)

    children: tuple[FilterNode, ...]


class Or(BaseValueObject):
    __match_args__: ClassVar[tuple[str, ...]] = ("children",)

    children: tuple[FilterNode, ...]


class Not(BaseValueObject):
    __match_args__: ClassVar[tuple[str, ...]] = ("child",)

    child: FilterNode


FilterNode = And | Or | Not | Comparison

# The union is self-referential, so the forward reference in each node's field
# can only be resolved once `FilterNode` exists.
And.model_rebuild()
Or.model_rebuild()
Not.model_rebuild()
Comparison.model_rebuild()


def parse_filter(obj: FilterNode | Mapping[str, Any], *, _depth: int = 0) -> FilterNode:
    """Validate + build a :data:`FilterNode` from the dict wire form.

    An already-typed node passes straight through, so callers can accept either
    shape at their boundary without branching.
    """
    if isinstance(obj, (And, Or, Not, Comparison)):
        return obj
    if _depth > MAX_FILTER_DEPTH:
        raise BadRequestError(f"filter nesting exceeds {MAX_FILTER_DEPTH} levels")
    if "and" in obj:
        if not obj["and"]:
            raise BadRequestError("empty 'and' filter")
        return And(children=tuple(parse_filter(c, _depth=_depth + 1) for c in obj["and"]))
    if "or" in obj:
        if not obj["or"]:
            raise BadRequestError("empty 'or' filter")
        return Or(children=tuple(parse_filter(c, _depth=_depth + 1) for c in obj["or"]))
    if "not" in obj:
        return Not(child=parse_filter(obj["not"], _depth=_depth + 1))
    if "column" not in obj or "op" not in obj:
        raise BadRequestError(f"malformed filter node: {obj!r}")
    try:
        op = FilterOp(obj["op"])
    except ValueError:
        raise BadRequestError(f"unknown operator {obj['op']!r}") from None
    return Comparison(column=obj["column"], op=op, value=obj.get("value"))


@runtime_checkable
class FilterSource(Protocol):
    """How a column *name* becomes SQL — the one thing consumers differ on.

    Implementations decide where a column lives and how its values are typed.
    ``grid`` extracts from a JSONB payload and casts per field type; a plain
    mapped-table consumer resolves an ORM attribute (see :class:`ColumnSource`).
    """

    def check(self, column: str, op: FilterOp) -> None:
        """Raise ``BadRequestError`` if ``column`` does not support ``op``."""

    def raw_expr(self, column: str) -> sa.ColumnElement[Any]:
        """The untyped (text-ish) expression — used for NULL and LIKE tests."""
        ...

    def typed_expr(self, column: str) -> sa.ColumnElement[Any]:
        """The typed expression — used for ordered comparisons."""
        ...

    def operand(self, column: str, value: object) -> sa.ColumnElement[Any]:
        """``value`` coerced to a bound expression comparable with ``typed_expr``."""
        ...

    def like_lhs(self, column: str) -> sa.ColumnElement[Any]:
        """The left-hand side of a LIKE/ILIKE — text by construction."""
        ...


class ColumnSource:
    """A :class:`FilterSource` over the plain mapped columns of one entity.

    The generic case the dead ``database.query`` DSL was reaching for: give it
    an ORM class and callers get filtering over its columns for free, with no
    per-consumer compiler.

    ``allowed`` restricts which columns may be filtered — always pass it when
    the filter comes from an untrusted caller, otherwise every mapped column
    (including ones you did not mean to expose) is filterable.
    """

    def __init__(self, entity: object, *, allowed: set[str] | None = None) -> None:
        self._entity = entity
        self._allowed = allowed

    def _column(self, column: str) -> sa.ColumnElement[Any]:
        if self._allowed is not None and column not in self._allowed:
            raise BadRequestError(f"column {column!r} is not filterable")
        attr = getattr(self._entity, column, None)
        if attr is None:
            raise BadRequestError(f"unknown column {column!r}")
        return attr

    def check(self, column: str, op: FilterOp) -> None:
        self._column(column)  # existence/allow-list check only; all ops permitted

    def raw_expr(self, column: str) -> sa.ColumnElement[Any]:
        return self._column(column)

    def typed_expr(self, column: str) -> sa.ColumnElement[Any]:
        return self._column(column)

    def operand(self, column: str, value: object) -> sa.ColumnElement[Any]:
        return sa.literal(value)

    def like_lhs(self, column: str) -> sa.ColumnElement[Any]:
        return sa.cast(self._column(column), sa.Text)


def compile_filter(node: FilterNode, source: FilterSource) -> sa.ColumnElement[bool]:
    """Compile a :data:`FilterNode` to a SQLAlchemy boolean expression.

    Structural, so it is identical for every consumer; only ``source`` varies.
    """
    match node:
        case And(children=children):
            return sa.and_(*(compile_filter(c, source) for c in children))
        case Or(children=children):
            return sa.or_(*(compile_filter(c, source) for c in children))
        case Not(child=child):
            return sa.not_(compile_filter(child, source))
        case Comparison():
            return compile_comparison(node, source)
    raise BadRequestError(f"unsupported filter node: {node!r}")  # pragma: no cover


def _like_pattern(op: FilterOp, value: object) -> str:
    """Escape LIKE metacharacters, then wrap per operator.

    ``\\``, ``%`` and ``_`` are escaped so a user-supplied term is matched
    literally rather than acting as a wildcard.
    """
    term = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return {
        FilterOp.contains: f"%{term}%",
        FilterOp.icontains: f"%{term}%",
        FilterOp.starts_with: f"{term}%",
        FilterOp.ends_with: f"%{term}",
    }[op]


def compile_comparison(node: Comparison, source: FilterSource) -> sa.ColumnElement[bool]:
    """Compile a single leaf comparison."""
    column, op, value = node.column, node.op, node.value
    source.check(column, op)

    if op is FilterOp.is_null:
        want_null = True if value is None else bool(value)
        expr = source.raw_expr(column)
        return expr.is_(None) if want_null else expr.isnot(None)

    if op is FilterOp.fuzzy:
        # pg_trgm similarity; requires the extension on the target database.
        return source.like_lhs(column).op("%")(str(value))

    if op in LIKE_OPS:
        lhs = source.like_lhs(column)
        pattern = _like_pattern(op, value)
        return lhs.ilike(pattern, escape="\\") if op is FilterOp.icontains else lhs.like(pattern, escape="\\")

    if op in (FilterOp.in_, FilterOp.not_in):
        items = list(value or [])
        if len(items) > MAX_IN_ITEMS:
            raise BadRequestError(f"'{op.value}' list exceeds {MAX_IN_ITEMS} items")
        rhs = [source.operand(column, v) for v in items]
        expr = source.typed_expr(column)
        return expr.notin_(rhs) if op is FilterOp.not_in else expr.in_(rhs)

    if op is FilterOp.between:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise BadRequestError("'between' requires [low, high]")
        return source.typed_expr(column).between(source.operand(column, value[0]), source.operand(column, value[1]))

    lhs, rhs = source.typed_expr(column), source.operand(column, value)
    return {
        FilterOp.eq: lhs == rhs,
        FilterOp.ne: lhs != rhs,
        FilterOp.lt: lhs < rhs,
        FilterOp.lte: lhs <= rhs,
        FilterOp.gt: lhs > rhs,
        FilterOp.gte: lhs >= rhs,
    }[op]


__all__ = [
    "LIKE_OPS",
    "MAX_FILTER_DEPTH",
    "MAX_IN_ITEMS",
    "And",
    "ColumnSource",
    "Comparison",
    "FilterNode",
    "FilterOp",
    "FilterSource",
    "Not",
    "Or",
    "SortDirection",
    "SortKey",
    "compile_comparison",
    "compile_filter",
    "parse_filter",
]
