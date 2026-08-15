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

"""Reads and writes on ``forktex_flow.run`` and ``forktex_flow.run_event``.

One aggregate per module: everything here is scoped to a run's own row and its append-only
event stream. Step-level state lives in :mod:`steps`, signals in :mod:`signals`.

``NOTIFY`` goes through ``sa.select(sa.func.pg_notify(channel, payload))`` rather than the
``NOTIFY`` statement, which takes only literals — so both the channel and the payload
travel as bind parameters instead of being spliced into SQL text.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from forktex_core.flow.domain.types import TERMINAL_STATUSES, RunInfo, RunStatus
from forktex_core.flow.persist.mappers import to_run_info
from forktex_core.flow.persist.models import Run, RunEvent
from forktex_core.log import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from forktex_core.flow.flow import Flow

logger = get_logger(__name__)


async def notify_run(conn: AsyncConnection, schema: str, run_id: UUID) -> None:
    """``NOTIFY`` consumers of ``flow.stream(run_id)``. The channel
    name is per-schema so multiple Flow instances on the same DB
    don't bleed updates.

    Uses ``pg_notify(channel, payload)`` rather than the ``NOTIFY`` statement:
    ``NOTIFY`` takes only literals, so both the channel and the payload had to
    be interpolated into the SQL string. ``pg_notify`` is an ordinary function,
    so both travel as bind parameters and nothing is spliced into SQL text.
    """
    await conn.execute(
        sa.select(sa.func.pg_notify(f"{schema}_run", str(run_id))),
    )


async def insert_run(
    flow: Flow,
    *,
    run_id: UUID,
    workflow_name: str,
    workflow_version: int,
    input: dict[str, Any],
    metadata: dict[str, Any],
    triggered_by: str = "manual",
) -> None:
    async with flow.session() as session:
        run = Run(
            id=run_id,
            workflow_name=workflow_name,
            workflow_version=workflow_version,
            status="pending",
            input=input or {},
            metadata_=metadata or {},
            triggered_by=triggered_by,
        )
        session.add(run)
        await session.flush()
        await notify_run(await session.connection(), flow.schema, run_id)
        await session.commit()


async def update_run_status(
    flow: Flow,
    run_id: UUID,
    *,
    status: RunStatus,
    output: dict[str, Any] | None = None,
    error: str | None = None,
    cancel_reason: str | None = None,
) -> None:
    async with flow.session() as session:
        values: dict[str, Any] = {"status": status}
        if output is not None:
            values["output"] = output
        if error is not None:
            values["error"] = error
        if cancel_reason is not None:
            values["cancel_reason"] = cancel_reason
        if status in TERMINAL_STATUSES:
            values["finished_at"] = sa.func.now()
        if status == "cancelled":
            values["cancelled_at"] = sa.func.now()
        await session.execute(update(Run).where(Run.id == run_id).values(**values))
        await session.flush()
        await notify_run(await session.connection(), flow.schema, run_id)
        await session.commit()


async def fetch_run(flow: Flow, run_id: UUID) -> RunInfo | None:
    async with flow.session() as session:
        row = (
            await session.execute(select(Run).options(selectinload(Run.steps)).where(Run.id == run_id))
        ).scalar_one_or_none()
        if row is None:
            return None
        return to_run_info(row)


async def list_runs(
    flow: Flow,
    *,
    workflow_name: str | None,
    statuses: list[str] | None,
    metadata_filter: dict[str, Any] | None,
    started_after: datetime | None,
    started_before: datetime | None,
    limit: int,
) -> AsyncIterator[RunInfo]:
    stmt = select(Run).options(selectinload(Run.steps)).order_by(Run.started_at.desc()).limit(limit)
    if workflow_name is not None:
        stmt = stmt.where(Run.workflow_name == workflow_name)
    if statuses:
        stmt = stmt.where(Run.status.in_(statuses))
    if started_after is not None:
        stmt = stmt.where(Run.started_at >= started_after)
    if started_before is not None:
        stmt = stmt.where(Run.started_at < started_before)
    if metadata_filter:
        # JSONB containment: ``metadata @> :filter``. Bind the filter
        # explicitly with the ``JSONB`` type so asyncpg encodes it as
        # ``jsonb`` and the operator resolves to ``@>``.
        from sqlalchemy.dialects.postgresql import JSONB

        stmt = stmt.where(Run.metadata_.op("@>")(sa.bindparam("md_filter", value=metadata_filter, type_=JSONB)))

    async with flow.session() as session:
        rows = (await session.execute(stmt)).scalars().all()
        for row in rows:
            yield to_run_info(row)


async def update_run_output(
    flow: Flow,
    run_id: UUID,
    state: dict[str, Any],
) -> None:
    """Write the current accumulated state to run.output after each node completes.
    Called by execute_graph_run after every node so crash recovery can resume
    from the last-good state rather than starting with the initial input.
    """
    async with flow.session() as session:
        await session.execute(update(Run).where(Run.id == run_id).values(output=state))
        await session.commit()


async def emit_run_event(
    flow: Flow,
    run_id: UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    async with flow.session() as session:
        session.add(RunEvent(run_id=run_id, event_type=event_type, payload=payload or {}))
        await session.flush()
        await notify_run(await session.connection(), flow.schema, run_id)
        await session.commit()


async def claim_pending_runs(flow: Flow, limit: int) -> list[tuple[UUID, str, int]]:
    """Atomically pick up to ``limit`` pending runs: marks them
    ``running`` and returns ``(run_id, name, version)`` tuples for
    dispatch.

    Core composes this exactly, contrary to the comment that used to sit here
    claiming otherwise: ``with_for_update(skip_locked=True)`` on a scalar
    subquery renders the locking clause *inside* the subselect, which is where
    Postgres needs it, so the single-statement atomicity is preserved. Both
    halves were already used elsewhere in this module and in ``driver.py``; only
    this site never combined them. Building it in Core also means the schema no
    longer has to be re-interpolated by hand — ``schema_translate_map`` handles
    it, which was the sole reason for the f-string.
    """
    claimable = (
        select(Run.id)
        .where(Run.status == "pending")
        .order_by(Run.started_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        update(Run)
        .where(Run.id.in_(claimable))
        .values(status="running")
        .returning(Run.id, Run.workflow_name, Run.workflow_version)
        .execution_options(synchronize_session=False)
    )
    async with flow.session() as session:
        rows = (await session.execute(stmt)).all()
        return [(r.id, r.workflow_name, r.workflow_version) for r in rows]
