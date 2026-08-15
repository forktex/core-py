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

"""Reads and writes on ``forktex_flow.workflow_definition``.

Namespace-track workflows are declared at runtime and persisted here; platform-track ones
live only in code. The upsert relies on the column defaults — `config` is mapped ``JSONB``
so the dict binds directly, and `id` declares ``default=uuid.uuid7`` — which is why there
is no hand-written cast, no ``json.dumps`` and no explicit id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from forktex_core.flow.persist.models import Run, WorkflowDefinitionRow
from forktex_core.log import get_logger

if TYPE_CHECKING:
    from forktex_core.flow.flow import Flow

logger = get_logger(__name__)


async def upsert_namespace_definition(
    flow: Flow,
    *,
    name: str,
    version: int,
    namespace: str,
    type_: str,
    config: dict[str, Any],
) -> None:
    """Persist or update a namespace-track workflow definition.
    Uses ON CONFLICT to upsert (update config + updated_at if already exists).

    The Core form removes three hand-rolled workarounds the raw statement
    needed: the ``::jsonb`` cast (``config`` is already mapped ``JSONB``, so the
    dict binds directly), the manual ``json.dumps``, and the explicit
    ``uuid7()`` (the column already declares that default).
    """
    stmt = pg_insert(WorkflowDefinitionRow).values(
        name=name,
        version=version,
        namespace=namespace,
        type=type_,
        config=config,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            WorkflowDefinitionRow.name,
            WorkflowDefinitionRow.version,
            WorkflowDefinitionRow.namespace,
        ],
        set_={"config": stmt.excluded.config, "updated_at": sa.func.now()},
    )
    async with flow.session() as session:
        await session.execute(stmt)
        await session.commit()


async def delete_namespace_definition(
    flow: Flow,
    name: str,
    namespace: str,
) -> None:
    """Delete all versions of a namespace-track definition.
    Raises ValueError if any runs for this definition are currently running.
    """
    async with flow.session() as session:
        active = (
            await session.execute(
                select(Run.id)
                .where(
                    Run.workflow_name == name,
                    Run.metadata_["__namespace__"].astext == namespace,
                    Run.status.in_(["pending", "running"]),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if active is not None:
            raise ValueError(
                f"Cannot delete definition {name!r} in namespace {namespace!r}: "
                f"active runs exist (e.g. run_id={active})"
            )

        await session.execute(
            sa.delete(WorkflowDefinitionRow).where(
                WorkflowDefinitionRow.name == name,
                WorkflowDefinitionRow.namespace == namespace,
            )
        )
        await session.commit()


async def load_namespace_definitions(
    flow: Flow,
    namespace: str | None = None,
) -> list[dict[str, Any]]:
    """Load all namespace-track definitions from DB.
    Returns list of dicts with keys: name, version, namespace, type, config.
    Used at startup to hydrate the in-memory registry.
    """
    # `WHERE 1=1` plus string `+=` is precisely what conditional `.where()`
    # chaining exists to replace; the pattern was already used in
    # `execute_instance_query` below.
    stmt = select(
        WorkflowDefinitionRow.name,
        WorkflowDefinitionRow.version,
        WorkflowDefinitionRow.namespace,
        WorkflowDefinitionRow.type,
        WorkflowDefinitionRow.config,
    )
    if namespace is not None:
        stmt = stmt.where(WorkflowDefinitionRow.namespace == namespace)
    stmt = stmt.order_by(
        WorkflowDefinitionRow.namespace,
        WorkflowDefinitionRow.name,
        WorkflowDefinitionRow.version,
    )

    async with flow.session() as session:
        rows = (await session.execute(stmt)).all()
        return [
            {
                "name": r.name,
                "version": r.version,
                "namespace": r.namespace,
                "type": r.type,
                "config": r.config,
            }
            for r in rows
        ]
