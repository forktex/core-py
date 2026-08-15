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

"""Sync entry-point into the ForkTex substrates for alembic env.py.

Consumers that drive their own alembic pipeline AND maintain DB-level
cross-schema FKs into ``forktex_grid.*`` / ``forktex_flow.*`` need the substrate
to exist *before* their own ``upgrade head`` runs — otherwise the FK creation
fails because target tables don't yet exist.

The canonical mechanisms are still :func:`forktex_core.grid.apply_migrations`
and :func:`forktex_core.flow.apply_migrations` (async, advisory-locked,
idempotent, and identical in shape). This module just bridges sync→async for
callers that live in alembic env.py, removing the boilerplate:

    # alembic/env.py
    from forktex_core.alembic import ensure_substrate

    def run_migrations_online() -> None:
        ensure_substrate(settings.db_url)              # grid only (the default)
        ensure_substrate(settings.db_url, flow=True)   # grid + flow
        ...  # then your usual context.configure + run_migrations

Consumers that follow the loose-coupling pattern (no DB-level FKs into
the substrate; ORM-level references only — same as network and cloud)
don't need this helper at all. Substrate init happens in their FastAPI
lifespan as usual (``Grid``/``Namespace`` setup, or ``Flow.init()``).
"""

from __future__ import annotations

import asyncio

from .database import Database

__all__ = ["ensure_substrate"]


def ensure_substrate(
    db_url: str,
    schema: str = "forktex_grid",
    *,
    grid: bool = True,
    flow: bool = False,
    flow_schema: str = "forktex_flow",
) -> None:
    """Apply substrate migrations from a sync context.

    Thin wrapper around the async ``apply_migrations`` functions: builds one
    short-lived ``AsyncEngine``, applies each requested substrate's pending
    migrations under its own advisory lock, and disposes the engine. Safe to
    call multiple times — the underlying runners no-op when nothing is pending.

    ``db_url`` is the standard SQLAlchemy async URL (e.g.
    ``postgresql+asyncpg://user:pw@host/db``). Pass your alembic config's
    ``settings.db_url`` directly.

    ``grid`` and ``flow`` select which substrates to bring up. Grid is on by
    default because it is the only one this helper used to handle; a consumer
    that keeps cross-schema FKs into ``forktex_flow`` needs ``flow=True``, and
    one that uses flow alone passes ``grid=False``.

    ``flow`` is applied with no extensions: extension columns are declared on a
    ``Flow`` instance, so they belong to ``Flow.init()`` rather than to a
    migration hook that never sees them.
    """
    if not grid and not flow:
        raise ValueError("ensure_substrate: enable at least one of grid= or flow=")

    async def _run() -> None:
        # A dedicated short-lived handle, not the module-level default: this
        # runs inside its own `asyncio.run`, so leaving a disposed engine behind
        # as the process-wide default would break anything that ran afterwards.
        db = Database(db_url)
        try:
            if grid:
                from .grid import apply_migrations as apply_grid

                await apply_grid(db.engine, schema=schema)
            if flow:
                from .flow import apply_migrations as apply_flow

                await apply_flow(db.engine, schema=flow_schema)
        finally:
            await db.dispose()

    asyncio.run(_run())
