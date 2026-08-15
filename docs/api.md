# `forktex_core.api`

A preconfigured FastAPI factory: trace-id propagation, security headers, CORS, the
`AppError` → `ErrorEnvelope` handler, and liveness/readiness endpoints — assembled in the right
order, then handed back for you to extend.

```bash
pip install "forktex-core[api]"
```

Unlike every other optional package, `api` raises at **import time** rather than lazily, because its
middleware subclasses Starlette's `BaseHTTPMiddleware` at class-definition time. The error names the
extra:

```
ImportError: Install 'forktex-core[api]' (fastapi) to use forktex_core.api
```

## Wiring

Shape C — you own the app object; there is no global state.

```python
from forktex_core.api import AppConfig, create_app

app = create_app(
    AppConfig(
        title="Projects API",
        version="1.4.0",
        cors_origins=["https://app.example.com"],
    )
)

app.include_router(projects_router)
```

That is a complete, correctly-layered app. You do **not** need to add the envelope handler, the
trace-id middleware or the security headers yourself.

### Startup and shutdown

Pass your lifespan through `AppConfig` — there is no need to assign `app.router.lifespan_context`
afterwards:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from forktex_core.api import AppConfig, create_app
from forktex_core.database import close_engine, init_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_engine(settings.db_url)
    yield
    await close_engine()


app = create_app(AppConfig(title="Projects API", lifespan=lifespan))
```

### Readiness probes

`/health` is liveness and always returns `ok`. `/health/ready` runs each probe you register and
reports them individually, so you can extend the built-in endpoint rather than hiding it and
mounting your own:

```python
async def db_ready() -> bool:
    async with get_session() as session:
        await session.execute(sa.text("SELECT 1"))
    return True


app = create_app(AppConfig(readiness_probes={"database": db_ready}))
```

A probe that raises and a probe that returns `False` are both reported as not-ready; the raising one
is logged, so the two remain distinguishable.

## Public surface

| Name | Purpose |
|:---|:---|
| `create_app(config)` | Build the configured FastAPI instance |
| `AppConfig` | The configuration model — every switch below |
| `HealthProbe` | The readiness-probe callable type |
| `LivenessResponse` / `ReadinessResponse` | Response models for the health endpoints |
| `SecurityHeadersMiddleware` | `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, CSP, and HSTS outside debug |

`ExceptionEnvelopeMiddleware` is not exported from the package root; reach it at
`forktex_core.api.middleware` if you are adding it to an app you built yourself.

### `AppConfig`

| Field | Default | Effect |
|:---|:---|:---|
| `title` / `version` / `description` | — | Passed to FastAPI and OpenAPI |
| `debug` | `False` | FastAPI debug mode; also relaxes HSTS |
| `lifespan` | `None` | Your startup/shutdown context manager |
| `enable_trace_id` | `True` | Wire `TraceIDMiddleware` |
| `enable_security_headers` | `True` | Wire `SecurityHeadersMiddleware` |
| `enable_exception_handler` | `True` | `AppError` → `ErrorEnvelope` |
| `handle_unexpected` | `True` | Also map uncaught `Exception` to a 500 envelope |
| `cors_origins` | `None` | `None` leaves CORS off entirely |
| `cors_allow_credentials` / `_methods` / `_headers` | `True` / `["*"]` / `["*"]` | CORS detail |
| `readiness_probes` | `{}` | Name → async predicate, run by `/health/ready` |

## Middleware order

`add_middleware` applies in reverse, so the assembled stack is, outermost first:

**security headers · CORS · trace-id · envelope**

The envelope sits *inside* trace-id so the contextvar is still live when the error body is built —
which is what makes the response `traceId`, the `X-Request-ID` header and the log records agree.
Security headers are outermost so they stamp every response, errors and CORS preflights included.

## Errors

`ImportError` at import time without the `[api]` extra. Beyond that the package raises nothing of
its own; it converts your `AppError`s into responses — see [error.md](error.md).

## Gotchas

- `cors_origins=None` means CORS middleware is not added at all, which is not the same as an empty
  allow-list.
- The health routes are registered on the app. To keep them out of a generated SDK, set
  `include_in_schema = False` on them rather than removing them.
- A custom `AppError` subclass with a code outside `AppErrorCode` maps to HTTP 500. See the caveat in
  [error.md](error.md).
