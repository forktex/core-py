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

"""Integration tests for forktex_core.database CRUD helpers."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from forktex_core.database import (
    BaseDBModel,
    PageResponse,
    ScrollResponse,
    TimestampMixin,
    create,
    find_one_by,
    get,
    list_all,
    paginate,
    paginate_scroll,
)


class _CrudItem(BaseDBModel, TimestampMixin):
    __tablename__ = "crud_item_test"
    id: Mapped[uuid.UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False, unique=True)
    score: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)


@pytest_asyncio.fixture
async def db_session(postgres_url_str: str, fresh_schema: str):
    """Session pointing at fresh_schema; maps forktex_grid too to avoid schema errors."""
    engine = create_async_engine(
        postgres_url_str,
        execution_options={
            "schema_translate_map": {
                None: fresh_schema,  # schema=None tables → fresh_schema
                "forktex_grid": fresh_schema,  # data module tables → fresh_schema
            }
        },
    )
    async with engine.begin() as conn:
        await conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{fresh_schema}"'))
        await conn.run_sync(BaseDBModel.metadata.create_all)

    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_and_get(db_session: AsyncSession):
    item = await create(db_session, _CrudItem, name="alpha", score=10)
    await db_session.commit()
    fetched = await get(db_session, _CrudItem, item.id)
    assert fetched is not None
    assert fetched.name == "alpha"
    assert fetched.score == 10


@pytest.mark.asyncio
async def test_get_missing_returns_none(db_session: AsyncSession):
    result = await get(db_session, _CrudItem, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_get_unknown_key_raises_attribute_error(db_session: AsyncSession):
    with pytest.raises(AttributeError, match="no attribute 'bogus_column'"):
        await get(db_session, _CrudItem, "x", key="bogus_column")


@pytest.mark.asyncio
async def test_find_one_by(db_session: AsyncSession):
    await create(db_session, _CrudItem, name="beta", score=20)
    await db_session.commit()
    found = await find_one_by(db_session, _CrudItem, name="beta")
    assert found is not None
    assert found.score == 20


@pytest.mark.asyncio
async def test_find_one_by_no_match(db_session: AsyncSession):
    result = await find_one_by(db_session, _CrudItem, name="nonexistent-xyz")
    assert result is None


@pytest.mark.asyncio
async def test_duplicate_name_raises_already_exists(db_session: AsyncSession):
    """A unique violation means the row already exists, so `create` raises
    `AlreadyExistsError` (404-adjacent 409 semantics), not the blanket
    `ConflictError` it used to raise for *every* IntegrityError.

    The driver message — which quotes the offending value — must not reach the
    user-facing error; it stays on `__cause__` for the logs.
    """
    from sqlalchemy.exc import IntegrityError

    from forktex_core.error import AlreadyExistsError

    await create(db_session, _CrudItem, name="gamma")
    await db_session.commit()
    with pytest.raises(AlreadyExistsError) as exc_info:
        await create(db_session, _CrudItem, name="gamma")

    assert exc_info.value.code == "already_exists"
    assert "gamma" not in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, IntegrityError)


@pytest.mark.asyncio
async def test_list_all(db_session: AsyncSession):
    for i in range(3):
        await create(db_session, _CrudItem, name=f"list-{i}-{uuid.uuid4().hex[:6]}")
    await db_session.commit()
    items = await list_all(db_session, _CrudItem)
    assert len(items) >= 3


@pytest.mark.asyncio
async def test_paginate_splits_correctly(db_session: AsyncSession):
    suffix = uuid.uuid4().hex[:6]
    for i in range(5):
        await create(db_session, _CrudItem, name=f"page-{i}-{suffix}", score=i)
    await db_session.commit()

    conditions = [_CrudItem.name.like(f"page-%-{suffix}")]
    p1: PageResponse = await paginate(db_session, _CrudItem, page=1, page_size=3, conditions=conditions)
    p2: PageResponse = await paginate(db_session, _CrudItem, page=2, page_size=3, conditions=conditions)

    assert len(p1.data) == 3
    assert p1.has_more is True
    assert p1.total_count == 5
    assert p1.total_pages == 2
    assert len(p2.data) == 2
    assert p2.has_more is False


@pytest.mark.asyncio
async def test_paginate_scroll_has_more(db_session: AsyncSession):
    suffix = uuid.uuid4().hex[:6]
    for i in range(4):
        await create(db_session, _CrudItem, name=f"scroll-{i}-{suffix}")
    await db_session.commit()

    conditions = [_CrudItem.name.like(f"scroll-%-{suffix}")]
    result: ScrollResponse = await paginate_scroll(db_session, _CrudItem, limit=2, conditions=conditions)
    assert len(result.data) == 2
    assert result.has_more is True


@pytest.mark.asyncio
async def test_paginate_scroll_no_more_when_exhausted(db_session: AsyncSession):
    suffix = uuid.uuid4().hex[:6]
    for i in range(2):
        await create(db_session, _CrudItem, name=f"exhaust-{i}-{suffix}")
    await db_session.commit()

    conditions = [_CrudItem.name.like(f"exhaust-%-{suffix}")]
    result: ScrollResponse = await paginate_scroll(db_session, _CrudItem, limit=10, conditions=conditions)
    assert len(result.data) == 2
    assert result.has_more is False


@pytest.mark.asyncio
async def test_paginate_clamps_invalid_page_and_page_size(db_session: AsyncSession):
    suffix = uuid.uuid4().hex[:6]
    for i in range(3):
        await create(db_session, _CrudItem, name=f"clamp-{i}-{suffix}")
    await db_session.commit()

    conditions = [_CrudItem.name.like(f"clamp-%-{suffix}")]
    result: PageResponse = await paginate(db_session, _CrudItem, page=0, page_size=0, conditions=conditions)
    assert result.current_page == 1  # page<1 clamps to 1
    assert result.limit == 10  # page_size<1 clamps to 10
    assert len(result.data) == 3


@pytest.mark.asyncio
async def test_paginate_scroll_clamps_invalid_limit(db_session: AsyncSession):
    suffix = uuid.uuid4().hex[:6]
    for i in range(3):
        await create(db_session, _CrudItem, name=f"scroll-clamp-{i}-{suffix}")
    await db_session.commit()

    conditions = [_CrudItem.name.like(f"scroll-clamp-%-{suffix}")]
    result: ScrollResponse = await paginate_scroll(db_session, _CrudItem, limit=0, conditions=conditions)
    assert result.limit == 20  # limit<1 clamps to 20
    assert len(result.data) == 3


@pytest.mark.asyncio
async def test_paginate_scroll_can_actually_advance_past_page_one(db_session: AsyncSession):
    """Regression guard for a documented-but-absent feature.

    `paginate_scroll` was described as cursor-based but accepted no cursor and
    never populated `next_cursor`, so every call returned the same first page.
    """
    suffix = uuid.uuid4().hex[:6]
    for i in range(5):
        await create(db_session, _CrudItem, name=f"seek-{i}-{suffix}", score=i)
    await db_session.commit()

    conditions = [_CrudItem.name.like(f"seek-%-{suffix}")]
    keyset = [_CrudItem.name, _CrudItem.id]  # last level is the unique tiebreaker

    p1 = await paginate_scroll(
        db_session,
        _CrudItem,
        limit=2,
        conditions=conditions,
        order_by=keyset,
        keyset=keyset,
    )
    assert [i.name for i in p1.data] == [f"seek-0-{suffix}", f"seek-1-{suffix}"]
    assert p1.has_more is True
    assert p1.next_cursor is not None  # previously always None

    p2 = await paginate_scroll(
        db_session,
        _CrudItem,
        limit=2,
        conditions=conditions,
        order_by=keyset,
        keyset=keyset,
        cursor=p1.next_cursor,
    )
    assert [i.name for i in p2.data] == [f"seek-2-{suffix}", f"seek-3-{suffix}"]

    p3 = await paginate_scroll(
        db_session,
        _CrudItem,
        limit=2,
        conditions=conditions,
        order_by=keyset,
        keyset=keyset,
        cursor=p2.next_cursor,
    )
    assert [i.name for i in p3.data] == [f"seek-4-{suffix}"]
    assert p3.has_more is False
    assert p3.next_cursor is None  # exhausted

    # no row appeared twice and none was skipped
    seen = [i.name for i in (*p1.data, *p2.data, *p3.data)]
    assert seen == sorted(seen)
    assert len(set(seen)) == 5


@pytest.mark.asyncio
async def test_paginate_scroll_without_keyset_still_returns_a_first_page(
    db_session: AsyncSession,
):
    """Back-compat: the old signature had no keyset, and must keep working."""
    suffix = uuid.uuid4().hex[:6]
    for i in range(3):
        await create(db_session, _CrudItem, name=f"nokeyset-{i}-{suffix}")
    await db_session.commit()

    result = await paginate_scroll(
        db_session,
        _CrudItem,
        limit=2,
        conditions=[_CrudItem.name.like(f"nokeyset-%-{suffix}")],
    )
    assert len(result.data) == 2
    assert result.has_more is True
    assert result.next_cursor is None  # cannot build one without a keyset
