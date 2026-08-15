# forktex_core.worker

A queue consumer as an object, plus three hosts that can run it: a standalone process, a background task inside a host that owns the loop, or one process per core. `queue` owns *what* runs; this package owns *where the consumer lives*.

## Install

```bash
pip install "forktex-core[worker]"   # arq — the same dependency as [queue]
```

`worker` is a thin layer over `forktex_core.queue`, and it inherits that module's lazy failure: `import forktex_core.worker` succeeds without `arq`, and the `ImportError` naming `forktex-core[queue]` surfaces when `Worker.start()` reaches `make_worker`. Optional and consumer-wired: `[database]` (advisory locks), `[grid]`/`[space]` (tasks over grids), `[flow]` (pipelines).

## Wiring

Shape C — a consumer-owned object, no module-level singleton and no registry. You build a `WorkerConfig`, hand it to a `Worker` or one of the hosts, and that object owns its lifecycle. `Worker.__aenter__` runs the startup hooks, calls `queue.init()`, and builds the arq worker; `__aexit__` closes the arq worker and the queue pool.

```python
# myservice/worker.py
from forktex_core.storage import register as register_storage
from forktex_core.worker import WorkerConfig, run_worker


async def init_storage() -> None:
    register_storage("default", url="http://minio:9000", bucket="kb", access_key="...", secret_key="...")


if __name__ == "__main__":
    from myservice import tasks  # noqa: F401  side-effect: registers @task

    run_worker(
        WorkerConfig(
            redis_url="redis://redis:6379/0",
            queue_name="myservice",
            max_jobs=8,
            startup_hooks=[init_storage],
        )
    )
```

Inside a host that already owns the loop and the signals, use `background` — arq's signal handlers stay off so they cannot fight the host's shutdown:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from forktex_core.worker import WorkerConfig, background


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with background(WorkerConfig(redis_url="redis://redis:6379/0")):
        yield


app = FastAPI(lifespan=lifespan)
```

| Host | Owns the loop | Owns the signals |
| --- | --- | --- |
| `run_worker(config)` | yes (`asyncio.run`) | arq installs SIGTERM/SIGINT |
| `background(config)` | no — the host does | no — the host does |
| `run_worker_pool(config, processes=N)` | one per child | parent forwards to children |

**Adoption note:** nothing inside `forktex_core` imports this package, and both services that run workers today hand-roll their own loop around `queue.make_worker` (see [`queue.md`](queue.md)). This is a convenience layer over that same call, not a required path.

## Public surface

`__all__`:

| Name | What it is |
| --- | --- |
| `WorkerConfig` | Pydantic model: `redis_url`, `queue_name="default"`, `max_jobs=10`, `job_timeout=300`, `startup_hooks=[]`. |
| `Worker(config, *, handle_signals=True)` | Async context manager plus awaitable `run()`. Also `start()`, `stop()`, `aclose()`, and properties `config` / `arq_worker`. |
| `run_worker(config)` | Blocking entrypoint: `asyncio.run` over `async with Worker(..., handle_signals=True)` then `run()`. |
| `background(config, *, drain_timeout=DEFAULT_DRAIN_TIMEOUT)` | Async CM yielding a started `Worker` consuming in a background task; drains on exit. |
| `run_worker_pool(config, *, processes=1)` | `processes` independent `run_worker` children via the `spawn` context. |
| `create_worker(config)` | `WorkerConfig` → bare `arq.Worker`, no hooks and no pool. For handing to arq's own CLI. |
| `StartupHook` | Alias for `Callable[[], Awaitable[None]]`. |
| `DEFAULT_DRAIN_TIMEOUT` | `30.0` seconds. |

`startup_hooks` fire in declared order before the queue pool initialises — register storage/vector clients, start a flow driver, warm caches. A raising hook aborts startup, and so does a failing `queue.init()`: consuming with a dead pool would silently drop any work a task tried to chain.

This package does **not** register tasks, wire a flow driver, or restart failed processes. Tasks arrive through `[queue]`'s import-side-effect registration; a crash-loop is the process manager's to see.

## Errors

| Raised | When |
| --- | --- |
| `ImportError` | `arq` missing when `Worker.start()` builds the arq worker. |
| `RuntimeError("Worker.run() before start() …")` | `run()` on a worker that was never entered. |
| `RuntimeError("Worker.start() has not run …")` | Reading `.arq_worker` before `start()`. |
| `ValueError` | `run_worker_pool(..., processes=0)` or negative. |
| whatever a hook raises | A `startup_hooks` callable failed — startup aborts and the exception propagates. |
| `forktex_core.queue.QueueError` | Propagated from `queue.init()`; logged as `worker: queue.init failed` before it re-raises. |

`aclose()` swallows exceptions from closing the arq worker and the pool — teardown never masks the original failure.

## Gotchas

- **Enter before you run.** `Worker.run()` on an unstarted worker raises rather than consuming with half-wired dependencies. Use `async with Worker(...)`.
- **`create_worker` has no lifecycle.** No startup hooks, no `queue.init()`, and it ignores `handle_signals` entirely (it always takes `make_worker`'s default of `True`). Prefer `Worker` or `run_worker`.
- **`background` re-raises a dead consumer.** If the worker task fails on its own, the exception is logged and re-raised on exit rather than leaving the host serving with nothing consuming. On drain timeout it logs a warning and cancels in-flight jobs.
- **`processes=1` skips the supervisor** and calls `run_worker` directly — an extra process would only add a signal hop.
- **The pool uses `spawn`, not `fork`.** The parent may already hold an event loop, Redis sockets, or a DB pool, none of which survive a fork. That means `WorkerConfig` and everything in `startup_hooks` must be picklable, and each child re-runs the hooks in its own process.
- **`Worker.stop()` is a method call, not a signal.** It invokes arq's `handle_sig(SIGUSR1)` internally, which is arq's own "finish in-flight jobs and return" path; no real signal is delivered.
- **Per-task `timeout`/`retries` still come from `@task`.** `WorkerConfig.job_timeout` is only the worker-wide fallback.
