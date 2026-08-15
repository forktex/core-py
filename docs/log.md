# `forktex_core.log` — Structured logging

> One-call `setup_logging()` replaces every service's bespoke `logging_config.py` — JSON output (Loki-ready), coroutine-safe trace_id (+ a stable root_trace_id for the whole call chain) via contextvars, structured extra fields, an `@traced` decorator, optional FastAPI middleware.

## Overview

`log` depends only on `forktex_core.iso` (canonical UTC timestamp formatting) — no third-party dependencies. It works in any Python process: FastAPI services, CLI workers, background scripts. Every service's `logging_config.py` is 2–3 lines calling this module.

`forktex_core`'s own internals (`flow`, `database`, `cache`, `space`, `worker`) use this exact same `get_logger(__name__)` — not a bare `logging.getLogger()`. You get the same logger the library uses on itself; see [Interaction with forktex_core's own internals](#interaction-with-forktex_cores-own-internals) below for what that means for your `setup_logging()` call.

```bash
pip install forktex-core   # log is always included, no extras
```

## Quick start

```python
from forktex_core.log import setup_logging, get_logger, TraceIDMiddleware

# Main entry point (FastAPI, worker, CLI)
setup_logging(service="my-service")  # JSON, INFO, stdout

log = get_logger(__name__)
log.info("ready")
# → {"timestamp":"2026-05-02T14:30:00+00:00","level":"INFO","logger":"my.module","service":"network","message":"ready"}

# Dev mode
setup_logging(service="my-service", debug=True)
# → 2026-05-02 14:30:00 | INFO     | my.module | ready

# FastAPI — auto trace_id per request
app.add_middleware(TraceIDMiddleware)
```

## API reference

```python
def setup_logging(
    *,
    service: str | None = None,        # included in every record
    level: int | str | None = None,    # default: INFO, or $FORKTEX_LOG_LEVEL
    debug: bool | None = None,         # → DEBUG level + human-readable format
                                        # default: $FORKTEX_LOG_DEBUG, else False
    json: bool | None = None,          # override format
                                        # default: $FORKTEX_LOG_JSON, else JSON if not debug
    quiet: list[str] | None = None,    # extra loggers to silence
    quiet_level: int = logging.WARNING,
    quiet_defaults: bool = True,       # False → skip DEFAULT_QUIET_LOGGERS entirely
    fmt: str | None = None,            # override HumanFormatter's line format
    datefmt: str | None = None,        # override HumanFormatter's date format
    handlers: list[logging.Handler] | None = None,  # custom transport(s);
                                        # still gets context filter + formatter + level
    queue: bool = False,               # non-blocking: QueueHandler/QueueListener (stdlib)
) -> None
```

Explicit arguments always win; env vars are only consulted when the
corresponding argument is left `None`. `DEFAULT_QUIET_LOGGERS` (the list of
loggers silenced unless `quiet_defaults=False`) is a public, importable
constant — extend or inspect it directly instead of guessing the defaults.

```python
def get_logger(name: str) -> logging.Logger
    # Thin wrapper — use __name__

def set_trace_id(value: str | None) -> None
    # Low-level: set trace_id for the current context, no auto-restore.
    # Prefer trace_context() below unless you specifically need this.

def get_trace_id() -> str | None

def get_root_trace_id() -> str | None
    # Stable across a whole chain of nested trace_context() calls (set once
    # by whichever scope enters first) — unlike trace_id, which is fresh per call.

@contextmanager
def trace_context(value: str | None = None) -> Generator[str, None, None]:
    # Scope a trace_id to this block (any process: CLI, worker, script) and
    # restore the previous value on exit. Mints a time-ordered uuid7 if value is None.
    # Also establishes root_trace_id if none is set yet (see get_root_trace_id).
    # with trace_context(f"job-{job_id}"): ...

@asynccontextmanager
async def async_trace_context(value: str | None = None) -> AsyncGenerator[str, None]:
    # Async counterpart of trace_context() — same behavior, `async with`.

def traced(fn=None, *, name: str | None = None, level: int = logging.INFO) -> Callable:
    # Wrap a sync or async callable: logs one entry line, one exit line (with
    # duration) on success, or log.exception() + re-raise on failure. Opens
    # trace_context()/async_trace_context() around the call — usable bare or
    # parametrized. Standalone; composes with other decorators (e.g. queue.task()).
    # @traced
    # async def process_job(job_id: str) -> None: ...

@contextmanager
def log_context(**fields) -> None
    # Inject structured fields for sync block

@asynccontextmanager
async def async_log_context(**fields) -> None
    # Inject structured fields for async block (coroutine-scoped)

class TraceIDMiddleware:
    # Pure-ASGI middleware — sets trace_id per request (X-Request-ID or a
    # minted uuid) via trace_context(), scoped over the whole call incl.
    # streaming + background tasks; zero deps. A malformed/hostile header is
    # rejected in favor of a minted id.
    # app.add_middleware(TraceIDMiddleware)
    # app.add_middleware(TraceIDMiddleware, header="X-Trace-ID")
```

`setup_logging` selects the formatter for you — `JsonFormatter` / `HumanFormatter`
are **internal** (not in `__all__`); the `service` field is injected onto each record
by the internal context filter, so there is a single source for it.

## Patterns

### Pattern 1 — Service startup

```python
# my_service/logging_config.py
from forktex_core.log import setup_logging as _setup


def setup_logging(debug: bool = False) -> None:
    _setup(service="my-service", debug=debug)
```

### Pattern 2 — Request-scoped structured context

```python
from forktex_core.log import async_log_context, get_logger
log = get_logger(__name__)

@router.post("/orders")
async def create_order(org_id: UUID, ...):
    async with async_log_context(org_id=str(org_id), endpoint="create_order"):
        log.info("creating order")   # → {..."org_id":"...","endpoint":"create_order"}
        result = await service.create(...)
        log.info("order created", extra={"order_id": str(result.id)})
```

### Pattern 3 — Worker / CLI job with a scoped trace_id

```python
from forktex_core.log import setup_logging, async_trace_context, get_logger

setup_logging(service="ingest-worker")
log = get_logger(__name__)


async def process_job(job_id: str):
    async with async_trace_context(f"job-{job_id}"):
        log.info("processing")  # → {..."trace_id":"job-abc"}
        await do_work()
    # trace_id is restored automatically — safe even if do_work() raises,
    # and safe in a long-running loop that processes many jobs in sequence
```

`trace_context()`/`async_trace_context()` are the same primitive `TraceIDMiddleware`
uses per-request, generalized to any block of code — a worker job, a CLI
run, a scheduled task — not just an ASGI request. Prefer them over the
lower-level `set_trace_id()` (which has no auto-restore, so a forgotten
"clear" call leaks a trace_id into whatever runs next in that context).

### Pattern 4 — Non-blocking I/O / custom transport (FastAPI under load)

```python
from logging.handlers import RotatingFileHandler
from forktex_core.log import setup_logging

# Move log I/O off the event loop; write to a rotating file instead of stdout.
setup_logging(
    service="my-service",
    handlers=[RotatingFileHandler("service.log", maxBytes=10_000_000, backupCount=3)],
    queue=True,  # QueueHandler/QueueListener — stdlib, no extra dependency
)
```

### Pattern 5 — `@traced` composed with a worker job / flow step

```python
from forktex_core import queue
from forktex_core.log import traced


@queue.task()
@traced()
async def send_invoice(ctx, invoice_id: str) -> None: ...


# → each job execution gets its own trace_id, logged on entry/exit with
# duration, and an exception() line + re-raise on failure. @traced doesn't
# touch @queue.task()'s "decorated function is unchanged" registration
# contract — they compose as independent wrappers, not a merged behavior.
```

`@traced` never needs to know whether `setup_logging()` has run yet: it
calls `logging.getLogger(func.__module__)` at *decoration* time, but stdlib
logging resolves handlers/level at *emit* time (when a log call actually
fires), not at `getLogger()` time — so the order between "import a module
with `@traced` functions in it" and "call `setup_logging()` at process
startup" never matters.

## Interaction with forktex_core's own internals

`forktex_core.flow`, `.database`, `.cache`, `.space`, and `.worker` all call `get_logger(__name__)` internally — the same function you call. This raises an obvious question: does that conflict with *your* `setup_logging()` call? No, and the reason is worth understanding precisely rather than taking on faith:

- **Every logger returned by `get_logger()` has no handlers of its own.** When forktex_core's internal code logs (e.g. from `forktex_core.flow.driver`), stdlib logging walks the name hierarchy looking for an effective level, finds none set on any forktex_core logger, and falls through to the **root** logger's level. If that passes, the record propagates up to the root logger's handlers — which are exactly what your `setup_logging()` call configured. There is one delivery pipeline, not two.
- **`forktex_core` never calls `setup_logging()` itself** — only your application does. So there's exactly one place (root) ever getting configured. If forktex_core's internals *also* called it, that would be a real conflict (`setup_logging()` clears `root.handlers` before re-adding its own, so whichever call ran last would win, silently discarding the other's configuration) — which is exactly why the library never does this.
- **Once you call `setup_logging()`, forktex_core's internal log lines automatically carry your `trace_id`/`root_trace_id`/`service` fields too** — same `_ContextFilter`, same handler, no extra wiring on your part.
- **Import-time ordering never matters.** `get_logger()` inside forktex_core's modules runs at import time (likely before your `main()` calls `setup_logging()`), but that's safe for the same reason `@traced` is safe: handler/level resolution happens at emit-time, not at `getLogger()`-time.
- **New capability this gives you**: because forktex_core's internals now use real qualified logger names (`forktex_core.flow.driver`, `forktex_core.cache.ops`, …) instead of the anonymous root namespace, you can selectively quiet just forktex_core's internal chatter exactly like you would any third-party library:

  ```python
  setup_logging(service="my-app", quiet=["forktex_core.flow"])
  # or, equivalently, at any point:
  import logging

  logging.getLogger("forktex_core.cache").setLevel(logging.WARNING)
  ```

- **If you never call `setup_logging()` at all**: unchanged baseline behavior — Python's `logging.lastResort` handler dumps `WARNING`+ to stderr in a bare, unformatted line; `INFO`/`DEBUG` records (from forktex_core or your own code) are simply never handled anywhere.

## Known limitations (not fixed here — documented as backlog items)

- **No thread/multiprocess context propagation.** `trace_id`/`root_trace_id`/`log_context()`'s fields are `contextvars` — they propagate automatically across `asyncio` tasks and coroutines, but **not** across a `ThreadPoolExecutor`/`multiprocessing` boundary (a worker thread starts with an empty context). A facade that spawns worker threads and wants the calling request's trace_id to show up in that thread's logs will need to capture `get_trace_id()`/`get_extra_fields()` in the calling context and re-establish them (`trace_context()`/`log_context()`) inside the thread — there's no automatic helper for this today.
- **`TraceIDMiddleware`'s header validation isn't configurable.** The `_TRACE_ID_RE` sanitization pattern and the 128-character cap are hardcoded. A consumer wanting a looser or stricter format (e.g. accepting a W3C `traceparent` header) would need to fork the check, not pass an option.
- **`setup_logging(queue=True)`'s listener thread has no shutdown hook.** Already covered above (idempotent-but-not-cleaned-up) — noted here again because it's the same category of gap as the other two: fine for a long-running service's single startup call, awkward for anything wanting a clean teardown (tests, short-lived CLI tools).

## Anti-patterns

```python
# ❌ ContextVar mutable default is shared across all coroutines
_extra = ContextVar("extra", default={})  # shared reference!

# ✅ Use None default + factory in getter
_extra = ContextVar("extra", default=None)


def get_extra():
    return _extra.get(None) or {}


# ❌ setup_logging called multiple times without clearing handlers
setup_logging(...)  # handler 1
setup_logging(...)  # handler 2 → duplicate output
# ✅ setup_logging is idempotent (clears root.handlers first)
```

---

## Agent guide

### Canonical forms

**Replace any `logging_config.py`:**
```python
from forktex_core.log import setup_logging as _core


def configure_logger() -> None:  # keep old function name for compat
    _core(service="my-service")


def setup_logging(debug: bool = False):  # if old API had debug param
    _core(service="my-service", debug=debug)
```

**JSON output fields (exact keys):**
```json
{
  "timestamp": "2026-05-02T14:30:00.123456+00:00",
  "level": "INFO",
  "logger": "my.module.name",
  "service": "my-service",
  "message": "request handled",
  "trace_id": "req-abc-123",
  "root_trace_id": "req-abc-123",
  "org_id": "org-xyz",
  "exception": "Traceback ..."
}
```
- `timestamp` — always ISO 8601 with UTC timezone (via `forktex_core.iso.to_iso`)
- `trace_id` — only present when set via middleware, `trace_context()`, `@traced`, or `set_trace_id()`
- `root_trace_id` — only present once something has established it (same list as `trace_id`); stable across a whole nested chain, where `trace_id` is fresh per call
- `exception` — only present on `log.exception()` or `log.error(exc_info=True)`
- Extra fields (`org_id`, etc.) — injected via `log_context()` or `extra={}` kwarg
- Colliding field name — a core field (`timestamp`/`level`/`logger`/`service`/`message`/`trace_id`/`exception`)
  always wins; a colliding `log_context()`/`extra={}` field is dropped, not merged

### Edge cases

| Scenario | Behaviour |
|---|---|
| `log_context()` nested | Merges — inner fields shadow outer with same name |
| `async_log_context()` across `await` | Safe — contextvar is task-scoped, not thread-local |
| `log_context(level=..., message=...)` colliding with a core field | Dropped — core field wins, no overwrite (`extra={"message": ...}` is rejected by stdlib `logging` before it gets here) |
| `trace_context()`/`async_trace_context()` — sync vs async | Both work in either context (neither `await`s anything); the async variant is style parity with `async with`, not a functional requirement |
| `trace_context()` raises inside the block | Still restores the previous trace_id (`try/finally`) |
| `set_trace_id(None)` | Clears the trace_id; subsequent records have no `trace_id` key — no auto-restore, so prefer `trace_context()` |
| `TraceIDMiddleware` dependencies | Pure ASGI — imports and runs with no starlette/fastapi installed |
| `TraceIDMiddleware` + background tasks / streaming | trace_id is scoped over the whole ASGI call, so both keep it |
| `TraceIDMiddleware` + a malformed/hostile `X-Request-ID` (e.g. containing `\n`) | Rejected — falls back to a minted `uuid7` instead of trusting the header |
| `setup_logging(debug=True, json=True)` | Forces JSON even in debug mode |
| `setup_logging()` called twice | Second call replaces handlers — idempotent (`queue=True` still leaves the prior call's listener thread running — see below) |
| `setup_logging(queue=True)` called repeatedly | Each call starts a new `QueueListener` thread that is never stopped — call once at startup |
| `$FORKTEX_LOG_LEVEL` / `$FORKTEX_LOG_DEBUG` / `$FORKTEX_LOG_JSON` set | Used only when the matching argument is left `None`; an explicit argument always wins |
| `_ContextFilter.filter()` — `record.trace_id` | Always set; may be `None` |
| `@traced` nested inside another `@traced` call | Inner call gets its own fresh `trace_id`; both share the same `root_trace_id` (established by whichever entered first) |
| `@traced` on a function whose module is imported before `setup_logging()` runs | Safe — `logging.getLogger(func.__module__)` is called at decoration time, but handlers/level are resolved at emit time, not `getLogger()` time |
| `@traced` + `queue.task()` order | Either order works; put `@traced` closer to the function so it sees the real call args, not `queue.task()`'s registration side-effects |

### Integration map

```
log ──── replaces ──── any service's bespoke logging_config.py
log ──── (standalone) ── CLI workers, scripts, background processes
log ──── depends on ──── forktex_core.iso (canonical UTC timestamp formatting)
```

### Checklist

- [ ] `setup_logging(service="…")` called exactly once at startup (before any `get_logger()`)
- [ ] `TraceIDMiddleware` added before other middleware so trace_id is set first
- [ ] `async_log_context()` used inside route handlers (not sync `log_context`)
- [ ] `trace_context()`/`async_trace_context()` used to scope a trace_id per job in long-running workers (not bare `set_trace_id()`)
- [ ] `quiet=["noisy.logger"]` passed to silence known-noisy third-party loggers
- [ ] `queue=True` passed if the process is a high-throughput async service (FastAPI) — moves log I/O off the event loop
- [ ] Ops override level/format via `$FORKTEX_LOG_LEVEL`/`$FORKTEX_LOG_DEBUG`/`$FORKTEX_LOG_JSON` rather than a code change
- [ ] `@traced` used on worker job handlers / flow steps for entry/exit/exception logging instead of hand-rolled try/except + timing boilerplate
