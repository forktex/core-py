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

"""User-facing query builder and result types for workflow instances.

The primary entry point is :class:`InstanceQuery`, a fluent builder
that filters, paginates, and aggregates workflow run records.  Result
types are plain dataclasses — no SQLAlchemy bleed-through.

Typical usage::

    page = await (
        flow.query()
        .workflow("deploy.cloud", version=2)
        .namespace("acme")
        .status("running", "pending")
        .since(datetime(2026, 1, 1, tzinfo=timezone.utc))
        .limit(25)
        .fetch()
    )

    for instance in page.items:
        print(instance.instance_id, instance.current_node)

SQL execution is delegated to :meth:`Flow._execute_query`; the query
builder itself is pure Python with no I/O.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from pydantic import PrivateAttr

from forktex_core.database.pagination import Page
from forktex_core.database.pagination import decode_cursor as _decode_cursor
from forktex_core.database.pagination import encode_cursor as _encode_cursor
from forktex_core.flow.domain.types import RunInfo, StepRunInfo
from forktex_core.flow.persist import runs as _runs
from forktex_core.types import BaseWireValueObject, JsonValue

if TYPE_CHECKING:
    from forktex_core.flow.flow import Flow


class NodeInstance(BaseWireValueObject):
    """Per-node execution record within a :class:`WorkflowInstance`.

    Maps to a single ``step_run`` row, enriched with the human-readable
    node name extracted from the step's qualname.
    """

    name: str
    """Node name, e.g. ``"provision"`` or ``"__parallel_2__"``."""

    status: str
    """One of ``"pending"``, ``"running"``, ``"completed"``, ``"failed"``,
    ``"skipped"``, or ``"cancelled"``."""

    attempt: int
    """Current (or final) attempt number (1-based)."""

    started_at: datetime | None
    finished_at: datetime | None

    duration: timedelta | None
    """``finished_at - started_at`` when both are set; else ``None``."""

    state_delta: dict[str, Any] | None
    """Partial state update returned by this node (the step's output),
    or ``None`` if the node has not yet completed."""

    error: str | None
    """Last error traceback string, or ``None``."""


class WorkflowInstance(BaseWireValueObject):
    """A running or completed workflow run — the primary user-facing handle.

    Constructed from :class:`~forktex_core.flow.domain.types.RunInfo` via
    :meth:`_from_run_info`.  Methods that mutate live state (cancel,
    send, wait) delegate back to the :class:`~forktex_core.flow.flow.Flow`
    instance stored in ``_flow``.
    """

    instance_id: UUID
    workflow_name: str
    workflow_version: int
    namespace: str | None
    status: str
    state: dict[str, Any]
    """Current accumulated state — ``run.output`` if available, else ``run.input``."""
    metadata: dict[str, Any]
    current_node: str | None
    """Name of the node currently executing, or ``None``."""
    started_at: datetime
    finished_at: datetime | None
    nodes: list[NodeInstance]

    _flow: Flow | None = PrivateAttr(default=None)
    """Back-reference to the :class:`~forktex_core.flow.flow.Flow` instance.
    Set by :meth:`_from_run_info`; not serialised."""

    def __init__(self, _flow: Flow | None = None, **data: object) -> None:
        """Accept ``_flow`` as a constructor kwarg too (Pydantic's generated
        ``__init__`` only accepts declared public fields).

        Named explicitly rather than popped from an untyped bag, so the private
        attribute keeps its declared type through the bridge.
        """
        super().__init__(**data)
        if _flow is not None:
            self._flow = _flow

    async def cancel(self) -> None:
        """Cancel this run. Idempotent on already-terminal runs."""

        if self._flow is None:
            raise RuntimeError("WorkflowInstance has no bound Flow — cannot cancel")
        await _runs.update_run_status(
            self._flow,
            self.instance_id,
            status="cancelled",
            cancel_reason="cancelled by operator",
        )

    async def wait(self, timeout: float | None = None) -> WorkflowInstance:
        """Block until this run reaches a terminal state.

        Returns a refreshed :class:`WorkflowInstance` reflecting the
        final state.  Polls every 0.5 s — adequate for tests and CLI
        use.
        """
        if self._flow is None:
            raise RuntimeError("WorkflowInstance has no bound Flow — cannot wait")
        run_info = await self._flow.wait(self.instance_id, timeout=timeout)
        return WorkflowInstance._from_run_info(run_info, self._flow)

    async def send(self, event: str, payload: dict[str, Any] | None = None) -> None:
        """Send an external signal to this run (e.g. to advance a
        ``wait_edge`` node or trigger a manual graph transition)."""
        if self._flow is None:
            raise RuntimeError("WorkflowInstance has no bound Flow — cannot send")
        await self._flow.send(self.instance_id, event=event, payload=payload)

    async def refresh(self) -> WorkflowInstance:
        """Return a fresh :class:`WorkflowInstance` snapshot by
        re-fetching the run from the database."""
        if self._flow is None:
            raise RuntimeError("WorkflowInstance has no bound Flow — cannot refresh")
        run_info = await self._flow.get(self.instance_id)
        return WorkflowInstance._from_run_info(run_info, self._flow)

    async def stream(self) -> AsyncIterator[WorkflowInstance]:
        """Async generator that yields a refreshed :class:`WorkflowInstance`
        on every run or step transition until the run reaches a terminal state.

        Backed by :meth:`Flow.stream` (polling in V1; LISTEN/NOTIFY in V2).
        """
        if self._flow is None:
            raise RuntimeError("WorkflowInstance has no bound Flow — cannot stream")
        async for _update in self._flow.stream(self.instance_id):
            yield await self.refresh()

    @classmethod
    def _from_run_info(cls, run_info: RunInfo, flow: Flow) -> WorkflowInstance:
        """Build a :class:`WorkflowInstance` from a
        :class:`~forktex_core.flow.domain.types.RunInfo`.

        - ``state`` = ``run_info.output`` when set, else ``run_info.input``
        - ``namespace`` = ``run_info.metadata.get("__namespace__")``
        - ``current_node`` = first step_run with ``status == "running"``
          whose qualname matches the ``__node__:{graph}:{name}`` pattern,
          falling back to bare ``step_name``.
        - ``nodes`` = :class:`NodeInstance` list derived from
          ``run_info.steps``.
        """
        state: dict[str, Any] = dict(run_info.output) if run_info.output else dict(run_info.input)
        namespace: str | None = run_info.metadata.get("__namespace__")

        node_instances = [_step_to_node_instance(s) for s in run_info.steps]

        current_node: str | None = None
        for step in run_info.steps:
            if step.status == "running":
                current_node = _node_name_from_qualname(step.step_name)
                break

        return cls(
            instance_id=run_info.run_id,
            workflow_name=run_info.workflow_name,
            workflow_version=run_info.workflow_version,
            namespace=namespace,
            status=run_info.status,
            state=state,
            metadata=run_info.metadata,
            current_node=current_node,
            started_at=run_info.started_at,
            finished_at=run_info.finished_at,
            nodes=node_instances,
            _flow=flow,
        )


#: One page of :class:`WorkflowInstance` results — the library-wide
#: :class:`forktex_core.database.pagination.Page`, not a parallel shape. It used
#: to be a separate model that was never actually constructed: `fetch()`
#: ``cast()``-ed the engine's bare 3-tuple to it, so `page.items` raised
#: ``AttributeError`` at runtime. `fetch()` now builds this for real.
InstancePage = Page[WorkflowInstance]


class InstanceSummary(BaseWireValueObject):
    """Aggregate statistics over a matching set of workflow instances."""

    total: int
    by_status: dict[str, int]
    avg_duration_seconds: float | None
    p95_duration_seconds: float | None
    oldest_started_at: datetime | None
    newest_started_at: datetime | None


class InstanceQuery:
    """Fluent builder for querying workflow instances.

    All filter methods mutate and return ``self`` so calls can be chained.
    The actual SQL is executed lazily when :meth:`fetch`, :meth:`count`,
    :meth:`first`, or :meth:`summary` is awaited; prior to that the
    object holds only the filter parameters.

    SQL execution is delegated to :meth:`Flow._execute_query` so the
    query builder itself is I/O-free and unit-testable without a live
    database.
    """

    def __init__(self, flow: Flow) -> None:
        self._flow = flow
        self._workflow_name: str | None = None
        self._workflow_version: int | None = None
        self._namespace: str | None = None
        self._statuses: list[str] = []
        self._metadata_filter: dict[str, Any] = {}
        self._state_filter: dict[str, Any] = {}
        self._current_node_filter: list[str] = []
        self._since: datetime | None = None
        self._until: datetime | None = None
        self._triggered_by: list[str] = []
        self._sort_field: str = "started_at"
        self._sort_desc: bool = True
        self._limit: int = 50

    def workflow(self, name: str, version: int | None = None) -> InstanceQuery:
        """Filter to a specific workflow name (and optionally version)."""
        self._workflow_name = name
        self._workflow_version = version
        return self

    def namespace(self, ns: str) -> InstanceQuery:
        """Filter to runs whose ``metadata.__namespace__`` matches ``ns``."""
        self._namespace = ns
        return self

    def status(self, *statuses: str) -> InstanceQuery:
        """Filter to runs in any of the given statuses.

        Valid values: ``"pending"``, ``"running"``, ``"completed"``,
        ``"failed"``, ``"cancelled"``.
        """
        self._statuses = list(statuses)
        return self

    def metadata(self, **kv: JsonValue) -> InstanceQuery:
        """Filter by metadata key-value pairs (JSONB containment ``@>``)."""
        self._metadata_filter.update(kv)
        return self

    def state(self, **kv: JsonValue) -> InstanceQuery:
        """Filter by state field values (JSONB containment on ``run.input``)."""
        self._state_filter.update(kv)
        return self

    def current_node(self, *node_names: str) -> InstanceQuery:
        """Filter to runs where the currently-executing node is one of
        the given names."""
        self._current_node_filter = list(node_names)
        return self

    def since(self, dt: datetime) -> InstanceQuery:
        """Filter to runs started at or after ``dt``."""
        self._since = dt
        return self

    def until(self, dt: datetime) -> InstanceQuery:
        """Filter to runs started before ``dt``."""
        self._until = dt
        return self

    def triggered_by(self, *triggers: str) -> InstanceQuery:
        """Filter by the ``triggered_by`` label (e.g. ``"manual"``,
        ``"schedule"``, ``"replay"``)."""
        self._triggered_by = list(triggers)
        return self

    def sort(self, field: str, *, desc: bool = True) -> InstanceQuery:
        """Set sort field and direction.  Default is ``started_at DESC``."""
        self._sort_field = field
        self._sort_desc = desc
        return self

    def limit(self, n: int) -> InstanceQuery:
        """Maximum results per page.  Default 50."""
        if n <= 0:
            raise ValueError(f"limit must be > 0, got {n!r}")
        self._limit = n
        return self

    async def fetch(self, cursor: str | None = None) -> InstancePage:
        """Execute the query and return a paginated :class:`InstancePage`.

        ``cursor`` is an opaque base64-encoded bookmark returned in the
        previous page's :attr:`InstancePage.next_cursor`.  Pass it on
        subsequent calls to advance through the result set.

        Delegates to :meth:`Flow._execute_query` for the actual SQL.
        """
        page = await self._flow._execute_query(self, cursor=cursor)
        return self._to_instance_page(page)

    async def count(self) -> int:
        """Return the total number of matching instances without fetching rows.

        Delegates to :meth:`Flow._execute_query` with ``mode="count"``.
        """
        return cast(int, await self._flow._execute_query(self, mode="count"))

    async def first(self) -> WorkflowInstance | None:
        """Return the first matching instance, or ``None``."""
        page = self._to_instance_page(await self._flow._execute_query(self, limit_override=1))
        return page.items[0] if page.items else None

    def _to_instance_page(self, page: object) -> InstancePage:
        """Map the engine's ``Page[RunInfo]`` onto the user-facing page.

        The engine deals in ``RunInfo`` (a row shape); the query API deals in
        ``WorkflowInstance`` (a bound, refreshable object). Pagination metadata
        carries across unchanged.

        Takes ``object`` because ``Flow._execute_query`` is mode-dispatched and so
        returns a page, an ``int`` or a summary dict; the check turns a wrong mode
        into a clear error instead of an ``AttributeError`` three frames later.
        """
        if not isinstance(page, Page):
            raise TypeError(f"expected a Page of runs, got {type(page).__name__}")
        return InstancePage(
            items=[WorkflowInstance._from_run_info(r, self._flow) for r in page.items],
            has_more=page.has_more,
            next_cursor=page.next_cursor,
            total=page.total,
        )

    async def summary(self) -> InstanceSummary:
        """Return aggregate statistics for the matching set.

        Delegates to :meth:`Flow._execute_query` with ``mode="summary"``.
        """
        data = cast(dict[str, Any], await self._flow._execute_query(self, mode="summary"))
        return InstanceSummary(
            total=data["total"],
            by_status=data["by_status"],
            avg_duration_seconds=data["avg_duration_seconds"],
            p95_duration_seconds=data["p95_duration_seconds"],
            oldest_started_at=data["oldest_started_at"],
            newest_started_at=data["newest_started_at"],
        )


#: Cursor encoding lives in :mod:`forktex_core.database.pagination`, re-exported
#: here so `flow`'s public surface still names it. flow used to carry its own
#: ``{"started_at", "id"}``-shaped codec, which is what pinned the keyset
#: predicate to `started_at`: the payload could not describe any other sort key.
#: The shared codec is a positional array, so it describes whatever the query
#: sorted by — and raises ``BadRequestError`` on a malformed token instead of
#: returning ``None`` and silently restarting from page 1.
encode_cursor = _encode_cursor
decode_cursor = _decode_cursor


def _node_name_from_qualname(step_name: str) -> str:
    """Extract a human-readable node name from a step qualname.

    Patterns handled:
    - ``__node__:{graph_name}:{node_name}`` → ``node_name``
    - ``__graph_state__:{state_name}`` → ``state_name``
    - ``__parallel_{i}__`` → kept as-is
    - Anything else → returned as-is
    """
    if step_name.startswith("__node__:"):
        # Pattern: __node__:<graph>:<node>
        parts = step_name.split(":", 2)
        return parts[2] if len(parts) == 3 else step_name
    if step_name.startswith("__graph_state__:"):
        return step_name[len("__graph_state__:") :]
    return step_name


def _step_to_node_instance(step: StepRunInfo) -> NodeInstance:
    """Convert a :class:`~forktex_core.flow.domain.types.StepRunInfo` to a
    :class:`NodeInstance`."""
    node_name = _node_name_from_qualname(step.step_name)

    duration: timedelta | None = None
    if step.started_at is not None and step.finished_at is not None:
        duration = step.finished_at - step.started_at

    state_delta: dict[str, Any] | None = None
    if isinstance(step.output, dict):
        state_delta = step.output

    return NodeInstance(
        name=node_name,
        status=step.status,
        attempt=step.attempts,
        started_at=step.started_at,
        finished_at=step.finished_at,
        duration=duration,
        state_delta=state_delta,
        error=step.error,
    )


__all__ = [
    "InstancePage",
    "InstanceQuery",
    "InstanceSummary",
    "NodeInstance",
    "WorkflowInstance",
    "decode_cursor",
    "encode_cursor",
]
