# `forktex_core.space`

Level-3 substrate facade. Multi-Grid bundle + rich-content (FILE +
VECTOR) field handlers + cross-Grid traversal.

## Install

```bash
poetry add "forktex-core[space]"
```

Pulls `[grid]` + `[graph]`. Lazy-imports `[vector]` and `[storage]`
when a row write or archive actually touches a VECTOR / FILE field.
Pure-tabular consumers stay on `[grid]` and pay no Qdrant / MinIO cost.

## Purpose

The wrapper between `[grid]` and rich content. A `Bundle` declares a
group of related Grids that share rich-content config (vector
collection prefix, storage bucket, edge vocabulary) and a list of
consumer-defined sync drivers. Importing `forktex_core.space`
side-effect-registers the rich `FILE` and `VECTOR` field handlers
into the `[grid]` field-type registry.

Querying a member grid goes through `[grid]`, so a `Bundle` inherits the shared
filter AST and page shape from [`database`](database.md) — `Grid.query()` returns
`Page[Row]` (`items` / `rows` / `has_more` / `next_cursor` / `total`) and accepts
the same `parse_filter` JSON. `space` itself holds **no** raw SQL and takes an
injected session rather than reaching for a global engine, so it composes inside a
caller's transaction.

## Public API

```python
from forktex_core.space import (
    Bundle,
    BundleConfig,
    VectorDefaults,
    StorageDefaults,
    SyncSourceConfig,
)
from forktex_core.space.types.file import RichFileType, FileConfig
from forktex_core.space.types.vector import RichVectorType, VectorConfig
```

`Bundle` exposes: `Bundle.declare(...)`, `Bundle.bind(...)`,
`Bundle.attach(grid)`, `Bundle.detach(slug)`, `Bundle.grid(slug)`,
`Bundle.list_grids()`, `Bundle.materialize()`, `Bundle.to_graph(...)`,
`Bundle.traverse(start_row_id, max_depth=, edge_kind=, direction=)`.

**Naming.** `forktex_core.space` exports `Bundle`, and `forktex_core.grid` exports
`Namespace`. Neither is called `Space`, deliberately: a `Namespace` is a session scoped to
one namespace (schema + data for that tenant), while a `Bundle` groups several Grids under
shared rich-content config. Calling either one "Space" said less than the word it replaced.
The *package* keeps the name `space` because the extra it installs is `[space]`.

## Quick example

```python
from forktex_core.grid import FieldType, Grid, TableSpec
from forktex_core.space import Bundle, BundleConfig, VectorDefaults
from forktex_core.storage import register as register_storage
from forktex_core.vector import register as register_vector

register_storage("default", url=..., bucket="kb", access_key=..., secret_key=...)
register_vector("default", qdrant_url="http://qdrant:6333")

documents = await Grid.declare(
    session,
    TableSpec.from_dicts(
        slug="documents",
        label="Documents",
        namespace=str(org_id),
        columns=[
            {"key": "title", "label": "Title", "type_id": FieldType.text.value},
            {
                "key": "source",
                "label": "Source",
                "type_id": "file",          # registered by importing forktex_core.space
                "config": {"client_name": "default"},
            },
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

## See also

- [`grid.md`](grid.md) — the substrate a `Bundle` sits on.
- [`graph.md`](graph.md) — `Bundle.to_graph()` returns this graph type.
- `examples/space_bundle.py` — runnable demo with FILE + VECTOR fields.
- `tests/test_stories/test_knowledge_ingestion.py` — full ingestion
  lifecycle (Bundle → MinIO → Qdrant → cross-Grid traversal → archive).
- `tests/test_stories/test_multitenant_isolation.py` — namespace
  isolation across the bundle.
- `tests/test_stories/test_vector_storage_modes.py` — substrate-mode
  contract for the four `VectorConfig.storage_mode` settings.
