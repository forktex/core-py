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

"""Async SQLAlchemy engine and session management for PostgreSQL.

Two ways in, one implementation:

- **:class:`Database`** — an explicit handle owning one engine + sessionmaker.
  Use it when a component needs its *own* pool, or several pools must coexist
  (``forktex_core.flow`` holds one per ``Flow``, since two Flow instances may
  run against different schemas in the same process).
- **module-level functions** — ``init_engine`` / ``get_session`` /
  ``session_scope`` / ``close_engine`` operate on a single default
  :class:`Database`. This is the convenient path for an application that only
  ever needs one pool, and it is what a FastAPI dependency uses.

Both paths share the same session semantics, so they cannot drift apart.

Usage:
    # Basic — default pool settings
    init_engine("postgresql+asyncpg://user:pass@host/db")

    # With pool tuning
    init_engine(
        "postgresql+asyncpg://user:pass@host/db",
        pool_size=20,
        max_overflow=10,
        pool_pre_ping=True,
    )

    # FastAPI lifespan
    @asynccontextmanager
    async def lifespan(app):
        init_engine(settings.db_url)
        yield
        await close_engine()

    # Route handler — note session_scope, not get_session: a FastAPI
    # dependency must be a plain async generator, and get_session is the
    # asynccontextmanager-wrapped variant.
    async def my_route(session: AsyncSession = Depends(session_scope)):
        ...

    # Service layer — either the decorator...
    @with_transactional_session
    async def my_service(session: AsyncSession, ...):
        ...

    # ...or the context manager
    async with get_session() as session:
        ...

    # An owned pool, independent of the module-level default
    db = Database(url, schema_translate_map={"forktex_flow": "my_schema"})
    async with db.session() as session:
        ...
    await db.dispose()
"""

import functools
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from forktex_core.error import AppError, AppErrorCode
from forktex_core.log import get_logger

logger = get_logger(__name__)


class DatabaseNotInitializedError(AppError, RuntimeError):
    """A session was requested before :func:`init_engine` configured a default.

    Inherits both bases deliberately, the same way ``queue.QueueError`` does:
    ``AppError`` gives it a ``code`` and makes it renderable by any transport
    that already handles ``AppError``, while ``RuntimeError`` keeps
    pre-existing ``except RuntimeError`` call sites working. ``INTERNAL``
    because a missing ``init_engine()`` is a wiring mistake, not something the
    caller of an endpoint can fix or retry.
    """

    code = AppErrorCode.INTERNAL


class Database:
    """One async engine + sessionmaker, owned explicitly.

    Construction is **lazy** in the same sense ``create_async_engine`` is: no
    connection is opened until a session actually executes something. That
    matters for callers that build a handle purely to read configuration off
    it, and for tests that construct one against an unreachable URL.
    """

    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        schema_translate_map: dict[str, str | None] | None = None,
        **engine_kwargs: object,
    ) -> None:
        """Build the engine + sessionmaker.

        Args:
            url: SQLAlchemy async database URL (e.g. postgresql+asyncpg://...).
            echo: Echo SQL statements to stdout.
            schema_translate_map: SQLAlchemy schema translation map applied to
                every session. Use to reroute library-owned schemas (e.g.
                ``"forktex_flow"``, ``"forktex_grid"``) to the public schema or
                any other target at runtime — without changing model
                definitions. A ``None`` value maps to the public schema.
            **engine_kwargs: Forwarded to ``create_async_engine`` (pool_size,
                max_overflow, pool_pre_ping, pool_recycle, ...).
        """
        self.url = url
        self.schema_translate_map = schema_translate_map
        engine = create_async_engine(url, echo=echo, **engine_kwargs)
        if schema_translate_map:
            # execution_options returns a new engine proxy — no connection overhead.
            engine = engine.execution_options(schema_translate_map=schema_translate_map)
        self._engine: AsyncEngine = engine
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=engine, expire_on_commit=False, class_=AsyncSession
        )

    @property
    def engine(self) -> AsyncEngine:
        """The underlying engine — for DDL, advisory locks, and migrations."""
        return self._engine

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        """The session factory, for callers that need to open a bare
        (non-auto-committing) session."""
        return self._sessionmaker

    async def session_scope(self) -> AsyncGenerator[AsyncSession]:
        """Yield a transactional session: commit on success, rollback on error.

        A plain async generator, so it works directly as a FastAPI dependency.
        """
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """:meth:`session_scope` as an ``async with`` context manager."""
        async for session in self.session_scope():
            yield session

    async def dispose(self) -> None:
        """Dispose the engine and its pool. Idempotent."""
        await self._engine.dispose()


# The single default Database backing the module-level functions.
_default: Database | None = None


def _require_default() -> Database:
    if _default is None:
        raise DatabaseNotInitializedError("Engine/sessionmaker not initialized — call init_engine() first")
    return _default


def init_engine(
    db_url: str,
    *,
    echo: bool = False,
    schema_translate_map: dict[str, str | None] | None = None,
    **engine_kwargs: object,
) -> async_sessionmaker:
    """Initialize the default :class:`Database` and return its sessionmaker.

    Replaces any previously configured default **without disposing it** — call
    :func:`close_engine` first if the old pool should be released. See
    :class:`Database` for the argument semantics.
    """
    global _default
    if _default is not None:
        logger.warning(
            "init_engine() replacing an existing default engine without disposing it; "
            "call close_engine() first to release the old pool"
        )
    _default = Database(db_url, echo=echo, schema_translate_map=schema_translate_map, **engine_kwargs)
    logger.debug(
        "database engine initialised",
        extra={"schema_translate_map": schema_translate_map},
    )
    return _default.sessionmaker


async def close_engine() -> None:
    """Dispose the default engine on app shutdown.

    Also clears the default handle — leaving it set would let
    ``get_session()`` keep silently handing out sessions bound to the
    now-disposed engine instead of raising "not initialized", and every
    checkout would fail with a confusing connection error instead.
    """
    global _default
    if _default is not None:
        await _default.dispose()
        _default = None


async def session_scope() -> AsyncGenerator[AsyncSession]:
    """Yield a transactional session from the default ``Database``.

    A **plain async generator**, which is what FastAPI's dependency system
    requires::

        async def my_route(session: AsyncSession = Depends(session_scope)):
            ...

    Service/library code that wants an ``async with`` block should use
    :func:`get_session`, which is this same generator wrapped in
    ``asynccontextmanager``. The two share one implementation so the
    commit/rollback semantics can't drift apart.

    (``Depends(get_session)`` would inject the ``_AsyncGeneratorContextManager``
    object itself rather than an ``AsyncSession`` — hence the split.)
    """
    db = _require_default()
    async for session in db.session_scope():
        yield session


get_session = asynccontextmanager(session_scope)
"""``session_scope`` as an async context manager, for use in service code::

    async with get_session() as session:
        ...

For a FastAPI dependency use :func:`session_scope` directly.
"""


def __getattr__(name: str) -> object:
    """Keep ``connection.engine`` / ``connection._async_sessionmaker`` working.

    Both used to be module globals assigned by ``init_engine``; they are now
    derived from the default :class:`Database` so they always reflect the
    *current* default rather than a reference captured at init time. Resolved
    lazily through ``__getattr__`` (only consulted when normal lookup fails),
    which also means rebinding the default is picked up automatically.
    """
    if name == "engine":
        return None if _default is None else _default.engine
    if name == "_async_sessionmaker":
        return None if _default is None else _default.sessionmaker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def with_transactional_session(func: Callable) -> Callable:
    """Decorator that guarantees the wrapped function runs inside a
    committed transaction boundary.

    Three call shapes, all transactional:

    - **No session provided.** A fresh session opens via
      ``get_session()`` (which commits on success, rolls back on
      raise). Same as v1.x behaviour.
    - **Caller passes a session with NO active transaction.** The
      decorator opens one via ``session.begin()`` and commits on
      success.
    - **Caller passes a session WITH an active transaction.** The
      decorator opens a ``SAVEPOINT`` via ``session.begin_nested()``,
      commits the savepoint on success, rolls back on raise. The
      outer transaction still owns the actual commit-to-DB.

    This closes the v1-v3 footgun where callers passed a session and
    forgot to commit — silently rolling back the wrapped function's
    work at session close. v3's ``next_in_series`` gapless-invariant
    "bug" was a symptom of this; v3's ``bulk_insert_rows`` documented
    it as a caller responsibility. v4 fixes it at the source: every
    decorated function now always persists or always rolls back,
    atomically.

    Detects the caller-provided session anywhere in ``args``/``kwargs``
    (not just the first positional arg) — so this also works decorating
    an instance method, where ``self`` occupies position 0.
    """

    @functools.wraps(func)
    async def wrapper(*args: object, **kwargs: object) -> object:
        # Detect a caller-provided session anywhere in the call — not just
        # args[0], which would miss it entirely on an instance method
        # (self is args[0] there) or any session passed after other
        # positional args.
        provided_session: AsyncSession | None = next(
            (a for a in args if isinstance(a, AsyncSession)),
            next((v for v in kwargs.values() if isinstance(v, AsyncSession)), None),
        )

        if provided_session is not None:
            # Wrap in an appropriate transaction boundary. begin_nested
            # cuts a SAVEPOINT when an outer tx is already open; begin
            # opens a fresh one otherwise. Both auto-commit on context
            # exit and roll back on exception — the guarantee.
            if provided_session.in_transaction():
                async with provided_session.begin_nested():
                    return await func(*args, **kwargs)
            else:
                async with provided_session.begin():
                    return await func(*args, **kwargs)

        # No session provided — manage one end-to-end via get_session()
        # which commits on success / rolls back on raise.
        async with get_session() as session:
            return await func(session, *args, **kwargs)

    return wrapper
