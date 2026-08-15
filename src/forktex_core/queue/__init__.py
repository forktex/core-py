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

"""Lightweight background-job queue (arq-backed, Redis-native).

Fire-and-forget work that isn't durable — use ``flow`` for durable execution
with replay, state, and guaranteed delivery. ``queue`` is for work where
at-least-once delivery via Redis is sufficient and overhead matters.

arq chosen over celery/dramatiq: native asyncio, Redis already bundled as a
core dependency, minimal runtime overhead.

    # Define tasks (import side-effect registers them)
    @task
    async def send_email(ctx: JobCtx, to: str, subject: str) -> None:
        await smtp.send(to, subject)

    @task(queue="high-priority", timeout=30, retries=2)
    async def notify_webhook(ctx: JobCtx, url: str, payload: dict) -> None:
        await http.post(url, json=payload)

    # Init + enqueue
    await init("redis://localhost:6379/1")
    job_id = await enqueue(send_email, "user@example.com", "Welcome!")
    job_id = await enqueue_at(send_email, "user@example.com", "Reminder",
                              eta=datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc))

    # Operator visibility
    info = await inspect_job(job_id)  # {"status": "queued"|"in_progress"|..., ...}
    ok   = await cancel_job(job_id)   # True if cancelled before worker picked it up
    jobs = await list_jobs("default")  # [{job_id, status, enqueue_time, ...}, ...]
    health = await worker_health()      # {"pending": 5, "in_flight": 2, "failed": 0}

    # Worker entrypoint
    WorkerSettings = make_worker("redis://localhost:6379/1")

Requires: pip install forktex-core[queue]  (arq)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypedDict, cast

from forktex_core.error import AppError, AppErrorCode
from forktex_core.log import get_logger

if TYPE_CHECKING:
    from types import ModuleType

    from arq import ArqRedis
    from arq.worker import Worker

logger = get_logger(__name__)

_registry: dict[str, Callable] = {}
_redis_url: str | None = None
_pool: ArqRedis | None = None

# Default arq queue name; also the fallback when a function reaches enqueue
# without the ``@task`` decorator's queue marker.
DEFAULT_QUEUE = "default"

# Attribute names the @task decorator stamps onto the function. Named once
# so the decorator and the enqueue accessors stay in sync.
_QUEUE_NAME_ATTR = "_queue_name"
_JOB_TIMEOUT_ATTR = "_job_timeout"
_JOB_RETRIES_ATTR = "_job_retries"


def _task_queue_name(fn: Callable) -> str:
    """Queue a ``@task`` function was registered on, or the default.

    A bare (non-``@task``) callable yields ``DEFAULT_QUEUE`` — matching both
    the decorator's own default and arq's — so ad-hoc enqueues stay valid.
    """
    name = getattr(fn, _QUEUE_NAME_ATTR, DEFAULT_QUEUE)
    return name if isinstance(name, str) else DEFAULT_QUEUE


def _get_arq() -> ModuleType:
    try:
        import arq

        return arq
    except ImportError as exc:
        raise ImportError("Install 'forktex-core[queue]' (arq) to use forktex_core.queue") from exc


class JobCtx(TypedDict, total=False):
    """Context dict injected as first arg into every task function by arq."""

    redis: Any  # arq.ArqRedis
    job_id: str
    job_try: int
    enqueue_time: datetime
    score: float


def task(
    _fn: Callable | None = None,
    *,
    queue: str = DEFAULT_QUEUE,
    timeout: int = 300,
    retries: int = 0,
) -> Callable:
    """Register an async function as a background task.

    Can be used with or without arguments::

        @task
        async def simple_job(ctx: JobCtx, x: int) -> None: ...

        @task(queue="high-priority", timeout=60, retries=3)
        async def critical_job(ctx: JobCtx, x: int) -> None: ...

    Args:
        queue: arq queue name (default ``"default"``).
        timeout: Max seconds the job may run before being killed. Applied
            per-function by :func:`make_worker`.
        retries: Additional attempts on failure (``0`` = run once, no retry).
            Applied per-function by :func:`make_worker` as arq's
            ``max_tries = retries + 1``.

    The decorated function is unchanged — ``@task`` is a pure registration
    side-effect; :func:`make_worker` reads these back when it builds the arq
    ``Worker``.

    To control the *delay* between retries, raise ``arq.Retry(defer=seconds)``
    from the job body — arq has no per-function retry-delay setting, so a
    ``retry_delay`` argument here could not have been honoured (an earlier
    version accepted one and silently ignored it).
    """

    def decorator(fn: Callable) -> Callable:
        if fn.__name__ in _registry:
            logger.warning("task %r already registered, overwriting", fn.__name__)
        _registry[fn.__name__] = fn
        setattr(fn, _QUEUE_NAME_ATTR, queue)
        setattr(fn, _JOB_TIMEOUT_ATTR, timeout)
        setattr(fn, _JOB_RETRIES_ATTR, retries)
        return fn

    if _fn is not None:
        return decorator(_fn)
    return decorator


async def init(redis_url: str) -> None:
    """Set the Redis URL used by ``enqueue`` / ``enqueue_at`` / operator ops.

    Closes any previously cached pool first — calling ``init()`` again (e.g.
    to reconfigure) never leaks the old connection.
    """
    global _redis_url, _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
    _redis_url = redis_url


def _require_init() -> str:
    if _redis_url is None:
        raise QueueError("Queue not initialized — call await queue.init(redis_url) first")
    return _redis_url


async def _get_pool() -> ArqRedis:
    """Return the cached ``ArqRedis`` pool, creating it on first use.

    Every caller (``enqueue``, ``cancel_job``, ``inspect_job``, ...) used to
    call ``arq.create_pool()`` fresh on every invocation and never closed
    the result — an unbounded connection leak under load. One pool is now
    created lazily and reused for the module's lifetime (mirroring
    ``forktex_core.cache``'s single persistent client), until ``init()`` or
    ``close()`` resets it.
    """
    global _pool
    pool = _pool
    if pool is None:
        arq = _get_arq()
        url = _require_init()
        pool = await arq.create_pool(arq.connections.RedisSettings.from_dsn(url))
        _pool = pool
    return pool


async def close() -> None:
    """Close the cached Redis pool, if any. Idempotent."""
    global _pool, _redis_url
    if _pool is not None:
        await _pool.aclose()
        _pool = None
    _redis_url = None


async def enqueue(fn: Callable, *args: object, _queue_name: str | None = None, **kwargs: object) -> str:
    """Enqueue ``fn`` for immediate execution. Returns the arq job ID."""
    pool = await _get_pool()
    queue_name = _queue_name or _task_queue_name(fn)
    # arq's enqueue_job stub declares named types for its reserved _job_id/
    # _defer_until/_defer_by/_expires/_job_try kwargs; kwargs here is the
    # caller's arbitrary job-function arguments (deliberately object-typed —
    # arq itself distinguishes them from its own reserved names at runtime),
    # so a static splat against that stub is a structural mismatch, not a
    # real type error.
    job = await pool.enqueue_job(fn.__name__, *args, _queue_name=queue_name, **cast(Any, kwargs))
    if job is None:
        raise QueueError(f"arq rejected enqueue for {fn.__name__!r} (deduplicated or pool unhealthy)")
    return job.job_id


async def enqueue_at(
    fn: Callable,
    *args: object,
    eta: datetime,
    _queue_name: str | None = None,
    **kwargs: object,
) -> str:
    """Enqueue ``fn`` to run at ``eta`` (must be timezone-aware). Returns job ID."""
    pool = await _get_pool()
    queue_name = _queue_name or _task_queue_name(fn)
    job = await pool.enqueue_job(
        fn.__name__,
        *args,
        _defer_until=eta,
        _queue_name=queue_name,
        **cast(Any, kwargs),
    )
    if job is None:
        raise QueueError(f"arq rejected scheduled enqueue for {fn.__name__!r}")
    return job.job_id


async def inspect_job(job_id: str, *, queue_name: str = DEFAULT_QUEUE) -> dict[str, Any] | None:
    """Return status and metadata for a job, or ``None`` if not found.

    ``queue_name`` must match the queue the job was enqueued on — see
    ``cancel_job``'s docstring for why this matters: arq's ``Job`` defaults
    to its own ``"arq:queue"`` name, and a mismatch makes a genuinely
    queued/deferred job misreport as ``"not_found"`` (its ``status()``
    can't resolve a score from the wrong sorted set).

    Returns a dict with keys:
    ``status`` (``"queued"`` | ``"in_progress"`` | ``"complete"`` | ``"not_found"``),
    ``enqueue_time``, ``start_time``, ``finish_time``, ``result``, ``error``.
    """
    from arq.jobs import Job, JobResult  # arq 0.26+: Job is in arq.jobs

    pool = await _get_pool()
    job = Job(job_id, pool, _queue_name=queue_name)
    info = await job.info()
    if info is None:
        return None
    status = await job.status()
    # ``info`` is ``JobDef`` for queued/in-progress jobs; ``JobResult``
    # (a JobDef subclass) once the job has finished. Result-only fields
    # are only available on the latter.
    is_result = isinstance(info, JobResult)
    return {
        "job_id": job_id,
        "status": str(status),
        "function": info.function,
        "args": info.args,
        "kwargs": info.kwargs,
        "enqueue_time": info.enqueue_time,
        "start_time": info.start_time if is_result else None,
        "finish_time": info.finish_time if is_result else None,
        "result": info.result if is_result else None,
        "error": str(info.result) if is_result and info.success is False else None,
        "tries": info.job_try,
    }


async def cancel_job(job_id: str, *, queue_name: str = DEFAULT_QUEUE) -> bool:
    """Mark ``job_id`` for abort and wait (up to 1s) for confirmation.

    ``queue_name`` must match the queue the job was enqueued on (via
    ``enqueue(..., _queue_name=...)`` or a ``@task(queue=...)``) — arq's
    ``Job`` defaults to its own ``"arq:queue"`` name, which doesn't match
    this package's ``DEFAULT_QUEUE`` ("default"). Passing the wrong queue
    name makes ``Job.abort()``'s internal queue lookup always miss, so it
    silently resolves ``False`` no matter the job's real state.

    Returns ``True`` only if a worker was actively running the job *and*
    confirmed the cancellation within the timeout. A job that's merely
    queued/deferred with no worker currently processing it will resolve
    ``False`` — arq's ``abort()`` waits for the job's *result* to observe a
    cancellation, which requires a worker to actually pick the job up.
    ``False`` also covers "already completed" and "not found".
    """
    from arq.jobs import Job

    pool = await _get_pool()
    job = Job(job_id, pool, _queue_name=queue_name)
    try:
        # abort() returns its own True/False — a nonexistent/already-started/
        # finished job doesn't raise, it just resolves False. TimeoutError
        # (a real abort attempt that didn't get confirmed in time) also
        # counts as "not cancelled"; other errors (e.g. a dead Redis pool)
        # are real and must surface.
        return await job.abort(timeout=1)
    except TimeoutError:
        return False


async def list_jobs(
    queue_name: str = DEFAULT_QUEUE,
    *,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List jobs still waiting in ``queue_name`` (queued or deferred),
    with an optional status filter.

    Args:
        queue_name: arq queue name to inspect.
        status: Filter by status — typically ``"queued"`` or ``"deferred"``
                for jobs this function can see (``None`` returns both).

    Returns a list of dicts in the same format as ``inspect_job``.

    arq's actual queue structure is a Redis sorted set literally named
    ``queue_name`` (job IDs as members, score = due time) — this reads that
    directly, so results are genuinely scoped to this queue. Once a worker
    claims a job it's removed from this sorted set (arq itself doesn't
    track "which queue" a job came from after that point), so in-progress
    and completed jobs aren't visible here — use ``inspect_job(job_id)`` for
    a specific job you already have the ID for, regardless of its status.
    """
    url = _require_init()
    await _get_pool()  # validates Redis is reachable

    import redis.asyncio as redis_lib

    _MAX_LIST_JOBS = 10_000
    r = redis_lib.from_url(url)
    try:
        raw_ids = await r.zrange(queue_name, 0, _MAX_LIST_JOBS - 1)
        if len(raw_ids) >= _MAX_LIST_JOBS:
            logger.warning(
                "list_jobs: truncated at %d jobs — use status filter to narrow results",
                _MAX_LIST_JOBS,
            )
    finally:
        await r.aclose()

    job_ids = [jid if isinstance(jid, str) else jid.decode() for jid in raw_ids]

    jobs = []
    for jid in job_ids:
        info = await inspect_job(jid)
        if info and (status is None or info.get("status") == status):
            jobs.append(info)
    return jobs


async def worker_health(redis_url: str | None = None, *, queue_name: str = DEFAULT_QUEUE) -> dict[str, int]:
    """Return a snapshot of queue depth across all statuses.

    Args:
        redis_url: Override the global Redis URL. Defaults to the initialized URL.
        queue_name: Which queue to measure. Must match the queue the jobs were
            enqueued on (``enqueue(..., _queue_name=...)`` / ``@task(queue=...)``)
            and that the worker serves (``make_worker(queue_name=...)``).

    Returns ``{"pending": int, "in_flight": int, "failed": int}``.

    ``pending`` and ``in_flight`` are read straight from Redis and are always
    accurate. ``failed`` is a **cumulative** count published by a running
    worker in arq's own health-check key (the same source ``arq --check``
    reads); it is ``0`` when no worker has recorded health yet, since Redis
    holds no per-job status that could be counted without deserialising
    every stored result.
    """
    url = redis_url or _require_init()
    import redis.asyncio as redis_lib
    from arq.constants import health_check_key_suffix, in_progress_key_prefix

    # redis-py's async ``Redis`` is dual-typed (sync + async stubs share methods);
    # pyright picks the sync overload and flags ``await r.zcard(...)``. Cast to
    # ``Any`` so the async overloads resolve correctly at the call sites below.
    r: Any = redis_lib.from_url(url)
    try:
        # arq uses ``queue_name`` *directly* as the Redis key and stores the
        # queue as a sorted set. The previous implementation called ``llen``
        # on a hardcoded ``"arq:queue:default"``: wrong key, and wrong type
        # (``llen`` against a zset raises WRONGTYPE, and returned 0 whenever
        # the key happened not to exist).
        pending = int(await r.zcard(queue_name) or 0)
        in_flight = len(await r.keys(f"{in_progress_key_prefix}*") or [])
        failed = await _failed_from_health_key(r, queue_name, health_check_key_suffix)
        return {"pending": pending, "in_flight": in_flight, "failed": failed}
    finally:
        await r.aclose()


async def _failed_from_health_key(r: Any, queue_name: str, suffix: str) -> int:  # noqa: ANN401
    """Parse ``j_failed`` out of arq's health-check string, or 0 if absent.

    arq writes ``"<ts> j_complete=N j_failed=N j_retried=N j_ongoing=N
    queued=N"`` to ``{queue_name}:health-check`` with a TTL, so the key is
    only present while a worker is alive and recording.
    """
    raw = await r.get(f"{queue_name}{suffix}")
    if not raw:
        return 0
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    for field in text.split():
        key, _, value = field.partition("=")
        if key == "j_failed":
            try:
                return int(value)
            except ValueError:  # pragma: no cover — arq always writes an int
                return 0
    return 0


def make_worker(
    redis_url: str,
    *,
    queue_name: str = DEFAULT_QUEUE,
    max_jobs: int = 10,
    job_timeout: int = 300,
    handle_signals: bool = True,
) -> Worker:
    """Build an arq ``Worker`` configured with all ``@task`` functions.

    Returns an arq ``Worker`` instance. Pass it to ``arq.run_worker()`` or
    call ``.run()`` directly for programmatic use. ``allow_abort_jobs=True``
    is set so ``cancel_job()`` can actually cancel a job this worker has
    already picked up (arq defaults this to ``False`` — without it, an
    abort signal is never checked and ``cancel_job()`` can only ever return
    ``False``, no matter the job's state).

    Usage in a worker entrypoint module::

        # my_service/worker.py
        import forktex_core.queue as q
        from my_service import tasks   # side-effect: registers @task functions

        # For arq CLI: expose a Worker class (not instance)
        # Run: arq my_service.worker.WorkerClass
        WorkerClass = q.make_worker("redis://localhost:6379/1")

    Or run programmatically::

        settings = q.make_worker("redis://localhost:6379/1")
        await settings.async_run()

    To embed the consumer in a host that already handles signals (a FastAPI
    lifespan, a process supervisor), pass ``handle_signals=False`` — or use
    :mod:`forktex_core.worker`, which does that for you.
    """
    arq = _get_arq()
    return arq.Worker(
        functions=[_as_arq_function(fn) for fn in _registry.values()],
        redis_settings=arq.connections.RedisSettings.from_dsn(redis_url),
        queue_name=queue_name,
        max_jobs=max_jobs,
        job_timeout=job_timeout,
        allow_abort_jobs=True,
        # Signal ownership belongs to whoever owns the process. Left on for a
        # standalone worker; a host that embeds the consumer (an API's lifespan,
        # a supervisor's child) must turn it off or arq's handlers fight the
        # host's own shutdown path.
        handle_signals=handle_signals,
    )


def _as_arq_function(fn: Callable) -> object:
    """Wrap a registered task so its ``@task`` settings actually reach arq.

    ``@task(timeout=…, retries=…)`` stamps the function; without this wrapper
    those values were stamped and never read, so per-task timeouts and retries
    silently did nothing and every job inherited the worker-wide defaults.

    arq's ``max_tries`` counts *total* attempts, so ``retries`` (additional
    attempts) maps to ``retries + 1``.
    """
    from arq.worker import func

    timeout = getattr(fn, _JOB_TIMEOUT_ATTR, None)
    retries = getattr(fn, _JOB_RETRIES_ATTR, None)
    return func(
        fn,
        name=fn.__name__,
        timeout=timeout,
        max_tries=None if retries is None else retries + 1,
    )


class QueueError(AppError, RuntimeError):
    """Raised on queue configuration or connection errors.

    Inherits from **both** bases deliberately. ``AppError`` gives it a
    ``code``/``details`` and makes it renderable by any transport that
    already handles ``AppError`` (e.g. ``forktex_core.api``'s envelope
    middleware) instead of surfacing as a masked 500. ``RuntimeError`` is
    retained so pre-existing ``except RuntimeError`` call sites keep
    working unchanged — both derive from ``Exception``, so the MRO is
    consistent and neither guarantee is lost.
    """

    code = AppErrorCode.INTERNAL


__all__ = [
    "JobCtx",
    "QueueError",
    "cancel_job",
    "close",
    "enqueue",
    "enqueue_at",
    "init",
    "inspect_job",
    "list_jobs",
    "make_worker",
    "task",
    "worker_health",
]
