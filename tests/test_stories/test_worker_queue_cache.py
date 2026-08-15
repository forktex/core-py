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

"""STORY: an arq task uses ``@cached`` for a hot read; the cache survives
job replays and invalidates when the underlying value changes.

Cross-module story for ``[queue]`` + ``[worker]`` + ``[cache]``.
Real Redis (testcontainer). The "hot read" is simulated by a counter:
each direct call to the inner function increments it, so a cache hit
keeps the counter stable across calls.

  Act 1. Initialise the cache + queue against the testcontainer Redis.
         Register a ``@cached`` fetch + a ``@task`` that calls it.
  Act 2. Enqueue the task; run the arq worker in burst mode; assert
         the task ran (job exists in the registry, side effect visible).
  Act 3. Call the cached fetch twice with the same key — the
         underlying counter advances exactly once (cache hit on the
         second call).
  Act 4. Invalidate via ``cache.delete``; call again; underlying counter
         advances (cache miss after invalidation).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from pydantic import BaseModel

pytest.importorskip("arq", reason="arq not installed")

import forktex_core.cache as cache
import forktex_core.queue as q
from forktex_core.cache import cached
from forktex_core.queue import JobCtx, enqueue, make_worker, task


class WQCState(BaseModel):
    """In-flight state across the four acts."""

    namespace: str = ""
    fetch_calls: int = 0
    task_ran: bool = False
    last_value: str | None = None


# Module-level task + cached fetch — arq registers via decorator at import.
# We close over a single ``WQCState`` instance via the ``state`` fixture
# so the counter is observable from the test methods.

_STATE: WQCState = WQCState()


def _user_cache_key(user_id: str) -> str:
    """Stable, knowable key so the story can invalidate cleanly."""
    return f"story.wqc.user:{user_id}"


@cached(ttl=60, key_builder=_user_cache_key)
async def _cached_user_fetch(user_id: str) -> dict:
    """Stand-in for an expensive read. Each direct call advances the
    counter; cache hits never get here."""
    _STATE.fetch_calls += 1
    return {"id": user_id, "name": f"User-{user_id[:6]}"}


@task
async def _hydrate_user(ctx: JobCtx, user_id: str) -> dict:
    """arq task: hydrate a user via the cached fetch."""
    result = await _cached_user_fetch(user_id)
    _STATE.task_ran = True
    _STATE.last_value = result["name"]
    return result


class TestWorkerQueueCache:
    """Worker + Queue + Cache integration as one consumer journey."""

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def state(self, redis_url: str):
        await cache.init(redis_url)
        await q.init(redis_url)

        _STATE.namespace = f"story-wqc-{uuid.uuid4().hex[:6]}"
        _STATE.fetch_calls = 0
        _STATE.task_ran = False
        _STATE.last_value = None

        yield _STATE

        try:
            await cache.invalidate_prefix("story.wqc.user:")
        except Exception:
            pass
        # Close the cached queue pool inside this fixture's own event loop
        # (loop_scope="class") — leaving it open lets a later test file's
        # queue.init() try to close a pool tied to an already-closed loop.
        await q.close()
        await cache.close()

    # ── Act 1 ────────────────────────────────────────────────────────

    @pytest.mark.asyncio(loop_scope="class")
    async def test_act1_register_cached_task(self, state: WQCState):
        assert "_cached_user_fetch" not in q._registry
        assert "_hydrate_user" in q._registry, "story task should be registered at module import"
        # Cache is initialized and reachable
        assert cache.available()

    # ── Act 2 ────────────────────────────────────────────────────────

    @pytest.mark.asyncio(loop_scope="class")
    async def test_act2_worker_consumes_enqueued_task(self, state: WQCState, redis_url: str):
        assert not state.task_ran

        user_id = uuid.uuid4().hex
        await enqueue(_hydrate_user, user_id)

        # Burst-mode worker: consume queued jobs and exit when the queue
        # is empty (or when ``max_jobs`` reached). The arq Worker emits
        # SystemExit on graceful shutdown.
        worker = make_worker(redis_url, max_jobs=1)
        worker.poll_delay = 0.05
        try:
            await asyncio.wait_for(worker.async_run(), timeout=10)
        except asyncio.TimeoutError, SystemExit:
            pass
        finally:
            try:
                await worker.close()
            except Exception:
                pass

        assert state.task_ran, "task did not execute against the burst-mode worker"
        assert state.last_value is not None
        assert state.last_value.startswith("User-")
        # The task ran exactly once → cache miss → counter advanced.
        assert state.fetch_calls == 1, f"expected exactly one underlying fetch from the task, got {state.fetch_calls}"

    # ── Act 3 ────────────────────────────────────────────────────────

    @pytest.mark.asyncio(loop_scope="class")
    async def test_act3_repeat_call_hits_cache(self, state: WQCState):
        baseline = state.fetch_calls
        # The cache key is keyed on call args (default behaviour). Call
        # with the SAME id so the cache hits on the second invocation.
        same_id = "deadbeef"
        first = await _cached_user_fetch(same_id)
        second = await _cached_user_fetch(same_id)
        assert first == second
        # Exactly one underlying invocation; the second was a cache hit.
        assert state.fetch_calls == baseline + 1, (
            f"second call should have hit the cache; counter went {baseline} → {state.fetch_calls}"
        )

    # ── Act 4 ────────────────────────────────────────────────────────

    @pytest.mark.asyncio(loop_scope="class")
    async def test_act4_invalidate_then_miss(self, state: WQCState):
        baseline = state.fetch_calls
        # Invalidate via the exact key the key_builder produced for act 3.
        await cache.delete(_user_cache_key("deadbeef"))
        # Same id as act 3 — but the cache is now cold, so the underlying
        # fetch should run again.
        await _cached_user_fetch("deadbeef")
        assert state.fetch_calls == baseline + 1, (
            f"after delete, call should have missed the cache; counter went {baseline} → {state.fetch_calls}"
        )
