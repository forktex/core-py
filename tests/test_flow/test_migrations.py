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

"""Schema + migration runner integration tests against a
real Postgres testcontainer.

Verifies:
- ``Flow.init()`` is idempotent (cold-start safe; re-runs are no-ops).
- All declared tables + indexes exist after init.
- The advisory lock serialises concurrent applies (multi-worker safe).
- Extension columns are picked up at init and added if missing.
- The schema is fully owned by the library — operator-supplied schema
  names are validated; unsafe identifiers are rejected.
- Forward-only migration tracking via ``flow_schema_version`` works.
"""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from forktex_core.error import BadRequestError
from forktex_core.flow import ColumnDef, Flow


pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────────────────


async def _table_exists(engine, schema: str, name: str) -> bool:
    async with engine.connect() as conn:
        row = await conn.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.tables
                 WHERE table_schema = :schema AND table_name = :name
                """
            ),
            {"schema": schema, "name": name},
        )
        return row.scalar_one_or_none() is not None


async def _column_exists(engine, schema: str, table: str, column: str) -> bool:
    async with engine.connect() as conn:
        row = await conn.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = :schema
                   AND table_name = :table
                   AND column_name = :column
                """
            ),
            {"schema": schema, "table": table, "column": column},
        )
        return row.scalar_one_or_none() is not None


async def _index_exists(engine, schema: str, name: str) -> bool:
    async with engine.connect() as conn:
        row = await conn.execute(
            sa.text(
                """
                SELECT 1 FROM pg_indexes
                 WHERE schemaname = :schema AND indexname = :name
                """
            ),
            {"schema": schema, "name": name},
        )
        return row.scalar_one_or_none() is not None


async def _applied_versions(engine, schema: str) -> list[int]:
    async with engine.connect() as conn:
        rows = await conn.execute(sa.text(f'SELECT version FROM "{schema}".flow_schema_version ORDER BY version'))
        return [row[0] for row in rows.fetchall()]


# ── Tests ────────────────────────────────────────────────────────────


async def test_init_creates_schema_and_tables(flow: Flow):
    """All declared tables exist under the library's schema."""
    expected = [
        "workflow",
        "run",
        "step_run",
        "run_event",
        "scheduled_run",
        "flow_schema_version",
        "flow_schema_extension",
    ]
    for table in expected:
        assert await _table_exists(flow.engine, flow.schema, table), f"missing table {flow.schema}.{table}"


async def test_init_creates_indexes(flow: Flow):
    """Indexes declared in the migration are created."""
    expected = [
        "ix_run_workflow_name",
        "ix_run_status",
        "ix_run_started_at",
        "ix_run_metadata_gin",
        "ix_step_run_status_heartbeat",
        "ix_step_run_run_id_index",
        "ix_run_event_run_id_ts",
        "ix_scheduled_run_enabled_next",
    ]
    for idx in expected:
        assert await _index_exists(flow.engine, flow.schema, idx), f"missing index {flow.schema}.{idx}"


async def test_init_records_applied_versions(flow: Flow):
    """``flow_schema_version`` is populated with the applied migration
    revision(s)."""
    versions = await _applied_versions(flow.engine, flow.schema)
    assert 1 in versions, f"v0001 not applied; got {versions}"


async def test_init_is_idempotent(flow: Flow):
    """Calling init() twice doesn't double-apply or error."""
    await flow.init()
    await flow.init()
    versions = await _applied_versions(flow.engine, flow.schema)
    # Each version recorded exactly once.
    assert len(versions) == len(set(versions)), f"duplicate version: {versions}"


async def test_init_rejects_unsafe_schema_name(db_url: str):
    """The schema name is templated into raw DDL; reject anything that
    isn't strict snake_case."""
    bad_names = [
        'public"; DROP SCHEMA public CASCADE; --',
        "with space",
        "UPPERCASE",
        "1leading_digit",
        "",
    ]
    for bad in bad_names:
        f = Flow(database_url=db_url, schema=bad)
        # BadRequestError (an AppError) rather than a bare ValueError: the
        # migration runner now validates through the shared
        # `database.identifiers`, so this is reportable via the standard
        # envelope like any other caller-input rejection. The rejection itself
        # is unchanged — that is the security-relevant part.
        with pytest.raises(BadRequestError, match="schema"):
            await f.init()
        await f.close()


async def test_concurrent_init_is_serialised(db_url: str, fresh_schema: str):
    """Two Flow instances racing init() must converge to identical
    state — the advisory lock serialises concurrent applies, the
    second wakeup is a no-op."""
    f1 = Flow(database_url=db_url, schema=fresh_schema)
    f2 = Flow(database_url=db_url, schema=fresh_schema)
    try:
        # Race them with gather() so the event loop interleaves their
        # advisory-lock acquisitions.
        await asyncio.gather(f1.init(), f2.init())

        v1 = await _applied_versions(f1.engine, fresh_schema)
        v2 = await _applied_versions(f2.engine, fresh_schema)
        assert v1 == v2
        assert v1 == sorted(set(v1))  # no duplicates
    finally:
        await f1.close()
        await f2.close()


async def test_run_table_check_constraints(flow: Flow):
    """status + triggered_by constraints are enforced by the DB."""
    async with flow.engine.begin() as conn:
        # Insert a sentinel row through SQL (the engine API isn't wired
        # until later phases; this is a constraint smoke test).
        await conn.execute(
            sa.text(
                f"""
                INSERT INTO "{flow.schema}".workflow (name, version)
                VALUES ('test.dummy', 1)
                """
            )
        )
        # Bad status rejected
        with pytest.raises(Exception):  # asyncpg raises CheckViolationError
            await conn.execute(
                sa.text(
                    f"""
                    INSERT INTO "{flow.schema}".run
                        (id, workflow_name, workflow_version, status)
                    VALUES (gen_random_uuid(), 'test.dummy', 1, 'bogus')
                    """
                )
            )


# ── Extension column pickup (schema runner side) ──


class _OrgScopedExt:
    """Test-only extension declaring an extra column on run + step_run."""

    def extra_run_columns(self):
        return [
            ColumnDef(name="org_id", type_=UUID(as_uuid=True), nullable=True, index=True),
            ColumnDef(name="trace_id", type_=sa.String(64), nullable=True),
        ]

    def extra_step_run_columns(self):
        return [ColumnDef(name="org_id", type_=UUID(as_uuid=True), nullable=True)]


async def test_extension_columns_added_on_init(db_url: str, fresh_schema: str):
    """Extension's declared columns are added to run + step_run."""
    f = Flow(database_url=db_url, schema=fresh_schema, extensions=[_OrgScopedExt()])
    try:
        await f.init()
        assert await _column_exists(f.engine, fresh_schema, "run", "org_id")
        assert await _column_exists(f.engine, fresh_schema, "run", "trace_id")
        assert await _column_exists(f.engine, fresh_schema, "step_run", "org_id")
        # Index requested for run.org_id
        assert await _index_exists(f.engine, fresh_schema, "ix_run_org_id")
    finally:
        await f.close()


async def test_extension_columns_idempotent(db_url: str, fresh_schema: str):
    """Re-running init with the same extension is a no-op."""
    f = Flow(database_url=db_url, schema=fresh_schema, extensions=[_OrgScopedExt()])
    try:
        await f.init()
        await f.init()  # second run shouldn't error
        # Verify the audit table has rows for each added column.
        async with f.engine.connect() as conn:
            count = (
                await conn.execute(sa.text(f'SELECT COUNT(*) FROM "{fresh_schema}".flow_schema_extension'))
            ).scalar_one()
            assert count == 3, f"expected 3 extension columns recorded; got {count}"
    finally:
        await f.close()


async def test_extension_column_rejects_unsafe_name(db_url: str, fresh_schema: str):
    """Extension column names are rejected before reaching DDL.

    Belt and braces now: `database.ddl.AddColumn` renders through the dialect's
    identifier preparer, so this name would be *quoted* rather than executed
    even if it slipped through. The name policy still rejects it, because a
    column called `foo"; DROP TABLE x; --` is a caller bug either way."""

    class _BadExt:
        def extra_run_columns(self):
            return [ColumnDef(name='foo"; DROP TABLE x; --', type_=sa.String(8))]

        def extra_step_run_columns(self):
            return []

    f = Flow(database_url=db_url, schema=fresh_schema, extensions=[_BadExt()])
    try:
        with pytest.raises(BadRequestError, match="column"):
            await f.init()
    finally:
        await f.close()


async def test_consumer_alembic_can_filter_schema_out(flow: Flow):
    """The forktex_flow schema is not in 'public', so consumer alembic
    that defaults to ``include_schemas=False`` will never see these
    tables. Smoke test: ``information_schema.schemata`` lists both
    ``public`` and the lib's schema as distinct entries."""
    async with flow.engine.connect() as conn:
        rows = await conn.execute(
            sa.text("SELECT schema_name FROM information_schema.schemata WHERE schema_name IN ('public', :s)"),
            {"s": flow.schema},
        )
        names = {row[0] for row in rows.fetchall()}
    assert "public" in names
    assert flow.schema in names
    assert flow.schema != "public"


async def test_drop_schema_cascade_uninstalls_cleanly(db_url: str, fresh_schema: str):
    """Operator can fully uninstall by dropping the schema. Re-init on
    the same schema is a clean cold start."""
    f = Flow(database_url=db_url, schema=fresh_schema)
    try:
        await f.init()
        async with f.engine.begin() as conn:
            await conn.execute(sa.text(f'DROP SCHEMA "{fresh_schema}" CASCADE'))
        # Re-init brings everything back from scratch.
        await f.init()
        versions = await _applied_versions(f.engine, fresh_schema)
        assert 1 in versions
    finally:
        await f.close()
