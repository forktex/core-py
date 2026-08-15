# `forktex_core.database`

Async Postgres substrate: SQLAlchemy 2 engine and session lifecycle, ORM bases and mixins, CRUD
helpers, keyset pagination, advisory locks, a SQL-file migration runner, and the shared primitives
`flow`, `grid` and `space` are built on (filters, DDL, reflection, identifier policy, integrity
boundaries).

Always bundled — no extra required.

```bash
pip install forktex-core
```

Everything is SQLAlchemy-native. No module in this library builds a SQL string: DDL that Core lacks
(`ADD COLUMN`, `DROP COLUMN`) is a `DDLElement` with a `@compiles` hook, so identifiers are quoted by
the dialect's preparer and every statement can be compiled and asserted with no database attached.

## Wiring

Shape A — a module-level default, brought up once at startup and disposed at shutdown.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from forktex_core.database import close_engine, init_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_engine(
        "postgresql+asyncpg://user:pass@localhost/db",
        pool_size=10,
        pool_pre_ping=True,   # validate a pooled connection on checkout
        pool_recycle=1800,    # pre-empt server-side idle timeouts
    )
    yield
    await close_engine()


app = FastAPI(lifespan=lifespan)
```

`init_engine` returns the `async_sessionmaker` if you want to hold it directly. Any surplus keyword
is forwarded to `create_async_engine`.

Call it **once, in the lifespan** — not at module import. Import-time initialisation binds the pool
to whichever event loop happens to be current, which is not necessarily the one serving requests.
Re-initialising without disposing first logs a warning and leaks the previous pool; call
`close_engine()` before any deliberate re-init.

### Getting a session

Two forms over one implementation, so their commit/rollback semantics cannot drift:

```python
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.database import get_session, session_scope


# In a route — a plain async generator, which is what Depends requires.
async def list_projects(session: AsyncSession = Depends(session_scope)):
    ...


# In service, worker or script code.
async def sync_projects():
    async with get_session() as session:
        ...
```

> `Depends(session_scope)` is correct. `Depends(get_session)` is **not**: `get_session` is
> `session_scope` wrapped in `asynccontextmanager`, so FastAPI injects the context-manager object
> itself instead of an `AsyncSession`. This is the single most-reimplemented API in the library —
> a hand-written `get_db` that opens a sessionmaker, yields, commits on success and rolls back on
> error is exactly `session_scope`.

Both commit on success and roll back on exception. Do not commit inside the block.

### Accessing the live engine

`connection.engine` and `connection._async_sessionmaker` resolve through a module `__getattr__` to
the *current* default, so they stay correct across re-initialisation. A re-export shim in your own
`shared/database/connection.py` is unnecessary — import from `forktex_core.database` directly.

## Public surface

Import everything from the package root; the submodules below are exported deliberately.

### Connection

| Name | Purpose |
|:---|:---|
| `init_engine(url, *, echo=False, schema_translate_map=None, **kw)` | Create the default engine; returns its sessionmaker |
| `close_engine()` | Dispose the default engine and clear the handle |
| `session_scope()` | Async generator — the FastAPI dependency form |
| `get_session()` | The same, as an async context manager |
| `with_transactional_session(fn)` | Decorator injecting a session when the caller omits one |
| `Database` | The explicit object form, when a module-level default will not do |
| `DatabaseNotInitializedError` | Raised when a default is required but absent |

### Models

| Name | Purpose |
|:---|:---|
| `BaseDBModel` | Declarative base; maps `StrEnum` to a non-native string column and `datetime` to `timestamptz` |
| `substrate_base(schema)` | A base on its **own** `MetaData`, for library-owned schemas |
| `UtcDateTime` | The canonical timezone-aware column type |
| `TimestampMixin` | `created_at` / `updated_at` with server defaults |
| `AuditMixin` | Adds actor columns, soft delete, and archive-consistency constraints |
| `NamespacedMixin` | A plain `namespace` string column for tenant isolation — no FK |
| `JsonModelColumn` | Serialise lists of Pydantic models into JSON columns |
| `ReprMixin` | Readable `__repr__` |

`substrate_base` exists because `BaseDBModel.metadata` belongs to *you*: `create_all` on it is the
documented way to build your own tables. Library substrates (`forktex_flow`, `forktex_grid`) map onto
their own metadata so your `create_all` never tries to create them.

### CRUD and pagination

`get`, `create`, `find_one_by`, `list_all`, `paginate`, `paginate_scroll`, `ConflictError`.

Page shapes: `Page`, `PageResponse`, `ScrollResponse`. Cursor helpers: `encode_cursor`,
`decode_cursor`, `keyset_predicate`.

### Locks, identifiers, integrity, migrations

`advisory_lock`, `try_advisory_lock`, `xact_lock`, `advisory_key`, `key_from_uuid` ·
`validate_identifier`, `validate_schema`, `validate_slug`, `validate_relation`, `is_identifier` ·
`integrity_boundary`, `read_boundary` · `SchemaMigrationRunner` · the `ddl` and `reflect` submodules.

`integrity_boundary` maps an `IntegrityError` onto a typed error using the constraint name, rather
than regex-scraping the driver message — prefer it to pattern-matching asyncpg strings.

## Errors

`ConflictError` (re-exported from `forktex_core.error`) on a uniqueness violation, and
`DatabaseNotInitializedError` when no default engine has been created. Both are `AppError`
subclasses, so a single handler over `AppError` catches them — see [error.md](error.md).

## Gotchas

- A bare `Mapped[datetime]` becomes `timestamptz`, not a naive `timestamp`. This is deliberate:
  asyncpg rejects writing an aware datetime into a naive column, so a naive column would be a latent
  crash for anything assigning `iso.now()`.
- `AuditMixin` requires `__tablename__` to be a string on the concrete model; it validates this in
  `__init_subclass__`.
- `NamespacedMixin.namespace` carries no foreign key. The library takes no position on which of your
  tables holds tenant identity.
- `keyset_predicate` builds an OR-chain rather than a row-value comparison. It is correct, but on
  deep pages Postgres filters instead of seeking. See `backlog/core-py-state.md`.
