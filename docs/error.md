# `forktex_core.error` — App error hierarchy + wire envelope

> One exception hierarchy, one wire shape. Every service boundary (HTTP handler, CLI top-level, worker dead-letter writer) turns an `AppError` subclass into the same `ErrorEnvelope` JSON — consumers see one error contract regardless of transport.

## Overview

`error` is a Level-0 primitive — always pulled, no extra to install, zero third-party dependency beyond `pydantic` (already a core dependency via `forktex_core.types`). `AppError` carries only a `code` for consumers to switch on — it has no notion of HTTP or any other transport. `to_envelope()` projects any `AppError` onto the tiny `{code, message, details, traceId}` wire shape. A transport that needs an HTTP status (or any other transport-specific signal) owns its own mapping from `code` — see [`api.md`](api.md) for the one built-in example.

## Quick start

```python
from forktex_core.error import NotFoundError, to_envelope
from forktex_core.log import get_trace_id

try:
    raise NotFoundError("user not found", details={"user_id": user_id})
except NotFoundError as exc:
    envelope = to_envelope(exc, trace_id=get_trace_id())
    # envelope.model_dump(by_alias=True) ==
    #   {"code": "not_found", "message": "user not found",
    #    "details": {"user_id": "..."}, "traceId": "..."}
    return JSONResponse(status_code=http_status_for(exc), content=envelope.model_dump(by_alias=True))
```

The `[api]` extra wires an equivalent handler automatically — see [`api.md`](api.md) and the "Interaction with other primitives" section below.

## API reference

```python
class AppErrorCode(StrEnum):
    # Core's generic, cross-cutting vocabulary — services reuse these and
    # add their own domain codes as needed (the wire code is an open str).
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    BAD_REQUEST = "bad_request"
    VALIDATION = "validation"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"

class AppError(Exception):
    code: AppErrorCode = AppErrorCode.INTERNAL      # the only class attribute — no HTTP status here
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None: ...
    # .message and .details are always set (details defaults to {}, never None)

class NotFoundError(AppError):            code = NOT_FOUND
class AlreadyExistsError(AppError):       code = ALREADY_EXISTS
class BadRequestError(AppError):          code = BAD_REQUEST
class UnprocessableEntityError(AppError): code = VALIDATION
class UnauthorizedError(AppError):        code = UNAUTHORIZED
class ForbiddenError(AppError):           code = FORBIDDEN
class ConflictError(AppError):            code = CONFLICT
class TooManyRequestsError(AppError):     code = RATE_LIMITED
class ServiceUnavailableError(AppError):  code = UNAVAILABLE
# AlreadyExistsError and ConflictError, and BadRequestError/UnprocessableEntityError,
# are each semantically distinct pairs that an HTTP transport happens to map to the
# same or adjacent status (409/409, 400/422) — `code` is the only distinguishing
# signal on the exception itself; AppError carries no HTTP status at all (see
# "Interaction with other primitives" below for where a transport owns that mapping).

class ErrorEnvelope(BaseAppModel):
    code: str                          # open str, not the closed enum — a service's
                                        # own error code round-trips through the same envelope
    message: str
    details: dict[str, Any] = {}       # non-JSON-serializable values fall back to str()
                                        # in model_dump(mode="json") — never crashes the error path
    trace_id: str | None = None        # wire alias `traceId`

def to_envelope(error: AppError, *, trace_id: str | None = None) -> ErrorEnvelope
    # Pure projection — reads error.code/.message/.details, doesn't touch
    # __cause__/__context__ (exception chaining on the original error survives untouched).
```

`AppErrorCode` is core's **generic** vocabulary — auth/domain-specific codes (e.g. `invalid_credentials`, `email_already_exists`) are service-owned and pass straight through `ErrorEnvelope.code`'s open `str` type.

**`BadRequestError` (400) vs. `UnprocessableEntityError` (422)**: 400 means the request itself is malformed or missing required input; 422 means the input is well-formed but fails a semantic/business-rule check (e.g. an end date before a start date). Not every API distinguishes these, but when a client needs to tell "you sent garbage" apart from "you sent something valid-shaped but wrong," this is the pair to use.

## Patterns

### Pattern 1 — Raise with structured details

```python
from forktex_core.error import BadRequestError

if not is_valid_email(email):
    raise BadRequestError("invalid email format", details={"field": "email", "value": email})
```

### Pattern 1b — Define a service-specific `AppError` subclass

There's no separate registration mechanism — a service subclasses `AppError` directly and sets its own `code` as a plain string. It round-trips through `ErrorEnvelope.code`'s open `str` type exactly like a core code does:

```python
from forktex_core.error import AppError


class WidgetLockedError(AppError):
    code = "widget_locked"  # a string, not an AppErrorCode member — that's fine, code is open


raise WidgetLockedError("widget is checked out by another user", details={"widget_id": "w-1"})
# to_envelope(...) → {"code": "widget_locked", "message": "...", "details": {...}}
```

### Pattern 2 — Catch multiple AppError subtypes at a boundary

```python
from forktex_core.error import AppError, to_envelope

try:
    result = await do_work()
except AppError as exc:  # catches any subclass — NotFoundError, ConflictError, etc.
    envelope = to_envelope(exc, trace_id=get_trace_id())
    log.warning("request failed", extra={"code": envelope.code})
    return envelope
```

### Pattern 3 — Convert a lower-level exception at a DB/infra boundary

The reference pattern for "translate a low-level failure into a typed `AppError` right at the boundary where you have enough context to know which one" — from `grid/_kernel/integrity.py`:

```python
from contextlib import asynccontextmanager
from sqlalchemy.exc import IntegrityError
from forktex_core.error import AlreadyExistsError, BadRequestError


@asynccontextmanager
async def integrity_boundary():
    try:
        yield
    except IntegrityError as exc:
        detail = str(getattr(exc, "orig", None) or exc).lower()
        if "unique" in detail or "duplicate key" in detail:
            raise AlreadyExistsError("resource already exists") from exc
        raise BadRequestError("write violates a database constraint") from exc


# usage:
async with integrity_boundary():
    session.add(obj)
    await session.flush()
```

Without this, a duplicate-key violation surfaces as a raw 500 `IntegrityError` instead of a clean 409. `database.crud.create()` uses the same idea directly (`forktex_core.error.ConflictError` on `IntegrityError`).

### Pattern 4 — CLI / worker usage outside HTTP

`AppError`/`to_envelope` aren't HTTP-specific — there's no HTTP-only field to ignore; a non-HTTP transport just reads `code`/`message`/`details` like any other:

```python
from forktex_core.error import AppError, to_envelope


async def run_cli_command() -> int:
    try:
        await do_work()
    except AppError as exc:
        envelope = to_envelope(exc)
        print(f"error [{envelope.code}]: {envelope.message}", file=sys.stderr)
        return 1
    return 0
```

## Interaction with other primitives

- **`error` → `types`**: `ErrorEnvelope` is a `BaseAppModel`, so it gets snake↔camel wire aliasing for free (`trace_id` ⇄ `traceId`) — no duplicated alias logic.
- **`error` + `log`**: `to_envelope(exc, trace_id=get_trace_id())` is the standard pairing — the envelope's `traceId` matches whatever `forktex_core.log`'s `TraceIDMiddleware`/`trace_context()` set for the request/job, so a client-reported error code correlates directly with server log lines. `api/middleware.py`'s `ExceptionEnvelopeMiddleware` does exactly this: it runs *inside* `TraceIDMiddleware` so the trace_id contextvar is active when the envelope is built.
- **`error` + `api`**: `create_app(...)` (see [`api.md`](api.md)) registers `ExceptionEnvelopeMiddleware` automatically. `AppError` itself has no notion of HTTP status — `api.middleware._HTTP_STATUS_BY_CODE` is the one place in the repo that maps `AppErrorCode` to an HTTP status, applied via `_http_status_for(exc)`; a service's own custom `code` (not in that table) falls back to 500. Any other unhandled exception is logged (via `forktex_core.log.get_logger`) and returned as a generic 500 envelope rather than leaking internals.

## Known inconsistency (not fixed here — documented as a backlog item)

Several other `forktex_core` modules raise bare `ValueError`/`RuntimeError`/`KeyError`, or maintain their own independent error hierarchy, instead of an `AppError` subclass — e.g. `flow`'s ~25 raise sites (workflow/run "not found" errors that look like `NotFoundError` candidates), `queue`/`cache`/`database.connection`'s "not initialized" guards (bare `RuntimeError`, identical pattern repeated three times), `graph`'s `KeyError` for unknown node ids, and `vector`/`storage`'s own independent exception classes (`vector/errors.py`, `storage.ObjectNotFoundError`). This is real, but migrating raise sites across six-plus modules changes what exception types consumers catch — a separate, larger, more carefully-reviewed initiative than finalizing this primitive, so it's named here rather than silently left for someone to rediscover.

## Anti-patterns

```python
# ❌ Bare ValueError/RuntimeError where an AppError subclass fits — consumers
# can't catch a stable typed error, and it never reaches a `code`.
if not row:
    raise ValueError("not found")
# ✅
if not row:
    raise NotFoundError("row not found", details={"id": str(row_id)})

# ❌ Conflating AlreadyExistsError and ConflictError because an HTTP transport
# happens to map both to 409 — they mean different things (resource already
# exists vs. a write conflicts with current state) and a client branches on
# `code`, which is the only thing AppError itself carries.

# ❌ Putting sensitive data in `details` — it serializes straight into the wire
# envelope. Never put raw credentials, tokens, or PII there.
raise UnauthorizedError("bad token", details={"token": raw_token})  # ❌ leaks the token
raise UnauthorizedError("bad token")  # ✅
```

## Agent guide

### JSON output fields (exact keys)

```json
{
  "code": "not_found",
  "message": "user not found",
  "details": {"user_id": "abc"},
  "traceId": "req-abc-123"
}
```

- `code` — open `str`; core producers pass `AppErrorCode` members (they're strings), services may pass their own domain codes.
- `details` — always a dict, never `None` (defaults to `{}` on construction).
- `traceId` — only present when `to_envelope(..., trace_id=...)` was given one.
- HTTP status is **not** in the envelope, and not on `AppError` either — `forktex_core.api.middleware._http_status_for(exc)` is the one place in the repo that derives an HTTP status from `code`, read separately from the envelope.

### Edge cases

| Scenario | Behaviour |
|---|---|
| `AlreadyExistsError` vs `ConflictError` | Both map to HTTP 409 in `api`'s mapping — distinguish by `code` (`already_exists` vs `conflict`); `AppError` itself has no status concept to fall back on |
| `BadRequestError` vs `UnprocessableEntityError` | Malformed/missing input vs. well-formed but semantically invalid — distinguish by `code` (`bad_request` vs `validation`) |
| Bare `AppError("msg")` | `code=internal` — the default/unexpected-failure case; `api`'s mapping sends this to HTTP 500 |
| `TooManyRequestsError` | Caller is rate-limited; raise it yourself when your own rate limiter trips, not for a 3rd-party's 429 (wrap that separately). `api` maps this to HTTP 429 |
| `ServiceUnavailableError` | The service is deliberately refusing requests (e.g. maintenance window, an open circuit-breaker), distinct from the bare `AppError` default (an unexpected failure). `api` maps this to HTTP 503 |
| `raise Y(...) from exc` | `__cause__` is preserved through to `to_envelope()` — the envelope only reads `code`/`message`/`details`, it doesn't touch exception chaining |
| Non-ASCII / unicode message | Passes straight through — no encoding special-casing needed, Pydantic handles it |
| `details` contains a value Pydantic can't serialize (e.g. a raw exception, a custom object) | `model_dump(mode="json")` falls back to `str(value)` for that key instead of raising `PydanticSerializationError` — the error-reporting path itself never crashes. `model_dump()` (no `mode="json"`) passes the raw value through unchanged |
| `ErrorEnvelope.model_validate({...})` missing `code`/`message` | Raises `pydantic.ValidationError`, same as any Pydantic model |
| A service's own error code (e.g. `"email_already_exists"`) | Round-trips through `ErrorEnvelope.code` unchanged — it's an open `str`, not the closed `AppErrorCode` enum. In `api`, a code outside `_HTTP_STATUS_BY_CODE` falls back to HTTP 500 |
| A service's own `AppError` subclass (e.g. `WidgetLockedError`) | No registration needed — subclass `AppError`, set `code` as a plain string (see Pattern 1b); `to_envelope()` works on it identically to a core subclass |

### Integration map

```
error ──── used by ──── api (ExceptionEnvelopeMiddleware owns its own AppErrorCode→HTTP mapping), grid, space, database.crud
error ──── pairs with ── log (to_envelope(exc, trace_id=get_trace_id()))
error ──── built on ──── types (ErrorEnvelope is a BaseAppModel)
error ──── (no deps on) ── any other forktex_core module
```

### Checklist

- [ ] Raise the most specific `AppError` subclass available, not a bare `ValueError`/`RuntimeError`
- [ ] Pass `details` for machine-actionable context, never secrets/PII
- [ ] Pair `to_envelope()` with `get_trace_id()` so client-reported errors correlate with server logs
- [ ] Use `raise X(...) from exc` to preserve the original cause when converting a lower-level exception
- [ ] At a DB/infra boundary, convert as close to the source as possible (see Pattern 3), not deep in a generic handler
- [ ] For malformed-vs-semantically-invalid input, use `BadRequestError` (400) vs `UnprocessableEntityError` (422) rather than reaching for one for both
- [ ] For a domain-specific failure with no fitting core subclass, subclass `AppError` directly with your own string `code` (Pattern 1b) instead of a bare exception
