# Grid binding — wiring the grid over an existing database

> **Status:** the mechanism is the `Grid` façade with declarative `Overlay` /
> `Extension` specs — not the procedural `bind_extension` / `set_extension` / `bind_table` /
> `query_bound` functions this note originally sketched (those 3.x names were removed). Overlay =
> `Grid.declare(TableSpec(binding=Overlay(...)))` (read-only projection); extension =
> `Grid.declare(TableSpec(binding=Extension(...)))` + `Grid.create(values, external_ref=host_pk)` +
> `Grid.get_by_external_ref(...)`, with extension rows interconnected by ordinary relations. The
> end-to-end coverage is `tests/test_grid/test_crm.py`. The requirement/rationale below still holds.

> How a host application wires `forktex_core[grid]` over its *existing* physical tables to (a) attach
> tenant-defined fields to existing rows, (b) treat existing tables as grid entities, and (c) relate
> them — behind a stable, evolvable seam.

## The requirement

A host already has typed tables (e.g. `client_record`, `invoice`, `project`) with UUID primary keys and
a tenant column. It wants to let tenants **extend** those entities with custom fields and **relate**
them — without the grid owning or altering the host tables, and without coupling the grid to the host's
ORM. The seam must stay stable while both the grid and the host evolve independently.

## Two mechanisms (deliberately separate)

### 1. Extension — custom fields on existing rows (non-invasive, primary)

An **extension** is an ordinary `owned` grid table whose every row is linked 1:1 to a host row by
`GridRow.external_ref` (the host row's UUID PK). Custom fields are ordinary grid columns; their values
live in the JSONB payload (and, if promoted, in the native sidecar). The host table is never touched.

- **Why this shape:** it reuses the *entire* existing engine — validation, typed columns, capabilities,
  filters/sort/pagination, promoted sidecars, relations, introspection — with **zero new query paths**.
  It is the mirror of the host storing a link to the grid; here the grid row stores `external_ref → host
  PK`, so the host joins `host.id = grid_row.external_ref` to read custom fields alongside its row.
- **API:** `bind_extension(session, *, slug, namespace, physical_relation, primary_key="id",
  columns=[...])` creates the owned table tagged (via `binding = {"kind": "extension",
  "physical_relation": ..., "primary_key": ...}`) as an extension; `set_extension(session, *, table,
  external_ref, values)` upserts the one grid row for a host id; reads are normal `query_rows` (rows
  carry `external_ref`).
- **This is the recommended default** for "add tenant fields to clients/invoices/…". It is additive,
  reversible, and never risks the host schema.

### 2. Bound overlay — query an existing table AS a grid entity

A `bound` grid table presents an existing physical table through the grid query API. Its `binding`
describes the mapping; the query engine reads from the host table instead of `grid_row`.

- **binding descriptor** (on `GridTable.binding`, already in the schema):
  ```json
  {"kind": "overlay", "physical_relation": "public.client_record", "primary_key": "id",
   "namespace_column": "org_id", "column_map": {"name": "display_name"},
   "column_types": {"id": "uuid", "org_id": "uuid", "name": "text"}, "writable": false}
  ```
  `column_map` is grid-key → host-column (defaulting to identity); `namespace_column` maps the grid
  namespace onto the host tenant column; `projection_predicate` (existing field) is always AND-ed in.
  `column_types` is reflected from the host at `bind_table` time (the host table must exist) and used to
  cast comparison literals to the host columns' **native** types — so filters/scoping run as `uuid =
  uuid` (not `uuid = varchar`) and stay index-able.
- **Read:** `query_rows` builds a SELECT over `physical_relation` (aliased), projecting `primary_key AS
  id` + mapped columns, compiling the capability-gated filter/sort/pagination against the host columns,
  and returns grid-shaped transient rows (`id` = host PK, `payload` = mapped values). Overlay rows are
  **not** persisted in `grid_row`. Pagination is **offset-only** (no cursor). Columns are plain
  projections — `ref` / `derived` / `promoted` are refused (no host column, no write path).
- **Write:** bound tables are **read-only** through the grid (write the host directly). Enriching a
  query with columns from another *bound* table is host-side (the host owns the physical join).
- Overlay is the heavier capability (a parallel FROM path) and is secondary to extension.

## Relations across the boundary

**Relations connect owned tables** (`create_relation` refuses a bound endpoint): `grid_edge` references
real `grid_row`s, and a bound overlay has no row to relate/resolve. A host links an existing row into the
grid graph through its **extension** row — a real owned `grid_row` (UUID id) keyed to the host PK — which
participates in relations, `on_delete`, `neighbors`/`traverse`/`subgraph`, and `derived` unchanged. This
is network's HYBRID+relation shape rebuilt cleanly, and is validated end-to-end by
`tests/test_grid/test_integration_host.py`.

**External ids are UUID — the common denominator (recommend `uuid7`).** `GridRow.external_ref` is `uuid`
(the frozen `v0001` baseline). Bound *reads* pass a non-UUID host PK through as the transient row id
(validated over a `bigint` host), but *linking* — extension or relations — is UUID-typed. A host with
non-UUID keys therefore supplies a **surrogate UUID** to extend/relate. Widening `external_ref` (and an
edge external-id encoding) to arbitrary keys is a possible additive `v0002`, not needed for the UUID-PK
(or surrogate-UUID) host case; the descriptor is forward-shaped (`primary_key` + a future
`primary_key_type`).

## The consumption seam — why registration + descriptor, not a mixin/SuperClass

Chosen: **a declarative binding descriptor (data) + stable `bind_extension` / `bind_table` registration
functions**, called once at host boot. Considered and rejected as the *primary* seam:

- **ORM mixin** (host model inherits `GridExtended`): couples the grid to the host's SQLAlchemy models;
  every host refactor risks the grid; hard to version. *Offer only as an optional thin ergonomic wrapper
  over `external_ref`, never the contract.*
- **Facade / SuperClass the host subclasses**: forces an inheritance hierarchy and in-process object
  identity; poor fit for a declarative, multi-tenant, data-driven catalog.

The registration + descriptor seam is **open ⊕ closed**: the public surface is `grid.__all__` + the
binding-descriptor JSON schema (both SemVer-frozen and guarded by the contract test); new capabilities
arrive as *additive* descriptor keys and new functions, so the grid and any host evolve independently
behind it. The binding is *data*, so it can be introspected, diffed (`describe_schema`/`apply_schema`),
serialized, and migrated — the same way the rest of the catalog already is.

## Implementation

Both mechanisms are the one `Grid` façade parameterised by its binding. **Overlay:**
`Grid.declare(TableSpec(binding=Overlay(physical_relation=..., namespace_column=..., column_map=...)))`
gives a read-only projection; queries run through the same compiler as owned tables (dispatched on
`table.storage.kind`), with host column types reflected at declare time. **Extension:**
`Grid.declare(TableSpec(binding=Extension(...)))` is an ordinary owned table; each row links to a
host row via `Grid.create(values, external_ref=host_pk)` (unique per `(table, external_ref)`), read back
with `Grid.get_by_external_ref(host_pk)`. Cross-boundary linkage goes through those extension rows — real
owned `grid_row`s keyed to the host PK — which participate in relations, `on_delete`, graph traversal, and
`derived` unchanged. See `tests/test_grid/test_crm.py` for the end-to-end coverage.
