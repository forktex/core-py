# `forktex_core.error`

The shared error vocabulary: an `AppError` hierarchy, a stable `AppErrorCode` enum, and the
`ErrorEnvelope` wire shape every ForkTex service returns on failure.

Always bundled — no extra required.

```bash
pip install forktex-core
```

`AppError` deliberately has **no notion of HTTP**. The status mapping lives in
`forktex_core.api`, the one layer that needs it, so a worker or CLI can raise and catch the same
errors without importing a web framework.

## Wiring

Shape C — plain classes and functions, no global state.

Raise the typed error from anywhere in your service:

```python
from forktex_core.error import NotFoundError

async def get_project(session, project_id):
    project = await session.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"project {project_id} not found")
    return project
```

Then let the boundary turn it into a response. If you build your app with `create_app`, this is
already wired — see [api.md](api.md):

```python
from forktex_core.api import AppConfig, create_app

app = create_app(AppConfig(title="Projects"))   # AppError → ErrorEnvelope, automatically
```

If you construct `FastAPI()` directly you get none of it, which is the usual reason services end up
hand-writing their own envelope. To adopt it without moving to `create_app`, add the middleware:

```python
from fastapi import FastAPI

from forktex_core.api.middleware import ExceptionEnvelopeMiddleware

app = FastAPI()
app.add_middleware(ExceptionEnvelopeMiddleware, handle_unexpected=True)
```

## Public surface

### The hierarchy

`AppError` is the base; catching it covers every error below, including the ones `database` and
other packages raise.

| Class | Code | HTTP |
|:---|:---|:---|
| `BadRequestError` | `bad_request` | 400 |
| `UnauthorizedError` | `unauthorized` | 401 |
| `ForbiddenError` | `forbidden` | 403 |
| `NotFoundError` | `not_found` | 404 |
| `ConflictError` | `conflict` | 409 |
| `AlreadyExistsError` | `already_exists` | 409 |
| `UnprocessableEntityError` | `validation` | 422 |
| `TooManyRequestsError` | `rate_limited` | 429 |
| `ServiceUnavailableError` | `unavailable` | 503 |

### `AppErrorCode`

The stable published vocabulary: `not_found`, `already_exists`, `bad_request`, `validation`,
`unauthorized`, `forbidden`, `conflict`, `rate_limited`, `unavailable`, `timeout`, `cancelled`,
`failed`, `internal`.

### `ErrorEnvelope` and `to_envelope`

`ErrorEnvelope` is the response body: an error code, a human-readable message, and an optional
trace id. `to_envelope(error)` builds one from any `AppError`.

## Extending with your own codes

`AppError.code` is an open `str`, not a closed enum, so a service can raise a domain-specific code
and keep the envelope:

```python
from forktex_core.error import AppError


class InsufficientCreditError(AppError):
    code = "insufficient_credit"
```

> **Caveat that bites.** `_http_status_for` maps a code it does not recognise to **500**. A custom
> code therefore surfaces as an Internal Server Error unless you also give it a status. Either
> subclass an existing error whose code is in the vocabulary (`BadRequestError` for a 400-shaped
> failure), or handle the custom class explicitly at your boundary.
>
> The same trap catches services that bridge a *separate* error hierarchy onto this one by string
> matching: `not_found` and `resource_not_found` are different codes, and the mismatch falls through
> to 500. Prefer raising `forktex_core.error` types directly to translating between vocabularies.

## Errors

This package defines errors rather than raising them. `to_envelope` accepts any `AppError`;
passing something else is a programming error.

## Gotchas

- Migrating from 2.x: `common.errors.AppError` carried a `.status_code` attribute. `error.AppError`
  does not. Code reading `exc.status_code` now gets `None` and falls back to 500 — see
  [migration-2.x-to-0.1.md](migration-2.x-to-0.1.md).
- `ConflictError` is re-exported by `forktex_core.database.crud`; it is the same class, so one
  handler catches both import paths.
- The trace id on the envelope comes from `forktex_core.log`'s contextvar, which is set by
  `TraceIDMiddleware`. Without that middleware the field is empty.
