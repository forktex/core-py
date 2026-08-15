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

"""Migration runner for forktex_core.flow.

Generic SQL-file runner is provided by ``forktex_core.database.migrate.SchemaMigrationRunner``.
This module wraps it and adds the flow-specific extension-column layer
(ADD COLUMN for FlowExtension columns).

The extension layer is built from ``forktex_core.database`` primitives —
``ddl.AddColumn``/``CreateIndex``/``CreateTable`` for the DDL,
``reflect.columns`` for the existence probe, and ``identifiers`` for the
name policy — so it renders through the dialect's identifier preparer
instead of interpolating names into SQL strings.

Forward-only — no downgrades.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from forktex_core.database import ddl, reflect
from forktex_core.database.identifiers import validate_identifier
from forktex_core.database.migrate import SchemaMigrationRunner
from forktex_core.database.models import UtcDateTime
from forktex_core.log import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from forktex_core.flow.extension import ColumnDef, FlowExtension

logger = get_logger(__name__)


def _extension_table(schema: str) -> sa.Table:
    """The ``flow_schema_extension`` tracker as a Core ``Table``.

    Built per call against ``schema`` on a throwaway ``MetaData``: the runner
    is handed an arbitrary schema at runtime, and this table is never mapped
    or reflected, so it must not join the shared declarative metadata.
    """
    return sa.Table(
        "flow_schema_extension",
        sa.MetaData(),
        sa.Column("extension_class", sa.String(255), primary_key=True),
        sa.Column("target_table", sa.String(64), primary_key=True),
        sa.Column("column_name", sa.String(64), primary_key=True),
        sa.Column("applied_at", UtcDateTime, nullable=False, server_default=sa.func.now()),
        schema=schema,
    )


def _migrations_dir() -> Path:
    return Path(str(files("forktex_core.flow.persist.migrations")))


async def apply_migrations(
    engine: AsyncEngine,
    schema: str,
    extensions: list[FlowExtension],
) -> None:
    """Apply unapplied SQL migrations then extension columns.

    Delegates the SQL-file migration phase to ``db.migrate.SchemaMigrationRunner``
    (advisory-lock-protected, idempotent, multi-worker-safe).

    Uses ``flow_schema_version`` as the version tracker table (flow-specific
    name, distinct from the generic ``schema_version`` used by other modules)
    and also bootstraps the ``flow_schema_extension`` tracking table.
    """
    runner = SchemaMigrationRunner(
        engine=engine,
        schema=schema,
        migrations_dir=_migrations_dir(),
        version_table="flow_schema_version",
    )
    await runner.apply()
    await _bootstrap_extension_table(engine, schema)
    await _apply_extension_columns(engine, schema, extensions)


async def _bootstrap_extension_table(engine: AsyncEngine, schema: str) -> None:
    """Create the flow_schema_extension tracker if absent."""
    async with engine.begin() as conn:
        await conn.execute(ddl.CreateTable(_extension_table(schema), if_not_exists=True))


async def _apply_extension_columns(
    engine: AsyncEngine,
    schema: str,
    extensions: list[FlowExtension],
) -> None:
    """Walk each extension's declared columns; ALTER ADD COLUMN any
    that don't already exist. Tracks applied additions in
    ``flow_schema_extension`` for idempotency + audit."""

    target_tables = {
        "run": "extra_run_columns",
        "step_run": "extra_step_run_columns",
    }
    tracker = _extension_table(schema)
    for ext in extensions:
        ext_class = f"{ext.__class__.__module__}.{ext.__class__.__qualname__}"
        for table, method_name in target_tables.items():
            method = getattr(ext, method_name, None)
            if method is None:
                continue
            cols: list[ColumnDef] = list(method() or [])
            for col in cols:
                validate_identifier(col.name, "column")
                validate_identifier(table, "table")
                async with engine.connect() as ck:
                    present = await reflect.columns(ck, table, schema=schema)
                if col.name in present:
                    continue
                type_obj = col.type_() if isinstance(col.type_, type) else col.type_
                # `AddColumn`/`CreateIndex` render from a `Column` object, so the
                # column has to be attached to a `Table`. A throwaway `MetaData`
                # keeps this out of the mapped metadata — nothing here is mapped.
                target = sa.Table(table, sa.MetaData(), schema=schema)
                column = sa.Column(col.name, type_obj, nullable=col.nullable)
                target.append_column(column)
                async with engine.begin() as conn:
                    await conn.execute(ddl.AddColumn(column, if_not_exists=True))
                    if col.index:
                        idx_name = f"ix_{table}_{col.name}"
                        validate_identifier(idx_name, "index")
                        await conn.execute(ddl.CreateIndex(sa.Index(idx_name, column), if_not_exists=True))
                    await conn.execute(
                        pg_insert(tracker)
                        .values(
                            extension_class=ext_class,
                            target_table=table,
                            column_name=col.name,
                        )
                        .on_conflict_do_nothing()
                    )
                logger.info(
                    "forktex_flow: extension %s added %s.%s.%s",
                    ext_class,
                    schema,
                    table,
                    col.name,
                )
