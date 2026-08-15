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

"""Integration tests for forktex_core.database.reflect — requires Postgres."""

from __future__ import annotations


import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from forktex_core.database import reflect


@pytest_asyncio.fixture
async def engine(postgres_url_str: str, fresh_schema: str):
    eng = create_async_engine(postgres_url_str)
    async with eng.begin() as conn:
        await conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{fresh_schema}"'))
        await conn.execute(
            sa.text(
                f'CREATE TABLE "{fresh_schema}".reflect_probe ('
                "  c_uuid uuid, c_int8 bigint, c_tstz timestamptz,"
                "  c_ts timestamp, c_bool boolean, c_text text)"
            )
        )
        await conn.execute(sa.text(f'CREATE INDEX ix_probe_text ON "{fresh_schema}".reflect_probe (c_text)'))
    yield eng
    await eng.dispose()


@pytest.mark.asyncio
async def test_columns_returns_names(engine, fresh_schema: str):
    async with engine.connect() as conn:
        names = await reflect.columns(conn, "reflect_probe", schema=fresh_schema)
    assert names == {"c_uuid", "c_int8", "c_tstz", "c_ts", "c_bool", "c_text"}


@pytest.mark.asyncio
async def test_relation_may_embed_the_schema(engine, fresh_schema: str):
    async with engine.connect() as conn:
        names = await reflect.columns(conn, f"{fresh_schema}.reflect_probe")
    assert "c_uuid" in names


@pytest.mark.asyncio
async def test_absent_table_yields_empty_rather_than_raising(engine, fresh_schema: str):
    """Callers treat 'absent' and 'no columns' alike — both mean nothing to
    reconcile against — so this must not raise."""
    async with engine.connect() as conn:
        assert await reflect.columns(conn, "no_such_table", schema=fresh_schema) == set()
        assert await reflect.column_types(conn, "no_such_table", schema=fresh_schema) == {}
        assert await reflect.indexes(conn, "no_such_table", schema=fresh_schema) == set()
        assert await reflect.has_table(conn, "no_such_table", schema=fresh_schema) is False


@pytest.mark.asyncio
async def test_column_types_are_lossless_about_timezone(engine, fresh_schema: str):
    """The reason this returns type *objects* and not name strings.

    `information_schema.udt_name` distinguishes `timestamptz` from `timestamp`,
    but SQLAlchemy's own type *naming* collapses both to "timestamp" — so a
    name-based round-trip silently discards timezone-awareness. The type object
    keeps it.
    """
    async with engine.connect() as conn:
        types = await reflect.column_types(conn, "reflect_probe", schema=fresh_schema)

    assert isinstance(types["c_tstz"], sa.TIMESTAMP)
    assert types["c_tstz"].timezone is True
    assert isinstance(types["c_ts"], sa.TIMESTAMP)
    assert types["c_ts"].timezone is False
    # and they render distinctly, which a name-keyed map could not express
    assert reflect.type_ddl(types["c_tstz"]) == "TIMESTAMP WITH TIME ZONE"
    assert reflect.type_ddl(types["c_ts"]) == "TIMESTAMP WITHOUT TIME ZONE"


@pytest.mark.asyncio
async def test_type_ddl_is_unambiguous_and_reversible(engine, fresh_schema: str):
    async with engine.connect() as conn:
        types = await reflect.column_types(conn, "reflect_probe", schema=fresh_schema)
    assert reflect.type_ddl(types["c_int8"]) == "BIGINT"
    assert reflect.type_ddl(types["c_bool"]) == "BOOLEAN"
    assert reflect.type_ddl(types["c_uuid"]) == "UUID"


@pytest.mark.asyncio
async def test_has_table_and_indexes(engine, fresh_schema: str):
    async with engine.connect() as conn:
        assert await reflect.has_table(conn, "reflect_probe", schema=fresh_schema) is True
        assert "ix_probe_text" in await reflect.indexes(conn, "reflect_probe", schema=fresh_schema)


@pytest.mark.asyncio
async def test_works_with_a_session_not_just_a_connection(postgres_url_str: str, fresh_schema: str, engine):
    """Prior callers were split between sessions and connections; one API serves
    both so neither has to open its own."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        names = await reflect.columns(session, "reflect_probe", schema=fresh_schema)
    assert "c_uuid" in names
