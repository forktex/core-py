# `forktex_core.queue` — Background job queue

> arq-backed (Redis-native, asyncio-first) fire-and-forget queue. Use `flow` for durable execution with replay; use `queue` for at-least-once delivery where Redis suffices.

## Overview

`queue` wraps arq with a simpler API: `@task` decorator for registration, `enqueue`/`enqueue_at` for dispatch, `make_worker` for the worker process, and operator visibility via `inspect_job`/`cancel_job`/`worker_health`.

`make_worker(..., handle_signals=True)` controls whether arq installs the
SIGTERM/SIGINT drain. Leave it on for a standalone worker; a host that embeds the
consumer (an API lifespan, a supervised child) must turn it off or arq's handlers
fight the host's own shutdown. [`forktex_core.worker`](worker.md) does this for
you and is the recommended entry point.

```bash
pip install forktex-core[queue]   # arq
```

## Quick start

```python
from forktex_core.queue import task, init, enqueue, enqueue_at, make_worker, JobCtx
from datetime import datetime, timezone

await init("redis://localhost:6379/1")


@task
async def send_email(ctx: JobCtx, to: str, subject: str) -> None:
    await smtp.send(to, subject)


@task(queue="critical", timeout=60, retries=2)
async def notify_webhook(ctx: JobCtx, url: str, payload: dict) -> None:
    await http.post(url, json=payload)


# Enqueue immediately
job_id = await enqueue(send_email, "user@example.com", "Welcome!")

# Enqueue at a specific time
job_id = await enqueue_at(
    send_email,
    "user@example.com",
    "7-day reminder",
    eta=datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc),
)
```

## API reference

```python
# Registration
def task(
    _fn=None, *, queue="default", timeout=300, retries=0,
) -> Callable    # @task or @task(...)
    # timeout/retries are applied per-function by make_worker (arq
    # max_tries = retries + 1). For the delay *between* retries, raise
    # arq.Retry(defer=seconds) from the job body — arq has no per-function
    # retry-delay setting.

class JobCtx(TypedDict):
    redis: Any; job_id: str; job_try: int; enqueue_time: datetime; score: float

# Lifecycle
async def init(redis_url: str) -> None
async def close() -> None
    # Closes the cached Redis connection pool, if any. Idempotent. Call in
    # test teardown / app shutdown to avoid leaking a pool across event loops.

# Dispatch
async def enqueue(fn, *args, _queue_name=None, **kwargs) -> str     # returns job_id
async def enqueue_at(fn, *args, eta: datetime, _queue_name=None, **kwargs) -> str

# Operator visibility — queue_name must match the queue the job was
# enqueued on (via enqueue(..., _queue_name=...) or @task(queue=...));
# it defaults to "default" to match enqueue()'s own default.
async def inspect_job(job_id: str, *, queue_name="default") -> dict | None
    # → {"job_id","status","function","args","kwargs","enqueue_time","start_time","finish_time","result","error","tries"}
    # status: "queued" | "in_progress" | "complete" | "not_found"
async def cancel_job(job_id: str, *, queue_name="default") -> bool
    # True only if a worker was actively running the job and confirmed the
    # cancellation within 1s. A merely queued/deferred job (no worker has
    # picked it up yet) resolves False — see Edge cases below.
async def list_jobs(queue_name="default", *, status=None) -> list[dict]
    # only sees queued/deferred jobs (arq's per-queue sorted set) — not
    # in-progress or completed ones
async def worker_health(redis_url=None, *, queue_name="default") -> dict[str, int]
    # → {"pending": int, "in_flight": int, "failed": int}
    # queue_name must match the queue the jobs were enqueued on. pending and
    # in_flight are read straight from Redis; failed is the cumulative count
    # arq publishes in its health-check key, so it is 0 until a worker has
    # recorded health.

# Worker factory
def make_worker(redis_url, *, queue_name="default", max_jobs=10, job_timeout=300) -> arq.Worker
```

## Patterns

### Pattern 1 — Worker entrypoint module

```python
# my_service/worker.py
import forktex_core.queue as q
from my_service import tasks  # side-effect: @task decorators register functions

WorkerSettings = q.make_worker("redis://localhost:6379/1")

# Run: arq my_service.worker.WorkerSettings
# Or programmatically:
if __name__ == "__main__":
    import asyncio

    worker = q.make_worker("redis://localhost:6379/1")
    asyncio.run(worker.async_run())
```

### Pattern 2 — Replace asyncio.create_task with durable queue

```python
# BEFORE: asyncio.create_task (lost on restart)
asyncio.create_task(_ingest_document(doc_id, ...))

# AFTER: queue (survives restart, retries on failure)
@task(retries=2, timeout=300)
async def ingest_document(ctx: JobCtx, doc_id: str, ...) -> None:
    await _do_ingest(doc_id, ...)

job_id = await enqueue(ingest_document, str(doc.id), ...)
```

### Pattern 3 — Priority queues

```python
@task(queue="high-priority", timeout=30)
async def urgent_notification(ctx: JobCtx, user_id: str) -> None: ...


@task(queue="low-priority", timeout=600)
async def bulk_export(ctx: JobCtx, org_id: str) -> None: ...


# Separate workers per queue (or one worker per queue_name)
high_worker = make_worker(redis_url, queue_name="high-priority", max_jobs=20)
low_worker = make_worker(redis_url, queue_name="low-priority", max_jobs=2)
```

## Anti-patterns

```python
# ❌ Using queue for durable execution needing replay
# → Use flow instead (guaranteed delivery, state, audit trail)

# ❌ Unbounded list_jobs on high-throughput queue
jobs = await list_jobs("default")  # may scan 10k+ keys

# ✅ Filter by status
pending = await list_jobs("default", status="queued")


# ❌ Registering the same task name twice (silently overwrites)
@task
async def my_job(ctx): ...
@task
async def my_job(ctx): ...  # WARNING logged, second registration wins


# ✅ Unique function names per registry
```

---

## Agent guide

### Canonical forms

**Service with queue integration:**
```python
# app/main.py
from forktex_core.queue import init as queue_init


@asynccontextmanager
async def lifespan(app):
    await queue_init(settings.redis_url)
    yield  # queue.close() not needed — pool is per-enqueue


# tasks.py
from forktex_core.queue import task, JobCtx


@task(retries=2, timeout=120)
async def process_document(ctx: JobCtx, doc_id: str) -> None:
    async with get_session() as session:
        await do_work(session, doc_id)


# routes.py
from forktex_core.queue import enqueue
from .tasks import process_document


@router.post("/items")
async def upload(doc_id: str):
    job_id = await enqueue(process_document, doc_id)
    return {"job_id": job_id}
```

**Inspect → cancel flow:**
```python
info = await inspect_job(job_id)
if info and info["status"] == "in_progress":
    # cancel_job() only confirms True for a job a worker is actively running
    cancelled = await cancel_job(job_id)
    print("cancelled:", cancelled)
```

**Cancelling a job on a non-default queue** (pass the matching `queue_name` or the lookup silently misses):
```python
job_id = await enqueue(bulk_export, org_id, _queue_name="low-priority")
...
await cancel_job(job_id, queue_name="low-priority")
await inspect_job(job_id, queue_name="low-priority")
```

### Edge cases

| Scenario | Behaviour |
|---|---|
| `enqueue()` before `init()` | `QueueError("Queue not initialized…")` |
| `@task` same name twice | Warning logged, second overwrites first |
| `list_jobs()` on large queue | Truncated at 10,000 keys with warning |
| `cancel_job()` — job merely queued/deferred, no worker running it yet | Returns `False` |
| `cancel_job()` — job actively running, worker confirms cancellation | Returns `True` |
| `cancel_job()` — job already completed, or unknown ID | Returns `False` |
| `cancel_job()`/`inspect_job()` — `queue_name` doesn't match the job's real queue | Job looks `"not_found"` / cancel silently returns `False`, even if the job is genuinely running |
| `inspect_job(unknown_id)` | Returns `None` |
| `make_worker().async_run()` | Runs until `SIGTERM` — burst mode exits after all queued jobs |

### Error catalogue

| Error | When |
|---|---|
| `ImportError("Install 'forktex-core[queue]'")` | `arq` not installed |
| `QueueError("Queue not initialized")` | `enqueue`/`enqueue_at` before `init()` — subclasses `RuntimeError` |
| `QueueError` | Other configuration or connection errors (e.g. `enqueue_job` rejected) |

### Integration map

```
queue ──── (requires) ──── cache  [Redis — same infra, different DB index]
queue ──── (complement) ── flow   [queue = fire-and-forget; flow = durable+replay]
```

### Checklist

- [ ] `await init(redis_url)` called at startup
- [ ] `@task` functions imported in the worker entrypoint (side-effect registration)
- [ ] `retries=` set for tasks that can fail transiently (network, external API)
- [ ] `timeout=` set conservatively — arq kills jobs that exceed it
- [ ] `eta` is timezone-aware datetime for `enqueue_at`
- [ ] `list_jobs()` used only for operator tooling, not in hot paths
