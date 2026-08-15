# forktex_core.store

Thin async MongoDB connector for schemaless documents: insert, find, update, delete, count and
multi-document transactions. Distinct from `forktex_core.storage`, which holds opaque binary blobs
with no query capability.

## Install

```bash
pip install forktex-core[store]   # pymongo
```

The `pymongo` import is deferred to `_make_client`, called from `StoreClient.__init__`, which
`register()` (and therefore `init()`) calls. A missing extra therefore raises **at
`register()`/`init()`**, not at `import forktex_core.store`:

```
ImportError: Install 'forktex-core[store]' (pymongo) to use forktex_core.store
```

`bson.ObjectId` is imported inside `_to_query_id`, i.e. on the first `_id`-bearing filter.

## Wiring

**Shape B — named-client registry.** `register(name, url, database)` builds a `StoreClient` and
stores it in a module dict; `get_client(name)` fetches it; `deregister(name)` drops it without
closing. `init(url, database)` is `register("default", ...)`, and every module-level operation
(`insert_one`, `find`, `transaction`, …) runs against `get_client()` — the `"default"` client.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

import forktex_core.store as store


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.register("audit", url=settings.mongo_url, database=settings.mongo_audit_db)
    await store.init(settings.mongo_url, settings.mongo_app_db)
    yield
    await store.close("audit")
    await store.close()


app = FastAPI(lifespan=lifespan)
```

The registry is a plain module-level dict and therefore **per-process**. A worker process must run
its own `register()`/`init()` at startup; it does not inherit the API process's clients. Unlike
`storage`, each `StoreClient` holds a persistent `AsyncMongoClient` with its own connection pool,
so a per-process registration is a real connection pool per process.

`register()` is idempotent by name — re-registering replaces the previous client, and the replaced
client is **not** closed for you.

## Public surface

```python
from forktex_core.store import (
    ClientNotRegisteredError,
    StoreClient,
    StoreConfig,
    StoreError,
    close,
    count,
    delete_one,
    find,
    find_one,
    get_client,
    init,
    insert_one,
    register,
    transaction,
    update_one,
)
```

| Name | Description |
|---|---|
| `register(name, url, database)` | Build and store a named client; returns it. |
| `get_client(name="default")` | Look up a registered client. |
| `init(url, database)` | Async. `register("default", ...)`. |
| `close(name="default")` | Async. Deregister *and* `await client.close()`. Idempotent. |
| `insert_one(collection, document, *, id=None, session=None)` | Async. Returns the `_id` as a string. |
| `find_one(collection, filter, *, session=None)` | Async. First match or `None`. |
| `find(collection, filter=None, *, limit=100, session=None)` | Async. Up to `limit` documents. |
| `update_one(collection, filter, update, *, upsert=False, session=None)` | Async. `$set` semantics; `True` if modified or upserted. |
| `delete_one(collection, filter, *, session=None)` | Async. `True` if a document was deleted. |
| `count(collection, filter=None, *, session=None)` | Async. `count_documents`. |
| `transaction()` | Async context manager yielding an `AsyncClientSession`. |
| `StoreClient` | Per-database client with the same seven operations plus `close()`. |
| `StoreConfig` | Frozen Pydantic value object: `url`, `database`. |
| `StoreError` | Base error, `AppErrorCode.INTERNAL`. |
| `ClientNotRegisteredError` | `AppErrorCode.INTERNAL`. |

**`deregister` is missing from `__all__`.** It exists and works, but must be reached through the
module rather than a star-import:

```python
import forktex_core.store as store

dropped = store.deregister("audit")   # StoreClient | None, idempotent, does NOT close
```

Transactions — every operation that must participate needs `session=`:

```python
async with store.transaction() as session:
    await store.insert_one("orders", order_doc, session=session)
    await store.update_one("inventory", {"_id": sku}, {"stock": n}, session=session)
# commits on clean exit; any exception aborts the whole block
```

## Errors

`StoreError` and `ClientNotRegisteredError` subclass `AppError`. Driver errors are **not** wrapped.

| Raised | When | Catch? |
|---|---|---|
| `ImportError` | `register()`/`init()` without `pymongo`. | No — install the extra. |
| `ClientNotRegisteredError` | `get_client(name)` for an unregistered name. Message lists the registered names. | No — a wiring bug. |
| `pymongo.errors.DuplicateKeyError` | `insert_one(..., id=…)` where that `_id` already exists. | Yes, when the id is a natural key. |
| `pymongo.errors.OperationFailure` | `transaction()` against a standalone `mongod`. | No — deployment shape, fix the topology. |
| `pymongo.errors.PyMongoError` and subclasses | Any other driver/network failure. | Yes, at a request boundary. |

Nothing here maps a driver error onto an `AppError`; a `PyMongoError` reaching an HTTP boundary
renders as a 500 unless the consumer translates it.

## Gotchas

- **`deregister` is absent from `__all__`** (see above), and unlike `close()` it does not close the
  underlying `AsyncMongoClient`. Dropping a client with `deregister` alone leaks its pool.
- **`_id` is always a string on the way out.** `_normalize` stringifies `ObjectId` on every document
  returned, so `find`/`find_one` never hand back a `bson.ObjectId`.
- **`_id` filters get coerced.** A filter value that is a 24-hex-char string passing
  `ObjectId.is_valid` is converted back to an `ObjectId` before querying — necessary because a
  string never matches a real `ObjectId`. The consequence: a *caller-supplied* `id=` that happens to
  be valid `ObjectId` hex was stored as a literal string but will be looked up as an `ObjectId`, and
  will not match. Avoid raw 24-hex-char custom ids.
- **`update_one` always wraps in `$set`.** You pass a plain field dict, not an update document —
  `{"$inc": …}` and friends are unreachable through this module.
- **`find()` defaults to `limit=100`** with no cursor or pagination. A caller who assumes "all"
  silently gets the first 100.
- **`update_one` returns `False` when the filter matched but nothing changed** — it is driven by
  `modified_count`, not `matched_count`.
- **Transactions need a replica set or sharded cluster.** A standalone `mongod` raises
  `OperationFailure` on `start_transaction()`.
- **An operation without `session=` inside a `transaction()` block runs outside it** — not rolled
  back, and blind to the transaction's uncommitted writes. Ordinary Mongo isolation, but easy to
  miss because the call still succeeds.
- **BSON documents cap at 16 MB.** Use `forktex_core.storage` for blobs and keep only the reference
  here.
