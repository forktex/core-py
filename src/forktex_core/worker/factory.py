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

"""The ``[worker]`` surface: a queue consumer any host can own.

The unit is :class:`Worker` — an object with an async lifecycle and an
awaitable ``run()``. Everything else here is a host wrapper around it:

- :func:`run_worker` — the standalone entrypoint. Owns the process: it owns
  ``asyncio.run`` and lets arq install the signal handlers.
- :func:`background` — an async context manager for a host that already owns
  the loop and the signals (an API's lifespan, a test). Runs the consumer as a
  task and cancels it with a bounded drain on exit.
- :func:`run_worker_pool` — process-level fan-out, for CPU-bound tasks that a
  single event loop cannot parallelise.

The split exists because the previous single ``run_worker`` owned
``asyncio.run`` *and* let arq claim SIGTERM/SIGINT, so it could only ever be a
process entrypoint — an API that wanted to consume its own queue in-process had
no way in. Signal ownership is a property of the host, not of the config, so it
is a :class:`Worker` constructor argument rather than a ``WorkerConfig`` field.
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from forktex_core import queue as _queue
from forktex_core.log import get_logger

if TYPE_CHECKING:
    from arq.worker import Worker as ArqWorker

logger = get_logger(__name__)


StartupHook = Callable[[], Awaitable[None]]
"""Coroutine fired before the arq consumer starts. Use to register
storage / vector clients, init flow drivers, warm caches, etc.

Hooks run in declared order. A raising hook aborts startup —
the worker doesn't proceed to consume jobs unless every hook
completes successfully."""

#: How long :func:`background` and :func:`run_worker_pool` wait for in-flight
#: work to finish before giving up on a graceful stop.
DEFAULT_DRAIN_TIMEOUT = 30.0


class WorkerConfig(BaseModel):
    """Inputs to :class:`Worker` and the host wrappers."""

    redis_url: str
    queue_name: str = "default"
    max_jobs: int = 10
    job_timeout: int = 300
    startup_hooks: list[StartupHook] = Field(default_factory=list)


class Worker:
    """A queue consumer with an explicit lifecycle.

    Use as an async context manager, then await :meth:`run`::

        async with Worker(config, handle_signals=False) as worker:
            await worker.run()

    ``__aenter__`` runs the startup hooks and initialises the queue pool;
    ``__aexit__`` closes the arq worker (draining in-flight jobs) and the pool.
    Entering is what makes the consumer usable — :meth:`run` on an unstarted
    worker raises rather than silently consuming with half-wired dependencies.
    """

    def __init__(self, config: WorkerConfig, *, handle_signals: bool = True) -> None:
        self._config = config
        self._handle_signals = handle_signals
        self._arq: ArqWorker | None = None
        self._started = False

    @property
    def config(self) -> WorkerConfig:
        return self._config

    @property
    def arq_worker(self) -> ArqWorker:
        """The underlying arq worker. Available only after :meth:`start`."""
        if self._arq is None:
            raise RuntimeError("Worker.start() has not run — no arq worker yet")
        return self._arq

    async def start(self) -> None:
        """Run startup hooks, initialise the queue pool, build the arq worker."""
        if self._started:
            return
        # Hooks first, so anything they register (storage clients, flow drivers,
        # embedding models) is ready before a single job is consumed.
        for hook in self._config.startup_hooks:
            await hook()

        # The module-level pool lets a running @task call `enqueue()` to chain
        # work. Fail-fast is the contract: if this cannot reach Redis, `enqueue`
        # from inside a task would quietly drop chained work, which is worse
        # than refusing to start.
        try:
            await _queue.init(self._config.redis_url)
        except Exception:
            logger.exception("worker: queue.init failed — aborting startup")
            raise

        self._arq = _queue.make_worker(
            redis_url=self._config.redis_url,
            queue_name=self._config.queue_name,
            max_jobs=self._config.max_jobs,
            job_timeout=self._config.job_timeout,
            handle_signals=self._handle_signals,
        )
        self._started = True
        logger.info(
            "worker: ready",
            extra={
                "queue": self._config.queue_name,
                "max_jobs": self._config.max_jobs,
                "handle_signals": self._handle_signals,
            },
        )

    async def run(self) -> None:
        """Consume jobs until stopped, cancelled, or (if it owns them) signalled."""
        if not self._started:
            raise RuntimeError("Worker.run() before start() — use `async with Worker(...)`")
        logger.info("worker: consuming", extra={"queue": self._config.queue_name})
        await self.arq_worker.async_run()

    async def stop(self) -> None:
        """Ask the consumer to finish in-flight jobs and return from :meth:`run`.

        Safe to call from a signal handler or another task. Nothing happens if
        the worker never started.
        """
        if self._arq is None:
            return
        logger.info("worker: stopping", extra={"queue": self._config.queue_name})
        # arq's own drain path. `SIGUSR1` is what it uses internally to mean
        # "stop after in-flight jobs", and it is not a real signal delivery —
        # `handle_sig` is a plain method call here.
        self._arq.handle_sig(signal.SIGUSR1)

    async def aclose(self) -> None:
        """Close the arq worker and the queue pool. Idempotent."""
        if self._arq is not None:
            with contextlib.suppress(Exception):
                await self._arq.close()
            self._arq = None
        with contextlib.suppress(Exception):
            await _queue.close()
        self._started = False

    async def __aenter__(self) -> Worker:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


def create_worker(config: WorkerConfig) -> ArqWorker:
    """Build a configured ``arq.Worker`` ready to consume jobs.

    Returns the bare arq worker with no lifecycle wiring — no startup hooks and
    no queue pool. Prefer :class:`Worker` (or :func:`run_worker`), which own
    both; this stays for callers handing a worker to arq's own CLI.
    """
    return _queue.make_worker(
        redis_url=config.redis_url,
        queue_name=config.queue_name,
        max_jobs=config.max_jobs,
        job_timeout=config.job_timeout,
    )


async def _bootstrap(config: WorkerConfig) -> None:
    """Async portion of :func:`run_worker` — a pure coroutine, for testability."""
    async with Worker(config, handle_signals=True) as worker:
        await worker.run()


def run_worker(config: WorkerConfig) -> None:
    """One-shot bootstrap: hooks → queue init → consume, until signalled.

    Owns the process: it calls ``asyncio.run`` and lets arq install the
    SIGTERM/SIGINT drain. Use from a worker entrypoint script::

        # myservice/worker.py
        from forktex_core.worker import WorkerConfig, run_worker
        from myservice import tasks  # noqa  side-effect: register @task

        if __name__ == "__main__":
            run_worker(WorkerConfig(redis_url="redis://localhost:6379/1"))

    Inside a host that already owns the loop, use :func:`background` instead.
    """
    asyncio.run(_bootstrap(config))


@contextlib.asynccontextmanager
async def background(
    config: WorkerConfig,
    *,
    drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
) -> AsyncIterator[Worker]:
    """Run the consumer alongside a host that owns the loop and the signals.

    Yields the started :class:`Worker` while it consumes in a background task,
    then asks it to drain and waits up to ``drain_timeout`` before cancelling.
    The host keeps its own signal handling — arq's are not installed.

    In a FastAPI lifespan::

        @asynccontextmanager
        async def lifespan(app):
            async with background(WorkerConfig(redis_url=...)):
                yield

    A worker task that dies on its own does not take the host down silently:
    the exception is logged and re-raised on exit.
    """
    worker = Worker(config, handle_signals=False)
    await worker.start()
    task = asyncio.create_task(worker.run(), name=f"forktex-worker:{config.queue_name}")
    try:
        yield worker
    finally:
        await worker.stop()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=drain_timeout)
        except TimeoutError:
            logger.warning(
                "worker: drain timed out — cancelling in-flight jobs",
                extra={"queue": config.queue_name, "drain_timeout": drain_timeout},
            )
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        except asyncio.CancelledError:  # pragma: no cover - host shutting down
            raise
        except Exception:
            logger.exception("worker: consumer task failed", extra={"queue": config.queue_name})
            raise
        finally:
            await worker.aclose()


def run_worker_pool(config: WorkerConfig, *, processes: int = 1) -> None:
    """Run ``processes`` independent workers, one per OS process.

    A single :func:`run_worker` is one event loop, so it parallelises *waiting*
    but not computing — CPU-bound tasks serialise behind the GIL no matter how
    high ``max_jobs`` goes. This is the escape hatch: each child is a full
    ``run_worker``, so they share nothing but the queue.

    The parent is a supervisor only. It forwards SIGTERM/SIGINT to the children
    and waits for them to drain; it does not restart them, because a crash-loop
    is the process manager's job to see and to back off from.
    """
    if processes < 1:
        raise ValueError(f"processes must be >= 1, got {processes}")
    if processes == 1:
        # No supervisor for a pool of one: an extra process would only add a
        # signal hop between the manager and the worker.
        run_worker(config)
        return

    # "spawn" rather than the Linux default "fork": the parent may already hold
    # an event loop, Redis sockets or a database pool, none of which survive a
    # fork intact.
    ctx = multiprocessing.get_context("spawn")
    children = [ctx.Process(target=run_worker, args=(config,), name=f"forktex-worker-{i}") for i in range(processes)]
    for child in children:
        child.start()
    logger.info(
        "worker: pool started",
        extra={"queue": config.queue_name, "processes": processes, "pids": [c.pid for c in children]},
    )

    stopping = False

    def _forward(signum: int, _frame: object) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        logger.info("worker: pool stopping", extra={"signal": signal.Signals(signum).name})
        for child in children:
            if child.pid is not None and child.is_alive():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child.pid, signal.SIGTERM)

    previous = {sig: signal.signal(sig, _forward) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for child in children:
            child.join()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    exit_codes = {child.name: child.exitcode for child in children}
    logger.info("worker: pool stopped", extra={"queue": config.queue_name, "exit_codes": exit_codes})


__all__ = [
    "DEFAULT_DRAIN_TIMEOUT",
    "StartupHook",
    "Worker",
    "WorkerConfig",
    "background",
    "create_worker",
    "run_worker",
    "run_worker_pool",
]
