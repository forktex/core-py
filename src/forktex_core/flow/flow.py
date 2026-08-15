# Copyright (C) 2026 FORKTEX S.R.L.
#
# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-ForkTex-Commercial
#
# This file is part of forktex-core.
#
# For commercial licensing -- including use in proprietary products, SaaS
# deployments, or any context where AGPL obligations cannot be met -- you
# MUST obtain a commercial license from FORKTEX S.R.L. (info@forktex.com).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""The ``Flow`` class — the single public entry point for the library.

Provides two declaration tracks:

- **Platform track**: ``@flow.scheduled``, ``@flow.pipeline``,
  ``@flow.graph``, ``@flow.step_template`` — code-defined, registered at
  import time.
- **Namespace track**: ``flow.define()`` / ``flow.undefine()`` — runtime
  config, persisted to DB and hydrated on startup.

Both tracks dispatch via ``flow.run()`` and query via ``flow.query()``.
"""

from __future__ import annotations

import asyncio
import zlib
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from forktex_core import iso
from forktex_core.database import Database
from forktex_core.error import NotFoundError
from forktex_core.flow.domain.definition import NodeDef, StepTemplateDef, WorkflowDefinition
from forktex_core.flow.domain.node import _NodeMeta, step_meta
from forktex_core.flow.domain.types import TERMINAL_STATUSES, RunInfo, RunStatus, RunUpdate
from forktex_core.flow.extension import FlowExtension
from forktex_core.flow.persist import definitions as _definitions
from forktex_core.flow.persist import runs as _runs
from forktex_core.flow.persist import signals as _signals
from forktex_core.flow.persist.migrations._runner import apply_migrations
from forktex_core.flow.read import query as _query
from forktex_core.flow.read.instance import InstanceQuery, WorkflowInstance
from forktex_core.flow.runtime.compiler import compile_config, compile_graph, compile_pipeline, compile_scheduled
from forktex_core.flow.runtime.driver import _Driver
from forktex_core.flow.runtime.registry import _FlowRegistry
from forktex_core.log import get_logger
from forktex_core.types import JsonValue

if TYPE_CHECKING:
    from forktex_core.flow.read.instance import InstanceQuery, WorkflowInstance
    from forktex_core.flow.runtime.driver import _Driver
    from forktex_core.flow.runtime.registry import _FlowRegistry

logger = get_logger(__name__)

# Default lock key for leader election. CRC32 of "forktex_flow.driver"
# (matches the user-visible identifier; collisions across other
# advisory-lock users in the same DB are minimised by the namespace).
_DEFAULT_LEADER_LOCK_KEY = zlib.crc32(b"forktex_flow.driver")


class _DefinitionHandle:
    """Handle to a registered WorkflowDefinition for dispatch + query.

    Returned by :meth:`Flow.workflow`; provides a convenient API for
    running and querying a specific workflow definition without repeating
    name/version/namespace at every call site.
    """

    def __init__(self, defn: WorkflowDefinition, flow: Flow) -> None:
        self._defn = defn
        self._flow = flow

    @property
    def name(self) -> str:
        return self._defn.name

    @property
    def version(self) -> int:
        return self._defn.version

    @property
    def namespace(self) -> str | None:
        return self._defn.namespace

    @property
    def nodes(self) -> dict[str, NodeDef]:
        return self._defn.nodes

    @property
    def edges(self) -> dict[str, list[Any]]:
        return self._defn.edges

    @property
    def schedule(self) -> str | None:
        return self._defn.schedule

    async def run(
        self,
        *,
        state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorkflowInstance:
        """Submit a run for this definition. Returns a WorkflowInstance."""
        return await self._flow.run(
            self._defn.name,
            state=state,
            metadata=metadata,
            namespace=self._defn.namespace,
            version=self._defn.version,
        )

    def query(self) -> InstanceQuery:
        """Return an InstanceQuery pre-filtered to this definition."""
        return self._flow.query().workflow(self._defn.name, self._defn.version)

    async def instances(self, **kwargs: object) -> list[WorkflowInstance]:
        """Fetch instances, applying any kwarg as a filter method."""
        q = self.query()
        for k, v in kwargs.items():
            if hasattr(q, k):
                q = getattr(q, k)(v)
        return (await q.fetch()).items


class Flow:
    """Durable workflow runtime. Construct once per process.

    Provides two declaration tracks:

    - Platform track: ``@flow.scheduled``, ``@flow.pipeline``,
      ``@flow.graph`` (code-defined)
    - Namespace track: ``flow.define()`` / ``flow.undefine()``
      (runtime config, stored in DB)

    Both tracks dispatch via ``flow.run()`` and query via ``flow.query()``.

    Args:
        database_url: SQLAlchemy async URL, e.g.
            ``postgresql+asyncpg://user:pass@host/db``. Builds a dedicated
            connection pool for this Flow. Mutually exclusive with
            ``database``.
        database: an existing :class:`forktex_core.database.Database` handle to
            **share** instead of building a pool. Use this when the application
            already has one (e.g. it also uses ``grid``) so the process opens a
            single pool rather than one per subsystem. Its
            ``schema_translate_map`` must remap ``forktex_flow`` to ``schema``,
            so construct it as
            ``Database(url, schema_translate_map={"forktex_flow": schema})``.
            A shared handle is *not* disposed by :meth:`close`.
        schema: Postgres schema the library owns. Default
            ``"forktex_flow"``. The migration runner creates it if
            absent and applies its own ordered migrations there. Never
            shared with consumer's ``public`` schema.
        extensions: list of :class:`FlowExtension` instances composed
            in order; their hooks fire in the order registered.
        leader_lock_key: 64-bit int used for the driver's advisory
            lock. Default derived from ``"forktex_flow.driver"``;
            override to coexist multiple Flow instances in one DB
            (rare — mostly for tests).
        poll_interval: seconds between driver ticks. Default 1.0.
        heartbeat_interval: how often a running step refreshes its
            ``heartbeat_at``. Default 10.0.
        stale_threshold: a step whose heartbeat is older than this
            many seconds is presumed orphaned and reclaimed by the
            next leader. Default 60.0.
        default_max_attempts: per-step retry cap unless the step's
            decorator overrides. Default 3.
        default_backoff: tuple of seconds-to-wait between retries
            (linear lookup; last value is used for any attempt past
            its index). Default ``(30, 120, 300)``.
        engine_kwargs: passed verbatim to ``create_async_engine``
            (pool_size, max_overflow, pool_pre_ping, etc.).
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        database: Database | None = None,
        schema: str = "forktex_flow",
        extensions: list[FlowExtension] | None = None,
        leader_lock_key: int = _DEFAULT_LEADER_LOCK_KEY,
        poll_interval: float = 1.0,
        heartbeat_interval: float = 10.0,
        stale_threshold: float = 60.0,
        default_max_attempts: int = 3,
        default_backoff: tuple[float, ...] = (30.0, 120.0, 300.0),
        echo: bool = False,
        **engine_kwargs: object,
    ) -> None:
        if default_max_attempts < 1:
            raise ValueError("default_max_attempts must be >= 1")
        if not default_backoff:
            raise ValueError("default_backoff must be a non-empty tuple")
        if (database_url is None) == (database is None):
            raise ValueError("pass exactly one of database_url or database")

        self.database_url = database_url if database_url is not None else database.url  # type: ignore[union-attr]
        self.schema = schema
        self.extensions: list[FlowExtension] = list(extensions or [])
        self.leader_lock_key = leader_lock_key
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.stale_threshold = stale_threshold
        self.default_max_attempts = default_max_attempts
        self.default_backoff = default_backoff

        # Connection management belongs to `database.Database`, not here. This
        # used to duplicate `database.connection.init_engine` almost line for
        # line, including the schema_translate_map trick below.
        #
        # ORM models hardcode their schema as ``forktex_flow`` (so the static
        # metadata is unambiguous); SQLAlchemy's ``schema_translate_map``
        # rewrites it to the user-configured schema at execution time. This is
        # exactly the use case that feature exists for.
        #
        # Pass ``database=`` to share a pool with the rest of the application —
        # that is how a consumer running grid *and* flow ends up with one pool
        # instead of two. Passing a URL builds a dedicated pool instead, which is
        # what standalone use and the test suite want (two Flow instances may run
        # concurrently against different schemas in one process, so this cannot
        # be the module-level default handle). Either way construction is lazy:
        # no connection opens until something executes.
        if database is not None:
            if database.schema_translate_map != {"forktex_flow": schema}:
                logger.warning(
                    "the supplied database handle does not remap forktex_flow to %r; "
                    "flow's tables will resolve to whatever its map says",
                    schema,
                    extra={"schema": schema, "map": database.schema_translate_map},
                )
            self._db = database
            self._owns_db = False
        else:
            self._db = Database(
                database_url,  # type: ignore[arg-type]  # guarded above
                echo=echo,
                schema_translate_map={"forktex_flow": schema},
                **engine_kwargs,
            )
            self._owns_db = True

        self._registry: _FlowRegistry = _FlowRegistry()

        self._driver: _Driver | None = None
        self._driver_task: asyncio.Task[None] | None = None
        self._migrated: bool = False

    @property
    def engine(self) -> AsyncEngine:
        """The engine backing this Flow — for DDL, advisory locks, migrations."""
        return self._db.engine

    def session(self) -> AbstractAsyncContextManager[AsyncSession]:
        """A transactional session on this Flow's pool: commits on success,
        rolls back on error.

        The public accessor for flow-internal database work. It previously
        existed with zero call sites while 28 places reached into the private
        ``_sessionmaker`` and hand-rolled their own commit; those now route
        through here, so the commit/rollback contract is stated once.
        """
        return self._db.session()

    async def init(self) -> None:
        """Idempotent setup: applies the library's migrations + any
        extension-declared columns. Safe to call multiple times — the
        runner is itself idempotent and serialises concurrent callers
        via a Postgres advisory lock. Also safe after an external
        ``DROP SCHEMA forktex_flow CASCADE`` reinstall.
        """

        await apply_migrations(self.engine, self.schema, self.extensions)
        self._migrated = True

    async def start_driver(self) -> None:
        """Begin the driver loop in a background task. Returns
        immediately; the loop runs until ``stop_driver`` or process
        exit. Calling on multiple workers is safe — only one acquires
        the advisory lock and actually drives.
        """
        if self._driver_task is not None and not self._driver_task.done():
            return  # already running
        await self.init()

        try:
            await self._load_namespace_definitions()
        except Exception:
            logger.warning("Failed to load namespace definitions from DB", exc_info=True)

        self._driver = _Driver(self)
        self._driver_task = asyncio.create_task(
            self._driver.run(),
            name=f"forktex_flow.driver[{self.schema}]",
        )

    async def stop_driver(self) -> None:
        """Stop the driver loop cleanly. Idempotent."""
        if self._driver is not None:
            self._driver.shutdown.set()
        if self._driver_task is not None:
            self._driver_task.cancel()
            try:
                await self._driver_task
            except asyncio.CancelledError:
                pass  # expected: we just cancelled it
            except Exception:
                # The driver task died with a real error before/instead of
                # cancelling. Surface it in logs — stop_driver still completes
                # so shutdown isn't blocked.
                logger.warning("Driver task raised during shutdown", exc_info=True)
            self._driver_task = None
            self._driver = None

    async def close(self) -> None:
        """Stop the driver and release this Flow's resources. Call from your
        FastAPI lifespan's shutdown handler.

        Disposes the connection pool only when this Flow created it. A pool
        supplied via ``database=`` belongs to the caller — disposing it here
        would tear the rest of the application's database access down with the
        Flow.
        """
        await self.stop_driver()
        if self._owns_db:
            await self._db.dispose()

    def scheduled(
        self,
        name: str,
        *,
        version: int,
        cron: str,
        state: type | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a single-function workflow that runs on a cron schedule.

        Example::

            @flow.scheduled("cloud.backup.create", version=1,
                            cron="0 2 * * *", state=BackupState)
            async def backup_create(ctx: Ctx, state: BackupState) -> dict: ...
        """

        def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:

            defn = compile_scheduled(fn, name=name, version=version, cron=cron, state_cls=state)
            self._registry.register_definition(defn)
            return fn

        return _decorator

    def pipeline(
        self,
        name: str,
        *,
        version: int,
        state: type | None = None,
        cron: str | None = None,
    ) -> Callable[[type], type]:
        """Register a linear pipeline workflow declared as a class with steps=[].

        Example::

            @flow.pipeline("cloud.deploy.up", version=4, state=DeployState)
            class DeployUp:
                steps = [provision, configure, health_check]
        """

        def _decorator(cls: type) -> type:

            defn = compile_pipeline(cls, name=name, version=version, cron=cron, state_cls=state)
            self._registry.register_definition(defn)
            return cls

        return _decorator

    def graph(
        self,
        name: str,
        *,
        version: int,
        state: type | None = None,
    ) -> Callable[[type], type]:
        """Register a graph/state-machine workflow declared as a class
        with topology=[].

        Example::

            @flow.graph("user.onboarding", version=1, state=OnboardingState)
            class UserOnboarding:
                entry = "email_pending"
                terminal = "verified"
                topology = [wait_edge("email_pending", "verified", on="email.verified")]
        """

        def _decorator(cls: type) -> type:

            defn = compile_graph(cls, name=name, version=version, state_cls=state)
            self._registry.register_definition(defn)
            return cls

        return _decorator

    def step_template(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a named step available to namespace-track workflow definitions.

        Example::

            @flow.step_template("network.reroute_traffic")
            async def reroute_traffic(ctx: Ctx, state: dict) -> dict: ...
        """

        def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:

            meta: _NodeMeta | None = step_meta(fn)
            template = StepTemplateDef(
                name=name,
                fn=fn,
                max_attempts=meta.max_attempts if meta else self.default_max_attempts,
                backoff=meta.backoff if meta else self.default_backoff,
            )
            self._registry.register_step_template(template)
            return fn

        return _decorator

    async def define(
        self,
        name: str,
        *,
        namespace: str,
        version: int,
        config: dict[str, Any],
    ) -> None:
        """Create or update a namespace-track workflow definition.

        ``config`` format::

            {"type": "pipeline", "steps": ["template.a", ...]}
            {"type": "graph", "topology": [...]}

        Persists to DB + registers in memory immediately.
        """

        defn = compile_config(config, self._registry.step_templates)
        defn.name = name
        defn.version = version
        defn.namespace = namespace

        # Update in-memory (may already exist — replace it).
        key = (name, version, namespace)
        self._registry.definitions[key] = defn

        await _definitions.upsert_namespace_definition(
            self,
            name=name,
            version=version,
            namespace=namespace,
            type_=config.get("type", "pipeline"),
            config=config,
        )

    async def undefine(self, name: str, *, namespace: str) -> None:
        """Delete all versions of a namespace-track definition.

        Raises ``ValueError`` if active runs exist.
        """

        await _definitions.delete_namespace_definition(self, name, namespace)

        keys = [(n, v, ns) for (n, v, ns) in list(self._registry.definitions) if n == name and ns == namespace]
        for key in keys:
            del self._registry.definitions[key]

    async def definitions(self, *, namespace: str) -> list[WorkflowDefinition]:
        """List all WorkflowDefinition objects registered for a namespace."""
        return self._registry.all_definitions_for_namespace(namespace)

    def workflow(
        self,
        name: str,
        *,
        namespace: str | None = None,
        version: int | None = None,
    ) -> _DefinitionHandle:
        """Return a :class:`_DefinitionHandle` for a workflow definition.

        Example::

            defn = flow.workflow("cloud.deploy.up")
            defn = flow.workflow("link_failure_response", namespace="org-abc")
        """
        defn = self._registry.get_definition(name, version=version, namespace=namespace)
        if defn is None:
            raise ValueError(f"workflow {name!r} (namespace={namespace!r}) not found")
        return _DefinitionHandle(defn=defn, flow=self)

    def step_templates(self) -> dict[str, Any]:
        """Return all registered step templates."""
        return dict(self._registry.step_templates)

    async def _load_namespace_definitions(self) -> None:
        """Hydrate in-memory registry with namespace-track definitions from DB."""

        rows = await _definitions.load_namespace_definitions(self)
        for row in rows:
            try:
                defn = compile_config(row["config"], self._registry.step_templates)
                defn.name = row["name"]
                defn.version = row["version"]
                defn.namespace = row["namespace"]
                key = (row["name"], row["version"], row["namespace"])
                self._registry.definitions[key] = defn
            except Exception:
                logger.warning(
                    "Failed to load namespace definition %s v%d (ns=%s)",
                    row["name"],
                    row["version"],
                    row["namespace"],
                    exc_info=True,
                )

    async def run(
        self,
        name: str,
        *,
        state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        namespace: str | None = None,
        version: int | None = None,
        triggered_by: str = "manual",
    ) -> WorkflowInstance:
        """Submit a new workflow run. Returns immediately with a
        :class:`~forktex_core.flow.read.instance.WorkflowInstance`.
        """
        from uuid import uuid7

        defn = self._registry.get_definition(name, version=version, namespace=namespace)
        if defn is None:
            ns_str = f" in namespace {namespace!r}" if namespace else ""
            raise ValueError(f"workflow {name!r}{ns_str} not registered")

        run_input = dict(state or {})
        run_metadata = dict(metadata or {})

        # Stamp namespace for query isolation.
        if namespace is not None:
            run_metadata.setdefault("__namespace__", namespace)

        # Extension before_start hooks.
        for ext in self.extensions:
            hook = getattr(ext, "before_start", None)
            if hook is None:
                continue
            extra = await hook(name, defn.version, run_input, run_metadata)
            if extra:
                run_metadata.update(extra)

        run_id = uuid7()
        await _runs.insert_run(
            self,
            run_id=run_id,
            workflow_name=name,
            workflow_version=defn.version,
            input=run_input,
            metadata=run_metadata,
            triggered_by=triggered_by,
        )

        run_info = await _runs.fetch_run(self, run_id)
        if run_info is None:
            raise RuntimeError(f"flow {name!r}: run {run_id} not found after start_run")
        return WorkflowInstance._from_run_info(run_info, self)

    async def send(self, run_id: UUID, *, event: str, payload: JsonValue = None) -> int:
        """Send a signal to a running workflow instance.

        Returns the signal's id.
        """

        return await _signals.insert_signal(self, run_id, event, payload)

    def query(self) -> InstanceQuery:
        """Start an :class:`~forktex_core.flow.read.instance.InstanceQuery` builder."""

        return InstanceQuery(self)

    async def _execute_query(
        self,
        query: InstanceQuery,
        mode: str = "fetch",
        cursor: str | None = None,
        limit_override: int | None = None,
    ) -> object:
        """Called by InstanceQuery terminal methods to execute SQL.

        Returns whatever the requested ``mode`` produces — a ``Page[RunInfo]``,
        an ``int``, or a summary dict. ``InstanceQuery`` maps the page onto
        ``WorkflowInstance``; the engine deliberately knows nothing about that
        user-facing shape.
        """

        return await _query.execute_instance_query(self, query, mode=mode, cursor=cursor, limit_override=limit_override)

    # ── Row-shaped conveniences ──────────────────────────────────────────
    # These return `RunInfo`/`UUID` rather than a bound `WorkflowInstance`, which is what
    # a caller wants when it is going to serialise the result or already has the id.
    # `run()` and `query()` are the richer object API; neither supersedes these.

    async def start(
        self,
        name: str,
        *,
        version: int | None = None,
        input: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        triggered_by: str = "manual",
    ) -> UUID:
        """Submit a new run. Returns the ``run_id`` immediately.

        Use :meth:`run` when you want a bound :class:`WorkflowInstance` to await or refresh.
        """
        instance = await self.run(
            name,
            version=version,
            state=input,
            metadata=metadata,
            triggered_by=triggered_by,
        )
        return instance.instance_id

    async def wait(
        self,
        run_id: UUID,
        timeout: float | None = None,
    ) -> RunInfo:
        """Block until the run reaches a terminal state, or ``timeout``
        elapses. Polls every 0.5s — adequate for tests and CLI use.
        Production callers should prefer ``stream`` for live updates.

        :meth:`WorkflowInstance.wait` is the same wait on a bound instance.
        """
        import time

        deadline = (time.monotonic() + timeout) if timeout is not None else None
        while True:
            info = await _runs.fetch_run(self, run_id)
            if info is None:
                raise NotFoundError(f"run {run_id} not found")
            if info.status in TERMINAL_STATUSES:
                return info
            if deadline is not None and time.monotonic() >= deadline:
                return info
            await asyncio.sleep(0.5)

    async def get(self, run_id: UUID) -> RunInfo:
        """Return full state of a run including per-step progress.

        Raises :class:`~forktex_core.error.NotFoundError` when the run does not exist.
        """

        info = await _runs.fetch_run(self, run_id)
        if info is None:
            raise NotFoundError(f"run {run_id} not found")
        return info

    async def list(
        self,
        *,
        workflow_name: str | None = None,
        status: list[RunStatus] | None = None,
        metadata: dict[str, Any] | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        limit: int = 50,
    ) -> list[RunInfo]:
        """Return runs matching the filter (most recent first).

        ``flow.query()`` is the fluent equivalent and supports cursor pagination; this is the
        one-shot form for a bounded list.
        """

        runs: list[RunInfo] = []
        async for r in _runs.list_runs(
            self,
            workflow_name=workflow_name,
            statuses=list(status) if status else None,
            metadata_filter=metadata,
            started_after=started_after,
            started_before=started_before,
            limit=limit,
        ):
            runs.append(r)
        return runs

    async def cancel(self, run_id: UUID) -> None:
        """Mark a run cancelled. Any in-flight step will see the cancel
        on its next replay; subsequent step calls raise."""

        await _runs.update_run_status(
            self,
            run_id,
            status="cancelled",
            cancel_reason="cancelled by operator",
        )

    async def stream(self, run_id: UUID) -> AsyncIterator[RunUpdate]:
        """Async iterator yielding updates as a run progresses.

        Implemented as a polling iterator for V1 — emits a fresh
        ``RunUpdate`` whenever the run's or any step's status changes
        relative to the last yielded snapshot. ``LISTEN``/``NOTIFY``
        backed streaming is a V2 enhancement (would reduce latency
        from ~1s to ~10ms).
        """

        last_status: str | None = None
        last_steps: dict[str, str] = {}
        # Iteration loop yields immediately for any state transition.
        while True:
            info = await _runs.fetch_run(self, run_id)
            if info is None:
                return
            now = iso.now()
            # Run-level transitions.
            if last_status != info.status:
                yield RunUpdate(
                    run_id=run_id,
                    timestamp=now,
                    event_type=(
                        "run_started"
                        if info.status == "running" and last_status is None
                        else "run_completed"
                        if info.status == "completed"
                        else "run_failed"
                        if info.status == "failed"
                        else "run_cancelled"
                        if info.status == "cancelled"
                        else "run_started"
                    ),
                    payload={"status": info.status, "error": info.error},
                )
                last_status = info.status
            # Step-level transitions.
            for step in info.steps:
                key = str(step.step_id)
                prev = last_steps.get(key)
                if prev != step.status:
                    et = (
                        "step_started"
                        if step.status == "running"
                        else "step_completed"
                        if step.status == "completed"
                        else "step_failed"
                        if step.status == "failed"
                        else "step_retried"
                        if step.status == "pending" and prev is not None
                        else "step_started"
                    )
                    yield RunUpdate(
                        run_id=run_id,
                        timestamp=now,
                        event_type=et,
                        payload={
                            "step_name": step.step_name,
                            "step_index": step.step_index,
                            "attempts": step.attempts,
                            "error": step.error,
                        },
                    )
                    last_steps[key] = step.status
            if info.status in TERMINAL_STATUSES:
                return
            await asyncio.sleep(0.5)

    async def __aenter__(self) -> Flow:
        await self.init()
        await self.start_driver()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


__all__ = ["Flow"]
