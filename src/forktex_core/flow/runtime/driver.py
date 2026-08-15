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

"""Driver loop — leader-elected dispatcher for runs.

One driver per ``Flow`` instance. Each ``Flow`` competes for a
session-scoped Postgres advisory lock; only the holder runs ticks.
On lock loss (process death drops the connection → lock auto-
released), another worker grabs the lock on its next attempt.

The tick:
  1. Reclaim stale steps (heartbeat older than threshold).
  2. Claim up to N pending runs, mark each ``running``.
  3. Dispatch each via ``replay.execute_run`` as a coroutine.

Concurrency: many runs can execute concurrently inside one leader
worker — they're async coroutines, bounded only by the worker's
event loop. Throughput per leader is ~hundreds of in-flight runs
(I/O-bound). For higher throughput, multiple leaders would need a
sharded lock scheme; documented YAGNI for V1.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from croniter import croniter
from sqlalchemy.dialects.postgresql import insert as pg_insert

from forktex_core import iso
from forktex_core.database.locks import try_advisory_lock
from forktex_core.flow.persist import runs as _runs
from forktex_core.flow.persist import steps as _steps
from forktex_core.flow.persist.models import Run, ScheduledRun, StepRun, Workflow
from forktex_core.flow.runtime.replay import execute_run
from forktex_core.log import get_logger

if TYPE_CHECKING:
    from forktex_core.flow.flow import Flow

logger = get_logger(__name__)


class _Driver:
    """Per-process driver. Compete for the leader lock; tick when held."""

    def __init__(self, flow: Flow) -> None:
        self.flow = flow
        self.shutdown = asyncio.Event()
        # Pending in-flight run tasks so we can join on shutdown.
        self._tasks: set[asyncio.Task[None]] = set()
        # Leadership state — exposed for tests + introspection.
        self.is_leader = False

    async def run(self) -> None:
        """Block until ``shutdown`` is set, alternating between
        leader-acquire attempts and (when leader) tick + sleep loops."""
        while not self.shutdown.is_set():
            try:
                await self._compete_for_leadership()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("driver loop crashed; will retry after backoff")
                try:
                    await asyncio.wait_for(self.shutdown.wait(), timeout=5.0)
                except TimeoutError:
                    continue
        await self._drain_tasks()

    async def _compete_for_leadership(self) -> None:
        """Hold a session-scoped advisory lock for as long as we're leader.

        Delegates to ``db.locks.try_advisory_lock`` which wraps
        ``pg_try_advisory_lock`` / ``pg_advisory_unlock``. The lock is
        released automatically when the connection closes (process death),
        which is the failover primitive.
        """
        async with try_advisory_lock(self.flow.engine, self.flow.leader_lock_key) as is_leader:
            if not is_leader:
                self.is_leader = False
                # A timeout here is the normal case: not the leader, so wait a
                # beat before the next election attempt rather than hot-looping.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.shutdown.wait(), timeout=2.0)
                return
            self.is_leader = True
            logger.info(
                "forktex_flow.driver: this worker is now leader (schema=%s)",
                self.flow.schema,
            )
            try:
                while not self.shutdown.is_set():
                    await self._tick()
                    try:
                        await asyncio.wait_for(self.shutdown.wait(), timeout=self.flow.poll_interval)
                    except TimeoutError:
                        continue
            finally:
                self.is_leader = False

    async def _tick(self) -> None:
        """One driver pass: fire due scheduled runs, reclaim stale
        steps, claim + dispatch runs."""
        try:
            await self._fire_due_scheduled_runs()
        except Exception:
            logger.exception("_fire_due_scheduled_runs failed")

        try:
            reclaimed = await _steps.reclaim_stale_steps(self.flow)
            if reclaimed:
                logger.info("forktex_flow.driver: reclaimed %d stale steps", reclaimed)
        except Exception:
            logger.exception("reclaim_stale_steps failed")

        try:
            # Also revive runs whose steps were just reclaimed (their
            # status went to 'pending' but the run row still says
            # 'running' from the earlier replay). Sweep them back to
            # 'pending' so they get re-claimed by the next tick.
            await self._revive_runs_with_pending_steps()
            claims = await _runs.claim_pending_runs(self.flow, limit=10)
        except Exception:
            logger.exception("claim_pending_runs failed")
            return

        for run_id, name, version in claims:
            task = asyncio.create_task(
                execute_run(self.flow, run_id, name, version),
                name=f"forktex_flow.run[{run_id}]",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _fire_due_scheduled_runs(self) -> None:
        """Submit a fresh run for every ``@flow.scheduled`` workflow
        whose ``next_fire_at`` has passed.

        Concurrency safety: the leader runs this; ``UPDATE … RETURNING``
        is atomic so even if a slow tick coincides with leader handover
        we won't double-fire.

        Short-circuit when the registry has no ``@flow.scheduled``
        workflows AND the ``scheduled_run`` table is empty — most
        consumers (cloud, intelligence) don't use scheduled flows, and
        without this we'd pay a query every tick for nothing.
        """
        from sqlalchemy import select

        sched_defns = self.flow._registry.scheduled_definitions()
        if not sched_defns:
            async with self.flow.session() as session:
                has_rows = (await session.execute(select(ScheduledRun.workflow_name).limit(1))).first() is not None
            if not has_rows:
                return

        now = iso.now()

        # First, ensure each registered @flow.scheduled has a row in
        # ``scheduled_run`` with a current ``next_fire_at`` — register
        # any new ones (idempotent on existing).
        async with self.flow.session() as session:
            for defn in sched_defns:
                name, version, cron = defn.name, defn.version, defn.schedule
                if cron is None:
                    # ``sched_defns`` is filtered to scheduled definitions
                    # upstream; a None schedule here would be a registration
                    # bug. Fail loud rather than skip silently.
                    raise RuntimeError(f"scheduled flow {name!r} has no cron expression")
                # Workflow row must exist before we can FK to it.
                await session.execute(
                    pg_insert(Workflow)
                    .values(name=name, version=version)
                    .on_conflict_do_nothing(index_elements=[Workflow.name, Workflow.version])
                )
                existing = (
                    await session.execute(
                        select(ScheduledRun).where(
                            ScheduledRun.workflow_name == name,
                            ScheduledRun.workflow_version == version,
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    next_fire = croniter(cron, now).get_next(datetime)
                    session.add(
                        ScheduledRun(
                            workflow_name=name,
                            workflow_version=version,
                            cron=cron,
                            enabled=True,
                            next_fire_at=next_fire,
                        )
                    )
            await session.commit()

        # Now claim any due rows. ``UPDATE … RETURNING`` advances each
        # row's ``next_fire_at`` atomically so a slower tick won't
        # double-fire.
        async with self.flow.session() as session:
            due_rows = (
                (
                    await session.execute(
                        select(ScheduledRun).where(
                            ScheduledRun.enabled.is_(True),
                            ScheduledRun.next_fire_at <= now,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in due_rows:
                # Compute the next fire-at relative to "now" so we
                # don't backlog if the leader fell behind.
                row.next_fire_at = croniter(row.cron, now).get_next(datetime)
                row.last_fired_at = now
            await session.commit()

        # Submit a run for each. Done outside the session to keep the
        # claim transaction tight.
        for row in due_rows:
            try:
                await self.flow.run(
                    row.workflow_name,
                    version=row.workflow_version,
                    state={},
                    triggered_by="schedule",
                )
            except Exception:
                logger.exception(
                    "scheduled run submit failed for %s v%d",
                    row.workflow_name,
                    row.workflow_version,
                )

    async def _revive_runs_with_pending_steps(self) -> None:
        """A run can be ``running`` while one of its steps is
        ``pending`` (post-reclaim or post-retry). Flip such runs back
        to ``pending`` so the next claim_pending_runs picks them up;
        replay re-invokes the workflow function with cached step
        outputs returning for the completed prefix and the pending
        step actually running this time."""
        from sqlalchemy import select, update

        ready_step_runs = (
            select(StepRun.run_id)
            .where(
                StepRun.status == "pending",
                sa.or_(
                    StepRun.next_attempt_at.is_(None),
                    StepRun.next_attempt_at <= sa.func.now(),
                ),
            )
            .scalar_subquery()
        )
        async with self.flow.session() as session:
            await session.execute(
                update(Run)
                .where(
                    Run.status == "running",
                    Run.id.in_(ready_step_runs),
                )
                .values(status="pending")
            )
            await session.commit()

    async def _drain_tasks(self, timeout: float = 30.0) -> None:
        """Wait for in-flight run tasks to complete, with a timeout."""
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning(
                "forktex_flow.driver: %d tasks still in flight at shutdown",
                len(self._tasks),
            )
