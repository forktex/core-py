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

"""Reads and writes on ``forktex_flow.signal`` — the out-of-band coordination inbox.

`ctx.wait_signal()` polls here and consumes on receipt; `flow.send_signal()` inserts. The
consumed payload is cached on the step row, so a replay after a crash reads back the same
value rather than waiting for a second signal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa

from forktex_core.flow.persist.models import Signal
from forktex_core.flow.persist.runs import notify_run
from forktex_core.log import get_logger
from forktex_core.types import JsonValue

if TYPE_CHECKING:
    from forktex_core.flow.flow import Flow

logger = get_logger(__name__)


async def insert_signal(
    flow: Flow,
    run_id: UUID,
    signal_name: str,
    payload: JsonValue,
) -> int:
    """Persist an external signal for a run. Returns the signal's
    autoincrement id."""
    async with flow.session() as session:
        sig = Signal(run_id=run_id, signal_name=signal_name, payload=payload)
        session.add(sig)
        await session.flush()
        await notify_run(await session.connection(), flow.schema, run_id)
        await session.commit()
        return sig.id


async def consume_signal(
    flow: Flow,
    run_id: UUID,
    signal_name: str,
) -> tuple[int, Any] | None:
    """Atomically claim the oldest unconsumed signal matching
    ``(run_id, signal_name)``. Returns ``(signal_id, payload)`` or
    None if nothing is waiting.

    Returning the id lets the caller record the consumption decision
    in a step_run cache so replay re-uses the same signal even if
    later signals arrive.
    """
    async with flow.session() as session:
        # SELECT … FOR UPDATE SKIP LOCKED on the oldest unconsumed row
        # so concurrent consumers don't double-claim. Then UPDATE
        # ``consumed_at``. SQLAlchemy ORM doesn't compose this cleanly
        # for a single-row claim, so use ``select(...).with_for_update(skip_locked=True)``.
        from sqlalchemy import select as _select

        row = (
            await session.execute(
                _select(Signal)
                .where(
                    Signal.run_id == run_id,
                    Signal.signal_name == signal_name,
                    Signal.consumed_at.is_(None),
                )
                .order_by(Signal.sent_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if row is None:
            await session.rollback()
            return None
        row.consumed_at = sa.func.now()
        await session.commit()
        return row.id, row.payload


async def fetch_consumed_signal(
    flow: Flow,
    signal_id: int,
) -> JsonValue:
    """Read back a previously-consumed signal's payload by id. Used
    by the replay path: when a workflow function re-runs after a
    crash, ``wait_signal`` looks up the cached step_run for its call
    site, finds the recorded ``signal_id``, and returns the same
    payload deterministically."""
    async with flow.session() as session:
        from sqlalchemy import select as _select

        row = (await session.execute(_select(Signal).where(Signal.id == signal_id))).scalar_one_or_none()
        return row.payload if row is not None else None
