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

"""Redis cache operations: get/set/delete, fetch-or-set, stale-while-revalidate."""

import asyncio
import builtins
import json
import time
from collections.abc import Callable
from typing import Any

import redis.asyncio as redis
from pydantic import BaseModel

from forktex_core.cache.connection import available, get_client
from forktex_core.cache.serialization import deserialize, serialize
from forktex_core.log import get_logger

LOCK_TTL = 10

logger = get_logger(__name__)

_background_tasks: builtins.set[asyncio.Task[Any]] = builtins.set()


def _safe_client() -> redis.Redis | None:
    if not available():
        return None
    try:
        return get_client()
    except RuntimeError:
        return None


async def get(key: str) -> str | None:
    """Get a value from cache. Returns None on miss or error."""
    c = _safe_client()
    if not c:
        return None
    try:
        return await c.get(key)
    except Exception:
        logger.exception("Cache GET failed")
        return None


async def set(key: str, value: str, ex: int) -> None:
    """Set a value in cache with TTL (seconds)."""
    c = _safe_client()
    if not c:
        return
    try:
        await c.set(key, value, ex=ex)
    except Exception:
        logger.exception("Cache SET failed")


async def delete(key: str) -> None:
    """Delete a key from cache."""
    c = _safe_client()
    if not c:
        return
    try:
        await c.delete(key)
    except Exception:
        logger.exception("Cache DELETE failed")


async def invalidate_key(key: str) -> None:
    """Alias for delete."""
    await delete(key)


async def invalidate_prefix(prefix: str) -> int:
    """Delete ``prefix`` itself (if set as its own key) plus every
    namespaced ``prefix:*`` key. Returns count of deleted keys.

    Matches on the ``:`` delimiter, not a bare string prefix — so
    ``invalidate_prefix("user")`` deletes ``"user"`` and ``"user:123"`` but
    never touches unrelated keys like ``"username:foo"``. ``prefix=""``
    matches every key in the cache.
    """
    c = _safe_client()
    if not c:
        return 0
    patterns = ["*"] if not prefix else [prefix, f"{prefix}:*"]
    try:
        keys: builtins.set[str] = builtins.set()
        for pattern in patterns:
            async for k in c.scan_iter(pattern):
                keys.add(k)
        if keys:
            return await c.delete(*keys)
        return 0
    except Exception:
        logger.exception("Cache invalidate_prefix failed")
        return 0


async def fetch_or_set(
    key: str,
    ttl: int,
    fn: Callable,
    args: tuple,
    kwargs: dict,
    response_model: type[BaseModel] | None,
) -> object:
    """Simple cache-aside: return cached value or compute and store.

    Blocks until fresh data is fetched on cache miss.
    """
    cached = await get(key)
    if cached:
        try:
            return deserialize(cached, response_model)
        except Exception:
            logger.warning("Cache decode failed, invalidating %s", key)
            await invalidate_key(key)

    result = await fn(*args, **kwargs)
    await set(key, serialize(result), ex=ttl)
    return result


async def fetch_swr(
    key: str,
    ttl: int,
    stale_ttl: int,
    fn: Callable,
    args: tuple,
    kwargs: dict,
    response_model: type[BaseModel] | None,
) -> object:
    """Stale-while-revalidate: return cached (possibly stale) value immediately,
    refresh in background if older than ``ttl``.

    - ``ttl``: seconds before background refresh triggers.
    - ``stale_ttl``: seconds before the cached entry expires entirely.
    """
    raw = await get(key)
    # Epoch seconds, deliberately not `forktex_core.iso`. `iso` owns the
    # canonical *text* form of a timestamp for values that cross a wire or
    # storage contract; `created_at` here is a private age counter, subtracted
    # numerically on every read and never surfaced to a consumer. Storing ISO
    # text would mean parsing it back on every cache hit to do arithmetic, for
    # no gain. Wall clock (not `monotonic`) is required because the value is
    # persisted in Redis and compared across processes.
    now = int(time.time())
    if raw:
        try:
            payload = json.loads(raw)
            age = now - payload["created_at"]
            value = deserialize(payload["value"], response_model)

            if age > ttl:
                await _refresh_in_background(key, stale_ttl, fn, args, kwargs)
            return value
        except Exception:
            logger.warning("SWR cache corrupted, invalidating %s", key)
            await invalidate_key(key)

    result = await fn(*args, **kwargs)
    payload = {"created_at": now, "value": serialize(result)}
    await set(key, json.dumps(payload), ex=stale_ttl)
    return result


async def _refresh_in_background(key: str, stale_ttl: int, fn: Callable, args: tuple, kwargs: dict) -> None:
    c = _safe_client()
    if not c:
        return

    lock_key = f"lock:{key}"
    acquired = await c.set(lock_key, "1", nx=True, ex=LOCK_TTL)
    if not acquired:
        return

    async def task() -> None:
        try:
            result = await fn(*args, **kwargs)
            payload = {"created_at": int(time.time()), "value": serialize(result)}
            await set(key, json.dumps(payload), ex=stale_ttl)
        except Exception:
            logger.exception("SWR refresh failed")
        finally:
            _background_tasks.discard(asyncio.current_task())

    t = asyncio.create_task(task())
    _background_tasks.add(t)


async def shutdown_background_tasks() -> None:
    """Wait for all SWR background tasks to finish. Call during app shutdown."""
    if _background_tasks:
        logger.info("Waiting for %d SWR tasks to finish...", len(_background_tasks))
        await asyncio.gather(*_background_tasks, return_exceptions=True)


__all__ = [
    "delete",
    "fetch_or_set",
    "fetch_swr",
    "get",
    "invalidate_key",
    "invalidate_prefix",
    "set",
    "shutdown_background_tasks",
]
