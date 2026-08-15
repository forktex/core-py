# `forktex_core.grid` — a fully-dynamic virtual database

> Runtime-defined tables, columns, sections, relations and indexes on top of Postgres. The grid is the dynamic middle tier of a **dual bijection**: generic tabular data above ⟺ grid ⟺ Postgres below. Tenant data lives in a shared JSONB `payload` by default, with opt-in promotion to native columns.

## Overview

A consumer can define tables and columns *at runtime* (described by rows in the `forktex_grid` catalog tables) without issuing DDL per tenant. The substrate is deliberately domain-neutral: no spreadsheet, document, or business coupling lives here.

Two axes describe any grid object:

- **Table ownership** — `owned` (rows live in `grid_row`) or `bound` (a view over an external physical table via a binding descriptor).
- **Column materialization** — `payload` (JSONB, the default), `promoted` (a native sidecar column), or `derived` (projected from a related row at query time).

Field **types** are an open registry (`grid_column.type_id` is a string, one `FieldTypeHandler` per type) — adding a type is a code-only change, no enum and no migration. Each type declares **non-refutable capabilities** (filterable / sortable / fuzzy / operator vocabulary / index kinds); a column may only *narrow* them, never widen.

```bash
pip install forktex-core          # grid runs on the core sqlalchemy/asyncpg deps
```

Provision the schema once (idempotent, forward-only):

```python
from forktex_core.grid import apply_migrations

await apply_migrations(engine, schema="forktex_grid")
```

## Quick start

```python
from forktex_core.grid import (
    FieldType,
    Grid,
    RelationShape,
    RelationSpec,
    TableSpec,
    declare_relation,
)

async with session_maker() as session:
    # Declare a table from a validated spec (columns are payload-materialized
    # by default). `TableSpec.from_dicts` is the JSON-friendly constructor.
    leads = await Grid.declare(
        session,
        TableSpec.from_dicts(
            slug="leads",
            label="Leads",
            namespace=str(org_id),
            columns=[
                {"key": "title", "label": "Title", "type_id": FieldType.text.value},
                {
                    "key": "status",
                    "label": "Status",
                    "type_id": FieldType.enum.value,
                    "config": {"options": ["open", "won", "lost"]},
                },
            ],
        ),
    )
    notes = await Grid.declare(
        session,
        TableSpec.from_dicts(
            slug="notes",
            label="Notes",
            namespace=str(org_id),
            columns=[{"key": "body", "label": "Body", "type_id": FieldType.text.value}],
        ),
    )

    # Create + patch + archive rows (values validated through the type handlers).
    lead = await leads.create({"title": "ACME Corp", "status": "open"})
    await leads.patch(lead.id, {"status": "won"})

    # Query: one filter AST, capability-gated, keyset/offset paging.
    page = await leads.query(
        filter={"column": "status", "op": "eq", "value": "won"},
        sort=[{"column": "title"}],
    )
    titles = [r.values["title"] for r in page.rows]

    # Declare a relation, then relate + traverse through the facade. The ORM rows
    # (`grid_relation` / `grid_edge`) are internal — the edge is maintained for you.
    note = await notes.create({"body": "Initial outreach"})
    await declare_relation(
        session,
        RelationSpec(key="has_note", source="leads", target="notes", shape=RelationShape.one_to_many),
        str(org_id),
    )
    await leads.relate("has_note", lead.id, note.id)

    linked = await leads.related("has_note", lead.id)     # -> [Row(note)]
    reachable = await leads.traverse(lead.id, direction="outbound", depth=2)
    await session.commit()
```

The `Grid` facade covers table declaration, single-row CRUD and query. Relations,
indexing, promotion, schema evolution and bound/extension tables are the
module-level functions (`declare_relation`, `reconcile_table_indexes`, …) — see
"Notes" below. An earlier procedural façade (`create_table` / `create_row` / `query_rows` /
`list_tables` / `describe_table` / …) is **not** part of 0.1.0; a contract test asserts
those names stay off the public surface.

## Field types & capabilities

Built-ins (`forktex_core.grid`): `text`, `integer`, `decimal`, `boolean`, `date`, `datetime`, `uuid`, `enum`, `json`, `ref`, `derived`. The `[space]` extra adds `vector` and `file` (rich handlers with write-time lifecycle hooks — VECTOR auto-embed, FILE cleanup on archive).

| type | filterable | sortable | fuzzy | notable ops |
|------|:---------:|:--------:|:-----:|-------------|
| text | ✓ | ✓ | ✓ | eq/ne/in/contains/startswith/endswith/fuzzy |
| integer, decimal | ✓ | ✓ | | eq/ne/lt/lte/gt/gte/in/between |
| date, datetime | ✓ | ✓ | | eq/ne/lt/lte/gt/gte/between |
| boolean | ✓ | ✓ | | eq/ne |
| enum | ✓ | ✓ | | eq/ne/in |
| uuid, ref | ✓ | | | eq/ne/in |
| json, derived | | | | (opaque to the typed filter path) |

`is_null` is available on every filterable type. The query engine rejects an operator or a sort a column's capabilities don't declare (a `BadRequestError`, never a 500).

## Relations, integrity & indexing

- **Relations** (`RelationSpec` + `declare_relation`, backed by internal `grid_relation`/`grid_edge` rows) model `one_to_one` / `one_to_many` / `many_to_one` / `many_to_many` (the last routes through a `through_table`; `many_to_one` is the canonical foreign key — many rows referencing one target). A `ref` column projects a relation, and writing the ref value transactionally maintains the edge (validating the target exists, is live, is in the target table, and shares the namespace); `derived` columns read a target field through it.
- **Deletion policy** — each relation carries an `on_delete` of `restrict` (block), `cascade` (archive the referencing source rows), or `set_null` (clear the `ref`). `archive_row` runs the deletion planner atomically (validates the whole cascade closure before mutating), cycle-safe.
- **Indexing** — declare a `GridIndex` (`btree` / `btree_numeric` / `trgm`) and call `reconcile_table_indexes`; it materializes the physical index idempotently. `is_unique` columns get a partial UNIQUE index; relation cardinality gets partial unique indexes on `grid_edge`. All enforcement is real DB-level integrity.
- **The DDL is SQLAlchemy, not strings.** Indexes and promoted-column sidecars are `sa.Index` / `sa.Table` constructs driven through `forktex_core.database.ddl`, so identifiers are quoted by the dialect's preparer and every statement can be rendered and asserted with no database attached (`build_payload_index` / `render_ddl` expose that; `build_payload_index_ddl` still returns the string for the out-of-band `CONCURRENTLY` path). The index expression and the query-side cast both derive from `PG_CAST_TYPES`, so they cannot drift apart and stop the planner using the index.
- **Numbering** — `next_in_series` allocates strictly-gapless `1, 2, 3, …` counters (deterministic row id + advisory lock + JSONB upsert), safe under concurrent writers.

## Introspection

The catalog is self-describing — read back everything you declared, as the same
round-trippable specs you declared it with:

```python
spec = leads.describe()            # -> TableSpec (columns, indexes, relations, binding)
schema = await space.describe()    # -> Schema (the whole namespace, interconnected)
```

`TableSpec`/`Schema` are the export format, the import format and the diff input —
one vocabulary rather than a separate lossy `*Descriptor` family. That makes them
the basis for schema export, an admin/studio UI, codegen, and diffing deployments.

## Schema evolution & bulk DML

The catalog evolves live. Per table, through the facade: `add_column`,
`alter_column` (non-type attributes), `rename_column` (payload data migrated in
place), `drop_column` (soft-drop; the physical index/sidecar is reconciled away).

Whole-namespace convergence goes through `Namespace`, declaratively:

```python
report = await ns.apply(desired_schema, prune=False, allow_destructive=False)
plan = await ns.apply(desired_schema, dry_run=True)      # the ChangeSet, no mutation
```

`apply` diffs the desired `Schema` against the catalog and converges idempotently;
`prune=True` removes what the desired state omits, and destructive changes are
refused unless `allow_destructive=True` (so a typo cannot drop a column). For
throughput, `Grid.create_many` batches row writes, and `Namespace.batch(schema, rows)`
applies a schema change plus a sequence of `RowOp`s in one transaction.

## Wiring over an existing physical database

The grid can be wired *over* a host application's existing Postgres tables — extending them and relating across the boundary without altering them. Three non-invasive mechanisms (see [grid-binding-design.md](grid-binding-design.md)):

- **Promoted columns** — `reconcile_table_promoted` materializes a `payload` column into a native column in a per-table sidecar (`grid_promoted_<crc32>`), dual-written inside the transaction, so native types/constraints/indexes back the data.
- **Bound tables** (an `Overlay` binding) — register an existing physical table as a **read-only** grid entity by declaring `binding=Overlay(...)` on its `TableSpec` (`physical_relation`, `primary_key`, `column_map`, `namespace_column`). Queries then read straight from the host table (no copy into `grid_row`), return grid-shaped rows (`id` = host PK), and apply filters/sorts/pagination + capability gating against the native columns — with comparison literals cast to the host's own types, reflected at bind time. Writes through the grid are refused; write the host directly.

  ```python
  from forktex_core.grid import FieldType, Grid, Overlay, TableSpec

  clients = await Grid.declare(
      session,
      TableSpec.from_dicts(
          slug="clients",
          label="Clients",
          namespace=str(org_id),
          columns=[{"key": "name", "label": "Name", "type_id": FieldType.text.value}],
          binding=Overlay(
              physical_relation="public.client_record",
              primary_key="id",
              namespace_column="org_id",
              column_map={"name": "display_name"},
          ),
      ),
  )
  page = await clients.query(filter={"column": "name", "op": "icontains", "value": "ac"})
  ```

- **Extension of host rows** (an `Extension` binding) — attach tenant-defined columns to an existing host row, keyed 1:1 to the host primary key via the row's `external_ref`, without touching the host table. `Grid.create(..., external_ref=host_pk)` writes one, `Grid.get_by_external_ref(host_pk)` reads it back.

## Migrations & the stability guarantee

The schema ships as forward-only SQL migrations applied by `apply_migrations`
(idempotent, advisory-locked, multi-worker safe) — the frozen baseline
`v0001__schema.sql` plus additive `v0002+` files. `flow` is brought up the
identical way (`forktex_core.flow.apply_migrations`, same signature), and both run
on `forktex_core.database.migrate.SchemaMigrationRunner`.

From 0.1.0 the public surface (`grid.__all__`) is a stability commitment guarded
by a contract test that fails on any silent addition *or* removal, and asserts the
the retired procedural names stay retired. The baseline is the "never break" anchor.

## Built on `database`

Grid keeps no private copies of the shared primitives — it holds the substrate
vocabulary, and everything mechanical comes from `forktex_core.database`:

| Grid surface | Implementation |
|:---|:---|
| `FilterOp`, `FilterNode`, `parse_filter`, `SortKey` | `database.filters` (promoted *from* grid — it had the only real implementation) |
| `Page` / `Row` | `Page` is `database.pagination.Page[Row]`; `rows` stays grid's spelling, in Python and on the wire |
| `_kernel.identifiers` | re-export shim over `database.identifiers` |
| `_kernel.integrity` | re-export shim over `database.integrity` (SQLSTATE-based) |
| reflection in reconcilers / bindings | `database.reflect` (`Inspector`-backed; no `information_schema` copies) |
| the numbering allocator's lock | `database.locks.xact_lock` + `key_from_uuid` |
| ORM base | `database.models.substrate_base("forktex_grid")` — its own `MetaData`, so a consumer's `create_all()` never builds grid's substrate |

`compile_filter` lives in `database` and takes a `FilterSource`; grid supplies the
resolver that maps a column key to its payload/overlay expression and applies the
capability gate. Consumers with ordinary ORM columns get the same filtering from
`database.filters.ColumnSource`.

## Notes

- **The `Grid` facade is intentional partial sugar.** `Grid.declare` / `create` / `get` / `patch` / `archive` / `query` / `relate` / `related` / `traverse` cover table declaration, single-row CRUD, query and edges. Whole-namespace declaration and convergence live on `Namespace` (`declare`, `apply`, `batch`, `describe`); `declare_relation` and the reconcilers are module-level. Together they are the full API; the facade is a convenience over the common path.
- **`patch_row` merges whole columns.** There is no JSONB sub-path (cell-level) update — patch a column by writing its new value. This is a deliberate design choice, not a missing feature.

## Related

- `forktex_core.space` — bundles member grids under a `Bundle`, adds the rich VECTOR/FILE handlers and cross-grid traversal. See [space.md](space.md).
- [grid-binding-design.md](grid-binding-design.md) — wiring the grid over an existing physical database (bound overlays + host-row extensions).
- Runnable example: [`examples/grid_crud.py`](../examples/grid_crud.py).
