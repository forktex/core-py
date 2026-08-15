# `forktex_core.grid`

A self-describing dynamic database: tables, columns, relations and rows defined at **runtime** and
stored in the `forktex_grid` schema. You declare the shape you want as data; grid converges the
schema toward it.

```bash
pip install "forktex-core[grid]"
```

Use it when the shape is not known at deploy time — per-tenant custom fields, user-defined record
types, agent-authored state. When your tables are known up front, use `database` and real DDL.

## Wiring

Shape C — you construct a `Namespace` over a session; there is no global state.

Migrate the substrate once at startup, then work through a namespace per request:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from forktex_core.database import close_engine, connection, init_engine
from forktex_core.grid import apply_migrations


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_engine(settings.db_url)
    await apply_migrations(connection.engine)
    yield
    await close_engine()
```

`apply_migrations` takes an `AsyncEngine` and an optional `schema=`. It is idempotent and safe to run
concurrently.

```python
from forktex_core.grid import ColumnSpec, Namespace, Schema, TableSpec

ns = Namespace(session, str(org_id))

await ns.apply(
    Schema(
        tables=[
            TableSpec(
                slug="invoice",
                columns=[
                    ColumnSpec(key="number", type_id="text"),
                    ColumnSpec(key="total", type_id="number"),
                    ColumnSpec(key="issued_at", type_id="date"),
                ],
            )
        ]
    )
)

invoices = await ns.table("invoice")
row = await invoices.create({"number": "INV-001", "total": 250})

page = await invoices.query(
    filter={"column": "total", "op": "gte", "value": 100},
    sort=[{"column": "issued_at", "direction": "desc"}],
    limit=50,
)
for row in page.rows:
    print(row.values["number"])
```

`filter` and `sort` accept the typed `FilterNode` / `SortKey` objects or the plain-dict wire forms
shown here, which are coerced at the boundary — so a filter can arrive straight from a request body.

`apply()` accepts a typed `Schema` **or** a plain JSON dict, so a schema can be built at runtime or
loaded from a file. Options: `prune=True` makes the schema authoritative (anything absent is
removed), `allow_destructive=True` is required before drops or type-tightening, and `dry_run=True`
plans without writing. It returns a reconcile report as a JSON dict.

Use `ns.batch(schema=..., rows=[...])` to apply schema and data in one transaction.

## Public surface

### Entry points

| Name | Purpose |
|:---|:---|
| `Namespace(session, namespace)` | The tenant-scoped front door |
| `Namespace.apply(schema, ...)` | Converge toward a declared schema |
| `Namespace.batch(schema, rows, ...)` | Schema plus data, one transaction |
| `Namespace.table(slug)` / `.declare(spec)` | Open or declare a table, returning a `Grid` |
| `Namespace.describe()` | Read the live schema back |
| `Grid` | A single table: rows, columns, relations |
| `apply_migrations(engine, *, schema=...)` | Bring up the `forktex_grid` substrate |

### Declaring

`Schema`, `TableSpec`, `ColumnSpec`, `IndexSpec`, `RelationSpec`, `declare_relation`,
`RelationShape`, `Cardinality`, `OnDelete`, `Materialization`, `Capabilities`, `Extension`.

### Rows and reading

`Row`, `RowOp`, `Page`, `CellValue`, `FilterOp`, `BrowseMode`, `Overlay`, `WriteContext`.

`Grid` methods: `create`, `create_many`, `get`, `get_by_external_ref`, `patch`, `archive`, `query`,
`relate`, `unrelate`, `related`, `traverse`, `describe`, `next_number`, `reconcile`, and the column
operations `add_column`, `alter_column`, `rename_column`, `drop_column`.

### Field types

`FieldType`, `FieldTypeHandler`, `register_field_type`, `is_registered`.

`type_id` is an open `VARCHAR(64)` validated against the handler registry, so the built-in enum is a
seed rather than a closed set — register your own handler to add a type. The `[space]` package
registers rich `file` and `vector` handlers this way; see [space.md](space.md).

### Agent surface

`grid.ops` exposes the operations as declarative, JSON-serialisable commands (`ApplySchema`,
`ApplyBatch`, `Insert`, `Patch`, `Archive`, `Query`, `Get`, `Relate`, `Unrelate`, `DescribeSchema`)
plus `TOOLS`, `tool_schemas()` and `run()` — intended for LLM tool-calling, where the model emits a
command object rather than Python.

## Errors

| Error | Raised when |
|:---|:---|
| `NotFoundError` | Table, row or relation does not exist |
| `BadRequestError` | Invalid spec, filter, or value for a column's type |
| `AlreadyExistsError` | Slug or key collides within the namespace |
| `ReadOnlyStorage` | A write was attempted against a read-only overlay |

All are `forktex_core.error` types, so one `AppError` handler covers them — see [error.md](error.md).

## Gotchas

- A destructive reconcile is refused unless `allow_destructive=True`. Pair it with `dry_run=True`
  first and read the report.
- `namespace` is a plain string, defaulting to `""`. Tenant isolation is by convention, enforced by
  passing the right value — there is no ambient tenant context.
- The `forktex_grid` schema is owned by this package and mapped onto its own metadata, so your
  `BaseDBModel.metadata.create_all()` will not create it. Use `apply_migrations`.
- Migrating from 2.x: the procedural API (`grid.service`, `grid.models`, `grid.schemas`,
  `grid.enums`, `grid.types.*`, `grid.adapters`, `grid.catalog`) is gone with no shim, and
  `grid.space.Space` is now `Namespace`. See
  [migration-2.x-to-0.1.md](migration-2.x-to-0.1.md).
