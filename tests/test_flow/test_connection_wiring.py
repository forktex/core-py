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

"""Flow's connection management is `forktex_core.database`'s, not its own."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from forktex_core.database import Database
from forktex_core.flow import Flow

pytestmark = pytest.mark.asyncio


async def test_flow_requires_exactly_one_of_url_or_handle():
    with pytest.raises(ValueError, match="exactly one"):
        Flow()
    with pytest.raises(ValueError, match="exactly one"):
        Flow("postgresql+asyncpg://x:y@h/d", database=Database("postgresql+asyncpg://x:y@h/d"))


async def test_a_url_builds_a_dedicated_pool():
    f = Flow(database_url="postgresql+asyncpg://x:y@nonexistent.invalid/d")
    try:
        assert f.engine is not None  # lazy: constructed, not connected
        assert f._owns_db is True
    finally:
        await f.close()


async def test_a_shared_handle_gives_the_process_one_pool(db_url: str, fresh_schema: str):
    """The DRY payoff: an application that already has a pool (because it also
    uses grid) hands it to Flow instead of Flow opening a second one."""
    db = Database(db_url, schema_translate_map={"forktex_flow": fresh_schema})
    f = Flow(database=db, schema=fresh_schema)
    try:
        assert f.engine is db.engine, "Flow built its own pool instead of sharing"
        assert f._owns_db is False
        # and it is a working pool
        await f.init()
        async with f.session() as session:
            assert (await session.execute(sa.text("SELECT 1"))).scalar_one() == 1
    finally:
        await f.close()
        await db.dispose()


async def test_close_does_not_dispose_a_borrowed_pool(db_url: str, fresh_schema: str):
    """Disposing a caller-owned pool would take the rest of the application's
    database access down with the Flow."""
    db = Database(db_url, schema_translate_map={"forktex_flow": fresh_schema})
    f = Flow(database=db, schema=fresh_schema)
    await f.init()
    await f.close()

    # still usable after the Flow closed
    async with db.session() as session:
        assert (await session.execute(sa.text("SELECT 1"))).scalar_one() == 1
    await db.dispose()


async def test_close_does_dispose_an_owned_pool(db_url: str, fresh_schema: str):
    """Asserted by observing the call, not by expecting later checkouts to fail:
    SQLAlchemy's ``dispose()`` releases the current pool but leaves the engine
    usable, lazily building a fresh pool on the next checkout."""
    f = Flow(database_url=db_url, schema=fresh_schema)
    await f.init()

    disposed = False
    original = f._db.dispose

    async def _spy() -> None:
        nonlocal disposed
        disposed = True
        await original()

    f._db.dispose = _spy  # type: ignore[method-assign]
    await f.close()
    assert disposed is True


async def test_session_commits_on_success_and_rolls_back_on_error(db_url: str, fresh_schema: str):
    """`Flow.session()` is `Database.session()`, so it carries the one
    commit/rollback contract rather than the hand-rolled commits that used to
    live at 28 separate call sites."""
    f = Flow(database_url=db_url, schema=fresh_schema)
    await f.init()
    try:
        async with f.session() as session:
            await session.execute(
                sa.text(f'INSERT INTO "{fresh_schema}".workflow (name, version) VALUES (:n, 1)'),
                {"n": "committed.wf"},
            )
        async with f.session() as session:
            found = (
                await session.execute(
                    sa.text(f'SELECT count(*) FROM "{fresh_schema}".workflow WHERE name = :n'),
                    {"n": "committed.wf"},
                )
            ).scalar_one()
        assert found == 1

        with pytest.raises(ValueError):
            async with f.session() as session:
                await session.execute(
                    sa.text(f'INSERT INTO "{fresh_schema}".workflow (name, version) VALUES (:n, 1)'),
                    {"n": "rolled.back"},
                )
                raise ValueError("boom")
        async with f.session() as session:
            found = (
                await session.execute(
                    sa.text(f'SELECT count(*) FROM "{fresh_schema}".workflow WHERE name = :n'),
                    {"n": "rolled.back"},
                )
            ).scalar_one()
        assert found == 0
    finally:
        await f.close()


async def test_schema_translate_map_still_rewrites_a_per_table_schema(db_url: str, fresh_schema: str):
    """Guard for the ORM-base move.

    flow's tables used to carry their schema on a private `MetaData`; they now
    carry it per table via `__table_args__` so the declarative base can be
    shared with grid. SQLAlchemy translates on `Table.schema` regardless of
    where it was set, but flow's whole multi-tenant test strategy depends on
    that, so it is asserted rather than assumed.
    """
    from forktex_core.flow.persist.models import Run

    assert Run.__table__.schema == "forktex_flow"  # static metadata is unambiguous

    f = Flow(database_url=db_url, schema=fresh_schema)
    await f.init()
    try:
        # A query built against the hardcoded schema must execute against the
        # per-instance one.
        async with f.session() as session:
            await session.execute(sa.select(Run).limit(1))
            landed = (
                await session.execute(
                    sa.text(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema = :s AND table_name = 'run'"
                    ),
                    {"s": fresh_schema},
                )
            ).scalar_one()
        assert landed == 1, f"run table not created in {fresh_schema}"
        assert fresh_schema != "forktex_flow"  # so the assertion above is meaningful
    finally:
        await f.close()
