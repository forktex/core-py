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

"""Embedded SQL migrations for the ``forktex_flow`` schema.

Each ``v{NNNN}__{description}.sql`` file carries one migration. The runner walks
the files in version order, applies each in a transaction, and records the
version in ``forktex_flow.flow_schema_version``.

Forward-only — a bad migration is fixed by the next version forward, never by a
downgrade. Same convention DBOS, Loki and Sentry follow.

The entry point mirrors ``forktex_core.grid.apply_migrations`` exactly, so the
two substrates are brought up the same way::

    from forktex_core.flow import apply_migrations

    await apply_migrations(engine, schema="forktex_flow")

``Flow.init()`` calls this for you; call it directly when a consumer drives its
own migration pipeline (see :func:`forktex_core.alembic.ensure_substrate`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from forktex_core.flow.persist.migrations._runner import apply_migrations as _apply

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from forktex_core.flow.extension import FlowExtension

__all__ = ["apply_migrations"]


async def apply_migrations(
    engine: AsyncEngine,
    schema: str = "forktex_flow",
    extensions: Sequence[FlowExtension] = (),
) -> None:
    """Apply unapplied SQL migrations for the ``forktex_flow`` schema.

    Idempotent + multi-worker-safe (uses an advisory lock under the hood). Call
    from consumer startup or alembic migrations.

    ``extensions`` adds each :class:`FlowExtension`'s declared columns after the
    SQL phase; it defaults to none so the signature matches ``grid``'s and the
    same call works for a consumer with no extensions.
    """
    await _apply(engine, schema, list(extensions))
