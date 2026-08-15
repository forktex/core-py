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

"""Unit tests for forktex_core.database.pagination — no container required."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from forktex_core.database.pagination import (
    Page,
    decode_cursor,
    encode_cursor,
    keyset_predicate,
)
from forktex_core.error import BadRequestError

_PG = postgresql.dialect()


class _Base(DeclarativeBase):
    pass


class Job(_Base):
    __tablename__ = "job"
    id: Mapped[int] = mapped_column(primary_key=True)
    finished_at: Mapped[str | None] = mapped_column(sa.String(30), nullable=True)


def _sql(expr) -> str:
    return " ".join(str(expr.compile(dialect=_PG, compile_kwargs={"literal_binds": True})).split())


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def test_page_defaults_are_an_empty_first_page():
    page: Page[int] = Page()
    assert page.items == []
    assert page.has_more is False
    assert page.next_cursor is None
    assert page.total is None  # counting is opt-in


def test_page_emits_camelcase_on_the_wire():
    """A BaseAppModel, so it matches the error envelope's convention instead of
    contradicting it the way the snake_case-only predecessors did."""
    dumped = Page[int](items=[1], has_more=True, next_cursor="abc", total=9).model_dump(by_alias=True)
    assert dumped == {"items": [1], "hasMore": True, "nextCursor": "abc", "total": 9}


def test_page_accepts_camelcase_input_too():
    page = Page[int].model_validate({"items": [], "hasMore": True, "nextCursor": "z"})
    assert page.has_more is True
    assert page.next_cursor == "z"


# ---------------------------------------------------------------------------
# Cursor round-trip
# ---------------------------------------------------------------------------


def test_cursor_round_trips_positional_values():
    token = encode_cursor(["2026-01-01T00:00:00+00:00", 42])
    assert decode_cursor(token) == ["2026-01-01T00:00:00+00:00", 42]


def test_cursor_is_url_safe():
    token = encode_cursor(["a" * 40, 1])
    assert "+" not in token and "/" not in token


@pytest.mark.parametrize(
    "bad",
    [
        "not-base64!!",  # invalid base64
        encode_cursor({"a": 1}) if False else "eyJhIjogMX0=",  # valid base64, not a list
        "",  # empty
    ],
)
def test_malformed_cursor_raises_rather_than_silently_restarting(bad):
    """flow's decoder returned None on garbage and the query then silently
    returned page 1 — handing the caller different data than they asked for."""
    with pytest.raises(BadRequestError, match="invalid cursor"):
        decode_cursor(bad)


def test_cursor_length_is_validated_against_the_sort():
    token = encode_cursor([1, 2])
    assert decode_cursor(token, expected_length=2) == [1, 2]
    with pytest.raises(BadRequestError, match="invalid cursor"):
        decode_cursor(token, expected_length=3)


# ---------------------------------------------------------------------------
# keyset_predicate
# ---------------------------------------------------------------------------


def test_single_level_keyset_is_a_plain_inequality():
    expr = keyset_predicate([(Job.id, 10)], ascending=True)
    assert _sql(expr) == "job.id > 10"
    expr = keyset_predicate([(Job.id, 10)], ascending=False)
    assert _sql(expr) == "job.id < 10"


def test_compound_keyset_is_lexicographic():
    """The whole point: (a > a0) OR (a = a0 AND id > id0). Getting this wrong is
    what made flow's paging skip and duplicate rows."""
    expr = keyset_predicate([(Job.finished_at, "x"), (Job.id, 7)], ascending=True)
    rendered = _sql(expr)
    assert rendered == ("job.finished_at > 'x' OR job.finished_at IS NULL OR job.finished_at = 'x' AND job.id > 7")


def test_a_null_boundary_level_is_skipped_as_an_inequality():
    """NULL cannot be compared with > / <, so that level contributes no strict
    inequality — but deeper levels still can."""
    expr = keyset_predicate([(Job.finished_at, None), (Job.id, 3)], ascending=True)
    rendered = _sql(expr)
    assert "finished_at >" not in rendered
    assert "job.finished_at IS NULL AND job.id > 3" in rendered


def test_all_null_boundary_is_rejected():
    with pytest.raises(BadRequestError, match="invalid cursor"):
        keyset_predicate([(Job.finished_at, None)], ascending=True)


def test_empty_levels_is_a_programming_error():
    with pytest.raises(ValueError, match="at least one level"):
        keyset_predicate([], ascending=True)


def test_crud_responses_are_the_shared_page():
    """`PageResponse` and `ScrollResponse` were two more parallel page shapes —
    `ScrollResponse` a verbatim re-declaration of `Page`'s four fields. Both are
    now `Page` subclasses; `data`/`total_count` survive as the wire and input
    names because that is what offset-paginating callers already read."""
    from forktex_core.database.crud import PageResponse, ScrollResponse

    assert issubclass(PageResponse, Page)
    assert issubclass(ScrollResponse, Page)

    page = PageResponse[int](data=[1, 2], has_more=True, limit=2, total_count=9, current_page=1, total_pages=5)
    assert page.items == [1, 2] == page.data
    assert page.total == 9 == page.total_count
    dumped = page.model_dump()
    assert dumped["data"] == [1, 2]
    assert dumped["totalCount"] == 9
    assert dumped["hasMore"] is True

    scroll = ScrollResponse[int](items=[3], limit=1, has_more=False, next_cursor=None)
    assert scroll.data == [3]
    assert scroll.model_dump()["data"] == [3]

    # `apply_to_page_data` / `apply_to_scroll_data` keep working through the rename.
    assert page.apply_to_page_data(str).items == ["1", "2"]
    assert scroll.apply_to_scroll_data(str).data == ["3"]
