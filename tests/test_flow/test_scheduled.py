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

"""Scheduled (cron-driven) workflow runtime.

Verifies that ``@flow.scheduled`` registers a row in
``forktex_flow.scheduled_run`` and the driver actually fires the
workflow when the cron expression's next-fire-at passes. A drop-in
replacement for APScheduler-style schedulers — same primitive, same
reliability, multi-worker-safe via the leader-election driver.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from forktex_core.flow import Ctx, Flow, step
from forktex_core.flow.persist.models import ScheduledRun

from .conftest import wait_for_status

pytestmark = pytest.mark.asyncio


async def test_scheduled_workflow_registers_in_scheduled_run_table(flow: Flow):
    """``@flow.scheduled`` causes a row to land in ``scheduled_run``
    on first driver tick."""

    @step
    async def trivial_sched(ctx: Ctx, state: dict) -> dict:
        return {**state, "r": 1}

    # ``* * * * *`` fires every minute; we won't wait for it here, just
    # assert the row materialises after start_driver has had time to
    # run a tick.
    @flow.scheduled("hourly_report", version=1, cron="* * * * *")
    async def scheduled_wf(ctx: Ctx, state: dict) -> dict:
        return {**state, "r": 1}

    await flow.start_driver()
    # Allow a tick to run.
    await asyncio.sleep(2.0)

    async with flow.session() as session:
        rows = (
            (await session.execute(sa.select(ScheduledRun).where(ScheduledRun.workflow_name == "hourly_report")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.cron == "* * * * *"
    assert row.enabled is True
    assert row.next_fire_at is not None


async def test_scheduled_workflow_fires_when_due(flow: Flow):
    """Force a scheduled row's ``next_fire_at`` to the past; the next
    driver tick must submit a run for that workflow."""

    @step
    async def trivial_daily(ctx: Ctx, state: dict) -> dict:
        return {**state, "r": 42}

    @flow.scheduled("daily_job", version=1, cron="0 0 * * *")
    async def daily(ctx: Ctx, state: dict) -> dict:
        return {**state, "r": 42}

    await flow.start_driver()
    # Wait for initial registration.
    for _ in range(20):
        async with flow.session() as session:
            row = (
                await session.execute(sa.select(ScheduledRun).where(ScheduledRun.workflow_name == "daily_job"))
            ).scalar_one_or_none()
        if row is not None:
            break
        await asyncio.sleep(0.2)
    assert row is not None

    # Force-due: backdate next_fire_at so the next tick fires it.
    async with flow.session() as session:
        await session.execute(
            sa.update(ScheduledRun)
            .where(ScheduledRun.workflow_name == "daily_job")
            .values(next_fire_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        await session.commit()

    # Wait for a triggered_by="schedule" run to appear and complete.
    triggered_run_id = None
    for _ in range(30):
        await asyncio.sleep(0.5)
        runs = await flow.list(workflow_name="daily_job")
        for r in runs:
            # We tagged scheduled runs via triggered_by="schedule" —
            # the public API doesn't expose it directly so we read via ORM.
            async with flow.session() as session:
                from forktex_core.flow.persist.models import Run

                row = (await session.execute(sa.select(Run.triggered_by).where(Run.id == r.run_id))).scalar_one()
                if row == "schedule":
                    triggered_run_id = r.run_id
                    break
        if triggered_run_id is not None:
            break
    assert triggered_run_id is not None, "no scheduled run materialised"

    final = await wait_for_status(flow, triggered_run_id, until={"completed"})
    assert final == "completed"
    info = await flow.get(triggered_run_id)
    assert info.output == {"r": 42}


async def test_scheduled_workflow_advances_next_fire_at(flow: Flow):
    """After firing, ``next_fire_at`` must be advanced to a future
    moment so the row doesn't re-fire on every tick."""

    @step
    async def trivial_quarterly(ctx: Ctx, state: dict) -> dict:
        return {**state, "r": 1}

    @flow.scheduled("quarterly", version=1, cron="*/15 * * * *")
    async def quarterly(ctx: Ctx, state: dict) -> dict:
        return {**state, "r": 1}

    await flow.start_driver()
    # Wait for registration.
    for _ in range(20):
        async with flow.session() as session:
            row = (
                await session.execute(sa.select(ScheduledRun).where(ScheduledRun.workflow_name == "quarterly"))
            ).scalar_one_or_none()
        if row is not None:
            break
        await asyncio.sleep(0.2)
    assert row is not None

    # Force-due.
    async with flow.session() as session:
        await session.execute(
            sa.update(ScheduledRun)
            .where(ScheduledRun.workflow_name == "quarterly")
            .values(next_fire_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        await session.commit()

    # Wait until next_fire_at is advanced past now.
    advanced = False
    for _ in range(20):
        await asyncio.sleep(0.5)
        async with flow.session() as session:
            row = (
                await session.execute(sa.select(ScheduledRun).where(ScheduledRun.workflow_name == "quarterly"))
            ).scalar_one()
        if row.next_fire_at > datetime.now(timezone.utc):
            advanced = True
            break
    assert advanced, f"next_fire_at did not advance: still {row.next_fire_at}"
