# forktex_core.vector

Async Qdrant connector for multi-modal vector search: collection lifecycle, point upsert, four
search strategies (dense, multimodal, hybrid, sparse), cross-collection fan-out and cosine
reranking. It does not embed anything — the caller supplies every vector.

## Install

```bash
pip install forktex-core[vector]   # qdrant-client
```

The `qdrant_client` import lives in `_make_client`, which is only called inside an operation, and
`qdrant_client.models` is imported inside each method that needs it. Nothing eager happens in
`Vector.__init__` or `register()`. A missing extra therefore raises **lazily, at the first
collection or search call**:

```
ImportError: Install 'forktex-core[vector]' (qdrant-client) to use forktex_core.vector
```

## Wiring

**Shape B — named-client registry**, alongside a directly-constructible client. `register(name,
qdrant_url, *, api_key=None)` builds a `Vector` and stores it in a module dict; `get_client(name)`
fetches it; `deregister(name)` drops it. There is no `init()` and no `close()` — `Vector` holds no
connection, and a fresh `AsyncQdrantClient` is created and closed per operation.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from forktex_core.vector import deregister, register


@asynccontextmanager
async def lifespan(app: FastAPI):
    register("default", qdrant_url=settings.qdrant_url)
    yield
    deregister("default")


app = FastAPI(lifespan=lifespan)
```

The registry is a plain module-level dict and therefore **per-process**. A worker process must run
its own `register()` at startup; it does not inherit the API process's clients.

`register()` is idempotent by name — re-registering replaces the previous client.

## Public surface

```python
from forktex_core.vector import (
    ClientNotRegisteredError,
    CollectionHandle,
    CollectionInfo,
    CollectionNotFoundError,
    DimensionMismatchError,
    SearchHit,
    SearchQuery,
    SparseVector,
    Vector,
    VectorError,
    VectorPoint,
    deregister,
    get_client,
    register,
)
```

| Name | Description |
|---|---|
| `Vector(qdrant_url, api_key=None)` | Entry point. Stateless; connects per operation. |
| `Vector.collection(name)` | Returns a `CollectionHandle`; the collection need not exist yet. |
| `Vector.list_collections(*, prefix=None)` | Async. Collection names, optionally prefix-filtered. |
| `Vector.search_across(names, query)` | Async. Fan-out search, merged and sorted by score, truncated to the query's limit. |
| `register` / `get_client` / `deregister` | Named-client registry, as above. |
| `CollectionHandle.create(dim, distance="cosine", *, multimodal_dim=None, sparse=False)` | Async. Idempotent create. |
| `CollectionHandle.delete()` | Async. Drop the collection and its data. |
| `CollectionHandle.info()` | Async. `CollectionInfo`. |
| `CollectionHandle.upsert(points)` | Async. Insert or overwrite by id. |
| `CollectionHandle.delete_points(ids)` | Async. Delete by id. |
| `CollectionHandle.search(query)` | Async. Ranked `SearchHit` list. |
| `CollectionHandle.rerank(query_vector, hits, top_k)` | Async. Re-score hits by cosine against stored dense vectors. |
| `VectorPoint` | Frozen value object: `id`, `vector`, `payload`, `multimodal_vector`, `sparse_vector`. |
| `SparseVector` | Frozen value object: `indices`, `values` (equal length). |
| `SearchHit` | Frozen value object: `id`, `score`, `payload`, `collection`. |
| `CollectionInfo` | Frozen value object: `name`, `vectors_count`, `dim`, `multimodal_dim`, `has_sparse`, `distance`. |
| `SearchQuery(vector)` | Chainable builder: `.limit(k)`, `.using(strategy)`, `.multimodal(vec)`, `.filter(d)`, `.score_threshold(t)`. |
| `VectorError` | Base error, `AppErrorCode.INTERNAL`. |
| `CollectionNotFoundError` | `AppErrorCode.NOT_FOUND`. |
| `DimensionMismatchError` | `AppErrorCode.VALIDATION`. |
| `ClientNotRegisteredError` | Subclasses `VectorError`; inherits `INTERNAL`. |

```python
from forktex_core.vector import SearchQuery, Vector, VectorPoint

vector = Vector(qdrant_url="http://qdrant:6333")
coll = vector.collection("org-abc--knowledge")
await coll.create(dim=1536, multimodal_dim=512, sparse=True)

await coll.upsert([VectorPoint(id=1, vector=embed(text), payload={"text": text})])

hits = await coll.search(
    SearchQuery(vector=embed(query)).limit(10).using("hybrid").score_threshold(0.6)
)
```

Collections carry named vector spaces: `"dense"` (always), `"multimodal"` (when `multimodal_dim`
is passed) and `"sparse"` (when `sparse=True`).

## Errors

`VectorError` and its subclasses are `AppError`s, so an HTTP transport renders a real status.

| Raised | When | Catch? |
|---|---|---|
| `ImportError` | The first Qdrant operation without `qdrant-client`. | No — install the extra. |
| `ClientNotRegisteredError` | `get_client(name)` for an unregistered name. Message lists the registered names. | No — a wiring bug. |
| `CollectionNotFoundError` | `info()` against a collection Qdrant does not have. | Yes. |
| `DimensionMismatchError` | `upsert()` where Qdrant returns 400 and the message mentions "dimension". | Yes — it is caller data. |
| `VectorError("Unknown search strategy: …")` | `search()` with a strategy outside the four literals. | No — a programming error. |
| `qdrant_client.http.exceptions.UnexpectedResponse` | Any other Qdrant rejection, unwrapped. | Yes, at a request boundary. |

## Gotchas

- **Two wiring paths coexist, and the registry is optional.** `register()`/`get_client()` exists and
  mirrors `storage`'s, but constructing `Vector(qdrant_url=...)` directly and passing it around is
  equally supported — `Vector` holds no connection, so there is nothing for the registry to
  lifecycle. Pick one per service; mixing them means two objects that look interchangeable but are
  not the same instance.
- **There is no `init()` and no `close()`** on this module, unlike `cache`, `storage` and `store`.
  `deregister()` is the only teardown, and it does nothing beyond removing the dict entry.
- **`DimensionMismatchError` is a string-sniff.** `upsert()` only converts a Qdrant 400 whose
  message contains "dimension"; every other 400 propagates as `UnexpectedResponse`.
- **`"hybrid"` and `"sparse"` run the same query.** Both take the dense prefetch path
  (`limit * 3`) with RRF fusion — the sparse vector on a point is stored but the `"sparse"`
  strategy does not query the sparse space on its own.
- **`multimodal` falls back to the dense vector.** `SearchQuery.using("multimodal")` without a
  preceding `.multimodal(vec)` searches the `"multimodal"` space using `query._vector`, which is
  almost never the right dimensionality.
- **`search()` never populates `SearchHit.collection`** — only `search_across` does, via
  `model_copy`. These are frozen value objects; assignment will fail.
- **`search_across` swallows per-collection failures.** A collection that errors is logged at
  `warning` and omitted; you get a partial result set with no exception.
- **`score_threshold` is applied by Qdrant *before* RRF fusion semantics**, and RRF scores are rank
  reciprocals, not similarities — a threshold tuned for dense cosine will filter almost everything
  under `"hybrid"`.
- **`rerank()` silently keeps the original score** for any hit whose dense vector cannot be
  retrieved, then sorts the mixed set — cosine scores and search scores end up compared directly.
- **`upsert([])` and `delete_points([])` return early** without opening a connection.
- **`info()` reads `points_count` with a `vectors_count` fallback**, and reports `0` if neither
  attribute is present on the installed client version.
- **The `qdrant-client` surface moves between versions.** `NamedSparseVector`/`NamedVector` existed
  in older releases and are gone by 1.19; check `qdrant_client.models` before upgrading.
