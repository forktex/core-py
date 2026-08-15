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

"""Reads and writes on ``forktex_flow.step_run`` — the durable step-attempt table.

A step row is the unit replay keys off: `upsert_pending_step` is the idempotence boundary
(same run + qualname + args hash returns the cached outcome), and the `mark_step_*`
transitions record what happened. Stale-attempt reclaim compares against the **database**
clock, never the worker's, so a skewed worker cannot reclaim live work.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from forktex_core.flow.persist.models import StepRun
from forktex_core.log import get_logger
from forktex_core.types import JsonValue

if TYPE_CHECKING:
    from forktex_core.flow.flow import Flow

logger = get_logger(__name__)


async def upsert_pending_step(
    flow: Flow,
    *,
    run_id: UUID,
    step_name: str,
    step_qualname: str,
    step_index: int,
    args_hash: str,
    max_attempts: int,
) -> tuple[UUID, str, Any]:
    """Upsert a (run, step_qualname, args_hash) row.

    Returns ``(step_id, status, output)``. Cache hit on a previously
    completed step lets the workflow function replay without re-running
    the body.
    """
    async with flow.session() as session:
        existing = (
            await session.execute(
                select(StepRun).where(
                    StepRun.run_id == run_id,
                    StepRun.step_qualname == step_qualname,
                    StepRun.args_hash == args_hash,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id, existing.status, existing.output

        step_id = uuid7()
        session.add(
            StepRun(
                id=step_id,
                run_id=run_id,
                step_name=step_name,
                step_qualname=step_qualname,
                step_index=step_index,
                args_hash=args_hash,
                status="pending",
                max_attempts=max_attempts,
                attempts=0,
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            # Lost the race against another worker / replay; refetch.
            await session.rollback()
            existing = (
                await session.execute(
                    select(StepRun).where(
                        StepRun.run_id == run_id,
                        StepRun.step_qualname == step_qualname,
                        StepRun.args_hash == args_hash,
                    )
                )
            ).scalar_one()
            return existing.id, existing.status, existing.output
        return step_id, "pending", None


async def mark_step_running(flow: Flow, step_id: UUID) -> None:
    async with flow.session() as session:
        await session.execute(
            update(StepRun)
            .where(StepRun.id == step_id)
            .values(
                status="running",
                attempts=StepRun.attempts + 1,
                started_at=sa.func.coalesce(StepRun.started_at, sa.func.now()),
                heartbeat_at=sa.func.now(),
            )
        )
        await session.commit()


async def heartbeat_step(flow: Flow, step_id: UUID) -> None:
    async with flow.session() as session:
        await session.execute(update(StepRun).where(StepRun.id == step_id).values(heartbeat_at=sa.func.now()))
        await session.commit()


async def mark_step_completed(flow: Flow, step_id: UUID, output: JsonValue) -> None:
    async with flow.session() as session:
        await session.execute(
            update(StepRun)
            .where(StepRun.id == step_id)
            .values(
                status="completed",
                output=output,
                finished_at=sa.func.now(),
            )
        )
        await session.commit()


async def mark_step_failed(
    flow: Flow,
    step_id: UUID,
    error: str,
    *,
    final: bool,
    retry_in_seconds: float | None = None,
) -> datetime | None:
    """Mark a step failed; when not ``final``, schedule its retry.

    ``retry_in_seconds`` is resolved against the **database** clock
    (``now() + interval``) rather than the app's, because the driver selects
    due retries with ``next_attempt_at <= sa.func.now()`` — also the DB clock.
    Computing the deadline app-side put two clocks on either side of that
    comparison, so retries fired early or late by whatever the app↔Postgres
    skew happened to be.

    Returns the stored ``next_attempt_at`` (as the database computed it) so
    callers can report the real value instead of an app-side estimate.
    """
    from datetime import timedelta

    new_status = "failed" if final else "pending"
    values: dict[str, Any] = {
        "status": new_status,
        "error": error[:5000],
        "next_attempt_at": (None if retry_in_seconds is None else sa.func.now() + timedelta(seconds=retry_in_seconds)),
    }
    if final:
        values["finished_at"] = sa.func.now()
    async with flow.session() as session:
        result = await session.execute(
            update(StepRun).where(StepRun.id == step_id).values(**values).returning(StepRun.next_attempt_at)
        )
        stored = result.scalar_one_or_none()
        await session.commit()
        return stored


async def mark_step_skipped(flow: Flow, step_id: UUID) -> None:
    """Mark a step_run as 'skipped' (when_fn returned False).
    Uses 'completed' status with output={'__skipped__': True} since
    the step_run table's CHECK constraint only allows the 5 existing statuses.
    """
    async with flow.session() as session:
        await session.execute(
            update(StepRun)
            .where(StepRun.id == step_id)
            .values(
                status="completed",
                output={"__skipped__": True},
                finished_at=sa.func.now(),
            )
        )
        await session.commit()


async def reclaim_stale_steps(flow: Flow) -> int:
    """Reset any ``running`` step_run rows whose heartbeat is older
    than ``stale_threshold`` to ``pending``.

    The cutoff is computed on the **database** clock, not the app's.
    ``heartbeat_at`` is written with ``sa.func.now()`` (see
    :func:`mark_step_running` / :func:`heartbeat_step`), so comparing it
    against a Python-side ``iso.now()`` cutoff put two different clocks on
    either side of the comparison: with the app clock even slightly ahead of
    Postgres, a *live* step could be reclaimed to ``pending`` and executed a
    second time concurrently.
    """
    from datetime import timedelta

    cutoff = sa.func.now() - timedelta(seconds=flow.stale_threshold)
    suffix = sa.literal("\n[reclaimed: heartbeat stale]")
    async with flow.session() as session:
        result = await session.execute(
            update(StepRun)
            .where(
                StepRun.status == "running",
                StepRun.heartbeat_at < cutoff,
            )
            .values(
                status="pending",
                error=sa.func.coalesce(StepRun.error, "") + suffix,
            )
        )
        await session.commit()
        # Result.rowcount on UPDATE is supported on every backend the
        # library targets; pyright's stub doesn't expose it on the base
        # Result type, so this getattr fallback keeps the call typed.
        return getattr(result, "rowcount", 0) or 0
