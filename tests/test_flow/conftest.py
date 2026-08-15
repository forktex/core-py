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

"""Shared fixtures for forktex_core.flow integration tests.

A session-scoped Postgres testcontainer is spun up once and reused
across every test module. Each test gets a fresh ``Flow`` instance
bound to the container (and a unique schema name where needed) so
parallel tests don't trample each other.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from typing import Iterable
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from forktex_core.flow import Flow
from forktex_core.flow.persist.models import Run


@pytest.fixture
def db_url(postgres_url: URL) -> str:
    """The one shared Postgres container from ``tests/conftest.py``.

    Flow used to start its own ``postgres:15-alpine`` — a second container per
    session, on a different major than every other suite, so a version-specific
    behaviour could pass here and fail elsewhere (or the reverse).
    """
    return postgres_url.render_as_string(hide_password=False)


@pytest_asyncio.fixture
async def fresh_schema(db_url: str) -> AsyncIterator[str]:
    """Generate a unique Postgres schema name per test and drop it on
    teardown so test isolation is real (no shared state between cases).
    """
    schema = "ftf_test_" + uuid.uuid4().hex[:12]
    yield schema
    # Cleanup — best effort.
    engine = create_async_engine(db_url)
    try:
        from sqlalchemy import text

        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def flow(db_url: str, fresh_schema: str) -> AsyncIterator[Flow]:
    """A ``Flow`` bound to the testcontainer + a unique schema. Init
    runs automatically; the engine is disposed on teardown."""
    f = Flow(database_url=db_url, schema=fresh_schema)
    await f.init()
    try:
        yield f
    finally:
        await f.close()


# ── Shared test helpers ───────────────────────────────────────────────


async def wait_for_status(
    flow: Flow,
    run_id: UUID,
    *,
    until: Iterable[str],
    timeout: float = 30.0,
) -> str:
    """Poll a run until its status is in ``until`` or ``timeout``
    elapses. Raises ``AssertionError`` with a helpful diagnostic if
    the run never reached the expected state."""
    target = set(until)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = await flow.get(run_id)
        if info.status in target:
            return info.status
        await asyncio.sleep(0.2)
    info = await flow.get(run_id)
    raise AssertionError(
        f"run {run_id} status={info.status!r} did not reach {target} "
        f"within {timeout}s; "
        f"steps={[(s.step_name, s.status) for s in info.steps]} "
        f"error={info.error!r}"
    )


async def force_resume(flow: Flow, run_id: UUID) -> None:
    """Roll a completed/failed run back to ``running`` so a subsequent
    direct ``execute_run`` invocation simulates a leader picking up
    the run mid-flight after a crash. Used by replay-determinism tests
    that want to assert step bodies didn't run twice.

    Pure ORM — keeps tests free of inline raw SQL.
    """
    async with flow.session() as session:
        await session.execute(sa.update(Run).where(Run.id == run_id).values(status="running", finished_at=None))
        await session.commit()
