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

"""Schema reflection via SQLAlchemy's ``Inspector``.

Three hand-written ``information_schema`` queries used to live in `grid` (twice)
and `flow` (once), each with a different signature and return shape, and one of
them ran a separate round-trip *per column*. SQLAlchemy already ships a
reflection API that is dialect-portable and maintained upstream, so this module
is a thin async adapter over it rather than more SQL.

Everything accepts either an ``AsyncSession`` or an ``AsyncConnection``, since
the former callers were split between the two.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.types import TypeEngine

__all__ = ["column_types", "columns", "has_table", "indexes", "type_ddl", "udt_names"]

_Executor = AsyncSession | AsyncConnection


async def _run_sync[R](executor: _Executor, fn: Callable[[Connection], R]) -> R:
    """Run a sync-only reflection callable on the executor's connection.

    ``Inspector`` is synchronous by design; SQLAlchemy's supported bridge is
    ``run_sync`` on the async connection.
    """
    if isinstance(executor, AsyncSession):
        return await executor.run_sync(lambda sync_session: fn(sync_session.connection()))
    return await executor.run_sync(fn)


def _split_relation(relation: str) -> tuple[str | None, str]:
    """Split ``"schema.table"`` into its parts; a bare name yields ``(None, name)``.

    ``None`` means "the connection's default search path", matching the
    ``COALESCE(:schema, current_schema())`` behaviour of the query this replaces.
    """
    schema, _, table = relation.rpartition(".")
    return (schema or None), table


async def columns(executor: _Executor, relation: str, *, schema: str | None = None) -> set[str]:
    """Names of the columns on ``relation``.

    ``relation`` may be ``"table"`` or ``"schema.table"``; an explicit
    ``schema=`` wins over one embedded in the name. Returns an empty set when
    the table does not exist, so callers can treat "absent" and "no columns"
    alike (both mean "nothing to reconcile against").
    """
    return set((await column_types(executor, relation, schema=schema)).keys())


async def column_types(executor: _Executor, relation: str, *, schema: str | None = None) -> dict[str, TypeEngine[Any]]:
    """Map column name → its **SQLAlchemy type** for ``relation``.

    Returns the reflected type *object*, not a type-name string. That matters:
    the predecessor of this function returned ``information_schema.udt_name``
    (``int8``, ``timestamptz``, ...) which callers then had to translate back
    into a SQLAlchemy type via a hand-maintained lookup table. Round-tripping
    through a name is lossy — ``timestamptz`` and ``timestamp`` both reduce to
    "timestamp" under SQLAlchemy's own type naming, silently discarding
    timezone-awareness — whereas the object preserves it
    (``TIMESTAMP(timezone=True)``).

    Empty dict when the table does not exist, so callers can treat "absent" and
    "no columns" alike; both mean "nothing to reconcile against".
    """
    embedded_schema, table = _split_relation(relation)
    target_schema = schema or embedded_schema

    def _reflect(conn: Connection) -> dict[str, TypeEngine[Any]]:
        insp = inspect(conn)
        if not insp.has_table(table, schema=target_schema):
            return {}
        return {str(col["name"]): col["type"] for col in insp.get_columns(table, schema=target_schema)}

    result = await _run_sync(executor, _reflect)
    return dict(result)


def type_ddl(type_: TypeEngine[Any]) -> str:
    """Render a reflected type as canonical Postgres DDL text.

    For callers that must *persist* a column's type (grid stores overlay column
    types in its binding JSON). ``BIGINT`` / ``TIMESTAMP WITH TIME ZONE`` —
    unambiguous, unlike the ``udt_name`` abbreviations, and reversible.
    """
    from sqlalchemy.dialects import postgresql

    return str(type_.compile(dialect=postgresql.dialect()))


async def has_table(executor: _Executor, relation: str, *, schema: str | None = None) -> bool:
    """Whether ``relation`` exists — replaces ``SELECT to_regclass(...)``."""
    embedded_schema, table = _split_relation(relation)
    target_schema = schema or embedded_schema

    def _check(conn: Connection) -> bool:
        return inspect(conn).has_table(table, schema=target_schema)

    return bool(await _run_sync(executor, _check))


async def udt_names(executor: _Executor, relation: str, *, schema: str | None = None) -> dict[str, str]:
    """Map column name → Postgres ``udt_name`` (``"int8"``, ``"timestamptz"``, …).

    The one place in the library that still queries ``information_schema``
    directly, and deliberately: ``udt_name`` is Postgres's own type token and has
    no lossless equivalent in a reflected ``TypeEngine`` (``VARCHAR`` and
    ``CITEXT`` both reflect as string types, and the name is what callers persist
    and later map back). :func:`column_types` is the right call for everything
    that wants SQLAlchemy types.

    Returns an empty mapping for a relation that does not exist.
    """
    embedded_schema, table = _split_relation(relation)
    target_schema = schema or embedded_schema
    stmt = sa.text(
        "SELECT column_name, udt_name FROM information_schema.columns "
        "WHERE table_name = :t AND table_schema = COALESCE(:s, current_schema())"
    )
    result = await executor.execute(stmt, {"t": table, "s": target_schema})
    return {row.column_name: row.udt_name for row in result}


async def indexes(executor: _Executor, relation: str, *, schema: str | None = None) -> set[str]:
    """Names of the indexes on ``relation``; empty when it does not exist."""
    embedded_schema, table = _split_relation(relation)
    target_schema = schema or embedded_schema

    def _reflect(conn: Connection) -> set[str]:
        insp: Inspector = inspect(conn)
        if not insp.has_table(table, schema=target_schema):
            return set()
        return {str(idx["name"]) for idx in insp.get_indexes(table, schema=target_schema) if idx.get("name")}

    return set(await _run_sync(executor, _reflect))
