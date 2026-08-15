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

"""Top-level session-scoped testcontainer fixtures for the full core-py suite.

All containers are started once per pytest session and shared across test
modules to avoid Docker pull + startup overhead on every file. Per-test
isolation is achieved at the schema/key/collection level, not the container.

Container bring-up lives in :mod:`tests._containers` so the example
sandbox (``scripts/run_examples.py``) can boot the same set without
duplicating logic. Pytest fixtures here are thin wrappers.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.engine import URL

from tests._containers import (
    ensure_minio_bucket,
    start_minio,
    start_mongo,
    start_postgres,
    start_qdrant,
    start_redis,
)


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[URL]:
    """Session-scoped Postgres testcontainer, shared by every DB-backed suite.

    A real Postgres, always: these suites exercise JSONB operators,
    ``FOR UPDATE SKIP LOCKED``, advisory locks, partial/GIN indexes,
    ``schema_translate_map`` and SQLSTATE codes. None of that can be faked, and
    none of it can run on SQLite — which is why there are no mocks here.

    One container on one version, deliberately. There used to be an opt-in
    ``GRID_EMBEDDED_PG=1`` path (an embedded server via the ``pgserver`` wheel)
    for machines without a container runtime, but it was never complete —
    ``tests/test_flow`` spun up its own container regardless — so it only ever
    half-worked, while adding a second code path and a dev dependency.
    """
    container, url = start_postgres()
    yield url
    container.stop()


@pytest.fixture(scope="session")
def postgres_url_str(postgres_url: URL) -> str:
    """A pure string rendering of the session-scoped ``postgres_url`` — no
    per-test state, so it's session-scoped too (a session-scoped fixture,
    e.g. ``tests/test_grid/conftest.py``'s ``grid_db_url``, can't depend on
    a function-scoped one)."""
    return postgres_url.render_as_string(hide_password=False)


@pytest_asyncio.fixture
async def fresh_schema(postgres_url_str: str) -> AsyncIterator[str]:
    """Unique schema per test, dropped on teardown."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    schema = "forktex_test_" + uuid.uuid4().hex[:12]
    yield schema
    engine = create_async_engine(postgres_url_str)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def redis_url() -> AsyncIterator[str]:
    """Session-scoped Redis 7 container."""
    container, url = start_redis()
    yield url
    container.stop()


@pytest_asyncio.fixture(scope="session")
async def minio_config() -> AsyncIterator[dict]:
    """Session-scoped MinIO container with ``test-bucket`` pre-created."""
    container, config = start_minio()
    await ensure_minio_bucket(config)
    yield config
    container.stop()


@pytest_asyncio.fixture(scope="session")
async def qdrant_url() -> AsyncIterator[str]:
    """Session-scoped Qdrant container."""
    container, url = start_qdrant()
    yield url
    container.stop()


@pytest_asyncio.fixture(scope="session")
async def mongo_url() -> AsyncIterator[str]:
    """Session-scoped MongoDB container."""
    container, url = start_mongo()
    yield url
    container.stop()
