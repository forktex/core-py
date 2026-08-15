# `forktex_core.api`

Level-3 bootstrap. FastAPI factory: an opt-in middleware stack (trace-id,
CORS, security headers), health probes, and `AppError` → `ErrorEnvelope`
mapping (with an optional catch-all for unexpected errors).

## Install

```bash
poetry add "forktex-core[api]"
```

Pulls FastAPI. Mandatory deps are level-0 only (`log` + `error`; `types`
comes transitively via the envelope's `BaseAppModel`). The `[grid]`,
`[space]`, `[database]`, `[cache]`, `[vault]` extras stay opt-in for
consumers that only want the factory shell.

## Purpose

Boilerplate-elimination for consumer API services. `create_app(config)`
returns a preconfigured FastAPI instance. Every capability is opt-in/opt-out
via `AppConfig`, so a service takes only what it needs (e.g. a purely
API-key API leaves `cors_origins` unset and gets no CORS).

Provided: trace-id propagation (`forktex_core.log.TraceIDMiddleware` — one
`X-Request-ID` header, correlated with the logs and the envelope `traceId`),
security headers, the `AppError` → `ErrorEnvelope` handler, an optional
unexpected-error catch-all, opt-in CORS, an ASGI `lifespan` passthrough, and
`/health` + `/health/ready`. Left to the consumer (service-owned): auth,
rate-limiting/idempotency, per-request DB sessions.

## Public API

```python
from forktex_core.api import (
    create_app,
    AppConfig,
    HealthProbe,
    LivenessResponse,  # {"status": "ok"}
    ReadinessResponse,  # {"status": ..., "checks": {name: bool}}
    SecurityHeadersMiddleware,
)
```

`AppConfig` fields: `title`, `version`, `description`, `debug`,
`enable_trace_id`, `enable_security_headers`, `enable_exception_handler`,
`handle_unexpected`, `cors_origins` (list; `None` ⇒ no CORS) +
`cors_allow_credentials`/`cors_allow_methods`/`cors_allow_headers`,
`lifespan` (ASGI passthrough), and `readiness_probes` (dict of name → async
coroutine returning bool).

The error envelope is `{code, message, details, traceId}` — see
[`error.md`](error.md).

## Quick example

```python
from forktex_core.api import AppConfig, create_app
from forktex_core.error import NotFoundError


async def db_ready() -> bool: ...
async def cache_ready() -> bool: ...


app = create_app(
    AppConfig(
        title="Intelligence",
        version="0.1.0",
        cors_origins=["https://app.example.com"],  # omit for an API-key-only service
        readiness_probes={"db": db_ready, "cache": cache_ready},
    )
)


@app.get("/widgets/{widget_id}")
async def get_widget(widget_id: str):
    if widget_id == "missing":
        raise NotFoundError("widget not found", details={"id": widget_id})
    return {"id": widget_id}
```

`/health` returns 200 always; `/health/ready` returns 200 when every probe
returns True, 503 otherwise. Both bodies are `BaseAppModel` models
(`LivenessResponse` / `ReadinessResponse`) rather than hand-built dicts, so the
health endpoints carry the same wire conventions and OpenAPI schema as everything
else.

A probe that **raises** and a probe that **returns False** are both "not ready",
but only one is a bug, so the raising case is logged with `logger.exception` and
the failing probe names are logged on every not-ready response.

## See also

- [`error.md`](error.md) — `AppError` hierarchy mapped automatically to the
  `ErrorEnvelope` wire shape.
- [`worker.md`](worker.md) — symmetric bootstrap for arq workers.
- `examples/api_minimal.py` — runnable factory demo with TestClient.
- `tests/test_api/test_factory.py` — middleware, envelope, trace-id, CORS,
  lifespan, and `.openapi()` regression tests.
```
