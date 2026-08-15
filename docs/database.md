# `forktex_core.database` — Async Postgres substrate

> Async SQLAlchemy 2 engine, session lifecycle, ORM base classes, composable mixins, CRUD helpers, advisory locks, SQL-file migration runner, and the shared primitives every higher package builds on: filters, pagination, DDL, reflection, identifiers and integrity boundaries.

## Overview

`database` is the lowest shipped Postgres facade — every other module that touches Postgres builds on top of it, and that is now literal rather than aspirational: `flow`, `grid` and `space` get their connection handling, filter AST, page shape, DDL, reflection, identifier policy, integrity mapping and advisory locks from here instead of carrying their own. It is always bundled (no extras required). The design philosophy: zero magic, minimal surface, explicit lifetimes.

**Everything here is SQLAlchemy-native.** No module in this library builds a SQL
string: DDL that Core lacks (`ADD COLUMN`, `DROP COLUMN`) is a `DDLElement` with
a `@compiles` hook, so identifiers are quoted by the dialect's preparer and every
statement can be compiled and asserted with no database attached.

```bash
pip install forktex-core   # database is always included
```

## Quick start

```python
from forktex_core.database import (
    init_engine,
    close_engine,
    get_session,
    BaseDBModel,
    AuditMixin,
    NamespacedMixin,
    create,
    paginate,
    SchemaMigrationRunner,
    advisory_lock,
)
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
import uuid

# 1. Boot — call once at startup
init_engine("postgresql+asyncpg://user:pass@localhost/db", pool_size=10)


# 2. Define a model
class Project(BaseDBModel, NamespacedMixin, AuditMixin):
    __tablename__ = "project"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(255))


# 3. Use a session
async with get_session() as session:
    project = await create(session, Project, namespace=str(org_id), name="Alpha")

# 4. Shutdown
await close_engine()
```

## API reference

### Connection

```python
class Database:
    """One engine + sessionmaker. Construct as many as the process needs."""

    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        schema_translate_map: dict[str | None, str | None] | None = None,
        **engine_kwargs,      # pool_size, max_overflow, pool_pre_ping, ...
    )
    engine: AsyncEngine
    sessionmaker: async_sessionmaker
    def session(self) -> AsyncContextManager[AsyncSession]   # commit/rollback
    def session_scope(self) -> AsyncGenerator[AsyncSession]  # for Depends()
    async def dispose(self) -> None


# Module-level API — a thin wrapper over one lazily-created default `Database`.
# Kept because a single-database service wants exactly this, and because
# `get_session` is the FastAPI dependency. A process that needs two pools (or
# `flow` sharing the pool a consumer already has) uses `Database` directly.
def init_engine(
    db_url: str,
    *,
    echo: bool = False,
    schema_translate_map: dict[str | None, str | None] | None = None,
    **engine_kwargs,          # pool_size, max_overflow, pool_pre_ping, ...
) -> async_sessionmaker

async def close_engine() -> None

@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]
    # Commits on exit, rolls back on exception

def with_transactional_session(func) -> func
    # Decorator: guarantees a committed transaction boundary around func.
    # Detects a caller-provided session anywhere in args/kwargs (works when
    # decorating an instance method too, where self is the first arg) — if
    # none is found, opens and manages one via get_session().
```

### Models & mixins

```python
class BaseDBModel(DeclarativeBase)
    # Base for CONSUMER ORM models. Maps StrEnum → VARCHAR(64) and
    # datetime → timestamptz (see UtcDateTime), so a bare Mapped[datetime]
    # cannot accidentally declare a naive column.

def substrate_base(schema: str) -> type[DeclarativeBase]
    # A base for a LIBRARY-OWNED schema, on its own MetaData. `flow` and `grid`
    # use it so `BaseDBModel.metadata.create_all()` — the documented way for a
    # consumer to build its own tables — never tries to create forktex's
    # internal substrate in schemas the consumer never asked for.

UtcDateTime = sa.TIMESTAMP(timezone=True)
    # The canonical temporal column type. Always tz-aware, to stay in sync with
    # `forktex_core.iso`: asyncpg *rejects* writing an aware datetime into a
    # naive `timestamp` column, so a naive column is a latent crash for any code
    # that assigns `iso.now()`.

class TimestampMixin
    # created_at, updated_at (server-side defaults)

class AuditMixin(TimestampMixin)
    # + created_by_id, updated_by_id (UUID, no FK — add your own)
    # + archived_at, is_active (soft delete)
    # + CHECK constraint: is_active ⟺ archived_at IS NULL
    # optional: unique_fields = ("org_id", "name") → partial unique index

class NamespacedMixin
    # namespace: str (255), indexed, NOT NULL
    # Tenant-scoping primitive with no FK — set to str(org_id) or any
    # tenant-discriminating string. Consumers with their own tenant
    # table declare a local FK mixin in their service.

class JsonModelColumn(Generic[T])
    # .serialize(models) → list[dict]
    # .deserialize(data, Model) → list[Model]
```

### CRUD helpers

```python
async def get(session, model, value, *, key="id", options=None) -> T | None
async def find_one_by(session, model, **filters) -> T | None
async def list_all(session, model, *, options=None) -> list[T]
async def create(session, model, **values) -> T   # raises AlreadyExistsError on unique violation
async def paginate(session, model, page=1, page_size=100, conditions=None, ...) -> PageResponse[T]
async def paginate_scroll(session, model, limit=20, ...) -> ScrollResponse[T]

# Both are `Page[T]` subclasses (see Pagination below), not parallel shapes.
class PageResponse[T](Page[T]):   # offset pagination
    limit: int; current_page: int | None; total_pages: int | None
    # `data` and `total_count` remain the accepted input and the wire names
    # (`items` / `total` are the canonical Python ones)

class ScrollResponse[T](Page[T]):  # keyset pagination
    limit: int                     # structurally `Page` + the echoed limit
```

### Advisory locks

```python
@asynccontextmanager
async def advisory_lock(engine, key: int) -> None
    # Blocking session-scoped lock. Waits until acquired.

@asynccontextmanager
async def try_advisory_lock(engine, key: int) -> bool
    # Non-blocking. Yields True if acquired, False if not.

async def xact_lock(session, key: int) -> None
    # Transaction-scoped: Postgres releases it when the caller's transaction
    # ends, so there is nothing to unlock and no context manager. Use to
    # serialise work already inside a transaction (grid's numbering allocator).

def advisory_key(*parts: object) -> int
    # One deterministic derivation, replacing crc32 / server-side `hashtext` /
    # ad-hoc UUID folding. `hashtext` is not stable across Postgres majors, so
    # moving off it is a fix, not a tidy-up.

def key_from_uuid(value: UUID) -> int
    # Folds a UUID's 128 bits into a signed bigint, for row-scoped locks that
    # want the id's full entropy rather than a CRC of its text.
```

### Migration runner

```python
class SchemaMigrationRunner:
    def __init__(
        self,
        engine: AsyncEngine,
        schema: str,
        migrations_dir: Path,
        *,
        target_schema: str | None = None,   # override DDL target
        version_table: str = "schema_version",
    )
    async def apply(self) -> None           # advisory-lock-protected, idempotent
```

### Filters

The canonical filter contract, promoted from `grid` (which had the only
implementation that was actually used and tested). `database` previously shipped
an unrelated `FilterSpec`/`SortSpec`/`QuerySpec` DSL with **no consumers
anywhere**; it is gone.

```python
FilterNode = Comparison | And | Or | Not      # the AST
class Comparison: field: str; op: FilterOp; value: Any
class FilterOp(StrEnum)                       # eq ne gt gte lt lte in_ nin
                                              # contains icontains startswith
                                              # endswith is_null between ...
def parse_filter(raw: dict | None) -> FilterNode | None   # JSON boundary
class SortKey: column: str; direction: SortDirection

def compile_filter(node: FilterNode, source: FilterSource) -> ColumnElement[bool]
class FilterSource(Protocol)   # resolve a field name to a SQL expression
class ColumnSource             # plain-ORM implementation, with an allow-list
```

`compile_filter` owns the AST walk, the op→SQL mapping, LIKE escaping and the
depth/`IN`-size guards; a consumer supplies only column resolution. `grid` plugs
in its capability-gated resolver; anything with ordinary ORM columns gets
filtering for free from `ColumnSource`.

### Pagination

```python
class Page[T](BaseAppModel):
    items: list[T]; has_more: bool; next_cursor: str | None; total: int | None

def encode_cursor(values: list[Any]) -> str
def decode_cursor(cursor: str, *, expected_length: int | None = None) -> list[Any]
def keyset_predicate(levels, *, ascending: bool) -> ColumnElement[bool]
```

One page shape, replacing four that disagreed. The cursor is a **positional**
JSON array, so it describes whatever the query sorted by — the dict-shaped
predecessors could only describe one hardcoded column, which is what made
`flow`'s cursor silently skip and repeat rows on any other sort field.
`decode_cursor` raises `BadRequestError` on a malformed token rather than
returning `None` and restarting from page 1.

`total` is optional and `None` by default: counting is a second full predicate
evaluation, and two of the four predecessors did it on every page.

### DDL

```python
CreateTable / DropTable / CreateIndex / DropIndex / CreateSchema / DropSchema
    # re-exported from SQLAlchemy, which supports if_not_exists / if_exists natively

class AddColumn(DDLElement)    # AddColumn(column, if_not_exists=True)
class DropColumn(DDLElement)   # DropColumn(table, column, schema=..., if_exists=True)
```

`ADD COLUMN` / `DROP COLUMN` are the only things Core lacks (they live in
alembic's operations layer, a migration-authoring tool rather than a runtime
dependency). Both compile with **no database attached**, so the rendered SQL is
unit-testable, and a hostile identifier is quoted by the preparer
(`foo"; DROP TABLE x; --` → `"foo""; DROP TABLE x; --"`) rather than guarded by
a regex and interpolated.

### Reflection

```python
async def columns(executor, relation, *, schema=None) -> set[str]
async def column_types(executor, relation, *, schema=None) -> dict[str, TypeEngine]
async def type_ddl(type_) -> str
async def has_table(executor, relation, *, schema=None) -> bool
async def indexes(executor, relation, *, schema=None) -> set[str]
async def udt_names(executor, relation, *, schema=None) -> dict[str, str]
```

`Inspector`-backed via `run_sync`, accepting a session *or* a connection.
Replaces three hand-written `information_schema` queries and two `to_regclass`
probes. `column_types` returns type **objects**, not names: `timestamptz`
collapses to `timestamp` under SQLAlchemy's type naming, which would silently
discard tz-awareness. `udt_names` is the one deliberate `information_schema`
query left in the library — `udt_name` is Postgres's own token and has no
lossless equivalent in a reflected type.

### Identifiers

```python
MAX_IDENT = 128
IDENT_RE / SLUG_RE / SCHEMA_RE
def validate_identifier(name, what="identifier") -> None   # mixed case, ≤128
def validate_slug(slug) -> None                            # + hyphens
def validate_schema(schema) -> None                        # lower-case only
def validate_relation(relation) -> None                    # schema.table
def is_identifier(name) -> bool                            # predicate form
```

Three near-copies existed with *incompatible* policies (lower-case-only, no
length limit, bare `ValueError` in the migration runners vs mixed-case, ≤128,
`BadRequestError` in grid). Collapsing them onto one rule would have either
loosened the runners or broken grid's mixed-case keys, so the differences are
named profiles instead. All raise `BadRequestError`.

This is defence in depth, not the primary defence — identifiers reaching DDL
travel through the constructs above, where the preparer quotes them.

### Integrity boundaries

```python
@asynccontextmanager
async def integrity_boundary() -> None
    # IntegrityError → AlreadyExistsError (unique) / BadRequestError (FK,
    # not-null, check), by SQLSTATE.

@asynccontextmanager
async def read_boundary() -> None
    # SQLSTATE class 22 (data exception) → BadRequestError. A stored value that
    # will not cast to the type a query compares against is a 400, not a 500.
```

Detection is by **SQLSTATE** (23505 / 23503 / 23502 / 23514), not by
substring-matching the driver's message — the old `"unique" in str(exc).lower()`
test was locale- and driver-dependent. Messages are fixed and non-leaking: a
driver message can quote the offending values, so it reaches the caller only via
`__cause__`.

## Patterns

### Pattern 1 — schema isolation via `schema_translate_map`

```python
# All tables with schema=None land in fresh_schema.
# All forktex_grid.* tables also land in fresh_schema.
# Use None (not "public") as the key for default-schema tables.
init_engine(
    url,
    schema_translate_map={
        None: "fresh_schema",  # schema=None → fresh_schema
        "forktex_grid": "fresh_schema",  # explicit schema → fresh_schema
    },
)
```

### Pattern 2 — tenant-scoped model

```python
class Invoice(BaseDBModel, NamespacedMixin, AuditMixin):
    __tablename__ = "invoice"
    unique_fields = ("namespace", "number")  # partial unique index on active rows
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    number: Mapped[str] = mapped_column(sa.String(50))
```

### Pattern 3 — advisory lock for leader election

```python
import zlib

LEADER_KEY = zlib.crc32(b"myapp.driver-leader")

async with try_advisory_lock(engine, LEADER_KEY) as is_leader:
    if not is_leader:
        return  # another process is leader, retry later
    while not shutdown.is_set():
        await do_leader_work()
        await asyncio.sleep(poll_interval)
# Lock released automatically on exit or process death
```

### Pattern 4 — a typed filter from an API body

`parse_filter` is the JSON boundary; `ColumnSource` names the columns a caller is
allowed to filter on, so an unexpected field is a 400 rather than a leak.

```python
from forktex_core.database.filters import ColumnSource, compile_filter, parse_filter

node = parse_filter(body.get("filter"))        # BadRequestError on bad shape
stmt = sa.select(Project)
if node is not None:
    source = ColumnSource(Project, allowed={"name", "namespace", "is_active"})
    stmt = stmt.where(compile_filter(node, source))
```

### Pattern 5 — cursor pagination over a compound sort

The cursor and the `ORDER BY` must be built from the *same* expressions, or pages
skip and repeat rows. Passing the ordering expressions themselves is what
guarantees it.

```python
from forktex_core.database.pagination import Page, decode_cursor, encode_cursor, keyset_predicate

levels = [Project.created_at, Project.id]      # last one must be unique
stmt = sa.select(Project).order_by(*(c.asc() for c in levels)).limit(limit + 1)
if cursor:
    boundary = decode_cursor(cursor, expected_length=len(levels))
    stmt = stmt.where(keyset_predicate(list(zip(levels, boundary)), ascending=True))

rows = (await session.execute(stmt)).scalars().all()
has_more = len(rows) > limit
rows = rows[:limit]
next_cursor = encode_cursor([to_iso(rows[-1].created_at), str(rows[-1].id)]) if has_more else None
return Page(items=rows, has_more=has_more, next_cursor=next_cursor)
```

## Anti-patterns

```python
# ❌ "public" key doesn't remap schema=None tables
init_engine(url, schema_translate_map={"public": target})

# ✅ Use None key
init_engine(url, schema_translate_map={None: target})

# ❌ Using assert in hot path (stripped by -O flag)
assert _session is not None, "not initialized"

# ✅ Explicit check
if _session is None:
    raise RuntimeError("not initialized")


# ❌ AuditMixin without a declarative base
class Bad(AuditMixin):  # raises TypeError — the mixin adds mapped columns
    ...


# ✅ Pair AuditMixin with a declarative base
class Good(BaseDBModel, AuditMixin):
    __tablename__ = "good"


# ❌ Building a SQL string because Core "doesn't have" the construct
await session.execute(sa.text(f'ALTER TABLE "{schema}"."{t}" ADD COLUMN "{c}" text'))

# ✅ A construct the dialect compiles and quotes — and that a test can assert
await session.execute(AddColumn(column, if_not_exists=True))


# ❌ Mapping a library-owned table onto the consumer's registry
class MySubstrate(BaseDBModel):        # a consumer's create_all() now builds it
    __tablename__ = "my_thing"
    __table_args__ = {"schema": "my_substrate"}

# ✅ Its own registry
_Base = substrate_base("my_substrate")

class MySubstrate(_Base):
    __tablename__ = "my_thing"
```

---

## Agent guide

### Canonical forms

**Init pattern (every service `main.py`):**
```python
from contextlib import asynccontextmanager
from forktex_core.database import init_engine, close_engine


@asynccontextmanager
async def lifespan(app):
    init_engine(settings.database_url, pool_size=20, pool_pre_ping=True)
    yield
    await close_engine()
```

**Model definition (canonical):**
```python
import uuid
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
from forktex_core.database import BaseDBModel, NamespacedMixin, AuditMixin


class Widget(BaseDBModel, NamespacedMixin, AuditMixin):
    __tablename__ = "widget"
    unique_fields = ("namespace", "slug")  # optional partial-unique
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    is_public: Mapped[bool] = mapped_column(sa.Boolean, default=False)
```

**Migration runner (library-owned schema):**
```python
from pathlib import Path
from forktex_core.database import SchemaMigrationRunner

runner = SchemaMigrationRunner(
    engine=engine,
    schema="my_lib",
    migrations_dir=Path(__file__).parent / "migrations",
    version_table="my_lib_schema_version",  # distinguish from other libs
)
await runner.apply()  # safe to call from every worker on startup
```

Migration `.sql` files may use `{schema}` as a placeholder — it's a plain
string replace, not `str.format()`, so literal `{`/`}` in the SQL itself
(JSONB defaults like `'{}'::jsonb`, PL/pgSQL blocks) need no escaping and
are left untouched.

### Edge cases

| Scenario | Behaviour |
|---|---|
| `get_session()` before `init_engine()` | `RuntimeError("Engine/sessionmaker not initialized…")` |
| `get_session()` after `close_engine()` | Same `RuntimeError` — `close_engine()` also clears the sessionmaker, not just the engine |
| `create()` with duplicate unique key | `ConflictError(str(exc))` — caller must catch |
| `get(session, Model, missing_id)` | Returns `None` — not an error |
| `get(session, Model, val, key="bad")` | `AttributeError` — column doesn't exist |
| `with_transactional_session` decorating an instance method | Still detects the caller's session correctly — it scans all of `args`/`kwargs`, not just position 0 (where `self` would be) |
| `advisory_lock` — process dies while holding | Postgres auto-releases on connection drop |
| `try_advisory_lock` — lock not available | Yields `False` immediately, no wait |
| `SchemaMigrationRunner(migrations_dir=bad_path)` | `ValueError` on construction, before any DB call |
| `SchemaMigrationRunner(schema="bad; sql")` | `ValueError` — schema/target_schema/version_table must match `^[a-z_][a-z0-9_]*$` |
| `paginate(page=0)` / `paginate(page_size=0)` | Clamped to `1` / `10` internally |
| `paginate_scroll(limit=0)` | Clamped to `20` internally |
| `schema_translate_map={None: "my_schema"}` | Remaps all `schema=None` tables only |

### Error catalogue

| Error | Module | When raised |
|---|---|---|
| `DatabaseNotInitializedError` (an `AppError` *and* a `RuntimeError`) | `db.connection` | `get_session()` before `init_engine()` (or after `close_engine()`) |
| `AlreadyExistsError` | `db.integrity` | unique violation (SQLSTATE 23505), e.g. from `create()` |
| `BadRequestError` | `db.integrity` | FK / not-null / check violation (23503 / 23502 / 23514) |
| `BadRequestError` | `db.integrity` | `read_boundary()` sees a data exception (SQLSTATE class 22) |
| `BadRequestError` | `db.identifiers` | a name fails its profile (`validate_identifier` / `_schema` / `_slug` / `_relation`) |
| `BadRequestError` | `db.pagination` | `decode_cursor` given a malformed or wrong-length token |
| `BadRequestError` | `db.filters` | filter JSON is malformed, too deep, or names a disallowed field |
| `AttributeError` | `db.crud` | `get(session, Model, val, key="bad")` — column doesn't exist |
| `BadRequestError` | `db.migrate` | `migrations_dir` path doesn't exist or isn't a directory |
| `ServiceUnavailableError` | `db.migrate` | asyncpg backend not configured |

### Integration map

```
db ──── (used by) ──── flow       [Database handle, substrate_base, ddl, reflect,
                                   pagination (Page + keyset), identifiers,
                                   integrity, migration runner]
db ──── (used by) ──── grid       [substrate_base, ddl, reflect, filters
                                   (AST + compiler), pagination, locks
                                   (xact_lock), identifiers, integrity]
db ──── (used by) ──── space      [session injection, filters/pagination re-exports]
db ──── (used by) ──── vault      [EncryptedJSON TypeDecorator column type]
db ──── (used by) ──── every API  [get_session as a FastAPI dependency, ORM models]
```

`flow` and `grid` hold a `Database` rather than the module-level default, so a
consumer that uses both opens **one** pool and can hand it to them:

```python
db = Database(url, schema_translate_map={"forktex_flow": schema})
flow = Flow(database=db, schema=schema)   # borrows; close() won't dispose it
```

### Checklist

- [ ] `init_engine()` called exactly once at process startup
- [ ] `close_engine()` awaited on shutdown
- [ ] Models inherit `BaseDBModel` (not just `DeclarativeBase`)
- [ ] `AuditMixin` paired with `BaseDBModel`
- [ ] `schema_translate_map` uses `None` key for default-schema tables
- [ ] `SchemaMigrationRunner` uses `version_table=` to avoid name collision between libraries
- [ ] Advisory lock keys derived via `zlib.crc32(b"unique-name")` — not hardcoded ints
