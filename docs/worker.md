# `forktex_core.worker`

Level-3 bootstrap. The queue consumer as an object, plus the three hosts that
can run it: a standalone process, an embedded background task, or a process pool.

`[queue]` owns *what* runs (`@task`, `enqueue`). This package owns *where a
consumer lives*.

## Install

```bash
poetry add "forktex-core[worker]"
```

Mandatory deps: `[queue]` + level-0 primitives. Optional consumer-wired
deps: `[database]` (advisory locks), `[grid]` / `[space]` (if tasks
operate on grids), `[flow]` (if pipelines are wired in).

## Purpose

`Worker` is the unit: an async lifecycle (startup hooks → queue pool → arq
worker) plus an awaitable `run()`. The hosts around it are thin, and they exist
because **signal and loop ownership belong to the host, not to the worker**. The
previous single `run_worker` claimed both — it owned `asyncio.run` *and* let arq
install the SIGTERM/SIGINT handlers — so a consumer could only ever be its own
process. An API that wanted to consume its own queue in-process had no way in.

| Host | Owns the loop | Owns the signals | Use for |
| --- | --- | --- | --- |
| `run_worker(config)` | yes | arq does | a worker entrypoint script |
| `background(config)` | no (the host does) | no (the host does) | an API lifespan, a test |
| `run_worker_pool(config, processes=N)` | per child | parent forwards | CPU-bound tasks |

## Public API

```python
from forktex_core.worker import (
    Worker,  # the consumer: async CM + awaitable run()
    WorkerConfig,  # redis_url, queue_name, max_jobs, job_timeout, startup_hooks
    background,  # async CM: consume alongside a host, drain on exit
    run_worker,  # blocks until signalled; owns the process
    run_worker_pool,  # one worker per OS process
    create_worker,  # WorkerConfig -> bare arq.Worker, no lifecycle
    DEFAULT_DRAIN_TIMEOUT,  # 30.0s
)
```

`startup_hooks` is a list of `Callable[[], Awaitable[None]]` fired in
declared order before the queue pool initialises. A raising hook —
or a failing `queue.init` — aborts startup so the consumer never
reaches `enqueue()` with a dead pool (which would silently drop work).

`Worker.run()` on an unstarted worker raises: consuming with half-wired
dependencies is worse than a loud error.

## Standalone process

```python
# myservice/worker.py
from forktex_core.worker import WorkerConfig, run_worker
from forktex_core.storage import register as register_storage
from myservice import tasks  # noqa: F401  side-effect: registers @task


async def init_storage():
    register_storage("default", url="http://minio:9000", bucket="kb", access_key="...", secret_key="...")


if __name__ == "__main__":
    run_worker(
        WorkerConfig(
            redis_url="redis://redis:6379/0",
            queue_name="myservice",
            max_jobs=8,
            startup_hooks=[init_storage],
        )
    )
```

## Embedded in an API

`background` runs the consumer as a task next to the server, with arq's signal
handlers switched off so they don't fight the host's shutdown. Leaving the block
asks the worker to drain and waits up to `drain_timeout` before cancelling.

```python
from contextlib import asynccontextmanager

from forktex_core.api import AppConfig, create_app
from forktex_core.worker import WorkerConfig, background


@asynccontextmanager
async def lifespan(app):
    async with background(WorkerConfig(redis_url="redis://redis:6379/0")):
        yield


app = create_app(AppConfig(lifespan=lifespan))
```

A consumer task that dies on its own is logged and re-raised on exit, rather
than leaving the host serving with nothing consuming.

## Process pool

One event loop parallelises *waiting*, not computing: CPU-bound tasks serialise
behind the GIL however high `max_jobs` goes. Each child is a full `run_worker`,
sharing nothing but the queue.

```python
run_worker_pool(WorkerConfig(redis_url="redis://redis:6379/0"), processes=4)
```

The parent is a supervisor only — it forwards SIGTERM/SIGINT and waits for the
children to drain. It does **not** restart them: a crash-loop is the process
manager's to see and back off from. `processes=1` skips the supervisor entirely,
since an extra process would only add a signal hop.

Children are started with the `spawn` context, not `fork`: the parent may
already hold an event loop, Redis sockets or a database pool, none of which
survive a fork intact.

## See also

- [`queue.md`](queue.md) — `[queue].task` registers task functions
  the worker consumes; `[queue].make_worker` is the underlying call and now
  takes `handle_signals=`.
- [`api.md`](api.md) — symmetric bootstrap for FastAPI services.
- `examples/worker_minimal.py` — runnable demo (builds and inspects, does not
  consume).
- `tests/test_worker/test_factory.py` — lifecycle ordering, the run-before-start
  guard, signal ownership, embedded draining, and the pool contract.
