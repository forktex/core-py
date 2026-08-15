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

"""Redis async connection management."""

import redis.asyncio as redis

from forktex_core.log import get_logger

logger = get_logger(__name__)

_redis_client: redis.Redis | None = None


def available() -> bool:
    """Check if the Redis client is initialized."""
    return _redis_client is not None


def get_client() -> redis.Redis:
    """Return the initialized Redis client. Raises if not initialized."""
    if _redis_client is None:
        logger.error("Cache client requested but not initialized")
        raise RuntimeError("Cache not initialized — call init() first")
    return _redis_client


async def init(url: str) -> None:
    """Initialize the async Redis client from a URL.

    Args:
        url: Redis connection URL (e.g. ``redis://localhost:6379/0``).
    """
    global _redis_client
    # Mask credentials in log: show only host portion after "@"
    safe_url = url.rsplit("@", 1)[-1] if "@" in url else url
    logger.info("Initializing cache: %s", safe_url)
    _redis_client = redis.from_url(url, encoding="utf-8", decode_responses=True)
    try:
        await _redis_client.ping()
        logger.info("Cache initialized: %s", safe_url)
    except Exception as exc:
        _redis_client = None
        raise RuntimeError(f"Cache initialization failed ({safe_url}): {exc}") from exc


async def close() -> None:
    """Close the Redis connection."""
    global _redis_client
    if _redis_client:
        logger.info("Closing cache connection")
        try:
            await _redis_client.close()
            logger.info("Cache closed successfully")
        except Exception as e:
            logger.exception("Error closing cache: %s", e)
        finally:
            _redis_client = None


__all__ = ["available", "close", "get_client", "init"]
