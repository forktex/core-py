# `forktex_core.store` — MongoDB document store

> Thin async MongoDB connector — insert, find, update, delete, count. Multi-database services register named clients; single-database services use module-level functions.

## Overview

`store` holds schemaless BSON/JSON documents with query support — distinct from `forktex_core.storage`, which holds opaque binary blobs with no query capability at all. There's no schema validation and no aggregation-pipeline builder; the module is a pure connector, matching `forktex_core.database`'s "no path conventions, no content negotiation" discipline.

Every document's `_id` is always a plain string at the API boundary. If you pass `id=` to `insert_one()`, that exact string is stored. Otherwise MongoDB assigns a real `ObjectId` (the ordinary, idiomatic default) — returned as its string form. Filtering by `_id` works transparently either way: a string that looks like valid `ObjectId` hex (24 hex chars) is converted back to an `ObjectId` before querying, since a string filter never matches a document whose `_id` is actually a BSON `ObjectId` (different BSON types, even when they print identically) — everything else is left as a literal string. See the edge-case table for the one narrow ambiguity this can't resolve.

Multi-document transactions are supported via `transaction()` — see Pattern 4. They require the MongoDB deployment to be a replica set or sharded cluster; a standalone `mongod` doesn't support them at all.

Unlike `storage`'s per-call S3 clients, `StoreClient` holds one persistent `AsyncMongoClient` for its lifetime — `pymongo`'s async client is designed to be constructed once and reused; it manages its own internal connection pool.

```bash
pip install forktex-core[store]   # pymongo
```

## Quick start

```python
import forktex_core.store as store

await store.init(url="mongodb://localhost:27017", database="app")

doc_id = await store.insert_one("events", {"kind": "signup", "user_id": "u-1"})
event = await store.find_one("events", {"_id": doc_id})
recent = await store.find("events", {"kind": "signup"}, limit=50)
await store.close()
```

## API reference

```python
# --- Single-database (module-level, "default" client) ---
async def init(url, database) -> None
async def close(name="default") -> None
async def insert_one(collection, document, *, id=None, session=None) -> str   # returns string _id
async def find_one(collection, filter, *, session=None) -> dict | None
async def find(collection, filter=None, *, limit=100, session=None) -> list[dict]
async def update_one(collection, filter, update, *, upsert=False, session=None) -> bool
async def delete_one(collection, filter, *, session=None) -> bool
async def count(collection, filter=None, *, session=None) -> int
def transaction() -> AbstractAsyncContextManager[AsyncClientSession]

# --- Multi-database (named clients) ---
def register(name, url, database) -> StoreClient
def get_client(name="default") -> StoreClient       # raises ClientNotRegisteredError
def deregister(name="default") -> StoreClient | None

# --- StoreClient (per-database) ---
class StoreClient:
    async def insert_one(collection, document, *, id=None, session=None) -> str
    async def find_one(collection, filter, *, session=None) -> dict | None
    async def find(collection, filter=None, *, limit=100, session=None) -> list[dict]
    async def update_one(collection, filter, update, *, upsert=False, session=None) -> bool
    async def delete_one(collection, filter, *, session=None) -> bool
    async def count(collection, filter=None, *, session=None) -> int
    def transaction() -> AbstractAsyncContextManager[AsyncClientSession]
        # async with client.transaction() as session: ... — commits on clean
        # exit, aborts on exception. Requires a replica set / sharded cluster.
    async def close() -> None

# --- Config ---
@dataclass
class StoreConfig:
    url: str; database: str
```

## Patterns

### Pattern 1 — Multi-database service

```python
from forktex_core.store import register, get_client

# At startup
register("analytics", url=mongo_url, database="analytics")
register("audit", url=mongo_url, database="audit")

# Usage — caller picks the right database
await get_client("audit").insert_one("log", {"actor": "u-1", "action": "delete"})
```

### Pattern 2 — Upsert instead of read-then-write

```python
# ❌ Race-prone: another writer can insert between the read and the write
existing = await store.find_one("counters", {"_id": "daily-signups"})
if existing is None:
    await store.insert_one("counters", {"_id": "daily-signups", "count": 1})
else:
    await store.update_one("counters", {"_id": "daily-signups"}, {"count": existing["count"] + 1})

# ✅ update_one(upsert=True) is atomic at the document level
await store.update_one("counters", {"_id": "daily-signups"}, {"count": 1}, upsert=True)
```

### Pattern 3 — Caller-supplied ids for natural keys

```python
# Use a natural/business key as the id when one exists, instead of a
# generated ObjectId — avoids a separate unique index for lookups.
await store.insert_one("webhooks", {"url": "https://...", "active": True}, id=f"org-{org_id}")
webhook = await store.find_one("webhooks", {"_id": f"org-{org_id}"})
```

### Pattern 4 — Atomic multi-document transaction

```python
# Insert an order and decrement stock as one atomic unit — either both
# happen or neither does, even across two different collections.
async with store.transaction() as session:
    order_id = await store.insert_one(
        "orders", {"sku": sku, "qty": qty}, session=session
    )
    await store.update_one(
        "inventory", {"sku": sku}, {"stock": new_stock}, session=session
    )
# Raising inside the block aborts the whole transaction — nothing is persisted.
```

`session=` must be passed to every operation that should participate in the transaction — an operation without it runs outside the transaction and won't see its uncommitted writes (ordinary Mongo read-isolation, not a `store`-specific quirk).

## Anti-patterns

```python
# ❌ A custom caller-supplied id that happens to be valid ObjectId hex —
# gets coerced back to ObjectId on lookup, mismatching the literal string
# it was actually stored as (see the edge-case table)
await store.insert_one("events", {...}, id="507f1f77bcf86cd799439011")

# ✅ Use a caller-supplied id that can't be confused with ObjectId hex
# (e.g. a UUID, or a prefixed business key), or just let Mongo assign one
doc_id = await store.insert_one("events", {...})
await store.find_one("events", {"_id": doc_id})

# ❌ Forgetting session= inside a transaction — the write happens outside it
async with store.transaction() as session:
    await store.insert_one("orders", order_doc)  # no session= — not atomic with the rest!
    await store.update_one("inventory", {...}, {...}, session=session)

# ✅ Pass session= to every operation that must be part of the transaction
async with store.transaction() as session:
    await store.insert_one("orders", order_doc, session=session)
    await store.update_one("inventory", {...}, {...}, session=session)

# ❌ Using store for opaque binary blobs
await store.insert_one("uploads", {"data": pdf_bytes})  # BSON document size limit is 16MB,
                                                          # and it isn't queryable content anyway

# ✅ Use storage for blobs; store for the document/metadata that references one
key = await storage_upload(pdf_bytes)
await store.insert_one("documents", {"title": "Invoice", "blob_key": key})

# ❌ Unbounded find() on a large collection
all_events = await store.find("events")  # limit defaults to 100, but a filter-less
                                           # find() on a big collection is still a smell

# ✅ Always filter, and treat count() separately from find() for totals
matching = await store.find("events", {"kind": "signup"}, limit=50)
total = await store.count("events", {"kind": "signup"})
```

---

## Agent guide

### Canonical forms

**Startup registration (multi-database):**
```python
from forktex_core.store import register

register(
    "audit",
    url=settings.mongo_url,
    database=settings.mongo_audit_db,
)
```

**Insert → find round-trip:**
```python
doc_id = await store.insert_one("events", {"kind": "signup", "user_id": user_id})
# doc_id is always a plain string (a real ObjectId's string form here, since
# no id= was given) — safe to store as a foreign-key-style reference in a
# Postgres row, log line, or API response
event = await store.find_one("events", {"_id": doc_id})
```

**Atomic transaction:**
```python
async with store.transaction() as session:
    order_id = await store.insert_one("orders", order_doc, session=session)
    await store.update_one("inventory", {"sku": sku}, {"stock": new_stock}, session=session)
# commits on clean exit; an exception anywhere in the block aborts all of it
```

### Edge cases

| Scenario | Behaviour |
|---|---|
| `insert_one(..., id=None)` | MongoDB assigns a real `ObjectId`, returned as its string form |
| `insert_one(..., id="existing-id")` | Raises `pymongo.errors.DuplicateKeyError` if that `_id` already exists in the collection |
| Filtering by a string that's valid 24-hex-char `ObjectId` form | Converted back to `ObjectId` before querying — matches auto-assigned ids correctly |
| A caller-supplied custom `id` that *also* happens to be valid `ObjectId` hex | Gets the same conversion on lookup, which mismatches the literal string it was stored as — avoid raw 24-hex-char custom ids if this matters |
| `find_one(missing_filter)` | Returns `None` — not an error |
| `find(missing_filter)` | Returns `[]` |
| `update_one(missing_filter)` | Returns `False` (no-op) unless `upsert=True`, which inserts and returns `True` |
| `delete_one(missing_filter)` | Returns `False` — idempotent, not an error |
| `get_client("unregistered")` | `ClientNotRegisteredError` with list of registered names |
| `find(filter, limit=N)` | Fetches at most `N` documents — use `count()` separately for a total |
| `transaction()` against a standalone (non-replica-set) deployment | Raises `pymongo.errors.OperationFailure` immediately |
| An operation inside a transaction called without `session=` | Runs outside the transaction — doesn't see its uncommitted writes, and isn't rolled back with it |

### Error catalogue

| Error | When |
|---|---|
| `ImportError("Install 'forktex-core[store]' (pymongo) to use forktex_core.store")` | `pymongo` not installed |
| `ClientNotRegisteredError` | `get_client(name)` — name was never `register()`ed |
| `pymongo.errors.DuplicateKeyError` | `insert_one()` with an `id` that already exists in the collection |
| `pymongo.errors.OperationFailure` | `transaction()` against a deployment that isn't a replica set / sharded cluster |
| `pymongo.errors.PyMongoError` (and subclasses) | Any other MongoDB/network error — not wrapped by this module |

### Integration map

```
store ──── (no dep on) ─── database, cache, queue, vector, storage, vault, graph
```

### Checklist

- [ ] `register()`/`init()` called at startup before any route handles requests
- [ ] Ids treated as opaque strings everywhere — never assume/parse a MongoDB `ObjectId` shape
- [ ] Custom caller-supplied ids avoid raw 24-hex-char strings (ambiguous with `ObjectId` form)
- [ ] `update_one(..., upsert=True)` used for atomic "create or update" instead of read-then-write
- [ ] `session=` passed to every operation that must be atomic with the rest of a `transaction()` block
- [ ] `storage` used for binary blobs; `store` only for queryable document/metadata records
- [ ] `find()` always called with a `filter` and a sane `limit` for anything beyond small collections
