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

"""Embedded SQL migrations for the ``forktex_grid`` schema.

Each ``v{NNNN}__{description}.sql`` file carries one migration. The
runner :func:`forktex_core.grid.persist.migrations.apply_migrations` walks
the files in version order, applies each in a transaction, and
records the version in ``forktex_grid.schema_version``.

Forward-only — bad migration is fixed by the next version forward,
never by a downgrade.

Usage::

    from forktex_core.grid.persist.migrations import apply_migrations

    await apply_migrations(engine, schema="forktex_grid")
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from forktex_core.database.migrate import SchemaMigrationRunner

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parent


async def apply_migrations(engine: AsyncEngine, schema: str = "forktex_grid") -> None:
    """Apply unapplied SQL migrations for the ``forktex_grid`` schema.

    Idempotent + multi-worker-safe (uses an advisory lock under the
    hood). Call from consumer startup or alembic migrations.
    """
    runner = SchemaMigrationRunner(
        engine=engine,
        schema=schema,
        migrations_dir=_migrations_dir(),
        # Stated rather than inherited from the runner default, so the pairing
        # with `flow`'s tracker is visible. The two names differ —
        # `forktex_grid.schema_version` vs `forktex_flow.flow_schema_version` —
        # only because flow's predates the per-schema convention; each table
        # already lives in its own schema, so flow's prefix is redundant. It is
        # kept because renaming a version tracker strands the applied history
        # recorded in it, which is not worth a cosmetic fix.
        version_table="schema_version",
    )
    await runner.apply()


__all__ = ["apply_migrations"]
