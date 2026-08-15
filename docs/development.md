# forktex-core — Development Reference

Internal documentation for contributors and maintainers. Library consumers do not need this.

---

## Module dependency graph

```
database    ← flow      (SchemaMigrationRunner, advisory_lock, try_advisory_lock)
database    ← grid      (BaseDBModel, TimestampMixin, engine)
database    ← vault     (EncryptedJSON TypeDecorator)
cache       ← queue     (Redis as arq backend)
─────────────────────────────────────────────────
log         ← iso       (canonical UTC timestamp formatting — a Level-0 sibling, not a facade)
─────────────────────────────────────────────────
vault, storage, vector  →  no deps on other core modules
```

Import direction is strict: no cross-module imports except the three above. `flow` imports from `database`; nothing imports from `flow`.

---

## Design principles

**No hardcoded paths in application code.** `output_dir`, `registry_path`, `archive_root` are interface-adapter concerns. Use cases take injected dependencies. Only adapters may hardcode filesystem conventions.

**No module-level imports of optional deps.** Every module that requires an extra (`vault`, `storage`, `queue`, `vector`) imports its dependency lazily and raises `ImportError("Install forktex-core[X]…")` — never at module import time.

**Assertions stripped in production.** Never use `assert` in hot paths or lifecycle methods. Always use explicit `if … raise RuntimeError(…)`.

**Library schemas are isolated.** `forktex_flow.*` and `forktex_grid.*` tables live in dedicated Postgres schemas. Consumer alembic never sees them. `SchemaMigrationRunner` manages them with advisory-lock-protected SQL file migration. Consumer services that can't create schemas use `schema_translate_map={None: "public", "forktex_flow": "public", "forktex_grid": "public"}` to route to public.

**`schema_translate_map` key for default-schema tables is `None`, not `"public"`.** Tables with `schema=None` (no explicit schema) are remapped by the `None` key. Using `"public"` as the key has no effect on those tables.

**`ContextVar` defaults must be immutable.** A mutable default (e.g. `ContextVar("x", default={})`) creates one shared dict for all coroutines that never set the var — mutations leak across task boundaries. Pattern: `ContextVar("x", default=None)` + factory in getter.

**Tenant isolation is via `namespace`, not FK.** The Grid module and any other generic data path uses a `namespace: str` column (typically set to `str(org_id)` by the consumer) rather than an FK to an org table — the library takes no position on what the tenant table is called. `NamespacedMixin` is the only tenant-scoping primitive in `database`. Consumers that want a DB-level FK to their own tenant table declare that mixin locally in their service.

**Internal modules use `forktex_core.log.get_logger(__name__)`, never bare `logging.getLogger()` or module-level `logging.info()`/`logging.warning()` calls.** The library dogfoods its own logging primitive: consumers reading forktex_core's source see the exact pattern it tells them to use, `record.logger` is always the real qualified module path (not `"root"`), and a consumer's `setup_logging(quiet=["forktex_core.flow"])` can selectively quiet just one internal module — none of which work if a module logs via the bare `logging` API. See `docs/log.md`'s "Interaction with forktex_core's own internals" for why this never conflicts with a consumer's own `setup_logging()` call — forktex_core never calls it itself, only the consumer does.

---

## Test infrastructure

### Running tests

```bash
cd core-py

# Full suite (requires Docker for container tests)
python3 -m pytest tests/ -q

# No-container tests only (fast — stdlib, Pydantic, SQLAlchemy unit tests)
python3 -m pytest tests/test_log/ tests/test_iso/ tests/test_database/test_query.py tests/test_vault/ -q

# Per-module with container
python3 -m pytest tests/test_database/ -v  # Postgres
python3 -m pytest tests/test_cache/ -v     # Redis
python3 -m pytest tests/test_storage/ -v   # MinIO
python3 -m pytest tests/test_vector/ -v    # Qdrant
python3 -m pytest tests/test_queue/ -v     # Redis + arq
python3 -m pytest tests/test_store/ -v     # MongoDB
```

### Container setup

Session-scoped containers (one per `pytest` run, shared across all tests) — the
complete picture of every testcontainer this repo runs, all defined in
`tests/_containers.py` and wired into fixtures in `tests/conftest.py`:

| Container | Image | Fixture | Tests |
|---|---|---|---|
| Postgres 17 | `postgres:17-alpine` | `postgres_url` / `postgres_url_str` | database, vault, grid, flow |
| Redis 7 | `redis:7-alpine` | `redis_url` | cache, queue |
| MinIO | `minio/minio:RELEASE.2025-09-07T16-13-09Z` | `minio_config` | storage |
| Qdrant | `qdrant/qdrant:v1.18.1` | `qdrant_url` | vector |
| MongoDB 7 | `mongo:7` | `mongo_url` | store |

Per-test isolation is via unique Postgres schemas / MongoDB database names (dropped
after each test), not separate containers — one container per service, shared
across the whole session. `flow` used to be the exception, starting its own
`postgres:15-alpine`: a second container per run, on a different major than every
other suite, so a version-specific behaviour could pass there and fail everywhere
else. It shares `postgres_url` now, and this table is the whole truth.

**A real container is required — there is no embedded or in-memory fallback.**
These suites exercise JSONB operators, `FOR UPDATE SKIP LOCKED`, advisory locks,
partial/GIN indexes, `schema_translate_map` and SQLSTATE codes; none of that can be
faked, and none of it runs on SQLite. An opt-in `GRID_EMBEDDED_PG=1` path (an
embedded server via the `pgserver` wheel) existed for machines without a container
runtime, but it only covered Postgres, and `flow` ignored it anyway — so it
half-worked at the cost of a second code path and a dev dependency. Removed.

**MongoDB is special-cased**: it's configured as a single-node replica set
(`--replSet rs0`, auth disabled) rather than the plain standalone instance you'd
get by default, because `store`'s `transaction()` requires one — MongoDB refuses
multi-document transactions on a standalone `mongod` entirely. Auth is
deliberately *off* for this one container (unlike Postgres/Redis, which don't need
it disabled) because enabling it changes the official image's startup sequence in
a way that reliably breaks when combined with the `--replSet` override — see
`tests/_containers.py::start_mongo()`'s docstring for the full story if you need to
touch this.

### Schema isolation in tests

ORM tests must map both default-schema and `forktex_grid.*` tables to `fresh_schema`:

```python
engine = create_async_engine(
    url,
    execution_options={
        "schema_translate_map": {
            None: fresh_schema,  # schema=None tables
            "forktex_grid": fresh_schema,  # data module tables
        }
    },
)
async with engine.begin() as conn:
    await conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{fresh_schema}"'))
    await conn.run_sync(BaseDBModel.metadata.create_all)
```

Using `"public"` as the key instead of `None` does not remap tables with `schema=None` — those land in the real `public` schema instead of `fresh_schema`, breaking isolation.

### Module-level ORM models in tests

Define ORM models at **module level**, not inside test functions. SQLAlchemy resolves `Mapped[T]` type annotations at class definition time using the module's global namespace. Defining a model inside a function means `Mapped` is not in `__globals__` and annotation resolution fails with `KeyError: 'Mapped'`.

```python
# ✅ Module level
class MyTestModel(BaseDBModel): ...


# ❌ Inside a function — raises KeyError during annotation resolution
def test_something():
    class MyTestModel(BaseDBModel): ...  # broken
```

---

## Runtime-specific gotchas discovered during development

### Qdrant: collection names

`AsyncQdrantClient` rejects collection names containing `:` with a 422 validation error. Use `--` as the tenant separator: `f"org-{org_id}--collection-name"`.

### Qdrant: point IDs

Point IDs must be unsigned integers or UUID strings. Arbitrary strings (e.g. `"doc-1"`, `"p1"`) → 400 Bad Request. Always use `int` or `str(uuid.uuid4())`.

### Qdrant: `AsyncQdrantClient` has no `async with`

The `AsyncQdrantClient` in qdrant-client ≥ 1.14 does not implement `__aenter__`/`__aexit__`. Use explicit `try/finally` + `await q.close()`.

### arq 0.28 API changes

- `arq.worker.WorkerSettings` doesn't exist — use `arq.Worker` directly
- `arq.Job` doesn't exist at the top level — use `arq.jobs.Job`
- `Worker.async_run()` for programmatic burst mode; `arq` CLI uses `arq module.WorkerSettings`

### MinIO environment variables

- `MINIO_ROOT_USER` + `MINIO_ROOT_PASSWORD` (not `MINIO_ROOT_SECRET`)
- Without `MINIO_ROOT_PASSWORD` set, the MinIO container exits immediately with no log output

### aioboto3 `ClientError` import

`botocore.exceptions.ClientError` is the typed exception for S3 errors. Matching on `"NoSuchKey" in str(exc)` is brittle — the SDK format can change. Preferred pattern:

```python
try:
    from botocore.exceptions import ClientError
except ImportError:
    ClientError = None
try:
    result = await s3.get_object(...)
except Exception as exc:
    if ClientError and isinstance(exc, ClientError):
        code = exc.response["Error"]["Code"]
        if code in ("NoSuchKey", "404"):
            raise ObjectNotFoundError(key) from exc
    raise StorageError(str(exc)) from exc
```

### `log._ContextFilter` — not auto-added to custom handlers

`_ContextFilter` is added to the root handler by `setup_logging()`. If you create a custom `logging.Handler` in tests (e.g. to capture output), you must add the filter manually — it's what actually sets `record.trace_id`, `record.root_trace_id`, `record.service`, and `record._forktex_extra` from the current contextvars; without it those fields are simply absent from captured records, not merely empty:

```python
from forktex_core.log import _ContextFilter

handler = CaptureHandler()
handler.addFilter(_ContextFilter(service="test"))
```

See `tests/test_log/conftest.py`'s `capture_json` fixture for the canonical pattern.

---

## Adding a new module

1. Create `src/forktex_core/<name>/` with `__init__.py`
2. Add optional dep to `pyproject.toml` `[project.optional-dependencies]`
3. Add to `src/forktex_core/__init__.py` docstring
4. Add `__all__` to all public submodules
5. Create `docs/<name>.md` (consumer reference: Overview → Quick start → API → Patterns → Anti-patterns → Agent guide)
6. Symlink `docs/sdk/forktex-core/<name>.md → ../../../core-py/docs/<name>.md` in the `/docs` repo
8. Create `tests/test_<name>/` with `__init__.py` and `test_<name>.py`
9. Add any required testcontainer fixture to `tests/conftest.py`
10. Add dev dep to `pyproject.toml` `[dependency-groups]`

### Module design checklist

- [ ] Optional dep imported lazily inside functions, not at module top
- [ ] `ImportError` message includes `"Install 'forktex-core[<name>]'"`
- [ ] `__all__` on every public submodule
- [ ] No `assert` — use explicit `if … raise`
- [ ] No mutable `ContextVar` defaults
- [ ] No cross-module imports unless `db` (for ORM modules) or `cache` (for Redis modules)
- [ ] Module-level global state documented (if any)

---

## Release process

```bash
# 1. Bump version in pyproject.toml
# 2. Run full test suite
python3 -m pytest tests/ -q

# 3. Build
python3 -m build

# 4. Upload to PyPI
twine upload dist/*

# 5. Tag
git tag v0.4.2 && git push --tags
```

---

## Known limitations

Where a capability stops, and who owns the rest of it. Nothing here is a promise — a
capability that is not listed is simply not in 0.1.0.

| Item | Where it stops |
|---|---|
| `vault` KEK rotation | `rotate_kek` re-wraps one blob; the bulk re-encrypt runner is a consumer concern (there is no envelope layer, so rotation is a migration over every row) |
| `vector` sparse vectors (SPLADE/BM25) | Types are defined; whether Qdrant accepts them depends on the server version |
| `queue` priority routing | Expressed as `queue_name` plus one worker per queue — there is no in-queue priority |
| `flow` step retry delay | Per-function retry delay is not an arq feature; raise `arq.Retry(defer=…)` from the task body |
| Offset pagination depth | Capped at `MAX_OFFSET`; past that, use cursor mode — deep offsets scan |
