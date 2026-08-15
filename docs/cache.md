# forktex_core.cache

Async Redis cache: connection lifecycle, plain get/set/delete, a `@cached` decorator with
optional stale-while-revalidate, namespaced keys, and Pydantic-aware JSON serialisation.

## Install

```bash
pip install forktex-core          # redis[hiredis] is a core dependency
pip install forktex-core[cache]   # same thing — the extra exists for symmetry and declares nothing
```

No optional extra is involved, so there is no `ImportError` path. The only startup failure
is `init()` being unable to reach Redis.

## Wiring

**Shape A — module-level singleton.** One Redis client per process, held in a module global,
created by `init(url)` and torn down by `close()`. There are no named clients.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

import forktex_core.cache as cache


@asynccontextmanager
async def lifespan(app: FastAPI):
    await cache.init(settings.redis_url)
    yield
    await cache.shutdown_background_tasks()
    await cache.close()


app = FastAPI(lifespan=lifespan)
```

The singleton is **per-process**. A worker process must call `init()` in its own startup hook;
it does not inherit the API process's client. Without `init()`, the read/write helpers degrade
to no-ops rather than raising (see Gotchas), so a missing worker-side `init()` is silent.

`shutdown_background_tasks()` awaits any in-flight stale-while-revalidate refresh tasks; call it
before `close()` so a refresh does not write against a closed client.

## Public surface

```python
from forktex_core.cache import (
    CachePrefix,
    available,
    cached,
    close,
    delete,
    deserialize,
    fetch_or_set,
    fetch_swr,
    get,
    get_client,
    init,
    invalidate_key,
    invalidate_prefix,
    key_for,
    serialize,
    set,
    shutdown_background_tasks,
)
```

| Name | Description |
|---|---|
| `init(url)` | Async. Create the client from a Redis URL and `PING` it. |
| `close()` | Async. Close the client and clear the global. Idempotent. |
| `available()` | `True` if the client global is set. |
| `get_client()` | The raw `redis.asyncio.Redis`. Raises if `init()` has not run. |
| `get(key)` | Async. Value or `None`. |
| `set(key, value, ex)` | Async. Write with a TTL in seconds. |
| `delete(key)` | Async. Delete one key. |
| `invalidate_key(key)` | Async. Alias for `delete`. |
| `invalidate_prefix(prefix)` | Async. Delete `prefix` and every `prefix:*` key; returns the count. |
| `cached(...)` | Decorator for async functions — see below. |
| `fetch_or_set(key, ttl, fn, args, kwargs, response_model)` | Async. Cache-aside primitive behind `@cached`. |
| `fetch_swr(key, ttl, stale_ttl, fn, args, kwargs, response_model)` | Async. Stale-while-revalidate primitive. |
| `shutdown_background_tasks()` | Async. Await outstanding SWR refresh tasks. |
| `key_for(prefix, *parts)` | Build `"prefix:part1:part2"`. |
| `CachePrefix` | Empty `StrEnum` base — consumers subclass it with their own prefixes. |
| `serialize(value)` | JSON string; `BaseModel` uses `model_dump_json()`, everything else `json.dumps(default=str)`. |
| `deserialize(data, model)` | Parse JSON; validates into `model` when one is given. |

`cached` signature:

```python
@cached(
    ttl=300,                      # seconds; with stale_ttl set, the refresh threshold instead
    stale_ttl=None,               # set → stale-while-revalidate
    key_builder=None,             # callable(*args, **kwargs) -> str
    response_model=None,          # pydantic model for deserialisation
)
async def get_org(org_id: str) -> OrgResponse: ...
```

Namespaced keys:

```python
from enum import StrEnum

from forktex_core.cache import invalidate_prefix, key_for


class Prefix(StrEnum):
    ORG = "org"


await invalidate_prefix(key_for(Prefix.ORG, org_id))
```

## Errors

| Raised | When | Catch? |
|---|---|---|
| `RuntimeError("Cache initialization failed (host): …")` | `init()` — `PING` failed. The global is reset to `None` first. | At startup only. Let it stop the process unless the cache is genuinely optional. |
| `RuntimeError("Cache not initialized — call init() first")` | `get_client()` before `init()`. | A wiring bug; do not catch. |
| `ValueError` | `key_for(prefix, ...)` with a `None` part. | A wiring bug; do not catch. |

Everything in `ops` (`get`/`set`/`delete`/`invalidate_prefix` and the two `fetch_*` primitives)
catches Redis exceptions, logs them via `logger.exception`, and returns a neutral value. Consumers
do not need `try/except` around ordinary cache reads and writes.

The URL is masked in logs: only the portion after `@` is printed.

## Gotchas

- **Failures are swallowed by design.** If `init()` never ran, or Redis dies mid-request, `get`
  returns `None`, `set`/`delete` no-op, and `invalidate_prefix` returns `0`. A misconfigured
  cache looks exactly like a 100% miss rate — check `available()` at startup if you need certainty.
- **`invalidate_prefix("")` scans every key** in the database and deletes all of them. Non-empty
  prefixes match on the `:` delimiter, so `invalidate_prefix("user")` hits `"user"` and `"user:123"`
  but never `"username:foo"`.
- **`key_for` rejects `None` parts** rather than dropping them — a dropped part would collapse a
  per-entity key onto the bare, shared `prefix` key. Empty-string parts *are* dropped.
- **`stale_ttl=0` still selects SWR.** Only `stale_ttl=None` picks plain `fetch_or_set`; `0` is a
  valid TTL and refreshes on every read.
- **The default key builder hashes `f"{module}.{name}:{args}:{kwargs}"`** with SHA-256. Argument
  values must have a stable `repr` or the key changes between equivalent calls; pass `key_builder`
  for anything non-trivial.
- **SWR refresh is Redis-locked** under `lock:<key>` with a 10-second TTL, so only one process
  refreshes at a time — but the refresh task itself is a bare `asyncio.Task`. Call
  `shutdown_background_tasks()` before `close()`.
- **`set` requires `ex`** — there is no way to write a key without a TTL through this module.
- **`cache.set` shadows the builtin** when imported bare; prefer `import forktex_core.cache as cache`.
