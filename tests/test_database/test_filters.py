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

"""Unit tests for forktex_core.database.filters — **no container required**.

Filter compilation is a pure function of the AST plus a source, so it can be
asserted by compiling to SQL text. Previously this logic lived inside grid and
could only be exercised through a live query against Postgres.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from forktex_core.database.filters import (
    MAX_FILTER_DEPTH,
    MAX_IN_ITEMS,
    And,
    ColumnSource,
    Comparison,
    FilterOp,
    Not,
    Or,
    SortDirection,
    SortKey,
    compile_filter,
    parse_filter,
)
from forktex_core.error import BadRequestError

_PG = postgresql.dialect()


class _Base(DeclarativeBase):
    pass


class Widget(_Base):
    __tablename__ = "widget"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))
    score: Mapped[int] = mapped_column(sa.Integer)


def _sql(node, *, allowed: set[str] | None = None) -> str:
    """Rendered SQL with literals inlined.

    Note the psycopg paramstyle doubles ``%`` to ``%%`` in rendered output, so
    assert on LIKE *patterns* via :func:`_params` rather than on this string.
    """
    expr = compile_filter(node, ColumnSource(Widget, allowed=allowed))
    return " ".join(str(expr.compile(dialect=_PG, compile_kwargs={"literal_binds": True})).split())


def _params(node, *, allowed: set[str] | None = None) -> dict:
    """The bound parameter values — paramstyle-independent, so LIKE patterns can
    be asserted exactly as the driver will receive them."""
    expr = compile_filter(node, ColumnSource(Widget, allowed=allowed))
    return dict(expr.compile(dialect=_PG).params)


# ---------------------------------------------------------------------------
# parse_filter — the JSON boundary
# ---------------------------------------------------------------------------


def test_parse_builds_a_nested_ast():
    node = parse_filter(
        {
            "and": [
                {"column": "name", "op": "eq", "value": "x"},
                {"or": [{"column": "score", "op": "gt", "value": 1}, {"not": {"column": "score", "op": "is_null"}}]},
            ]
        }
    )
    assert isinstance(node, And)
    assert isinstance(node.children[1], Or)
    assert isinstance(node.children[1].children[1], Not)


def test_parse_passes_a_typed_node_through():
    node = Comparison(column="name", op=FilterOp.eq, value="x")
    assert parse_filter(node) is node


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"and": []}, "empty 'and'"),
        ({"or": []}, "empty 'or'"),
        ({"column": "name"}, "malformed filter node"),
        ({"op": "eq"}, "malformed filter node"),
        ({"column": "name", "op": "NOPE"}, "unknown operator"),
    ],
)
def test_parse_rejects_malformed_input(payload, message):
    with pytest.raises(BadRequestError, match=message):
        parse_filter(payload)


def test_parse_rejects_pathological_nesting():
    deep: dict = {"column": "name", "op": "eq", "value": 1}
    for _ in range(MAX_FILTER_DEPTH + 2):
        deep = {"not": deep}
    with pytest.raises(BadRequestError, match="nesting exceeds"):
        parse_filter(deep)


# ---------------------------------------------------------------------------
# Structural compilation — the walker
# ---------------------------------------------------------------------------


def test_and_or_not_compile_structurally():
    a = Comparison(column="score", op=FilterOp.gt, value=1)
    b = Comparison(column="score", op=FilterOp.lt, value=9)
    assert _sql(And(children=(a, b))) == "widget.score > 1 AND widget.score < 9"
    assert _sql(Or(children=(a, b))) == "widget.score > 1 OR widget.score < 9"
    assert _sql(Not(child=a)) == "widget.score <= 1"


@pytest.mark.parametrize(
    "op,expected",
    [
        (FilterOp.eq, "widget.score = 5"),
        (FilterOp.ne, "widget.score != 5"),
        (FilterOp.lt, "widget.score < 5"),
        (FilterOp.lte, "widget.score <= 5"),
        (FilterOp.gt, "widget.score > 5"),
        (FilterOp.gte, "widget.score >= 5"),
    ],
)
def test_ordered_comparisons(op, expected):
    assert _sql(Comparison(column="score", op=op, value=5)) == expected


def test_is_null_truthiness():
    assert _sql(Comparison(column="name", op=FilterOp.is_null)) == "widget.name IS NULL"
    assert _sql(Comparison(column="name", op=FilterOp.is_null, value=False)) == "widget.name IS NOT NULL"


def test_between_requires_a_pair():
    assert _sql(Comparison(column="score", op=FilterOp.between, value=[1, 9])) == "widget.score BETWEEN 1 AND 9"
    with pytest.raises(BadRequestError, match=r"\[low, high\]"):
        _sql(Comparison(column="score", op=FilterOp.between, value=[1]))


def test_in_and_not_in():
    assert "widget.score IN (1, 2)" in _sql(Comparison(column="score", op=FilterOp.in_, value=[1, 2]))
    assert "widget.score NOT IN (1, 2)" in _sql(Comparison(column="score", op=FilterOp.not_in, value=[1, 2]))


def test_in_list_is_bounded():
    huge = list(range(MAX_IN_ITEMS + 1))
    with pytest.raises(BadRequestError, match="exceeds"):
        _sql(Comparison(column="score", op=FilterOp.in_, value=huge))


# ---------------------------------------------------------------------------
# LIKE escaping — a user term must never act as a wildcard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op,pattern",
    [
        (FilterOp.contains, "%term%"),
        (FilterOp.starts_with, "term%"),
        (FilterOp.ends_with, "%term"),
    ],
)
def test_like_patterns(op, pattern):
    """The wildcard wrapping differs per operator; assert the bound value."""
    assert pattern in _params(Comparison(column="name", op=op, value="term")).values()


def test_icontains_uses_ilike():
    assert "ILIKE" in _sql(Comparison(column="name", op=FilterOp.icontains, value="t"))


def test_like_metacharacters_in_the_user_term_are_escaped():
    """`%`, `_` and `\\` in a search term must match literally, not as wildcards.

    Without escaping, searching for "50%" would match everything.
    """
    node = Comparison(column="name", op=FilterOp.contains, value="50%_x")
    # the escaped term is wrapped in the operator's own (unescaped) wildcards
    assert r"%50\%\_x%" in _params(node).values()
    assert "ESCAPE" in _sql(node)


def test_fuzzy_uses_the_trigram_operator():
    assert "%" in _sql(Comparison(column="name", op=FilterOp.fuzzy, value="abc"))


# ---------------------------------------------------------------------------
# ColumnSource — the generic source the dead DSL never shipped
# ---------------------------------------------------------------------------


def test_unknown_column_is_rejected():
    with pytest.raises(BadRequestError, match="unknown column"):
        _sql(Comparison(column="nope", op=FilterOp.eq, value=1))


def test_allow_list_restricts_which_columns_are_filterable():
    """Always pass `allowed` for an untrusted filter, or every mapped column —
    including ones you did not mean to expose — becomes filterable."""
    node = Comparison(column="score", op=FilterOp.eq, value=1)
    assert _sql(node, allowed={"score"})  # permitted
    with pytest.raises(BadRequestError, match="not filterable"):
        _sql(node, allowed={"name"})


# ---------------------------------------------------------------------------
# SortKey
# ---------------------------------------------------------------------------


def test_sort_key_parses_dicts_and_passes_through_typed():
    assert SortKey.parse({"column": "name"}).direction is SortDirection.asc
    assert SortKey.parse({"column": "name", "direction": "DESC"}).direction is SortDirection.desc
    typed = SortKey(column="name", direction=SortDirection.desc)
    assert SortKey.parse(typed) is typed


def test_sort_key_rejects_a_bad_direction():
    with pytest.raises(BadRequestError, match="invalid sort direction"):
        SortKey.parse({"column": "name", "direction": "sideways"})
