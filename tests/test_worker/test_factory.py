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

"""Tests for forktex_core.worker: the `Worker` lifecycle and its three hosts.

The package used to expose only `run_worker`, which owned `asyncio.run` *and*
let arq claim SIGTERM/SIGINT — so a consumer could only ever be its own process.
`Worker` is now the unit and the hosts are thin, so these assert the seam:
lifecycle ordering, the run-before-start guard, who owns the signals, embedded
draining, and the pool's supervisor contract.
"""

from __future__ import annotations

import asyncio

import pytest

from forktex_core import queue
from forktex_core.worker import (
    Worker,
    WorkerConfig,
    background,
    create_worker,
    run_worker,
    run_worker_pool,
)
from forktex_core.worker.factory import _bootstrap


@pytest.fixture(autouse=True)
def _register_dummy_task():
    """arq requires ≥1 registered function to construct a Worker. Register a
    no-op task so the [worker] tests can build real Worker instances."""

    @queue.task()
    async def _noop(ctx: dict) -> str:
        return "ok"

    yield


class _FakeArq:
    """Stands in for `arq.Worker`: records the lifecycle without consuming."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.signals: list[int] = []
        self._stop = asyncio.Event()

    async def async_run(self) -> None:
        self.calls.append("run")
        await self._stop.wait()
        self.calls.append("returned")

    def handle_sig(self, signum: int) -> None:
        self.signals.append(signum)
        self._stop.set()

    async def close(self) -> None:
        self.calls.append("close")


@pytest.fixture
def fake_arq(monkeypatch):
    """Replace `queue.make_worker` — the single seam `Worker.start` builds through."""
    from forktex_core.worker import factory as worker_factory

    built: dict = {}
    fake = _FakeArq()

    def _make_worker(**kwargs):
        built.update(kwargs)
        return fake

    monkeypatch.setattr(worker_factory._queue, "make_worker", _make_worker)
    fake.built = built  # type: ignore[attr-defined]
    return fake


def test_worker_config_defaults():
    cfg = WorkerConfig(redis_url="redis://example:6379/0")
    assert cfg.redis_url == "redis://example:6379/0"
    assert cfg.queue_name == "default"
    assert cfg.max_jobs == 10
    assert cfg.job_timeout == 300
    assert cfg.startup_hooks == []


@pytest.mark.asyncio
async def test_start_runs_hooks_in_order_before_the_queue_pool(redis_url: str, fake_arq):
    calls: list[str] = []

    async def hook_one():
        calls.append("one")

    async def hook_two():
        calls.append("two")

    cfg = WorkerConfig(redis_url=redis_url, startup_hooks=[hook_one, hook_two])
    async with Worker(cfg) as worker:
        assert calls == ["one", "two"]
        # The pool is live, so a running @task could `enqueue()` a chained job.
        assert await queue.worker_health(redis_url) is not None
        assert worker.arq_worker is fake_arq


@pytest.mark.asyncio
async def test_a_raising_hook_aborts_before_any_consuming(redis_url: str, fake_arq):
    calls: list[str] = []

    async def boom():
        raise RuntimeError("hook failed")

    async def never_called():
        calls.append("never")  # pragma: no cover — should not run

    cfg = WorkerConfig(redis_url=redis_url, startup_hooks=[boom, never_called])
    worker = Worker(cfg)
    with pytest.raises(RuntimeError, match="hook failed"):
        await worker.start()
    assert calls == []
    assert fake_arq.calls == []


@pytest.mark.asyncio
async def test_unreachable_redis_aborts_startup():
    """Fail-fast: `enqueue()` from inside a task would otherwise drop work."""
    cfg = WorkerConfig(redis_url="redis://nonexistent.invalid.local:1/0")
    with pytest.raises(Exception):
        await _bootstrap(cfg)


@pytest.mark.asyncio
async def test_run_before_start_is_refused(redis_url: str):
    """Consuming with half-wired dependencies is worse than a loud error."""
    worker = Worker(WorkerConfig(redis_url=redis_url))
    with pytest.raises(RuntimeError, match="before start"):
        await worker.run()
    with pytest.raises(RuntimeError, match="has not run"):
        _ = worker.arq_worker


@pytest.mark.asyncio
async def test_standalone_owns_the_signals_and_embedded_does_not(redis_url: str, fake_arq):
    """The whole reason signal handling is a constructor argument: a host that
    already installed its own handlers must not have arq's fight them."""
    cfg = WorkerConfig(redis_url=redis_url)

    async with Worker(cfg, handle_signals=True):
        assert fake_arq.built["handle_signals"] is True
    async with Worker(cfg, handle_signals=False):
        assert fake_arq.built["handle_signals"] is False


@pytest.mark.asyncio
async def test_config_reaches_the_arq_worker(redis_url: str, fake_arq):
    cfg = WorkerConfig(redis_url=redis_url, queue_name="check", max_jobs=2, job_timeout=60)
    async with Worker(cfg):
        assert fake_arq.built["queue_name"] == "check"
        assert fake_arq.built["max_jobs"] == 2
        assert fake_arq.built["job_timeout"] == 60


@pytest.mark.asyncio
async def test_stop_makes_run_return(redis_url: str, fake_arq):
    cfg = WorkerConfig(redis_url=redis_url)
    async with Worker(cfg) as worker:
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0)  # let it enter the loop
        await worker.stop()
        await asyncio.wait_for(task, timeout=5)
    assert fake_arq.calls == ["run", "returned", "close"]


@pytest.mark.asyncio
async def test_stop_and_aclose_are_safe_before_start(redis_url: str, fake_arq):
    worker = Worker(WorkerConfig(redis_url=redis_url))
    await worker.stop()  # no-op
    await worker.aclose()  # no-op
    assert fake_arq.calls == []


@pytest.mark.asyncio
async def test_aclose_is_idempotent(redis_url: str, fake_arq):
    worker = Worker(WorkerConfig(redis_url=redis_url))
    await worker.start()
    await worker.aclose()
    await worker.aclose()
    assert fake_arq.calls == ["close"]


@pytest.mark.asyncio
async def test_background_consumes_alongside_the_host_and_drains(redis_url: str, fake_arq):
    """The embedded shape: the host keeps the loop, the worker runs as a task,
    and leaving the block drains it."""
    cfg = WorkerConfig(redis_url=redis_url)
    async with background(cfg) as worker:
        await asyncio.sleep(0)
        assert "run" in fake_arq.calls
        assert worker.config is cfg
    assert fake_arq.calls == ["run", "returned", "close"]


@pytest.mark.asyncio
async def test_background_cancels_when_the_drain_overruns(redis_url: str, monkeypatch):
    """A job that refuses to finish must not hang the host's shutdown forever."""
    from forktex_core.worker import factory as worker_factory

    class _StuckArq(_FakeArq):
        def handle_sig(self, signum: int) -> None:
            self.signals.append(signum)  # acknowledged, but never stops

        async def async_run(self) -> None:
            self.calls.append("run")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.calls.append("cancelled")
                raise

    stuck = _StuckArq()
    monkeypatch.setattr(worker_factory._queue, "make_worker", lambda **kw: stuck)

    async with background(WorkerConfig(redis_url=redis_url), drain_timeout=0.05):
        await asyncio.sleep(0)
    assert "cancelled" in stuck.calls
    assert "close" in stuck.calls


@pytest.mark.asyncio
async def test_background_reraises_a_consumer_that_dies_on_its_own(redis_url: str, monkeypatch):
    """Silently swallowing this would leave the host up with nothing consuming."""
    from forktex_core.worker import factory as worker_factory

    class _CrashingArq(_FakeArq):
        async def async_run(self) -> None:
            raise RuntimeError("consumer exploded")

    monkeypatch.setattr(worker_factory._queue, "make_worker", lambda **kw: _CrashingArq())

    with pytest.raises(RuntimeError, match="consumer exploded"):
        async with background(WorkerConfig(redis_url=redis_url)):
            await asyncio.sleep(0.01)


def test_run_worker_is_a_thin_asyncio_run_wrapper(monkeypatch):
    """``run_worker`` should call ``asyncio.run(_bootstrap(config))``."""
    captured: dict = {}

    async def fake_bootstrap(config):
        captured["config"] = config

    from forktex_core.worker import factory as worker_factory

    monkeypatch.setattr(worker_factory, "_bootstrap", fake_bootstrap)
    cfg = WorkerConfig(redis_url="redis://x:6379/0", queue_name="alt")
    run_worker(cfg)
    assert captured["config"] is cfg


def test_a_pool_of_one_skips_the_supervisor(monkeypatch):
    """An extra process would only add a signal hop between the process manager
    and the single worker."""
    from forktex_core.worker import factory as worker_factory

    spawned: list = []
    ran: list = []
    monkeypatch.setattr(worker_factory, "run_worker", lambda cfg: ran.append(cfg))
    monkeypatch.setattr(
        worker_factory.multiprocessing,
        "get_context",
        lambda _kind: spawned.append("ctx"),  # would raise if used
    )

    cfg = WorkerConfig(redis_url="redis://x:6379/0")
    run_worker_pool(cfg, processes=1)
    assert ran == [cfg]
    assert spawned == []


def test_a_pool_needs_at_least_one_process():
    with pytest.raises(ValueError, match="processes must be >= 1"):
        run_worker_pool(WorkerConfig(redis_url="redis://x:6379/0"), processes=0)


def test_create_worker_still_passes_through_to_queue(redis_url: str):
    """Kept for callers handing a worker to arq's own CLI — no lifecycle wiring."""
    cfg = WorkerConfig(redis_url=redis_url, queue_name="check", max_jobs=2, job_timeout=60)
    worker = create_worker(cfg)
    direct = queue.make_worker(redis_url=redis_url, queue_name="check", max_jobs=2, job_timeout=60)
    assert worker.queue_name == direct.queue_name
    assert worker.max_jobs == direct.max_jobs
    assert worker.job_timeout_s == direct.job_timeout_s


class _FakeProcess:
    """Stands in for `multiprocessing.Process` so the pool's supervisor logic is testable
    without actually spawning interpreters."""

    instances: list[_FakeProcess] = []

    def __init__(self, target, args, name):  # noqa: ANN001 — mirrors Process's signature
        self.target = target
        self.args = args
        self.name = name
        self.pid = 1000 + len(_FakeProcess.instances)
        self.started = False
        self.joined = False
        self.exitcode: int | None = None
        self._alive = True
        _FakeProcess.instances.append(self)

    def start(self) -> None:
        self.started = True

    def join(self) -> None:
        self.joined = True
        self._alive = False
        self.exitcode = 0

    def is_alive(self) -> bool:
        return self._alive


@pytest.fixture
def fake_pool(monkeypatch):
    """Replace the process context and `os.kill`, leaving the supervisor logic real."""
    from forktex_core.worker import factory as worker_factory

    _FakeProcess.instances = []
    signalled: list[tuple[int, int]] = []

    class _Ctx:
        Process = _FakeProcess

    monkeypatch.setattr(worker_factory.multiprocessing, "get_context", lambda kind: _Ctx() if kind == "spawn" else None)
    monkeypatch.setattr(worker_factory.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    return signalled


def test_a_pool_starts_and_joins_one_process_per_worker(fake_pool):
    cfg = WorkerConfig(redis_url="redis://x:6379/0", queue_name="pooled")
    run_worker_pool(cfg, processes=3)

    assert len(_FakeProcess.instances) == 3
    assert all(p.started and p.joined for p in _FakeProcess.instances)
    assert [p.name for p in _FakeProcess.instances] == [f"forktex-worker-{i}" for i in range(3)]
    # Each child runs the standalone entrypoint with the same config — nothing is shared.
    assert all(p.args == (cfg,) for p in _FakeProcess.instances)


def test_a_pool_uses_spawn_not_fork(monkeypatch):
    """`fork` would inherit the parent's event loop, Redis sockets and DB pool, none of
    which survive intact."""
    from forktex_core.worker import factory as worker_factory

    _FakeProcess.instances = []
    kinds: list[str] = []

    class _Ctx:
        Process = _FakeProcess

    def _get_context(kind: str) -> object:
        kinds.append(kind)
        return _Ctx()

    monkeypatch.setattr(worker_factory.multiprocessing, "get_context", _get_context)
    monkeypatch.setattr(worker_factory.os, "kill", lambda pid, sig: None)
    run_worker_pool(WorkerConfig(redis_url="redis://x:6379/0"), processes=2)
    assert kinds == ["spawn"]


def test_the_pool_restores_the_signal_handlers_it_installed(fake_pool):
    """The supervisor is a scope, not a takeover: a host that called `run_worker_pool` gets
    its own SIGTERM/SIGINT handling back afterwards."""
    import signal as signal_module

    before = {
        signal_module.SIGTERM: signal_module.getsignal(signal_module.SIGTERM),
        signal_module.SIGINT: signal_module.getsignal(signal_module.SIGINT),
    }
    run_worker_pool(WorkerConfig(redis_url="redis://x:6379/0"), processes=2)
    assert signal_module.getsignal(signal_module.SIGTERM) is before[signal_module.SIGTERM]
    assert signal_module.getsignal(signal_module.SIGINT) is before[signal_module.SIGINT]


# The signal-forwarding branch inside `run_worker_pool` is deliberately not unit-tested.
# Reaching it requires patching `signal.signal` to capture the installed handler, and the
# supervisor's own restore call overwrites that capture — the scaffolding ended up longer
# and less readable than the six lines it covers. It is exercised for real whenever a pool
# is stopped; a fragile test here would cost more than the gap.
