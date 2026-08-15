# forktex_core.space

A `Bundle` groups related Grids under one namespace with shared rich-content config, and importing the package registers the rich `file` and `vector` field-type handlers into `grid`. Pure-tabular consumers stay on bare `grid`.

## Install

```bash
pip install "forktex-core[space]"
```

The `[space]` extra declares no packages of its own — it runs on `grid` and `graph`, both of which sit on the core `sqlalchemy`/`asyncpg` dependencies. The rich handlers **soft-compose** the two extras that do pull packages:

| Extra | When it is needed | What happens without it |
| --- | --- | --- |
| `[storage]` (aioboto3) | A `file` field with `delete_on_archive=True` (the default) is archived. | Never raises — the archive already committed. Logs at **WARNING** with the orphan count: `space.file: delete_on_archive is set but the [storage] extra is not installed; orphaning N blob(s)`. |
| `[vector]` (qdrant-client) | A `vector` field with `storage_mode="remote"` or `"both"` is written. | **Raises `ImportError`** naming the extra and suggesting `storage_mode="inline"`. |

The write-path `ImportError` is deliberate: `remote` strips the inline vector from the row payload, so skipping the Qdrant upsert would report a successful write and lose the embedding. On the *archive* path, missing `[vector]` is soft — it logs at DEBUG and leaves the points orphaned, since the archive already committed.

## Wiring

Shape C — consumer-owned objects, no module-level singleton and nothing to close. A `Bundle` holds the `AsyncSession` you hand it, so it composes inside your transaction; `space` itself contains no raw SQL and never reaches for a global engine. The storage and vector clients it talks to are registered on their own modules.

Importing the package is not inert: `import forktex_core.space` side-effect-registers `RichFileType` and `RichVectorType` into `grid`'s field-type registry, process-wide. That registration is what makes `type_id: "file"` and `type_id: "vector"` resolvable in a `TableSpec`.

```python
from forktex_core.grid import FieldType, Grid, TableSpec
from forktex_core.space import Bundle, BundleConfig, VectorDefaults
from forktex_core.storage import register as register_storage
from forktex_core.vector import register as register_vector

register_storage("default", url="http://minio:9000", bucket="kb", access_key="...", secret_key="...")
register_vector("default", qdrant_url="http://qdrant:6333")

documents = await Grid.declare(
    session,
    TableSpec.from_dicts(
        slug="documents",
        label="Documents",
        namespace=str(org_id),
        columns=[
            {"key": "title", "label": "Title", "type_id": FieldType.text.value},
            {"key": "source", "label": "Source", "type_id": "file", "config": {"client_name": "default"}},
        ],
    ),
)

bundle = await Bundle.declare(
    session,
    namespace=str(org_id),
    slug="kb",
    config=BundleConfig(vector=VectorDefaults(dimensions=384, storage_mode="remote")),
    members=[documents],
)
```

## Public surface

`__all__`:

| Name | What it is |
| --- | --- |
| `Bundle` | The facade over a persisted `GridSpace` row and its member Grids. |
| `BundleConfig` | Frozen aggregate: `vector: VectorDefaults`, `storage: StorageDefaults`, `edge_vocab: tuple[str, ...]` (empty means no restriction). |
| `VectorDefaults` | Frozen: `model`, `dimensions`, `storage_mode` (`"none"`/`"inline"`/`"remote"`/`"both"`, default `"remote"`), `collection_prefix`. |
| `StorageDefaults` | Frozen: `bucket`, `prefix`. |
| `SyncSourceConfig` | Frozen contract for consumer-defined sync drivers: `kind`, `options`, `schedule`. Core holds the config; the driver lives on the consumer. |

`Bundle` methods: `Bundle.declare(session, *, namespace, slug, label=None, config=None, sync_sources=(), members=())`, `Bundle.bind(session, *, namespace, slug)`, `attach(grid)`, `detach(grid_slug)`, `grid(slug)`, `list_grids()`, `materialize()`, `to_graph(*, entity_slugs=None, include_inactive=False)`, `traverse(start_row_id, *, max_depth=3, edge_kind=None, direction="both", entity_slugs=None)`. Properties: `slug`, `id`.

Per-field config models are importable from their handler modules:

```python
from forktex_core.space.types.file import FileConfig, RichFileType
from forktex_core.space.types.vector import RichVectorType, VectorConfig
```

## Errors

| Raised | When |
| --- | --- |
| `AlreadyExistsError` | `Bundle.declare` with a `(namespace, slug)` that already exists. |
| `NotFoundError` | `Bundle.bind` on a missing bundle, or a member `Grid` handle whose catalog row is gone. |
| `KeyError` | `Bundle.grid(slug)` for a slug not in the binding map. |
| `BadRequestError` | A `file` cell that is not a string or a dict with a string `storage_key`; a `vector` cell that is not `list[float]` or a descriptor carrying `vector` or `point_id`; a vector whose length disagrees with `dimensions`. |
| `ImportError` | `[vector]` missing on a `remote`/`both` row write (above). |

Catch `AppError` (`AlreadyExistsError`, `NotFoundError`, `BadRequestError` all derive from it) at the API boundary — `forktex_core.api`'s envelope already renders it.

## Gotchas

- **Missing `[vector]` on a `remote`/`both` write now raises.** It previously logged at DEBUG and dropped the embedding silently. Set `storage_mode="inline"` if you genuinely want vectors in the row payload.
- **File cleanup never raises, but it is no longer silent.** Every path that leaves a blob behind — missing `[storage]`, unregistered client, a failing `delete` — logs at WARNING with the orphan count. Watch for that string rather than assuming clean archives.
- **Handler registration is global and unconditional.** Importing `forktex_core.space` anywhere in the process replaces the `file`/`vector` handlers for every Grid, not just bundled ones. The registration is guarded by `is_registered(...)`, so re-import is a no-op. To swap in your own handler afterwards, call `grid.register_field_type(MyHandler(), replace=True)` — a plain re-register raises `ValueError`.
- **`collection_prefix` on `BundleConfig` is not propagated.** `Bundle.declare` does not stamp it onto member Grids' `vector` fields. Copy it into each column's `config` yourself; the handler reads `VectorConfig.collection_prefix`, not the bundle's.
- **A `remote` write strips the inline vector after the upsert** and stamps `collection`/`point_id` back onto the cell. The Qdrant point id is always the row id, so re-running a write is idempotent.
- **`vector` and `file` cells are opaque to filter and sort.** Both handlers declare `Capabilities(filterable=False, sortable=False, fuzzy=False)`; near-search runs through the vector store, not the SQL query engine.
- **`to_graph()`/`traverse()` load a full snapshot.** O(N + E) per call across member Grids, frozen at call time. Narrow with `entity_slugs`, or post-filter with `graph.subgraph_around`.
- **`traverse()` only sees Grids currently in the binding map**, since it goes through `to_graph()`. `list_grids()` re-reads from the database; the in-memory `grids` dict can lag if another session attached a member.
- **Nothing here is called `Space`.** The package keeps the name because the extra is `[space]`, but the type is `Bundle` — `grid`'s `Namespace` is the per-tenant session.
