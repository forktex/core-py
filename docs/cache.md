# `forktex_core.cache` — Redis async cache

> Async Redis client with `@cached` decorator, namespaced keys, and stale-while-revalidate.

## Overview

`cache` wraps `redis.asyncio` with a consistent interface — connection lifecycle, transparent JSON serialization, and cache-aside + stale-while-revalidate patterns. Always bundled (no extras).

```bash
pip install forktex-core   # cache always included (redis[hiredis])
```

## Quick start

```python
from forktex_core.cache import init, close, cached, key_for, get, set, delete

await init("redis://localhost:6379/0")


@cached(ttl=300)
async def get_org(org_id: str) -> dict:
    return await db.fetch_org(org_id)


# Direct ops
await set("counter", "42", ex=60)
val = await get("counter")  # "42"
await delete("counter")

await close()
```

## API reference

```python
# Connection
async def init(url: str) -> None          # raises RuntimeError on ping failure
async def close() -> None
def available() -> bool
def get_client() -> redis.Redis

# Ops
async def get(key: str) -> str | None
async def set(key: str, value: str, ex: int) -> None
async def delete(key: str) -> None
async def invalidate_key(key: str) -> None   # alias for delete
async def invalidate_prefix(prefix: str) -> int   # returns count deleted

# Decorator
def cached(
    *,
    ttl: int = 60,
    stale_ttl: int | None = None,      # if set → stale-while-revalidate
    key_builder: Callable | None = None,
    response_model: type[BaseModel] | None = None,
)

# Namespacing
def key_for(prefix: str | CachePrefix, *parts: object) -> str
    # key_for("user", user_id) → "user:abc-123"
    # raises ValueError if any part is None (see Edge cases)

class CachePrefix(StrEnum): pass   # extend to define your own prefixes

# Serialization
def serialize(value: object) -> str      # JSON, handles Pydantic models
def deserialize(data: str, model: type[T] | None) -> object
```

## Patterns

### Pattern 1 — @cached with Pydantic model

```python
@cached(ttl=300, response_model=OrgResponse)
async def get_org(org_id: str) -> OrgResponse:
    return await fetch_from_db(org_id)


# Cache key derived from function name + args automatically
org = await get_org("org-abc")  # first call hits DB
org = await get_org("org-abc")  # second call hits cache
```

### Pattern 2 — Stale-while-revalidate

```python
@cached(ttl=60, stale_ttl=300)
async def get_feed(org_id: str) -> list[dict]:
    return await expensive_aggregation(org_id)


# Returns cached value immediately (even if up to 5 min stale)
# Triggers background refresh if age > 60s
```

### Pattern 3 — Namespace-scoped invalidation

```python
from enum import StrEnum
from forktex_core.cache import key_for, invalidate_prefix


class Prefix(StrEnum):
    ORG = "org"
    FEED = "feed"


await set(key_for(Prefix.ORG, org_id, "profile"), data, ex=300)
await set(key_for(Prefix.ORG, org_id, "members"), data, ex=300)

# Invalidate everything for this org
deleted = await invalidate_prefix(key_for(Prefix.ORG, org_id))
```

## Anti-patterns

```python
# ❌ Swallowing init failure silently
try:
    await init(url)
except Exception:
    pass  # cache disabled — silent

# ✅ Let it raise; handle at startup
await init(url)  # raises RuntimeError if Redis unreachable

# ❌ Using mutable default as ContextVar default
_extra = ContextVar("x", default={})  # shared reference across coroutines

# ✅ None default + factory
_extra = ContextVar("x", default=None)


def get_x():
    return _extra.get(None) or {}
```

---

## Agent guide

### Canonical forms

```python
# Service startup
async def lifespan(app):
    await cache.init(settings.redis_url)
    yield
    await cache.close()


# Route handler with cache decorator
@cached(ttl=60, stale_ttl=300, response_model=MyResponse)
async def get_resource(resource_id: str) -> MyResponse:
    return await db_fetch(resource_id)


# Manual cache with namespace
key = key_for("org", org_id, "profile")
cached_val = await get(key)
if cached_val is None:
    val = await compute()
    await set(key, serialize(val), ex=300)
```

### Edge cases

| Scenario | Behaviour |
|---|---|
| `init()` — Redis unreachable | `RuntimeError("Cache initialization failed (host:port): …")` |
| `get(missing_key)` | Returns `None` |
| `invalidate_prefix("user")` | Deletes `"user"` and `"user:*"` — matches on the `:` delimiter, so `"username:foo"` is untouched |
| `invalidate_prefix("")` | Scans ALL keys — use with care |
| `key_for(prefix, None)` | Raises `ValueError` — a `None` part almost always means an unresolved upstream ID; silently dropping it would collapse the key onto the shared, unscoped `prefix` key |
| `cached(stale_ttl=0)` | Still uses stale-while-revalidate (refreshes on every read) — `0` is a valid TTL, not "unset"; only `stale_ttl=None` selects plain `fetch_or_set` |
| `@cached` with mutable args | Default key builder uses `repr(args)` — works but may be slow for large args |
| `_safe_client()` — init not called | Returns `None`; `ops` functions silently no-op (`set`/`delete`) or return `None`/`0` (`get`/`invalidate_prefix`) instead of raising |
| Any `ops.*` call — Redis raises mid-request | Exception is logged (`logger.exception`) and swallowed; caller sees `None`/`0`/no-op, not the underlying error |

### Error catalogue

| Error | When |
|---|---|
| `RuntimeError("Cache initialization failed…")` | `init()` — Redis not reachable |
| `RuntimeError("Cache not initialized")` | `get_client()` before `init()` |
| `ValueError` | `key_for(prefix, ...)` — one of the variadic parts is `None` |

### Integration map

```
cache ──── (used by) ──── queue   [Redis is the queue backend]
cache ──── (standalone) ── any service needing Redis caching
```
