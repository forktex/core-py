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

"""Ctx — per-run execution context threaded through all @node functions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, PrivateAttr

from forktex_core.flow.domain.types import TERMINAL_STATUSES
from forktex_core.flow.persist import runs as _runs
from forktex_core.flow.persist import signals as _signals
from forktex_core.flow.persist import steps as _steps

if TYPE_CHECKING:
    from forktex_core.flow.flow import Flow


class _StepCounter(BaseModel):
    """Monotonic ordinal counter for step cache keys. Resets on each replay invocation."""

    _count: int = PrivateAttr(default=0)

    def next(self) -> int:
        self._count += 1
        return self._count


class Ctx(BaseModel):
    """Per-run handle threaded through workflow + node functions.

    Replaces FlowContext with a richer interface: node_name + attempt
    are set by the executor before each node call so observability hooks
    can see where in the graph execution currently is.  namespace
    supports namespace-track definitions alongside platform-track ones.

    The _flow and _step_counter attributes are internal (private, excluded
    from repr/model_dump); consumers should not access them directly.
    """

    run_id: UUID
    workflow_name: str
    workflow_version: int
    namespace: str | None  # None for platform-track
    node_name: str  # set by executor before each node call
    attempt: int  # retry count for current node
    metadata: dict[str, Any]

    _flow: Flow | None = PrivateAttr(default=None)
    _step_counter: _StepCounter = PrivateAttr(default_factory=_StepCounter)
    _spawn_ordinals: dict[str, int] = PrivateAttr(default_factory=dict)
    _wait_ordinals: dict[str, int] = PrivateAttr(default_factory=dict)

    def __init__(
        self,
        _flow: Flow | None = None,
        _step_counter: _StepCounter | None = None,
        _spawn_ordinals: dict[str, int] | None = None,
        _wait_ordinals: dict[str, int] | None = None,
        **data: object,
    ) -> None:
        """Accept the leading-underscore private attributes as constructor
        kwargs too (e.g. ``Ctx(..., _flow=flow)``) — Pydantic's generated
        ``__init__`` only accepts declared (public) fields, so private
        attrs need this small bridge to keep the existing call shape.

        Each is named explicitly rather than popped from an untyped bag, so the
        private attributes keep their declared types through the bridge.
        """
        flow = _flow
        step_counter = _step_counter
        spawn_ordinals = _spawn_ordinals
        wait_ordinals = _wait_ordinals
        super().__init__(**data)
        if flow is not None:
            self._flow = flow
        if step_counter is not None:
            self._step_counter = step_counter
        if spawn_ordinals is not None:
            self._spawn_ordinals = spawn_ordinals
        if wait_ordinals is not None:
            self._wait_ordinals = wait_ordinals

    def _require_flow(self) -> Flow:
        if self._flow is None:
            raise RuntimeError("Ctx method called outside a Flow context")
        return self._flow

    def _step_index(self) -> int:
        """Per-replay ordinal — increments each time a durable operation
        is issued inside one workflow invocation."""
        return self._step_counter.next()

    def _next_spawn_ordinal(self, workflow: str) -> int:
        """Per-(workflow, replay) spawn ordinal for __spawn__: cache keys."""
        self._spawn_ordinals[workflow] = self._spawn_ordinals.get(workflow, 0) + 1
        return self._spawn_ordinals[workflow]

    def _next_wait_ordinal(self, instance_id: UUID) -> int:
        """Per-(instance_id, replay) wait ordinal for __wait__: cache keys."""
        key = str(instance_id)
        self._wait_ordinals[key] = self._wait_ordinals.get(key, 0) + 1
        return self._wait_ordinals[key]

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Append a structured event to run_event for observability."""

        flow = self._require_flow()
        await _runs.emit_run_event(flow, self.run_id, event_type, payload)

    async def sleep(self, seconds: float) -> None:
        """In-process sleep.

        V1 implementation: if the driver restarts during the sleep the
        workflow is replayed from the top; cached step outputs fast-forward
        past completed work, but the sleep itself restarts.  Durable sleep
        (recording wake_at to the run row) is a V2 enhancement.
        """
        await asyncio.sleep(seconds)

    async def send(self, event: str, payload: dict[str, Any] | None = None) -> None:
        """Send a signal to this run (self-signal for state machine advance)."""

        flow = self._require_flow()
        await _signals.insert_signal(flow, self.run_id, event, payload)

    async def spawn(
        self,
        workflow: str,
        state: dict[str, Any],
        namespace: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UUID:
        """Launch a child workflow asynchronously. Returns instance_id.

        Durable: the child run_id is cached in a synthetic step_run row
        keyed __spawn__:{workflow}:{ordinal} so that on replay the same
        run_id is returned without launching a second child.
        """

        flow = self._require_flow()
        ordinal = self._next_spawn_ordinal(workflow)
        step_index = self._step_index()

        step_qualname = f"__spawn__:{workflow}:{ordinal}"
        args_hash = _stable_hash({"workflow": workflow, "state": state, "ordinal": ordinal, "step_index": step_index})

        step_id, status, cached_output = await _steps.upsert_pending_step(
            flow,
            run_id=self.run_id,
            step_name=step_qualname,
            step_qualname=step_qualname,
            step_index=step_index,
            args_hash=args_hash,
            max_attempts=1,
        )

        if status == "completed" and cached_output is not None:
            # Replay path: return the previously recorded child run_id.
            return UUID(cached_output["child_run_id"])

        # First execution: start the child workflow.
        await _steps.mark_step_running(flow, step_id)
        child_run_id = await flow.start(
            workflow,
            input=state,
            metadata={**(metadata or {}), "parent_run_id": str(self.run_id)},
            triggered_by="spawn",
        )
        await _steps.mark_step_completed(flow, step_id, {"child_run_id": str(child_run_id)})
        await self.emit("child_spawned", {"workflow": workflow, "child_run_id": str(child_run_id)})
        return child_run_id

    async def wait(
        self,
        instance_id: UUID,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Wait for a child workflow to reach terminal state. Returns final state.

        Polls every 0.5s.  The result is cached in a synthetic step_run row
        __wait__:{run_id}:{ordinal} so that on replay the same final state is
        returned deterministically without re-polling.
        """

        flow = self._require_flow()
        ordinal = self._next_wait_ordinal(instance_id)
        step_index = self._step_index()

        step_qualname = f"__wait__:{instance_id}:{ordinal}"
        args_hash = _stable_hash({"instance_id": str(instance_id), "ordinal": ordinal, "step_index": step_index})

        step_id, status, cached_output = await _steps.upsert_pending_step(
            flow,
            run_id=self.run_id,
            step_name=step_qualname,
            step_qualname=step_qualname,
            step_index=step_index,
            args_hash=args_hash,
            max_attempts=1,
        )

        if status == "completed" and cached_output is not None:
            # Replay: return the cached final state.
            return cached_output.get("final_state", {})

        await _steps.mark_step_running(flow, step_id)

        deadline = (time.monotonic() + timeout) if timeout is not None else None
        while True:
            info = await _runs.fetch_run(flow, instance_id)
            if info is None:
                raise ValueError(f"child run {instance_id} not found")
            if info.status in TERMINAL_STATUSES:
                final_state = info.output or {}
                await _steps.mark_step_completed(flow, step_id, {"final_state": final_state})
                await self.emit(
                    "child_completed",
                    {"child_run_id": str(instance_id), "status": info.status},
                )
                return final_state
            if deadline is not None and time.monotonic() >= deadline:
                await _steps.mark_step_failed(flow, step_id, "wait timeout", final=True)
                raise TimeoutError(f"child run {instance_id} did not complete within {timeout}s")
            await asyncio.sleep(0.5)

    async def map(
        self,
        workflow: str,
        states: list[dict[str, Any]],
        *,
        namespace: str | None = None,
        fail_fast: bool = True,
    ) -> list[dict[str, Any]]:
        """Scatter-gather: spawn N child workflows in parallel, wait for all.

        Returns list of final states in same order as input states.
        If fail_fast=True, cancels remaining on first failure.
        """
        # Spawn all children first, preserving deterministic ordinals.
        run_ids: list[UUID] = []
        for state in states:
            child_id = await self.spawn(workflow, state, namespace=namespace)
            run_ids.append(child_id)

        # Wait for all children concurrently.
        if fail_fast:
            # asyncio.gather propagates the first exception and cancels the rest.
            results = await asyncio.gather(
                *[self.wait(run_id) for run_id in run_ids],
                return_exceptions=False,
            )
        else:
            # Collect all results including failures.
            raw = await asyncio.gather(
                *[self.wait(run_id) for run_id in run_ids],
                return_exceptions=True,
            )
            results = []
            for r in raw:
                if isinstance(r, BaseException):
                    results.append({"error": str(r)})
                else:
                    results.append(r)

        return list(results)


def _stable_hash(payload: dict[str, Any]) -> str:
    """SHA-256 of a JSON-serialised dict for step cache key generation."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["Ctx"]
