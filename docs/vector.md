# `forktex_core.vector` — Multi-modal vector search

> Qdrant-backed async vector search with four strategies (dense, multimodal, hybrid, sparse), cross-collection fan-out, and cosine-similarity reranking — no dependency on any embedding model.

## Overview

`vector` is a thin stateless connector over Qdrant. It does not embed text — that is the caller's responsibility. It manages collections (creation, deletion, metadata), upserts points with named vector spaces, and executes searches with configurable strategies.

```bash
pip install forktex-core[vector]   # qdrant-client
```

## Quick start

```python
from forktex_core.vector import Vector, VectorPoint, SearchQuery
import uuid

vector = Vector(qdrant_url="http://qdrant:6333")

# Create collection (idempotent)
coll = vector.collection("org-abc--knowledge")
await coll.create(dim=1536, distance="cosine")

# Upsert — IDs must be int or UUID string
await coll.upsert(
    [
        VectorPoint(
            id=1,  # or str(uuid.uuid4())
            vector=my_embed("Paris is the capital of France"),
            payload={"text": "Paris is the capital of France", "source": "wiki"},
        )
    ]
)

# Dense search
hits = await coll.search(SearchQuery(vector=my_embed("capital of France")).limit(5))
for h in hits:
    print(h.id, h.score, h.payload["text"])

# Hybrid (dense + sparse RRF)
hits = await coll.search(SearchQuery(vector=my_embed(q)).limit(10).using("hybrid").score_threshold(0.6))
```

## API reference

```python
class Vector:
    def __init__(self, qdrant_url: str, api_key: str | None = None)
    def collection(self, name: str) -> CollectionHandle
    async def list_collections(self, *, prefix: str | None = None) -> list[str]
    async def search_across(self, collection_names: list[str], query: SearchQuery) -> list[SearchHit]

# Named-client registry — mirrors forktex_core.storage's register()/get_client()/deregister()
def register(name: str, qdrant_url: str, *, api_key: str | None = None) -> Vector
def get_client(name: str = "default") -> Vector       # raises ClientNotRegisteredError if unknown
def deregister(name: str = "default") -> Vector | None  # idempotent; returns the dropped client or None

class CollectionHandle:
    async def create(self, dim: int, distance: str = "cosine", *,
                     multimodal_dim: int | None = None, sparse: bool = False) -> None
    async def delete(self) -> None
    async def info(self) -> CollectionInfo
    async def upsert(self, points: list[VectorPoint]) -> None
    async def delete_points(self, ids: list[str | int]) -> None
    async def search(self, query: SearchQuery) -> list[SearchHit]
    async def rerank(self, query_vector: list[float], hits: list[SearchHit], top_k: int) -> list[SearchHit]

@dataclass
class VectorPoint:
    id: str | int                         # MUST be int or UUID string
    vector: list[float]                   # dense embedding (required)
    payload: dict[str, Any] = {}
    multimodal_vector: list[float] | None = None  # CLIP embedding
    sparse_vector: SparseVector | None = None     # BM25/SPLADE

@dataclass
class SparseVector:
    indices: list[int]
    values: list[float]

@dataclass
class SearchHit:
    id: str | int
    score: float
    payload: dict[str, Any]
    collection: str | None = None  # set by search_across

class SearchQuery:
    def __init__(self, vector: list[float])
    def limit(self, k: int) -> SearchQuery
    def using(self, strategy: Literal["dense","multimodal","hybrid","sparse"]) -> SearchQuery
    def multimodal(self, vector: list[float]) -> SearchQuery
    def filter(self, payload_filter: dict) -> SearchQuery
    def score_threshold(self, threshold: float) -> SearchQuery

@dataclass
class CollectionInfo:
    name: str; vectors_count: int; dim: int
    multimodal_dim: int | None; has_sparse: bool; distance: str
```

## Patterns

### Pattern 0 — Register once at startup, look up by name anywhere

```python
from forktex_core.vector import register, get_client

# At process startup:
register("default", qdrant_url=settings.qdrant_url)

# Anywhere else (e.g. the rich VECTOR field handler in [space]):
vector = get_client("default")
coll = vector.collection("org-abc--knowledge")
```

### Pattern 1 — Multi-tenant collection naming

```python
# ❌ Colon is forbidden in Qdrant collection names
coll = vector.collection(f"org-{org_id}:knowledge")  # raises 422

# ✅ Use -- as separator
coll = vector.collection(f"org-{org_id}--knowledge")
```

### Pattern 2 — Cross-collection knowledge base search

```python
# List all collections for org, then search across all
org_collections = await vector.list_collections(prefix=f"org-{org_id}--")
hits = await vector.search_across(
    org_collections,
    SearchQuery(vector=embed(query)).limit(10).using("hybrid"),
)
# hits[i].collection tells you which collection each hit came from
```

### Pattern 3 — Multimodal collection (text + image)

```python
coll = vector.collection("org-abc--media")
await coll.create(dim=1536, multimodal_dim=512)  # CLIP ViT-B/32

await coll.upsert(
    [
        VectorPoint(
            id=1,
            vector=text_embed("a cat sitting on a chair"),
            multimodal_vector=clip_embed(image_bytes),
            payload={"text": "a cat..."},
        )
    ]
)

# Search text space
hits = await coll.search(SearchQuery(vector=text_embed(q)).limit(5))
# Search image space
hits = await coll.search(SearchQuery(vector=clip_embed(q_img)).using("multimodal").limit(5))
```

### Pattern 4 — Rerank for precision

```python
# Fetch broad set, then rerank by cosine similarity
q_vec = embed(query)
raw = await coll.search(SearchQuery(q_vec).limit(30))  # broad
ranked = await coll.rerank(q_vec, raw, top_k=5)  # precise
```

## Anti-patterns

```python
# ❌ Colon in collection name → Qdrant 422 error
vector.collection("org-abc:knowledge")

# ❌ Arbitrary string as point ID
VectorPoint(id="my-document-title", vector=...)  # Qdrant rejects

# ✅ Use int or UUID string
VectorPoint(id=1, vector=...)
VectorPoint(id=str(uuid.uuid4()), vector=...)

# ❌ AsyncQdrantClient as context manager (not supported)
async with _make_client(url) as q:  # TypeError
    ...

# ✅ Use try/finally
q = _make_client(url)
try:
    result = await q.get_collections()
finally:
    await q.close()

# ❌ upsert([]) — no-op but wastes a Qdrant connection
await coll.upsert([])

# ✅ Guard before calling (module handles it internally)
if points:
    await coll.upsert(points)
```

---

## Agent guide

### Canonical forms

**Collection lifecycle (full cycle):**
```python
vector = Vector(qdrant_url=settings.qdrant_url)
name = f"org-{org_id}--{collection_slug}"  # no colons
coll = vector.collection(name)

# Create once (idempotent)
await coll.create(dim=1536, multimodal_dim=512, sparse=True)

# Upsert — id must be int or UUID str
for chunk in chunks:
    await coll.upsert(
        [
            VectorPoint(
                id=chunk_int_id,
                vector=embed(chunk.text),
                payload={"text": chunk.text, "document_id": str(chunk.doc_id)},
            )
        ]
    )

# Search
hits = await coll.search(SearchQuery(vector=embed(q)).limit(10))
# hits[0].id, hits[0].score, hits[0].payload

# Delete
await coll.delete()
```

**search_across with error attribution:**
```python
# Returns SearchHit.collection = source collection name
# Collections that fail are logged as warnings (not exceptions)
hits = await vector.search_across(names, query)
for h in hits:
    print(f"[{h.collection}] score={h.score:.3f} {h.payload.get('text', '')[:80]}")
```

### Edge cases

| Scenario | Behaviour |
|---|---|
| `collection_name` with `:` | Qdrant 422 `UnexpectedResponse` — use `--` separator |
| `VectorPoint(id="text-string")` | Qdrant 400 — id must be int or UUID string |
| `upsert([])` | Early return, no Qdrant call |
| `delete_points([])` | Early return, no Qdrant call |
| `rerank(q_vec, hits, top_k)` — hit vector not in Qdrant | Falls back to original search score for that hit |
| `search_across` — one collection fails | Warning logged, other collections' results returned |
| `list_collections(prefix=None)` | Returns ALL Qdrant collections |
| `AsyncQdrantClient` version ≥ 1.14 | Use `collection_exists()` not `get_collection()` for idempotent create |
| `upsert()` with a vector whose length ≠ the collection's configured `dim` | Raises `DimensionMismatchError` (a typed wrapper — not a raw `UnexpectedResponse`) |
| `get_client(name)` for an unregistered name | Raises `ClientNotRegisteredError`, message lists what *is* registered |
| `deregister(name)` for an already-unregistered/unknown name | Returns `None` — idempotent, doesn't raise |
| Upgrading `qdrant-client` major/minor versions | Check `qdrant_client.models` for renamed/removed types before upgrading — `NamedSparseVector`/`NamedVector` existed in older versions and were removed by 1.19; this module was broken by exactly that until fixed |

### Error catalogue

| Error | When |
|---|---|
| `ImportError("Install 'forktex-core[vector]'")` | `qdrant-client` not installed |
| `CollectionNotFoundError` | `info()` on non-existent collection |
| `DimensionMismatchError` | `upsert()` with a wrong-length vector for the collection's `dim` |
| `ClientNotRegisteredError` | `get_client(name)` for a name never `register()`-ed |
| `VectorError("Unknown search strategy")` | `query.using("invalid")` |
| `qdrant_client.http.exceptions.UnexpectedResponse 422` | Collection name contains `:` or other forbidden char |
| `qdrant_client.http.exceptions.UnexpectedResponse 400` | Point ID is not int or UUID (non-dimension 400s aren't wrapped) |

### Integration map

```
vector ──── (used by) ──── docs knowledge pipeline   [embed MemoryEntry → Qdrant]
vector ──── uses ──────────── log (forktex_core.log.get_logger — search_across warnings, rerank fallback debug logs)
vector ──── (no dep on) ── database, cache, flow, vault, storage, queue
```

### Checklist

- [ ] Collection names use `--` not `:` as separator
- [ ] Point IDs are `int` or `str(uuid.uuid4())` — never arbitrary strings
- [ ] `create()` called before first `upsert()` — it is idempotent
- [ ] `try/finally + q.close()` used for any direct `_make_client()` calls
- [ ] `search_across` used for org-wide queries; `search` for single collection
- [ ] `rerank()` used after a broad `limit(30–50)` initial search, not on the final set
- [ ] `register()`/`get_client()` used for the named-client registry instead of passing a `Vector` instance around manually
