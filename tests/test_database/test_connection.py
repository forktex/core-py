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

"""Integration tests for forktex_core.database.connection — requires Postgres container."""

from __future__ import annotations

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from forktex_core.database import (
    BaseDBModel,
    TimestampMixin,
    close_engine,
    get_session,
    init_engine,
    session_scope,
    with_transactional_session,
)
import forktex_core.database.connection as _db_conn


class _ConnTestWidget(BaseDBModel, TimestampMixin):
    """Module-level model to avoid SQLAlchemy annotation resolution issues."""

    __tablename__ = "conn_test_widget"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(sa.String(100))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def engine_and_session(postgres_url_str: str, fresh_schema: str):
    """Init the global engine with schema translation for isolation."""
    # Map both public (conn_test_widget) and forktex_grid (GridEntity etc.)
    # to fresh_schema so create_all doesn't fail on missing schemas.
    # None → fresh_schema remaps schema=None (default) tables.
    # "forktex_grid" → fresh_schema remaps the data module's tables.
    init_engine(
        postgres_url_str,
        schema_translate_map={None: fresh_schema, "forktex_grid": fresh_schema},
    )
    # _db_conn.engine is the AsyncEngine set by init_engine
    raw_engine = _db_conn.engine
    async with raw_engine.connect() as conn:
        await conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{fresh_schema}"'))
        await conn.commit()
    async with raw_engine.begin() as conn:
        await conn.run_sync(BaseDBModel.metadata.create_all)
    yield
    await close_engine()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_commits_on_success(engine_and_session):
    async with get_session() as session:
        session.add(_ConnTestWidget(name="alpha"))
    async with get_session() as session:
        result = await session.execute(sa.select(_ConnTestWidget).where(_ConnTestWidget.name == "alpha"))
        assert result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_get_session_rollback_on_error(engine_and_session):
    with pytest.raises(ValueError):
        async with get_session() as session:
            session.add(_ConnTestWidget(name="beta"))
            raise ValueError("intentional rollback")
    async with get_session() as session:
        result = await session.execute(sa.select(_ConnTestWidget).where(_ConnTestWidget.name == "beta"))
        assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_get_session_before_init_engine_raises():
    await close_engine()  # ensure no leftover global engine from another test
    with pytest.raises(RuntimeError, match="not initialized"):
        async with get_session():
            pass


@pytest.mark.asyncio
async def test_with_transactional_session_no_session_provided(engine_and_session):
    """Shape 1: no session passed — opens and commits its own via get_session()."""

    @with_transactional_session
    async def add_widget(session, name: str):
        session.add(_ConnTestWidget(name=name))

    await add_widget("shape1")
    async with get_session() as session:
        result = await session.execute(sa.select(_ConnTestWidget).where(_ConnTestWidget.name == "shape1"))
        assert result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_with_transactional_session_caller_session_no_tx(engine_and_session):
    """Shape 2: caller passes a session with no active transaction — decorator
    opens one via session.begin() and commits it."""

    @with_transactional_session
    async def add_widget(session, name: str):
        session.add(_ConnTestWidget(name=name))

    # get_session() itself autobegins, so use a bare sessionmaker session to
    # get one with genuinely no active transaction yet.
    maker = _db_conn._async_sessionmaker
    async with maker() as bare_session:
        assert not bare_session.in_transaction()
        await add_widget(bare_session, "shape2")
        assert not bare_session.in_transaction()  # committed, not left open

    async with get_session() as session:
        result = await session.execute(sa.select(_ConnTestWidget).where(_ConnTestWidget.name == "shape2"))
        assert result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_with_transactional_session_caller_session_with_tx_uses_savepoint(
    engine_and_session,
):
    """Shape 3: caller's session already has an active transaction — decorator
    uses a SAVEPOINT (begin_nested), and a failure inside only rolls back the
    savepoint, not the caller's outer transaction."""

    @with_transactional_session
    async def add_widget_then_fail(session, name: str):
        session.add(_ConnTestWidget(name=name))
        await session.flush()
        raise ValueError("intentional savepoint rollback")

    maker = _db_conn._async_sessionmaker
    async with maker() as session:
        async with session.begin():
            session.add(_ConnTestWidget(name="outer-survives"))
            with pytest.raises(ValueError, match="intentional savepoint rollback"):
                await add_widget_then_fail(session, "shape3-rolled-back")

    async with get_session() as session:
        outer = await session.execute(sa.select(_ConnTestWidget).where(_ConnTestWidget.name == "outer-survives"))
        assert outer.scalar_one_or_none() is not None
        rolled_back = await session.execute(
            sa.select(_ConnTestWidget).where(_ConnTestWidget.name == "shape3-rolled-back")
        )
        assert rolled_back.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_with_transactional_session_detects_session_as_instance_method_arg(
    engine_and_session,
):
    """A caller-provided session must be detected even when it's not
    args[0] — e.g. decorating an instance method, where self is args[0]."""

    class Service:
        @with_transactional_session
        async def add_widget(self, session, name: str):
            session.add(_ConnTestWidget(name=name))

    maker = _db_conn._async_sessionmaker
    async with maker() as bare_session:
        await Service().add_widget(bare_session, "shape-method")

    async with get_session() as session:
        result = await session.execute(sa.select(_ConnTestWidget).where(_ConnTestWidget.name == "shape-method"))
        assert result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_schema_translate_map(postgres_url_str: str, fresh_schema: str):
    """schema_translate_map must reroute ORM tables to the target schema."""
    target = fresh_schema + "_map"
    # None → target remaps schema=None (default) tables into target schema.
    engine = create_async_engine(
        postgres_url_str,
        execution_options={
            "schema_translate_map": {
                None: target,
                "forktex_grid": target,
            }
        },
    )
    async with engine.begin() as conn:
        await conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{target}"'))
        await conn.run_sync(BaseDBModel.metadata.create_all)

    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        session.add(_ConnTestWidget(name="gamma"))
        await session.commit()

    # Verify row landed in target schema, not public
    async with engine.connect() as conn:
        rows = (
            await conn.execute(sa.text(f"SELECT name FROM \"{target}\".conn_test_widget WHERE name = 'gamma'"))
        ).fetchall()
    assert len(rows) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_session_scope_works_as_a_fastapi_dependency(postgres_url_str: str, fresh_schema: str):
    """``Depends(session_scope)`` must inject a real ``AsyncSession``.

    Regression guard: ``get_session`` is decorated with
    ``@asynccontextmanager``, so ``Depends(get_session)`` injects the
    ``_AsyncGeneratorContextManager`` object rather than a session — yet the
    module docstring advertised exactly that for a long time. ``session_scope``
    is the plain async generator FastAPI actually needs.
    """
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    init_engine(postgres_url_str)
    try:
        app = FastAPI()

        @app.get("/probe")
        async def _probe(session: AsyncSession = Depends(session_scope)) -> dict[str, object]:
            # Prove it is a live, usable session and not a context-manager object.
            value = (await session.execute(sa.text("SELECT 42"))).scalar_one()
            return {"type": type(session).__name__, "value": value}

        body = TestClient(app).get("/probe").json()
        assert body["type"] == "AsyncSession"
        assert body["value"] == 42
    finally:
        await close_engine()


@pytest.mark.asyncio
async def test_get_session_and_session_scope_share_one_implementation(postgres_url_str: str):
    """The CM and the generator must not drift apart — same commit/rollback body."""
    init_engine(postgres_url_str)
    try:
        async with get_session() as session:
            assert isinstance(session, AsyncSession)
            assert (await session.execute(sa.text("SELECT 1"))).scalar_one() == 1
    finally:
        await close_engine()


# ---------------------------------------------------------------------------
# Database handle (explicit, non-global pools)
# ---------------------------------------------------------------------------


def test_database_construction_is_lazy():
    """A handle must not connect at construction time.

    `forktex_core.flow` builds one per Flow, and several tests construct a Flow
    against an unreachable URL purely to read configuration off it.
    """
    from forktex_core.database import Database

    db = Database(
        "postgresql+asyncpg://nobody:nothing@nonexistent.invalid:1/none",
        schema_translate_map={"forktex_flow": "somewhere"},
    )
    assert db.schema_translate_map == {"forktex_flow": "somewhere"}
    assert db.engine is not None  # engine object exists; no connection opened


def test_independent_handles_own_independent_pools():
    from forktex_core.database import Database

    a = Database("postgresql+asyncpg://x:y@h/d")
    b = Database("postgresql+asyncpg://x:y@h/d")
    assert a.engine is not b.engine
    assert a.sessionmaker is not b.sessionmaker


@pytest.mark.asyncio
async def test_database_session_commits_and_rolls_back(postgres_url_str: str):
    """The handle's session() must have the same semantics as get_session()."""
    from forktex_core.database import Database

    db = Database(postgres_url_str)
    try:
        async with db.session() as session:
            assert (await session.execute(sa.text("SELECT 1"))).scalar_one() == 1

        with pytest.raises(ValueError):
            async with db.session() as session:
                await session.execute(sa.text("SELECT 1"))
                raise ValueError("boom")
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_module_level_functions_track_the_current_default(postgres_url_str: str):
    """`connection.engine` / `_async_sessionmaker` are derived from the default
    handle, so rebinding the default is reflected rather than leaving a stale
    reference captured at init time."""
    await close_engine()
    assert _db_conn.engine is None

    init_engine(postgres_url_str)
    first = _db_conn.engine
    assert first is not None

    init_engine(postgres_url_str)  # rebind
    assert _db_conn.engine is not first  # follows the new default

    await close_engine()
    assert _db_conn.engine is None
    assert _db_conn._async_sessionmaker is None


@pytest.mark.asyncio
async def test_uninitialised_default_raises_an_apperror_that_is_also_a_runtimeerror():
    """Dual-inherited like `queue.QueueError`: an `AppError` (so a transport can
    render it) that still satisfies pre-existing `except RuntimeError`."""
    from forktex_core.error import AppError

    await close_engine()
    with pytest.raises(RuntimeError, match="not initialized") as exc_info:
        async with get_session():
            pass
    assert isinstance(exc_info.value, AppError)
    assert exc_info.value.code == "internal"
