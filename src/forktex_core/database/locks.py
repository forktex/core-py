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

"""Postgres advisory lock primitives.

Three patterns, in two scopes.

**Session-scoped** — held for the lifetime of a connection this module owns,
surviving ``COMMIT``/``ROLLBACK``. Both take an ``AsyncEngine`` because they
open their own connection:

1. ``advisory_lock`` — blocking. Used by the migration runner: every worker
   calls it, one proceeds, the rest wait.
2. ``try_advisory_lock`` — non-blocking, yields ``True``/``False``. Used for
   leader election: when the connection dies Postgres releases the lock
   automatically, which is what triggers failover.

**Transaction-scoped** — released by Postgres at transaction end, and takes the
caller's ``AsyncSession`` so it participates in *their* transaction:

3. ``xact_lock`` — for serialising work that is already inside a transaction.
   The engine-based helpers above cannot express this: they open a separate
   connection, so their lock has no relationship to the caller's transaction.
   Two call sites used to hand-roll this (one in Core, one in raw SQL) purely
   because it was missing here.

Keys are 64-bit signed integers. Derive them with :func:`advisory_key` rather
than by hand — it hashes deterministically in Python and folds into Postgres's
signed ``bigint`` range. Note it deliberately replaces one prior approach that
computed the key server-side with ``hashtext()``: that function's result is
**not stable across Postgres major versions**, so a cluster upgrade would
silently change which lock a caller takes.

Usage::

    from forktex_core.database.locks import advisory_key, advisory_lock, xact_lock

    # Migration-style: block until this process is the only one migrating
    async with advisory_lock(engine, advisory_key("myapp", "migrations")):
        await run_ddl()

    # Leader-election-style: non-blocking, hold for lifetime of block
    async with try_advisory_lock(engine, advisory_key("myapp", "driver-leader")) as is_leader:
        if is_leader:
            await run_leader_loop()

    # Inside an existing transaction, released at its end
    await xact_lock(session, advisory_key("namespace", ns))
"""

from __future__ import annotations

import zlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import sqlalchemy as sa

from forktex_core.log import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

logger = get_logger(__name__)

_SIGNED_64_MIN = -(1 << 63)
_UNSIGNED_64_WRAP = 1 << 64


def advisory_key(*parts: object) -> int:
    """Derive a stable advisory-lock key from ``parts``.

    Deterministic across processes and Postgres versions (unlike server-side
    ``hashtext``), and folded into the signed ``bigint`` range Postgres requires.

    ``parts`` are joined with a separator that cannot appear in an identifier, so
    ``("a", "b")`` and ``("ab",)`` do not collide.
    """
    if not parts:
        raise ValueError("advisory_key requires at least one part")
    material = "\x00".join(str(p) for p in parts).encode()
    key = zlib.crc32(material)
    # crc32 is unsigned 32-bit, so it already fits; the fold keeps the function
    # correct if the digest source is ever widened.
    if key >= 1 << 63:
        key -= _UNSIGNED_64_WRAP
    return key


def key_from_uuid(value: object) -> int:
    """Fold a UUID's 128 bits into a signed 64-bit advisory key.

    Kept distinct from :func:`advisory_key` because the row-level numbering lock
    keys directly off a row id and wants the full entropy of the UUID rather
    than a CRC of its text form.
    """
    as_int = getattr(value, "int", None)
    if as_int is None:  # pragma: no cover - guards a programming error
        raise TypeError(f"expected a UUID, got {type(value).__name__}")
    key = (as_int & 0xFFFFFFFFFFFFFFFF) ^ (as_int >> 64)
    if key >= 1 << 63:
        key -= _UNSIGNED_64_WRAP
    return key


async def xact_lock(session: AsyncSession, key: int) -> None:
    """Take a **transaction-scoped** advisory lock on the caller's session.

    Blocks until acquired, and Postgres releases it when the caller's
    transaction ends — there is nothing to unlock and no context manager,
    because the transaction boundary *is* the scope.

    Use this to serialise concurrent work that already runs inside a
    transaction. For a lock that must outlive a transaction, use
    :func:`advisory_lock` instead.
    """
    await session.execute(sa.select(sa.func.pg_advisory_xact_lock(key)))


@asynccontextmanager
async def advisory_lock(engine: AsyncEngine, key: int) -> AsyncGenerator[None]:
    """Blocking session-scoped advisory lock.

    Waits until the lock is available, acquires it, runs the body, then
    releases it explicitly. If the process dies, Postgres releases it
    automatically when the connection drops.

    Args:
        engine: The async engine whose connection pool to borrow from.
        key: 64-bit signed lock key — derive it with :func:`advisory_key`.
    """
    async with engine.connect() as conn:
        await conn.execute(sa.select(sa.func.pg_advisory_lock(key)))
        # pg_advisory_lock is already session-scoped regardless of commit —
        # this commit just closes SQLAlchemy's autobegin transaction so the
        # caller's own work inside the `yield` starts a clean transaction of
        # its own, instead of inheriting this empty one.
        await conn.commit()
        try:
            yield
        finally:
            try:
                await conn.execute(sa.select(sa.func.pg_advisory_unlock(key)))
                await conn.commit()
            except Exception:
                logger.debug("pg_advisory_unlock failed (connection may be dead)", exc_info=True)


@asynccontextmanager
async def try_advisory_lock(engine: AsyncEngine, key: int) -> AsyncGenerator[bool]:
    """Non-blocking session-scoped advisory lock.

    Attempts to acquire the lock immediately. Yields ``True`` if acquired,
    ``False`` if another session holds it. When ``True``, the lock is held
    for the entire duration of the ``async with`` block and released on exit
    (or automatically on connection close).

    Args:
        engine: The async engine whose connection pool to borrow from.
        key: 64-bit integer lock key.

    Usage::

        async with try_advisory_lock(engine, LEADER_KEY) as is_leader:
            if not is_leader:
                return  # retry later
            while not shutdown.is_set():
                await tick()
                await asyncio.sleep(poll_interval)
        # Lock released here (or if process dies, Postgres releases it)
    """
    async with engine.connect() as conn:
        result = await conn.execute(sa.select(sa.func.pg_try_advisory_lock(key)))
        acquired: bool = result.scalar_one()
        await conn.commit()
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    await conn.execute(sa.select(sa.func.pg_advisory_unlock(key)))
                    await conn.commit()
                except Exception:
                    logger.debug(
                        "pg_advisory_unlock failed (connection may be dead)",
                        exc_info=True,
                    )


__all__ = [
    "advisory_key",
    "advisory_lock",
    "key_from_uuid",
    "try_advisory_lock",
    "xact_lock",
]
