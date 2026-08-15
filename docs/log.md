# forktex_core.log

Structured logging for any Python process: JSON to stdout (Loki-ready) in production, human-readable in dev, with a coroutine-scoped `trace_id` and structured extra fields carried on `contextvars`.

## Install

Always bundled — `log` is a level-0 extra with no packages behind it. `pip install forktex-core` is enough; `forktex-core[log]` resolves but installs nothing extra. Stdlib only, plus `forktex_core.iso` for canonical UTC timestamps. Nothing here ever raises `ImportError` for a missing extra.

## Wiring

Shape C — no global object to build or close. `setup_logging()` configures the stdlib root logger once at process startup; everything else is a plain function, context manager, or a middleware class the consumer owns.

```python
from forktex_core.log import setup_logging, get_logger

setup_logging(service="my-service")   # JSON, INFO, stdout
log = get_logger(__name__)
log.info("ready")
# {"timestamp":"2026-05-02T14:30:00+00:00","level":"INFO","logger":"my.module",
#  "service":"my-service","message":"ready"}

setup_logging(service="my-service", debug=True)
# 2026-05-02 14:30:00 | INFO     | my.module | ready
```

`setup_logging` is idempotent — it clears `root.handlers` before installing its own. Call it once, before the app starts serving.

### TraceIDMiddleware (FastAPI / Starlette / any ASGI)

Do not hand-roll a request-id middleware; this one ships here. It is pure ASGI (no starlette or fastapi import), reads `X-Request-ID` or mints a `uuid7`, binds it for the *whole* ASGI call — endpoint, streaming body, background tasks — and echoes it on the response. An inbound header that does not match `^[A-Za-z0-9._-]{1,128}$` is rejected in favour of a minted id, so a client cannot splice `\n` into a log line.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from forktex_core.log import TraceIDMiddleware, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(service="my-service")
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(TraceIDMiddleware)                      # X-Request-ID
app.add_middleware(TraceIDMiddleware, header="X-Trace-ID")  # or another header
```

Add it before other middleware so the trace id is set first.

Outside HTTP (workers, CLI), use `trace_context()` / `async_trace_context()` — the same primitive the middleware uses:

```python
from forktex_core.log import async_trace_context, get_logger

log = get_logger(__name__)


async def process_job(job_id: str) -> None:
    async with async_trace_context(f"job-{job_id}"):
        log.info("processing")   # {..."trace_id": "job-abc"}
    # restored on exit, including when the block raises
```

## Public surface

`__all__`:

| Name | What it is |
| --- | --- |
| `setup_logging(*, service=None, level=None, debug=None, json=None, quiet=None, quiet_level=WARNING, quiet_defaults=True, fmt=None, datefmt=None, handlers=None, queue=False)` | Keyword-only. Configures the root logger; idempotent. |
| `get_logger(name)` | `logging.getLogger(name)` — one import site for the whole app. |
| `get_trace_id()` | Current trace id, or `None`. |
| `set_trace_id(value)` | Set the trace id for this context with no auto-restore. The supported way to read/write the id — the `log._context._trace_id` ContextVar is private. |
| `get_root_trace_id()` | Id established by the outermost `trace_context()`; stable across nesting. |
| `trace_context(value=None)` | Sync CM: scope a trace id, mint a `uuid7` if omitted, restore on exit. |
| `async_trace_context(value=None)` | Async counterpart. |
| `log_context(**fields)` | Sync CM: merge structured fields into every record in the block. |
| `async_log_context(**fields)` | Async counterpart (coroutine-scoped). |
| `traced(fn=None, *, name=None, level=INFO)` | Decorator, bare or parametrized, sync or async: entry line, exit line with `duration_ms`, `exception()` + re-raise on failure, inside a fresh `trace_context()`. |
| `TraceIDMiddleware` | Pure-ASGI trace-id middleware (above). |
| `DEFAULT_QUIET_LOGGERS` | The list silenced unless `quiet_defaults=False`: `uvicorn.access`, `uvicorn.error`, `sqlalchemy.engine`, `httpx`, `httpcore`, `asyncio`. |
| `get_extra_fields()` | The current `log_context()` fields as a dict (never `None`). |

Env overrides — consulted only when the matching argument is left `None`: `$FORKTEX_LOG_LEVEL`, `$FORKTEX_LOG_DEBUG`, `$FORKTEX_LOG_JSON`. An explicit argument always wins.

`JsonFormatter` / `HumanFormatter` are internal; `setup_logging` picks one for you.

### JSON record shape

`timestamp` (UTC, via `iso.to_iso`), `level`, `logger`, then `service`, `message`, `trace_id`, `root_trace_id` (each omitted when unset), then `log_context()`/`extra={}` fields, then `exception` when `exc_info` is present. Core keys win: a colliding context field is dropped, never overwrites.

## Errors

Nothing in this module raises an error of its own. `setup_logging`, `get_logger`, the context managers and `TraceIDMiddleware` have no failure mode a caller catches; `@traced` re-raises whatever the wrapped callable raised after logging it. There is no `LogError`.

## Gotchas

- **`set_trace_id()` has no restore.** It leaves the id set for whatever runs next in that context. Prefer `trace_context()` / `async_trace_context()`, which restore in a `finally`.
- **contextvars do not cross threads or processes.** A `ThreadPoolExecutor` worker or a `multiprocessing` child starts with an empty context. Capture `get_trace_id()`/`get_extra_fields()` in the caller and re-establish them inside — there is no helper for this.
- **`queue=True` starts a listener thread that is never stopped.** `setup_logging` stays idempotent for handlers, but each call with `queue=True` starts a new `QueueListener`. Call it once.
- **Header validation is not configurable.** `TraceIDMiddleware`'s charset and 128-char cap are hardcoded; a W3C `traceparent` needs your own middleware.
- **`forktex_core`'s own modules log through `get_logger(__name__)`** and never call `setup_logging()`. They inherit the root handlers you configure, so their lines carry your `service`/`trace_id` too — and you can quiet them by name: `setup_logging(quiet=["forktex_core.flow"])`.
- **Decoration order does not matter.** `@traced` resolves a logger at decoration time but stdlib logging resolves handlers and level at emit time, so importing a decorated module before `setup_logging()` is safe.
- **`handlers=[...]` replaces the transport, not the pipeline.** Your handlers still get the context filter, level, and chosen formatter applied.
