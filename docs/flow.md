# `forktex_core.flow`

Durable workflow engine on Postgres: pipelines and graphs of steps that survive process restarts,
with retries, scheduling, signals and replay. State lives in the `forktex_flow` schema, which the
package owns and migrates itself.

```bash
pip install "forktex-core[flow]"
```

## Wiring

Shape C — you construct and own a `Flow`; there is no module-level default. The lifecycle is
**construct → register workflows → `start_driver()` → … → `stop_driver()` → `close()`**.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from forktex_core.flow import Flow

flow = Flow(database_url="postgresql+asyncpg://user:pass@localhost/db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    register_workflows(flow)   # see below — must happen before start_driver()
    await flow.start_driver()
    yield
    await flow.stop_driver()
    await flow.close()


app = FastAPI(lifespan=lifespan)
```

> **Without `start_driver()` nothing executes.** `flow.run()` inserts a row into `flow_run` and
> returns; the driver is what picks it up. A service that only calls `init()` gets an engine that
> durably records work nobody performs.

`start_driver()` calls `init()` for you, so an explicit `init()` is only needed if you want the
schema migrated before the driver starts. Both are idempotent and serialise concurrent callers
through a Postgres advisory lock, so running several instances is safe — only one acquires the lock
and drives.

Registration must precede `start_driver()`, because it loads namespace definitions immediately
afterwards.

### Registering workflows

Two shapes, both in production use. Imperative, when the state classes live alongside:

```python
from forktex_core.flow import Flow

flow = Flow(database_url=url, extensions=[MyExtension()])
flow.pipeline("deploy.apply", version=4, state=DeployState)(DeployPipeline)
flow.scheduled("lifecycle.reconcile", version=1, cron="*/30 * * * *", state=ReconcileState)(reconcile)
```

Or decorator-based, where the modules are imported for their side effects between construction and
`start_driver()`:

```python
import importlib

for module in ("billing.pipelines", "crm.pipelines"):
    importlib.import_module(module)   # module-level @flow.pipeline decorators run here
await flow.start_driver()
```

Module-level decorators need the live `Flow` instance to exist first, which is why the import is
deferred rather than written at the top of the file.

## Public surface

### The engine

| Name | Purpose |
|:---|:---|
| `Flow` | The instance you construct and own |
| `Flow.pipeline(name, *, version, state)` | Register a linear pipeline |
| `Flow.graph(...)` | Register a graph workflow |
| `Flow.scheduled(name, *, version, cron, state)` | Register a cron-driven workflow |
| `Flow.step_template(name)` | Register a reusable step |
| `Flow.run(...)` | Enqueue a run; returns once persisted |
| `Flow.send(run_id, *, event, payload)` | Deliver a signal to a waiting run |
| `Flow.query()` / `Flow.instances()` | Read runs back |
| `Flow.init()` / `start_driver()` / `stop_driver()` / `close()` | Lifecycle |

### Authoring

`step`, `node`, `edge`, `conditional`, `parallel`, `wait_edge`, `START`, `END`, `Ctx`,
`NodeDef`, `StepSpec`, `StepTemplateDef`, `ColumnDef`, `ParallelGroup`,
`DirectEdge`, `ConditionalEdge`, `WaitEdge`, `WorkflowDefinition`.

### Reading

`WorkflowInstance`, `InstanceQuery`, `InstancePage`, `InstanceSummary`, `NodeInstance`,
`RunInfo`, `RunUpdate`, `StepRunInfo`.

### Extending and auditing

`FlowExtension` (declare extra columns and terminal hooks), `apply_migrations`,
`audit_workflows`, `AuditReport`.

`audit_workflows` hashes each workflow's AST so a definition change that would break in-flight runs
is caught in your own CI. It is a library function — wire it into a test.

## Errors

| Error | Raised when |
|:---|:---|
| `FlowError` | Base class — catch this to cover the package |
| `StepFailed` | A step raised and exhausted its retries |
| `WorkflowFailed` | The run terminated in a failed state |
| `WorkflowCancelled` | The run was cancelled |
| `GraphStuckError` | No edge is traversable and the graph cannot reach `END` |
| `SignalTimeout` | A `wait_edge` expired before its signal arrived |

## Gotchas

- The driver polls; a run is not executed synchronously by `run()`. Tests must either start the
  driver or drive the run explicitly.
- `close()` disposes the connection pool **only** when the `Flow` created it. A pool passed in via
  `database=` belongs to you.
- `stop_driver()` is idempotent and never raises on a driver that already died — it logs instead, so
  shutdown is not blocked.
- The `forktex_flow` schema is owned by this package and mapped onto its own metadata, so your own
  `BaseDBModel.metadata.create_all()` will not create or drop it. Use `apply_migrations`.
- Running the driver in several processes is safe, but only the lock holder makes progress; it is
  not a way to scale throughput.
