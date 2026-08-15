# `forktex_core.flow` — Durable execution

> Postgres-native durable workflow engine — graph-first pipelines, state machines, and AI agent loops. Guaranteed delivery, replay-safe, zero external process.

## Overview

`flow` runs inside your existing Postgres — no Temporal, no RabbitMQ, no separate scheduler. Each workflow run is a row. The driver is an asyncio loop that competes for a Postgres advisory lock; one process is leader. Replay makes every step idempotent.

Everything below the API is `forktex_core.database`: the connection handle, the
page shape, the keyset predicate, the DDL constructs, reflection and the
identifier policy. `flow` emits **no raw SQL** — the schema-templated
`forktex_flow` name that the old f-strings existed to re-interpolate is handled by
the engine's `schema_translate_map`, which SQLAlchemy Core honours on its own.

Always bundled. See `flow/__init__.py` docstring for the full 100-line reference.

```bash
pip install forktex-core   # flow always included
```

## Quick start

```python
from forktex_core.flow import Flow, Ctx, step, edge, wait_edge, START, END

flow = Flow(database_url="postgresql+asyncpg://user:pass@localhost/db")
await flow.init()

# Or share a pool a consumer already has (e.g. because it also uses grid), so
# the process opens one engine instead of two. Exactly one of the two arguments:
#   db = Database(url, schema_translate_map={"forktex_flow": schema})
#   flow = Flow(database=db, schema=schema)   # borrowed: close() won't dispose it


# Scheduled workflow
@flow.scheduled("reports.daily", version=1, cron="0 6 * * *")
async def daily_report(ctx: Ctx, state: dict) -> dict:
    data = await fetch_data()
    return {"report_url": await upload(data)}


# Pipeline
@step
async def send_welcome(ctx: Ctx, state: dict) -> dict:
    await smtp.send(state["email"], "Welcome!")
    return {}


@flow.pipeline("onboarding.user", version=1)
class UserOnboarding:
    steps = [send_welcome, create_workspace]


# Graph (state machine)
@flow.graph("invoice.approval", version=1)
class InvoiceApproval:
    entry = "pending"
    terminal = "approved"
    topology = [
        wait_edge("pending", "approved", on="invoice.approved"),
        wait_edge("pending", "rejected", on="invoice.rejected"),
    ]


# Dispatch
instance = await flow.run("onboarding.user", state={"email": "x@example.com"})
await instance.wait(timeout=60.0)

# Query
page = await flow.query().workflow("onboarding.user").status("completed").limit(25).fetch()
```

## API reference

The complete API is documented in `src/forktex_core/flow/__init__.py`. Key surface:

```python
class Flow:
    def __init__(self, database_url: str, schema: str = "forktex_flow",
                 poll_interval: float = 1.0, leader_lock_key: int = ...)
    async def init() -> None               # applies migrations, starts driver
    async def run(name, *, version=None, state={}, namespace=None, ...) -> WorkflowInstance
    async def get(run_id: UUID) -> RunInfo
    async def wait(run_id, timeout=None) -> RunInfo
    async def send(run_id, event, payload=None) -> None
    def query() -> InstanceQuery

# Decorators
@flow.scheduled(name, version, cron, state=dict)
@flow.pipeline(name, version, state=dict)
@flow.graph(name, version)
@flow.step_template(name)   # reusable step for namespace-track
@step                       # plain step function
@node                       # graph node
@parallel(steps)            # concurrent steps

# Graph DSL
edge(src, dst)
conditional(src, [(condition_fn, dst), ..., default_dst])
wait_edge(src, dst, on="event.name")
START, END

# Query API
flow.query()
  .workflow(name, version=None)
  .namespace(ns)
  .status(*statuses)
  .since(dt), .until(dt)
  .metadata(**kv), .state(**kv)
  .sort(field, desc=True)
  .limit(n)
  .fetch(cursor=None) -> InstancePage   # = database.pagination.Page[WorkflowInstance]
                                        # .items / .has_more / .next_cursor / .total
  .count() -> int
  .summary() -> InstanceSummary

# Extensions (custom columns on run/step_run)
class MyExt(FlowExtension):
    def extra_run_columns(self) -> list[ColumnDef]: ...
flow = Flow(..., extensions=[MyExt()])
```

### Persistence primitives

Flow persists durable execution state in Postgres through these ORM rows:

| Model | Purpose |
|:------|:--------|
| `Run` | One workflow instance, including status, state, metadata, and attempts. |
| `StepRun` | One step attempt/replay unit with heartbeat and retry state. |
| `RunEvent` | Append-only event stream used by `Flow.stream()` and audit readers. |
| `ScheduledRun` | Cron-backed recurring workflow cursor. |

The tables map onto `database.models.substrate_base("forktex_flow")` — their own
`MetaData`, deliberately not `BaseDBModel`'s. That registry belongs to the
consumer: `BaseDBModel.metadata.create_all()` is the documented way to build your
own tables, and it must not also try to create `forktex_flow.*` in a schema you
never asked for. `flow` owns its migration runner instead.

The driver loop uses `claim_pending_runs(flow, limit)` to atomically claim runnable work and `reclaim_stale_steps(flow)` to return timed-out step attempts to the pending pool. These are internal primitives, but they are the canonical reference for sibling services that need claim/reclaim semantics — the claim is
`with_for_update(skip_locked=True)` on a `scalar_subquery()` fed into
`update().returning()`, in Core.

`sort()` works with the cursor: the keyset predicate is built from the resolved
sort column, so paging by `finished_at` / `status` / `workflow` is correct. It
previously hardcoded the predicate to `started_at` while `ORDER BY` used whatever
was asked for, which skipped and repeated rows on every other sort field.

## Patterns

### Pattern 1 — Namespace track (tenant-defined workflows)

```python
await flow.define(
    name="failure.response",
    namespace=f"org-{org_id}",
    version=1,
    config={"type": "pipeline", "steps": ["my_service.handle_failure"]},
)
instance = await flow.run("failure.response", namespace=f"org-{org_id}", state={})
```

### Pattern 2 — Child workflows (scatter/gather)

```python
@step
async def process_all(ctx: Ctx, state: dict) -> dict:
    results = await ctx.map(
        "item.processor",
        inputs=[{"item_id": id} for id in state["item_ids"]],
    )
    return {"results": results}
```

### Pattern 3 — State reducers (parallel merge)

```python
from typing import Annotated
import operator


class ProcessState(TypedDict):
    results: Annotated[list[dict], operator.add]  # list merge on parallel steps
```

## Anti-patterns

```python
# ❌ Flow depends on external state (steps must be idempotent)
@step
async def send_email(ctx, state):
    if not state.get("email_sent"):  # wrong — replay breaks this
        await smtp.send(...)


# ✅ External idempotency key
@step
async def send_email(ctx, state):
    await smtp.send(..., idempotency_key=str(ctx.run_id))
    return {"email_sent": True}


# ❌ Raising inside a step for business logic
@step
async def validate(ctx, state):
    if not state.get("name"):
        raise ValueError("name required")  # marks step FAILED permanently


# ✅ Return validation outcome in state, branch in graph
@step
async def validate(ctx, state):
    return {"valid": bool(state.get("name"))}
```

---

## Agent guide

### Canonical forms

**Three declaration tracks (choose one per workflow):**

| Track | When |
|---|---|
| `@flow.scheduled(cron=...)` | Timed recurring jobs |
| `@flow.pipeline(steps=[...])` | Linear sequence, optional conditions |
| `@flow.graph(topology=[...])` | Branching, cycles, event-driven |

**Dispatch is identical for all tracks:**
```python
instance = await flow.run("my.workflow", state={"key": "value"})
instance = await instance.wait(timeout=120)
print(instance.status, instance.state)
```

### Edge cases

| Scenario | Behaviour |
|---|---|
| Step raises exception | Retried with backoff; after max retries → `StepFailed`, run `FAILED` |
| Driver process dies while holding lock | Postgres auto-releases on connection drop; next worker becomes leader |
| `flow.run()` — workflow not registered | `KeyError` at runtime |
| `wait_edge` — signal never arrives | Run stays in `running` until `ctx.send(event)` or timeout cancels |
| `flow.init()` called from multiple workers | Advisory lock serialises — only first applies migrations |

### Integration map

```
flow ──── (uses) ──── db.locks.try_advisory_lock    [leader election]
flow ──── (uses) ──── db.migrate.SchemaMigrationRunner  [forktex_flow.* schema]
flow ──── (uses) ──── forktex_flow.* schema         [separate from consumer alembic]
flow ──── (complement) ── queue                     [queue=fire-and-forget; flow=durable]
```
