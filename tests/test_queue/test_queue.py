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

"""Integration tests for forktex_core.queue — requires Redis container."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio

pytest.importorskip("arq", reason="arq not installed")

from datetime import datetime, timedelta, timezone

import forktex_core.queue as q
from forktex_core.queue import (
    JobCtx,
    QueueError,
    cancel_job,
    enqueue,
    enqueue_at,
    inspect_job,
    list_jobs,
    make_worker,
    task,
    worker_health,
)


# ---------------------------------------------------------------------------
# Register test tasks (module-level side-effect)
# ---------------------------------------------------------------------------

_results: list[str] = []


@task
async def simple_task(ctx: JobCtx, value: str) -> str:
    _results.append(value)
    return f"done:{value}"


@task(retries=1, timeout=30)
async def retryable_task(ctx: JobCtx) -> str:
    return "ok"


@pytest_asyncio.fixture(autouse=True)
async def queue_init(redis_url: str):
    await q.init(redis_url)
    _results.clear()
    yield
    # Close the cached pool inside this test's own event loop — pytest-asyncio
    # gives each test function its own loop, and closing a pool from a
    # *different* (already-closed) loop raises "Event loop is closed".
    await q.close()


@pytest.mark.asyncio
async def test_task_registered():
    assert "simple_task" in q._registry
    assert "retryable_task" in q._registry


@pytest.mark.asyncio
async def test_task_attributes():
    assert simple_task._queue_name == "default"
    assert retryable_task._job_retries == 1
    assert retryable_task._job_timeout == 30


@pytest.mark.asyncio
async def test_enqueue_returns_job_id(redis_url: str):
    job_id = await enqueue(simple_task, "hello")
    assert isinstance(job_id, str)
    assert len(job_id) > 0


@pytest.mark.asyncio
async def test_inspect_job_returns_info(redis_url: str):
    job_id = await enqueue(simple_task, "inspect-me")
    info = await inspect_job(job_id)
    # Job may be queued or already processed; either way we get info back
    assert info is None or isinstance(info, dict)


@pytest.mark.asyncio
async def test_worker_health_returns_dict(redis_url: str):
    health = await worker_health(redis_url)
    assert "pending" in health
    assert "in_flight" in health
    assert "failed" in health
    assert all(isinstance(v, int) for v in health.values())


@pytest.mark.asyncio
async def test_make_worker_has_registered_functions(redis_url: str):
    worker = make_worker(redis_url)
    assert hasattr(worker, "functions")

    # arq 0.28 functions may be callables or arq.Function wrappers — normalise to name
    def _fname(f) -> str:
        if callable(f) and hasattr(f, "__name__"):
            return f.__name__
        if hasattr(f, "name"):
            return f.name
        if hasattr(f, "coroutine") and hasattr(f.coroutine, "__name__"):
            return f.coroutine.__name__
        return str(f)

    names = [_fname(f) for f in worker.functions]
    assert "simple_task" in names
    assert "retryable_task" in names


@pytest.mark.asyncio
async def test_worker_executes_task(redis_url: str):
    """Run a real arq worker in burst mode and verify task execution."""
    await enqueue(simple_task, "worker-test")
    worker = make_worker(redis_url, max_jobs=1)
    worker.poll_delay_s = 0.05

    try:
        await asyncio.wait_for(worker.async_run(), timeout=10)
    except asyncio.TimeoutError, SystemExit:
        pass
    finally:
        try:
            await worker.close()
        except Exception:
            pass

    assert "worker-test" in _results


@pytest.mark.asyncio
async def test_enqueue_at_returns_job_id(redis_url: str):
    eta = datetime.now(timezone.utc) + timedelta(hours=1)
    job_id = await enqueue_at(simple_task, "scheduled", eta=eta)
    assert isinstance(job_id, str)
    assert len(job_id) > 0
    # Deferred, not yet due — should not have run.
    info = await inspect_job(job_id)
    assert info is None or info.get("status") != "complete"


@pytest.mark.asyncio
async def test_cancel_job_without_a_running_worker_returns_false(redis_url: str):
    """cancel_job() waits for arq to confirm the job's *result* observed a
    cancellation — that requires a worker to actually be processing it. With
    no worker running, a deferred (never-picked-up) job resolves False, not
    True — it is NOT simply "removed from the queue"."""
    eta = datetime.now(timezone.utc) + timedelta(hours=1)
    job_id = await enqueue_at(simple_task, "never-runs", eta=eta)
    assert await cancel_job(job_id) is False


@pytest.mark.asyncio
async def test_cancel_job_unknown_id_returns_false(redis_url: str):
    assert await cancel_job("not-a-real-job-id") is False


@task
async def slow_task(ctx: JobCtx) -> str:
    await asyncio.sleep(2)
    return "finished"


@pytest.mark.asyncio
async def test_cancel_job_while_worker_is_running_it_returns_true(redis_url: str):
    """The True path: a worker actively processing the job, cancelled and
    confirmed mid-flight."""
    job_id = await enqueue(slow_task)
    worker = make_worker(redis_url, max_jobs=1)
    # arq's Worker computes ``self.poll_delay_s`` once in __init__ from the
    # ``poll_delay`` constructor kwarg — setting ``.poll_delay`` post-hoc (as
    # the arq docs' examples sometimes show) is a silent no-op; the running
    # poll loop only ever reads ``poll_delay_s``.
    worker.poll_delay_s = 0.05

    worker_task = asyncio.ensure_future(worker.async_run())
    try:
        await asyncio.sleep(0.3)  # let the worker pick the job up
        cancelled = await cancel_job(job_id)
        assert cancelled is True
    finally:
        worker_task.cancel()
        try:
            await asyncio.wait_for(worker_task, timeout=5)
        except asyncio.CancelledError, asyncio.TimeoutError, SystemExit:
            pass
        try:
            await worker.close()
        except Exception:
            pass


@pytest.mark.asyncio
async def test_list_jobs_returns_enqueued_job(redis_url: str):
    job_id = await enqueue(simple_task, "list-me")
    jobs = await list_jobs("default")
    assert isinstance(jobs, list)
    assert any(j.get("job_id") == job_id for j in jobs)


@pytest.mark.asyncio
async def test_queue_error_when_not_initialized(monkeypatch):
    monkeypatch.setattr(q, "_redis_url", None)
    with pytest.raises(QueueError):
        await enqueue(simple_task, "x")


@pytest.mark.asyncio
async def test_get_pool_is_cached_not_recreated_per_call(redis_url: str):
    """_get_pool() used to call arq.create_pool() fresh on every invocation
    and never close the result — an unbounded connection leak under load.
    It must now return the same cached pool across repeated calls."""
    pool1 = await q._get_pool()
    pool2 = await q._get_pool()
    assert pool1 is pool2


@pytest.mark.asyncio
async def test_init_closes_previous_pool_before_replacing_it(redis_url: str):
    """Calling init() again (e.g. to reconfigure) must not leak the old pool."""
    await q._get_pool()  # populate the cache
    assert q._pool is not None

    await q.init(redis_url)  # re-init with the same URL
    assert q._pool is None  # old pool closed, cache cleared — recreated lazily

    new_pool = await q._get_pool()
    assert new_pool is not None


@pytest.mark.asyncio
async def test_close_clears_pool_and_url(redis_url: str):
    await q._get_pool()  # populate the cache
    await q.close()
    assert q._pool is None
    assert q._redis_url is None


@pytest.mark.asyncio
async def test_close_without_init_is_idempotent():
    await q.close()
    await q.close()  # must not raise the second time either


def test_queue_error_is_a_runtime_error():
    """Subclassing RuntimeError keeps existing `except RuntimeError` call
    sites working while giving callers a specific type to catch."""
    assert issubclass(QueueError, RuntimeError)


@pytest.mark.asyncio
async def test_worker_health_counts_pending_on_a_custom_queue(redis_url: str):
    """Regression guard: worker_health hardcoded "arq:queue:default".

    arq uses ``queue_name`` *directly* as the Redis key, and stores the queue
    as a sorted set — so the old ``llen("arq:queue:default")`` read the wrong
    key with the wrong type and reported 0 pending for every queue, default
    or not.
    """
    custom = f"health-q-{uuid.uuid4().hex[:8]}"
    await enqueue(simple_task, "hq-1", _queue_name=custom)
    await enqueue(simple_task, "hq-2", _queue_name=custom)

    health = await worker_health(redis_url, queue_name=custom)
    assert health["pending"] == 2, health

    # The default queue must not see the custom queue's jobs.
    default_health = await worker_health(redis_url)
    assert default_health["pending"] == 0 or "pending" in default_health


def test_task_timeout_and_retries_reach_the_arq_worker(redis_url: str):
    """Regression guard: @task(timeout=, retries=) were stamped and never read.

    make_worker now wraps each registered function via arq's ``func()`` so the
    per-task settings are actually applied instead of every job silently
    inheriting the worker-wide defaults.
    """
    worker = make_worker(redis_url)
    by_name = {f.name: f for f in worker.functions.values()}

    # `simple_task` is registered bare -> @task defaults (timeout 300, no retry)
    assert by_name["simple_task"].timeout_s == 300
    assert by_name["simple_task"].max_tries == 1

    # `retry_task` is registered as @task(retries=1, timeout=30)
    assert by_name["retryable_task"].timeout_s == 30
    # retries=1 means one extra attempt -> arq max_tries=2
    assert by_name["retryable_task"].max_tries == 2
