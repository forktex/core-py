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

"""Integration tests for db.migrate.SchemaMigrationRunner."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from forktex_core.error import BadRequestError
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

MIGRATION_SQL = """\
CREATE TABLE IF NOT EXISTS "{schema}".test_items (
    id   SERIAL PRIMARY KEY,
    name TEXT NOT NULL
);
"""


def _make_migrations_dir(tmp_path: Path) -> Path:
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "v0001__create_items.sql").write_text(MIGRATION_SQL)
    return mdir


@pytest.mark.asyncio
async def test_apply_creates_schema_and_table(postgres_url_str: str, fresh_schema: str, tmp_path: Path):
    from forktex_core.database.migrate import SchemaMigrationRunner

    engine = create_async_engine(postgres_url_str)
    mdir = _make_migrations_dir(tmp_path)
    runner = SchemaMigrationRunner(engine, schema=fresh_schema, migrations_dir=mdir)
    await runner.apply()

    async with engine.connect() as conn:
        rows = (await conn.execute(sa.text(f'SELECT 1 FROM "{fresh_schema}".test_items LIMIT 1'))).fetchall()
    assert rows == [] or rows is not None  # table exists
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_idempotent(postgres_url_str: str, fresh_schema: str, tmp_path: Path):
    """Running apply() twice must not raise or re-apply migrations."""
    from forktex_core.database.migrate import SchemaMigrationRunner

    engine = create_async_engine(postgres_url_str)
    mdir = _make_migrations_dir(tmp_path)
    runner = SchemaMigrationRunner(engine, schema=fresh_schema, migrations_dir=mdir)
    await runner.apply()
    await runner.apply()  # should be a no-op

    async with engine.connect() as conn:
        count = (await conn.execute(sa.text(f'SELECT COUNT(*) FROM "{fresh_schema}".schema_version'))).scalar_one()
    assert count == 1  # only version 1 recorded
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_concurrent_workers_safe(postgres_url_str: str, fresh_schema: str, tmp_path: Path):
    """Two concurrent apply() calls must serialise via advisory lock — no DDL race."""
    from forktex_core.database.migrate import SchemaMigrationRunner

    mdir = _make_migrations_dir(tmp_path)

    async def run_one():
        engine = create_async_engine(postgres_url_str)
        runner = SchemaMigrationRunner(engine, schema=fresh_schema, migrations_dir=mdir)
        await runner.apply()
        await engine.dispose()

    # Both workers start simultaneously
    await asyncio.gather(run_one(), run_one())

    engine = create_async_engine(postgres_url_str)
    async with engine.connect() as conn:
        count = (await conn.execute(sa.text(f'SELECT COUNT(*) FROM "{fresh_schema}".schema_version'))).scalar_one()
    assert count == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_migration_with_literal_braces_in_sql(postgres_url_str: str, fresh_schema: str, tmp_path: Path):
    """Migration SQL routinely contains literal `{`/`}` (JSONB defaults,
    PL/pgSQL blocks) alongside the {schema} placeholder — the runner must
    plain-string-replace {schema}, not str.format() the whole file, or any
    such content raises KeyError/IndexError from str.format's own parsing."""
    from forktex_core.database.migrate import SchemaMigrationRunner

    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "v0001__create_items.sql").write_text(
        """\
CREATE TABLE IF NOT EXISTS "{schema}".braces_items (
    id       SERIAL PRIMARY KEY,
    payload  JSONB NOT NULL DEFAULT '{}'::jsonb
);
"""
    )
    engine = create_async_engine(postgres_url_str)
    runner = SchemaMigrationRunner(engine, schema=fresh_schema, migrations_dir=mdir)
    await runner.apply()  # must not raise

    async with engine.connect() as conn:
        rows = (await conn.execute(sa.text(f'SELECT 1 FROM "{fresh_schema}".braces_items LIMIT 1'))).fetchall()
    assert isinstance(rows, list)
    await engine.dispose()


def test_invalid_schema_identifier_raises(tmp_path: Path):
    from forktex_core.database.migrate import SchemaMigrationRunner

    mdir = tmp_path / "migrations"
    mdir.mkdir()
    engine = create_async_engine("postgresql+asyncpg://unused/unused")
    # BadRequestError (an AppError) rather than a bare ValueError: the runner now
    # uses the shared `database.identifiers` validators, so a bad schema name is
    # reportable through the same envelope as any other caller-input error.
    with pytest.raises(BadRequestError, match="unsafe schema name"):
        SchemaMigrationRunner(engine, schema="bad; drop table x", migrations_dir=mdir)


def test_missing_migrations_dir_raises(tmp_path: Path):
    from forktex_core.database.migrate import SchemaMigrationRunner

    engine = create_async_engine("postgresql+asyncpg://unused/unused")
    with pytest.raises(BadRequestError, match="does not exist"):
        SchemaMigrationRunner(engine, schema="valid_schema", migrations_dir=tmp_path / "nope")


@pytest.mark.asyncio
async def test_apply_target_schema_override(postgres_url_str: str, fresh_schema: str, tmp_path: Path):
    """target_schema overrides where DDL runs."""
    from forktex_core.database.migrate import SchemaMigrationRunner

    engine = create_async_engine(postgres_url_str)
    target = fresh_schema + "_target"
    async with engine.begin() as conn:
        await conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{target}"'))

    mdir = _make_migrations_dir(tmp_path)
    runner = SchemaMigrationRunner(engine, schema="logical_name", migrations_dir=mdir, target_schema=target)
    await runner.apply()

    async with engine.connect() as conn:
        # Table should exist in target, not "logical_name"
        rows = (await conn.execute(sa.text(f'SELECT 1 FROM "{target}".test_items LIMIT 1'))).fetchall()
    assert isinstance(rows, list)
    await engine.dispose()
