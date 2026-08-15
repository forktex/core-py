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

"""Integration tests for forktex_core.cache — requires Redis container."""

from __future__ import annotations

import pytest
import pytest_asyncio

from forktex_core.cache import (
    available,
    cached,
    delete,
    deserialize,
    fetch_or_set,
    fetch_swr,
    get,
    get_client,
    init,
    invalidate_key,
    invalidate_prefix,
    key_for,
    serialize,
    set,
    shutdown_background_tasks,
)
from forktex_core.cache.connection import close


@pytest_asyncio.fixture(autouse=True)
async def cache_init(redis_url: str):
    await init(redis_url)
    yield
    await close()


@pytest.mark.asyncio
async def test_set_and_get():
    await set("test:k1", "hello", ex=60)
    val = await get("test:k1")
    assert val == "hello"


@pytest.mark.asyncio
async def test_get_miss_returns_none():
    val = await get("test:nonexistent:" + __name__)
    assert val is None


@pytest.mark.asyncio
async def test_delete():
    await set("test:k2", "world", ex=60)
    await delete("test:k2")
    val = await get("test:k2")
    assert val is None


@pytest.mark.asyncio
async def test_invalidate_key():
    await set("test:k3", "data", ex=60)
    await invalidate_key("test:k3")
    assert await get("test:k3") is None


@pytest.mark.asyncio
async def test_invalidate_prefix():
    await set("prefix:a", "1", ex=60)
    await set("prefix:b", "2", ex=60)
    deleted = await invalidate_prefix("prefix")
    assert deleted >= 2
    assert await get("prefix:a") is None
    assert await get("prefix:b") is None


@pytest.mark.asyncio
async def test_invalidate_prefix_does_not_match_unrelated_keys_sharing_the_string():
    """ "user" must not match "username:foo" — no bare-string substring match."""
    await set("user:1", "a", ex=60)
    await set("username:foo", "b", ex=60)
    deleted = await invalidate_prefix("user")
    assert deleted == 1
    assert await get("user:1") is None
    assert await get("username:foo") == "b"
    await delete("username:foo")


@pytest.mark.asyncio
async def test_invalidate_prefix_deletes_the_exact_prefix_key_itself():
    await set("org:abc123", "profile-data", ex=60)
    await set("org:abc123:members", "members-data", ex=60)
    deleted = await invalidate_prefix("org:abc123")
    assert deleted == 2
    assert await get("org:abc123") is None
    assert await get("org:abc123:members") is None


@pytest.mark.asyncio
async def test_fetch_or_set_caches_result():
    call_count = 0

    async def compute():
        nonlocal call_count
        call_count += 1
        return "computed"

    key = "test:fos:" + __name__
    result1 = await fetch_or_set(key, 60, compute, (), {}, None)
    result2 = await fetch_or_set(key, 60, compute, (), {}, None)
    assert result1 == "computed"
    assert result2 == "computed"
    assert call_count == 1  # second call hit cache


@pytest.mark.asyncio
async def test_cached_decorator():
    call_count = 0

    @cached(ttl=60)
    async def expensive(x: int) -> str:
        nonlocal call_count
        call_count += 1
        return f"result:{x}"

    r1 = await expensive(42)
    r2 = await expensive(42)
    r3 = await expensive(99)
    assert r1 == "result:42"
    assert r2 == "result:42"
    assert r3 == "result:99"
    assert call_count == 2  # 42 once, 99 once


@pytest.mark.asyncio
async def test_key_for():
    k = key_for("user", "abc-123")
    assert k == "user:abc-123"
    k2 = key_for("feed")
    assert k2 == "feed"


def test_key_for_none_part_raises_instead_of_collapsing_to_bare_prefix():
    """A None part almost always means an unresolved ID upstream — silently
    collapsing onto the bare "user" prefix key would corrupt every other
    caller's per-user cache entries under the same key."""
    with pytest.raises(ValueError):
        key_for("user", None)


@pytest.mark.asyncio
async def test_fetch_swr_returns_fresh_value_on_miss():
    async def compute():
        return "fresh"

    key = "test:swr:" + __name__
    result = await fetch_swr(key, ttl=60, stale_ttl=300, fn=compute, args=(), kwargs={}, response_model=None)
    assert result == "fresh"


@pytest.mark.asyncio
async def test_fetch_swr_serves_stale_value_and_refreshes_in_background():
    call_count = 0

    async def compute():
        nonlocal call_count
        call_count += 1
        return f"computed-{call_count}"

    key = "test:swr-stale:" + __name__
    # ttl=-1 → age (>= 0) is always > ttl, so every read is "stale" and
    # triggers a background refresh, regardless of clock-second rounding.
    first = await fetch_swr(key, ttl=-1, stale_ttl=300, fn=compute, args=(), kwargs={}, response_model=None)
    assert first == "computed-1"

    second = await fetch_swr(key, ttl=-1, stale_ttl=300, fn=compute, args=(), kwargs={}, response_model=None)
    assert second == "computed-1"  # still serves the stale value immediately

    await shutdown_background_tasks()
    assert call_count == 2  # background refresh ran


@pytest.mark.asyncio
async def test_cached_decorator_stale_ttl_zero_uses_swr_not_fetch_or_set():
    """stale_ttl=0 is a degenerate SWR config (refresh on every read), not
    "unset" — it must not silently fall back to plain fetch_or_set."""
    call_count = 0

    @cached(ttl=0, stale_ttl=0)
    async def compute(x: int) -> str:
        nonlocal call_count
        call_count += 1
        return f"v{call_count}"

    r1 = await compute(1)
    assert r1 == "v1"
    await shutdown_background_tasks()


def test_serialize_deserialize_roundtrip_plain_value():
    raw = serialize({"a": 1, "b": [1, 2, 3]})
    assert deserialize(raw, None) == {"a": 1, "b": [1, 2, 3]}


@pytest.mark.asyncio
async def test_available_and_get_client_not_initialized_after_close():
    assert available() is True
    get_client()  # does not raise while initialized

    await close()
    assert available() is False
    with pytest.raises(RuntimeError):
        get_client()
    assert await get("test:not-initialized:" + __name__) is None  # ops degrade, don't raise
