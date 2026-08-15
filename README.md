# Forktex Core

> **The shared Python substrate for Forktex services.**
> Pick-and-choose extras across four levels. One install. Zero framework lock-in.

[![PyPI](https://img.shields.io/pypi/v/forktex-core.svg)](https://pypi.org/project/forktex-core/)
[![Python](https://img.shields.io/pypi/pyversions/forktex-core.svg)](https://pypi.org/project/forktex-core/)
[![License](https://img.shields.io/badge/license-AGPL--3.0%20OR%20Commercial-blue.svg)](LICENSE)
[![Manifest](https://img.shields.io/badge/forktex.json-1.1.0-7E57C2.svg)](forktex.json)

```bash
pip install forktex-core                 # log · database · cache · queue · grid · flow
pip install forktex-core[vault]          # + Fernet encryption
pip install forktex-core[storage]        # + S3/MinIO blob storage
pip install forktex-core[store]          # + MongoDB document store
pip install forktex-core[vector]         # + Qdrant vector search
pip install forktex-core[all]            # everything
```

---

## Architecture

The four-level architecture below is the **single source of truth** for what
extras exist, how they relate, and which infra each requires. Tables are
generated from [`forktex_core/catalog/catalog.json`](src/forktex_core/catalog/catalog.json) — refresh with
`make catalog-update`; `make catalog-check` runs in CI.

### Levels overview

<!-- catalog:levels start -->
| Level | Name | Description | Extras |
|------:|:-----|:------------|:-------|
| 0 | **primitives** | Zero-dep cross-cutting utilities. Always pulled. | `log` · `error` · `types` · `iso` |
| 1 | **role_facades** | Role-named facades over one infra service each. Expose Database() / Cache() / Queue() / Vector() / Storage() / Store() / Vault() / Graph() user-facing classes. | `database` · `cache` · `queue` · `vector` · `storage` · `store` · `vault` · `graph` |
| 2 | **substrate_facades** | Substrate user-facing pillars composing level 0–1. | `grid` · `space` · `flow` |
| 3 | **bootstraps** | Process-level runtime wiring (FastAPI factory, queue-consumer hosts). | `api` · `worker` |
<!-- catalog:levels end -->

### Level 0 — Primitives

<!-- catalog:level0 start -->
_Zero-dep cross-cutting utilities. Always pulled._

| Extra | Role | Status |
|:------|:-----|:-------|
| [![log](https://img.shields.io/badge/log-logging-455A64?style=for-the-badge)](docs/log.md) | Structured logging protocol + request-id context propagation. | ✅ shipped |
| [![error](https://img.shields.io/badge/error-errors-C62828?style=for-the-badge)](docs/error.md) | AppError hierarchy + envelope shape + http exception mapper. | ✅ shipped |
| [![types](https://img.shields.io/badge/types-base%20types-5D4037?style=for-the-badge)](docs/types.md) | Base Pydantic models, frozen value objects, JSON-Schema conventions. | ✅ shipped |
| [![iso](https://img.shields.io/badge/iso-iso%20dates-455A64?style=for-the-badge)](docs/iso.md) | Canonical ISO-8601 date/time formatting and parsing — always UTC. | ✅ shipped |
<!-- catalog:level0 end -->

### Level 1 — Role facades

<!-- catalog:level1 start -->
_Role-named facades over one infra service each. Expose Database() / Cache() / Queue() / Vector() / Storage() / Store() / Vault() / Graph() user-facing classes._

| Extra | Role | Depends on | Status |
|:------|:-----|:-----------|:-------|
| [![database](https://img.shields.io/badge/database-postgres-336791?style=for-the-badge)](docs/database.md) | Relational/ACID structured-state (Postgres/SQLAlchemy): Database handle, BaseDBModel + substrate_base, AuditMixin, and the DDL/reflect/filters/pagination/locks/identifiers/integrity primitives every higher package builds on. | `log`, `error`, `types`, `iso` | ✅ shipped |
| [![cache](https://img.shields.io/badge/cache-redis-DC382D?style=for-the-badge)](docs/cache.md) | Key-value cache (Redis): get/set, decorators, prefix invalidation. | `log`, `error`, `types` | ✅ shipped |
| [![queue](https://img.shields.io/badge/queue-redis-EF6C00?style=for-the-badge)](docs/queue.md) | Async job queue (Redis/arq): tasks, enqueue, JobCtx. | `log`, `error`, `types` | ✅ shipped |
| [![vector](https://img.shields.io/badge/vector-qdrant-DC4A3D?style=for-the-badge)](docs/vector.md) | Vector store + similarity search (Qdrant): CollectionHandle, VectorPoint, search. | `log`, `error`, `types` | ✅ shipped |
| [![storage](https://img.shields.io/badge/storage-minio-FF9900?style=for-the-badge)](docs/storage.md) | Object/blob storage (S3/MinIO): presigned URLs, presigned POST. | `log`, `error`, `types` | ✅ shipped |
| [![store](https://img.shields.io/badge/store-mongo-47A248?style=for-the-badge)](docs/store.md) | Schemaless document records (BSON/JSON shape, eventual consistency), MongoDB-backed. Distinct from [storage], which holds opaque binary blobs. | `log`, `error`, `types` | ✅ shipped |
| [![vault](https://img.shields.io/badge/vault-vault-2E7D32?style=for-the-badge)](docs/vault.md) | Crypto-at-rest helpers (Fernet over [database] state). | `log`, `error`, `types`, `database` | ✅ shipped |
| [![graph](https://img.shields.io/badge/graph-graph-F57C00?style=for-the-badge)](docs/graph.md) | In-memory multi-edge typed-graph algebra. Graph() with BFS/DFS/closure/cycle, deterministic edge IDs, JSON export. No infra dep. | `log`, `error`, `types` | ✅ shipped |
<!-- catalog:level1 end -->

### Level 2 — Substrate facades

<!-- catalog:level2 start -->
_Substrate user-facing pillars composing level 0–1._

| Extra | Role | Depends on | Lazy imports | Status |
|:------|:-----|:-----------|:-------------|:-------|
| [![grid](https://img.shields.io/badge/grid-grid-1976D2?style=for-the-badge)](docs/grid.md) | Tabular state pillar — typed cells, rows, cell-pinned edges, recursive-CTE traversal, JSON-Schema import/export. Pure-tabular: no VECTOR/FILE handlers. | `log`, `error`, `types`, `iso`, `database` | — | ✅ shipped |
| [![space](https://img.shields.io/badge/space-space-7E57C2?style=for-the-badge)](docs/space.md) | Multi-grid bundle + rich-content wrapper (VECTOR/FILE field types) + cross-grid traversal + SyncSourceConfig contract. | `log`, `error`, `types`, `grid`, `graph` | `vector`, `storage` | ✅ shipped |
| [![flow](https://img.shields.io/badge/flow-flow-00897B?style=for-the-badge)](docs/flow.md) | Durable workflow execution. Tasks, runs, step events, scheduled runs. | `log`, `error`, `types`, `iso`, `database`, `queue`, `cache` | — | ✅ shipped |
<!-- catalog:level2 end -->

### Level 3 — Bootstraps

<!-- catalog:level3 start -->
_Process-level runtime wiring (FastAPI factory, queue-consumer hosts)._

| Extra | Role | Required | Optional for consumer | Status |
|:------|:-----|:---------|:----------------------|:-------|
| [![api](https://img.shields.io/badge/api-api%20server-0277BD?style=for-the-badge)](docs/api.md) | FastAPI factory: standard middleware stack, lifespan, typed health probes, error envelope with HTTP status mapping. | `log`, `error`, `types` | `grid`, `space`, `database`, `cache`, `vault` | ✅ shipped |
| [![worker](https://img.shields.io/badge/worker-worker-5E35B1?style=for-the-badge)](docs/worker.md) | Queue-consumer hosts: Worker (async lifecycle + awaitable run) run standalone, embedded in a host loop, or one per OS process. | `log`, `error`, `types`, `queue` | `database`, `grid`, `space`, `flow` | ✅ shipped |
<!-- catalog:level3 end -->

### Pick & choose

<!-- catalog:matrix start -->
| Consumer wants… | forktex_core extras | Infra services |
|:----------------|:--------------------|:---------------|
| Pure tabular registers (basic field types only) | `grid` | postgres |
| Tabular registers + vectors (VECTOR field added) | `grid`, `space`, `vector` | postgres, qdrant |
| Tabular registers + files (FILE field added) | `grid`, `space`, `storage` | minio, postgres |
| Multi-grid bundle with VECTOR + FILE | `grid`, `space`, `vector`, `storage` | minio, postgres, qdrant |
| In-memory graph analysis only | `graph` | (none) |
| Just durable workflows | `flow` | postgres, redis |
| API server, no DB | `api` | (none) |
| API server with grid CRUD | `api`, `grid` | postgres |
| API server with rich content + middleware | `api`, `grid`, `space`, `vector`, `storage`, `cache` | minio, postgres, qdrant, redis |
| Background worker, pure compute | `worker` | redis |
| Background worker with flow runs | `worker`, `flow` | postgres, redis |
| Background worker with grid + flow + vector embedding | `worker`, `flow`, `grid`, `space`, `vector` | postgres, qdrant, redis |
<!-- catalog:matrix end -->

### Filesystem

<!-- catalog:tree start -->
```
forktex_core/
│  ── Level 0: primitives ──
│   ├── log/  [log] → —  ✅ shipped
│   │       Structured logging protocol + request-id context propagation.
│   ├── error/  [error] → —  ✅ shipped
│   │       AppError hierarchy + envelope shape + http exception mapper.
│   ├── types/  [types] → —  ✅ shipped
│   │       Base Pydantic models, frozen value objects, JSON-Schema conventions.
│   ├── iso/  [iso] → —  ✅ shipped
│   │       Canonical ISO-8601 date/time formatting and parsing — always UTC.
│
│  ── Level 1: role_facades ──
│   ├── database/  [database] → postgres  ✅ shipped
│   │       Relational/ACID structured-state (Postgres/SQLAlchemy): Database handle, BaseDBModel + substrate_base, AuditMixin, and the DDL/reflect/filters/pagination/locks/identifiers/integrity primitives every higher package builds on.
│   ├── cache/  [cache] → redis  ✅ shipped
│   │       Key-value cache (Redis): get/set, decorators, prefix invalidation.
│   ├── queue/  [queue] → redis  ✅ shipped
│   │       Async job queue (Redis/arq): tasks, enqueue, JobCtx.
│   ├── vector/  [vector] → qdrant  ✅ shipped
│   │       Vector store + similarity search (Qdrant): CollectionHandle, VectorPoint, search.
│   ├── storage/  [storage] → minio  ✅ shipped
│   │       Object/blob storage (S3/MinIO): presigned URLs, presigned POST.
│   ├── store/  [store] → mongo  ✅ shipped
│   │       Schemaless document records (BSON/JSON shape, eventual consistency), MongoDB-backed. Distinct from [storage], which holds opaque binary blobs.
│   ├── vault/  [vault] → —  ✅ shipped
│   │       Crypto-at-rest helpers (Fernet over [database] state).
│   ├── graph/  [graph] → —  ✅ shipped
│   │       In-memory multi-edge typed-graph algebra. Graph() with BFS/DFS/closure/cycle, deterministic edge IDs, JSON export. No infra dep.
│
│  ── Level 2: substrate_facades ──
│   ├── grid/  [grid] → —  ✅ shipped
│   │       Tabular state pillar — typed cells, rows, cell-pinned edges, recursive-CTE traversal, JSON-Schema import/export. Pure-tabular: no VECTOR/FILE handlers.
│   ├── space/  [space] → —  ✅ shipped
│   │       Multi-grid bundle + rich-content wrapper (VECTOR/FILE field types) + cross-grid traversal + SyncSourceConfig contract.
│   ├── flow/  [flow] → —  ✅ shipped
│   │       Durable workflow execution. Tasks, runs, step events, scheduled runs.
│
│  ── Level 3: bootstraps ──
│   ├── api/  [api] → —  ✅ shipped
│   │       FastAPI factory: standard middleware stack, lifespan, typed health probes, error envelope with HTTP status mapping.
│   ├── worker/  [worker] → —  ✅ shipped
│   │       Queue-consumer hosts: Worker (async lifecycle + awaitable run) run standalone, embedded in a host loop, or one per OS process.
│
```
<!-- catalog:tree end -->

---

## Usage

### `log` — set up first, before anything else

```python
from forktex_core.log import setup_logging, get_logger, TraceIDMiddleware

setup_logging(service="my-service")  # JSON to stdout, INFO
# setup_logging(service="my-service", debug=True)  # human-readable, DEBUG

log = get_logger(__name__)
log.info("starting up")

# FastAPI: add middleware so every request gets a trace_id automatically
app.add_middleware(TraceIDMiddleware)
```

### `database` — Postgres connection + ORM

```python
from forktex_core.database import init_engine, get_session, BaseDBModel, NamespacedMixin, AuditMixin
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
import uuid

init_engine("postgresql+asyncpg://user:pass@host/db", pool_size=10)


class Invoice(BaseDBModel, NamespacedMixin, AuditMixin):
    __tablename__ = "invoice"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    amount: Mapped[int] = mapped_column(sa.Integer)


async with get_session() as session:  # auto-commit / rollback
    session.add(Invoice(namespace=str(org_id), amount=100))
```

### `cache` — Redis

```python
from forktex_core.cache import init, cached

await init("redis://localhost:6379/0")


@cached(ttl=300)
async def get_org(org_id: str) -> dict: ...


# Pair with structured log context (lives in forktex_core.log)
from forktex_core.log import async_log_context

async with async_log_context(org_id=str(org_id)):
    log.info("processing")  # → {..."org_id": "org-xyz"}
```

### `flow` — durable workflows

```python
from forktex_core.flow import Flow, step

flow = Flow(database_url="postgresql+asyncpg://...")
await flow.init()


@step
async def send_welcome(ctx, state): ...


@flow.pipeline("onboarding.user", version=1)
class UserOnboarding:
    steps = [send_welcome, create_workspace]


instance = await flow.run("onboarding.user", state={"email": "x@y.com"})
await instance.wait(timeout=60)
```

### `vault` — encryption at rest

```python
from forktex_core.vault import Vault, EncryptedJSON
import os

vault = Vault(kek=os.environ["FORKTEX_KEK"])


class Provider(BaseDBModel):
    __tablename__ = "provider"
    credentials: Mapped[bytes] = mapped_column(EncryptedJSON(vault))


provider.credentials = {"api_key": "sk-..."}  # transparent encrypt/decrypt
```

### `storage` — S3/MinIO

```python
from forktex_core.storage import register, get_client

# Register once at startup (supports multiple buckets)
register("docs", url="http://minio:9000", bucket="documents", access_key=KEY, secret_key=SECRET)

client = get_client("docs")
await client.upload("invoices/abc.pdf", pdf_bytes, content_type="application/pdf")
url = await client.presign("invoices/abc.pdf", expires_in=3600)

# Actor uploads directly to MinIO — no auth header needed, signature is in the URL
put_url = await client.presign("uploads/photo.jpg", method="put_object", content_type="image/jpeg", expires_in=900)
```

### `queue` — background jobs

```python
from forktex_core.queue import task, init, enqueue, make_worker, JobCtx

await init("redis://localhost:6379/1")


@task(retries=2, timeout=120)
async def process_document(ctx: JobCtx, doc_id: str) -> None: ...


job_id = await enqueue(process_document, str(doc.id))

# Worker entrypoint (run separately)
WorkerSettings = make_worker("redis://localhost:6379/1")
```

### `vector` — semantic search

```python
from forktex_core.vector import Vector, VectorPoint, SearchQuery

vector = Vector(qdrant_url="http://qdrant:6333")
coll = vector.collection(f"org-{org_id}--knowledge")  # use -- not : as separator
await coll.create(dim=1536)

await coll.upsert([VectorPoint(id=1, vector=embed(text), payload={"text": text})])

hits = await coll.search(SearchQuery(vector=embed(query)).limit(10).using("hybrid"))
for h in hits:
    print(h.score, h.payload["text"])
```

### `grid` — runtime tabular schemas

```python
from forktex_core.database import get_session
from forktex_core.grid import FieldType, Grid, TableSpec

async with get_session() as session:
    contacts = await Grid.declare(
        session,
        TableSpec.from_dicts(
            slug="contacts",
            label="Contacts",
            namespace=str(org_id),
            columns=[
                {
                    "key": "email",
                    "label": "Email",
                    "type_id": FieldType.text.value,
                    "is_required": True,
                },
            ],
        ),
    )
    row = await contacts.create({"email": "person@example.com"})
```

---

## FastAPI integration pattern

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from forktex_core.database import init_engine, close_engine
from forktex_core.cache import init as cache_init, close as cache_close
from forktex_core.log import setup_logging, TraceIDMiddleware

setup_logging(service="my-api")  # call before app creation


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_engine(settings.db_url, pool_size=20)
    await cache_init(settings.redis_url)
    yield
    await close_engine()
    await cache_close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(TraceIDMiddleware)
```

---

## Managed Postgres (no `CREATE SCHEMA`)

Library schemas (`forktex_flow`, `forktex_grid`) are isolated from your alembic by default. If your Postgres host doesn't allow `CREATE SCHEMA`, route them to `public`:

```python
init_engine(
    url,
    schema_translate_map={
        "forktex_flow": None,  # None = public schema
        "forktex_grid": None,
    },
)
```

---

## Gotchas

A short list of mistakes that will save you a debugging session:

| Rule | Why |
|---|---|
| Qdrant collection names: use `--` not `:` | Qdrant rejects `:` with 422 |
| Qdrant point IDs: `int` or `str(uuid.uuid4())` only | Arbitrary strings → 400 |
| `schema_translate_map`: use `None` key for default-schema tables | `"public"` key doesn't remap `schema=None` tables |
| `AuditMixin` requires `BaseDBModel` | Raises `TypeError` on class definition |
| `cache.init()` raises on failure | Doesn't silently degrade — handle at startup |
| `Grid.query().fetch()` returns `PageResponse[GridRow]` | Iterate `.data`, not the page itself |
| `queue.make_worker()` returns `arq.Worker` | Not `arq.worker.WorkerSettings` |

## Story tracks

End-to-end consumer journeys exercised against real testcontainers (no mocks). Each is the canonical answer to "what does this substrate actually do?" — read the test file, then run it locally with `pytest <path>`.

| Story | What it proves | Path |
|:------|:---------------|:-----|
| **Knowledge ingestion lifecycle** | Declare a Bundle, upload a doc to MinIO, chunk + embed into Qdrant, walk the cross-Grid edge from a search hit back to the parent doc, archive cascades blob + vector cleanup. | [`tests/test_stories/test_knowledge_ingestion.py`](tests/test_stories/test_knowledge_ingestion.py) |
| **Multi-tenant isolation** | Two tenants share Postgres + Qdrant + MinIO; verified isolation across `Grid.query()`, namespace-prefixed Qdrant collections, and `Bundle.to_graph()` snapshots; archive in tenant A doesn't touch B. | [`tests/test_stories/test_multitenant_isolation.py`](tests/test_stories/test_multitenant_isolation.py) |
| **VECTOR storage modes** | Round-trips the four `storage_mode` settings (`none` / `inline` / `remote` / `both`) and asserts cell shape + Qdrant state per mode. | [`tests/test_stories/test_vector_storage_modes.py`](tests/test_stories/test_vector_storage_modes.py) |

Each story is decomposed into act-based test methods (`test_act1_declare_space` → `test_act5_archive_cascade`) so a regression localises to its act instead of stopping the whole flow.

## Reading paths

Curated entry points whether you're an LLM or a human reading cold:

- **Per-module reference** — one `docs/<extra>.md` per shipped extra (linked from each badge in the level tables above). Each page covers purpose, install, public API, quick example, and cross-refs. Module reference: [database](docs/database.md) · [cache](docs/cache.md) · [flow](docs/flow.md) · [vault](docs/vault.md) · [storage](docs/storage.md) · [store](docs/store.md) · [queue](docs/queue.md) · [vector](docs/vector.md) · [grid](docs/grid.md) · [graph](docs/graph.md) · [space](docs/space.md) · [api](docs/api.md) · [worker](docs/worker.md) · [log](docs/log.md) · [error](docs/error.md) · [types](docs/types.md).
- **Catalog** (source of truth) — [`src/forktex_core/catalog/catalog.json`](src/forktex_core/catalog/catalog.json). Loaded at runtime as `forktex_core.catalog.current`; every README table is rendered from it.
- **Examples** — five runnable scripts under [`examples/`](examples/) (`graph_in_memory.py`, `api_minimal.py`, `worker_minimal.py`, `grid_crud.py`, `space_bundle.py`). Each exposes a `run()` callable; integration tests in `tests/test_examples/` keep them current.
- **Story tracks** — see the table above. Real testcontainers, no mocks.
