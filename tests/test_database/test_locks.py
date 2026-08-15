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

"""Integration tests for db.locks — advisory lock primitives."""

from __future__ import annotations

import asyncio
import uuid
import zlib

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from forktex_core.database.locks import advisory_lock, try_advisory_lock

LOCK_KEY = zlib.crc32(b"test.advisory.lock")


@pytest.mark.asyncio
async def test_advisory_lock_serialises_access(postgres_url_str: str):
    """Two concurrent coroutines competing on the SAME key must not overlap.

    advisory_lock is blocking — the second caller waits until the first
    releases. The order list will show A:enter, A:exit, B:enter, B:exit
    (or B first), never A:enter, B:enter interleaved.
    """
    engine = create_async_engine(postgres_url_str)
    order: list[str] = []
    shared_key = LOCK_KEY  # both workers use the same key → they serialise

    async def worker(name: str):
        async with advisory_lock(engine, shared_key):
            order.append(f"{name}:enter")
            await asyncio.sleep(0.05)
            order.append(f"{name}:exit")

    await asyncio.gather(worker("A"), worker("B"))

    # Serialised means: whichever went first finished before the other entered
    a_enter, a_exit = order.index("A:enter"), order.index("A:exit")
    b_enter, b_exit = order.index("B:enter"), order.index("B:exit")
    assert a_exit < b_enter or b_exit < a_enter, f"Lock did not serialise: {order}"
    await engine.dispose()


@pytest.mark.asyncio
async def test_try_advisory_lock_non_blocking_acquires(postgres_url_str: str):
    """try_advisory_lock must yield True when the lock is free."""
    engine = create_async_engine(postgres_url_str)
    key = zlib.crc32(b"test.try.lock.free")

    async with try_advisory_lock(engine, key) as acquired:
        assert acquired is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_try_advisory_lock_contention_second_caller_gets_false(postgres_url_str: str):
    """While a holder keeps the lock open, a second competitor on the SAME
    key must get False immediately (non-blocking), not wait or deadlock."""
    engine = create_async_engine(postgres_url_str)
    key = zlib.crc32(b"test.try.lock.contended")
    holder_ready = asyncio.Event()
    release_holder = asyncio.Event()
    second_acquired: list[bool] = []

    async def holder():
        async with try_advisory_lock(engine, key) as acquired:
            assert acquired is True
            holder_ready.set()
            await release_holder.wait()

    async def contender():
        await holder_ready.wait()
        async with try_advisory_lock(engine, key) as acquired:
            second_acquired.append(acquired)
        release_holder.set()

    await asyncio.gather(holder(), contender())
    assert second_acquired == [False]
    await engine.dispose()


@pytest.mark.asyncio
async def test_advisory_lock_released_after_context(postgres_url_str: str):
    """Lock must be acquirable again immediately after context exit."""
    engine = create_async_engine(postgres_url_str)
    key = zlib.crc32(b"test.release.lock")

    async with advisory_lock(engine, key):
        pass  # acquire + release

    # Must succeed again (no deadlock)
    async with advisory_lock(engine, key):
        pass

    await engine.dispose()


@pytest.mark.asyncio
async def test_advisory_lock_different_keys_do_not_block(postgres_url_str: str):
    """Workers on different keys must run concurrently (no contention)."""
    engine = create_async_engine(postgres_url_str)
    entered: list[str] = []

    async def worker(name: str, key: int):
        async with advisory_lock(engine, key):
            entered.append(name)
            await asyncio.sleep(0.05)

    key_a = zlib.crc32(b"lock.key.a")
    key_b = zlib.crc32(b"lock.key.b")
    await asyncio.gather(worker("A", key_a), worker("B", key_b))
    # Both should have entered (different keys = no blocking)
    assert "A" in entered and "B" in entered
    await engine.dispose()


# ---------------------------------------------------------------------------
# Key derivation (pure — no container)
# ---------------------------------------------------------------------------


def test_advisory_key_is_deterministic_and_fits_signed_bigint():
    from forktex_core.database.locks import advisory_key

    a = advisory_key("myapp", "migrations")
    assert a == advisory_key("myapp", "migrations")  # stable across calls
    assert -(2**63) <= a < 2**63  # Postgres bigint is signed


def test_advisory_key_parts_cannot_collide_by_concatenation():
    """("a", "b") must not equal ("ab",) — the separator is a NUL, which cannot
    appear in an identifier."""
    from forktex_core.database.locks import advisory_key

    assert advisory_key("a", "b") != advisory_key("ab")
    assert advisory_key("a_b") != advisory_key("a", "b")


def test_advisory_key_requires_a_part():
    from forktex_core.database.locks import advisory_key

    with pytest.raises(ValueError, match="at least one part"):
        advisory_key()


def test_key_from_uuid_folds_128_bits_into_signed_64():
    import uuid as _uuid

    from forktex_core.database.locks import key_from_uuid

    u = _uuid.UUID("12345678-1234-5678-1234-567812345678")
    k = key_from_uuid(u)
    assert k == key_from_uuid(u)  # deterministic
    assert -(2**63) <= k < 2**63
    assert key_from_uuid(_uuid.uuid4()) != key_from_uuid(_uuid.uuid4())


def test_key_from_uuid_rejects_non_uuid():
    from forktex_core.database.locks import key_from_uuid

    with pytest.raises(TypeError):
        key_from_uuid("not-a-uuid")


# ---------------------------------------------------------------------------
# Transaction-scoped lock (the scope the engine-based helpers cannot express)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_xact_lock_is_released_at_transaction_end(postgres_url_str: str):
    """The point of `xact_lock`: it participates in the *caller's* transaction,
    so it needs no unlock and cannot outlive the transaction. The engine-based
    helpers open their own connection and therefore cannot do this."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from forktex_core.database.locks import advisory_key, xact_lock

    key = advisory_key("test", "xact", uuid.uuid4().hex)
    engine = create_async_engine(postgres_url_str)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with maker() as s1:
            async with s1.begin():
                await xact_lock(s1, key)
                # while s1's transaction is open, another session cannot take it
                async with maker() as s2:
                    held = (await s2.execute(sa.select(sa.func.pg_try_advisory_lock(key)))).scalar_one()
                    assert held is False
                    if held:  # pragma: no cover - defensive cleanup
                        await s2.execute(sa.select(sa.func.pg_advisory_unlock(key)))
            # s1's transaction has committed -> Postgres released the lock
            async with maker() as s3:
                free = (await s3.execute(sa.select(sa.func.pg_try_advisory_lock(key)))).scalar_one()
                assert free is True
                await s3.execute(sa.select(sa.func.pg_advisory_unlock(key)))
    finally:
        await engine.dispose()
