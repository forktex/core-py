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

"""Replay-on-resume core: workflow execution + step durability wrapper.

The contract — the same one DBOS / Temporal / Restate use — is:
- A workflow function is called from the top each time the run is
  picked up by the driver (initial run or post-crash resume).
- Step calls inside the workflow route through ``dispatch_step``.
  ``dispatch_step`` checks ``step_run`` for a completed cache entry
  matching ``(run_id, step_qualname, args_hash)``; if found, returns
  the cached output (no re-run, no side effects). If not, dispatches
  the step body and records the result.
- A step that raises propagates to the workflow boundary; the run is
  marked ``failed`` after retries exhaust.

The current workflow's :class:`Ctx` is published on a
``ContextVar`` so step decorators (which can't take ``ctx`` from a
workflow-level closure) can pick it up during dispatch.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import traceback
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from forktex_core.flow.domain.definition import END, ConditionalEdge, DirectEdge, WaitEdge
from forktex_core.flow.domain.state import apply_state_update
from forktex_core.flow.errors import GraphStuckError, SignalTimeout, StepFailed, WorkflowCancelled
from forktex_core.flow.persist import runs as _runs
from forktex_core.flow.persist import signals as _signals
from forktex_core.flow.persist import steps as _steps
from forktex_core.flow.persist.models import StepRun
from forktex_core.flow.runtime.ctx import Ctx
from forktex_core.iso import to_date_iso, to_iso
from forktex_core.log import get_logger
from forktex_core.types import JsonValue


class _StepRetryRequested(Exception):
    """Internal sentinel raised when a step body fails but has retries
    remaining. ``execute_run`` catches it, marks the run ``pending``,
    and the driver re-picks the run after the step's backoff window."""


if TYPE_CHECKING:
    from typing import Protocol

    from forktex_core.flow.domain.definition import NodeDef, WorkflowDefinition
    from forktex_core.flow.domain.types import RunInfo
    from forktex_core.flow.flow import Flow
    from forktex_core.flow.runtime.ctx import _StepCounter

    class _SignalCtx(Protocol):
        """What ``dispatch_wait_signal`` needs from a context.

        Not ``Ctx``: the graph path passes an adapter exposing ``.flow``, while
        ``Ctx`` stores the same handle as ``._flow``. A structural type describes
        both without either having to change.
        """

        run_id: UUID
        flow: Flow
        _step_counter: _StepCounter

    class _StepDef(Protocol):
        """Structural type for what ``dispatch_step`` needs from a step descriptor."""

        qualname: str
        max_attempts: int
        backoff: tuple[float, ...]
        fn: Callable[..., Any]


logger = get_logger(__name__)


_current_ctx: contextvars.ContextVar[Ctx | None] = contextvars.ContextVar("forktex_flow.current_ctx", default=None)


def _step_index_counter_for(ctx: _SignalCtx | Ctx) -> int:
    """Per-run, per-replay ordinal used for ``step_run.step_index``."""
    return ctx._step_counter.next()


def _hash_args(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    call_ordinal: int,
) -> str:
    """Stable hash over a step call's positional + keyword args
    PLUS its call-site ordinal within the workflow.

    Including ``call_ordinal`` (the per-replay step_index counter)
    means two calls to the same step with identical args get distinct
    cache rows. That matches the developer's intuition for control
    flow like::

        for item in items:
            await process(ctx, item)   # each iteration is its own step run

    AND it's load-bearing for ``ctx.wait_signal(name)``: each call
    site consumes a fresh signal even when name + timeout are the
    same. Replay determinism still holds because ``step_index``
    increments deterministically each time the workflow function is
    re-invoked from the top.

    Args must be JSON-serialisable. Steps that receive non-serialisable
    values (e.g., DB sessions) should not — pass IDs and look them up
    inside the step body. This is the standard durability constraint.
    """
    payload = {"args": list(args), "kwargs": kwargs, "ordinal": call_ordinal}

    def _default(o: object) -> object:
        # Ctx is the first positional arg; strip it from the hash so the
        # same step+args from different runs hash the same.
        if isinstance(o, Ctx):
            return "<flow_ctx>"
        # Route temporal values through `iso` rather than the object's own
        # `.isoformat()`. `to_iso` normalizes to UTC first, so the same instant
        # expressed in two different offsets produces the same hash — with a
        # bare `.isoformat()` it did not, and a step's cache lookup could miss
        # on replay purely because the caller's tzinfo differed.
        # datetime must be checked before date: it is a subclass of it, and
        # `to_iso` deliberately rejects a plain date.
        if isinstance(o, datetime):
            return to_iso(o)
        if isinstance(o, date):
            return to_date_iso(o)
        if isinstance(o, UUID):
            return str(o)
        return str(o)

    canonical = json.dumps(payload, sort_keys=True, default=_default)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def dispatch_step(
    step_def: _StepDef,
    ctx: Ctx,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> JsonValue:
    """Durable step boundary. Cache hit → return cached output;
    cache miss → run the body, record output (or error+retry)."""
    flow = ctx._flow
    if flow is None:
        raise RuntimeError("dispatch_step called outside a workflow context")

    # Step ordinal is per-replay (resets each time the workflow function
    # is re-invoked). It feeds into args_hash so each call site gets a
    # distinct cache row even when args repeat (e.g. loops, signal waits).
    step_index = _step_index_counter_for(ctx)
    args_hash = _hash_args(args, kwargs, call_ordinal=step_index)

    step_id, status, cached_output = await _steps.upsert_pending_step(
        flow,
        run_id=ctx.run_id,
        step_name=step_def.qualname.rsplit(".", 1)[-1],
        step_qualname=step_def.qualname,
        step_index=step_index,
        args_hash=args_hash,
        max_attempts=step_def.max_attempts,
    )

    if status == "completed":
        # Cache hit — return cached output without re-running the body.
        await _runs.emit_run_event(
            flow,
            ctx.run_id,
            "step_replayed",
            {"step_name": step_def.qualname, "step_id": str(step_id)},
        )
        return cached_output
    if status == "cancelled":
        raise WorkflowCancelled(f"step {step_def.qualname} previously cancelled")

    await _steps.mark_step_running(flow, step_id)
    await _runs.emit_run_event(
        flow,
        ctx.run_id,
        "step_started",
        {"step_name": step_def.qualname, "step_id": str(step_id)},
    )

    heartbeat_task = asyncio.create_task(_heartbeat_loop(flow, step_id))
    try:
        # ``args`` already carries ``ctx`` as its first positional —
        # the wrapper passes through everything the workflow body
        # supplied. Don't prepend a second ``ctx`` here.
        result = await step_def.fn(*args, **kwargs)
    except WorkflowCancelled:
        await _steps.mark_step_failed(flow, step_id, "cancelled", final=True)
        raise
    except Exception as e:
        # Read attempt count to decide retry vs final-fail.
        # `attempts` was just incremented by mark_step_running; if it
        # equals max_attempts, this is the final try.
        tb = traceback.format_exc()
        attempts = await _read_step_attempts(flow, step_id)
        final = attempts >= step_def.max_attempts
        if final:
            await _steps.mark_step_failed(flow, step_id, tb, final=True)
            await _runs.emit_run_event(
                flow,
                ctx.run_id,
                "step_failed",
                {"step_name": step_def.qualname, "attempts": attempts, "error": str(e)},
            )
            raise StepFailed(f"step {step_def.qualname} failed after {attempts} attempts") from e
        # Schedule retry: the DB computes next_attempt_at on its own clock
        # (the same clock the driver selects due retries with) and hands the
        # stored value back, so the event carries the real deadline.
        idx = min(attempts - 1, len(step_def.backoff) - 1)
        next_at = await _steps.mark_step_failed(flow, step_id, tb, final=False, retry_in_seconds=step_def.backoff[idx])
        await _runs.emit_run_event(
            flow,
            ctx.run_id,
            "step_retried",
            {
                "step_name": step_def.qualname,
                "attempts": attempts,
                "next_attempt_at": None if next_at is None else to_iso(next_at),
                "error": str(e),
            },
        )
        # Internal sentinel: bubble out of the workflow body so the
        # ``execute_run`` boundary can mark the run ``pending`` for
        # the driver to re-pick after the backoff. Distinct from
        # ``StepFailed`` (which signals a TERMINAL step failure and
        # marks the run ``failed``).
        raise _StepRetryRequested(
            f"step {step_def.qualname} retry scheduled (attempt {attempts}/{step_def.max_attempts})"
        ) from e
    else:
        await _steps.mark_step_completed(flow, step_id, result)
        await _runs.emit_run_event(
            flow,
            ctx.run_id,
            "step_completed",
            {"step_name": step_def.qualname, "step_id": str(step_id)},
        )
        return result
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        except Exception:
            # The loop handles its own errors; reaching here on teardown is
            # unexpected but must not mask the step's own outcome — log, don't raise.
            logger.debug("heartbeat task raised on teardown", exc_info=True)


async def _heartbeat_loop(flow: Flow, step_id: UUID) -> None:
    """Refresh the step's ``heartbeat_at`` at the configured interval
    until the surrounding step body returns or raises."""
    try:
        while True:
            await asyncio.sleep(flow.heartbeat_interval)
            try:
                await _steps.heartbeat_step(flow, step_id)
            except Exception:  # pragma: no cover — DB blip
                logger.debug("heartbeat update failed", exc_info=True)
    except asyncio.CancelledError:
        return


async def dispatch_wait_signal(
    ctx: _SignalCtx,
    signal_name: str,
    timeout: float | None,
    poll_interval: float,
) -> dict[str, Any]:
    """Durable signal-wait. Polls ``forktex_flow.signal`` for an
    unconsumed entry matching ``(run_id, signal_name)``; on receipt,
    consumes it (sets ``consumed_at``) and caches the
    ``(signal_id, payload)`` in a synthetic step_run row keyed by the
    call-site ordinal.

    On replay, the cached step_run output short-circuits the poll:
    ``fetch_consumed_signal(signal_id)`` returns the same payload so
    the workflow function deterministically continues with the value
    it had on the original run.

    Returns ``{"name": ..., "payload": ...}`` so transition predicates
    can read both.
    """
    flow = ctx.flow
    if flow is None:
        raise RuntimeError("wait_signal called outside a workflow context")

    step_index = _step_index_counter_for(ctx)
    qualname = f"__signal__:{signal_name}"
    args_hash = _hash_args((signal_name,), {"timeout": timeout}, call_ordinal=step_index)

    step_id, status, cached_output = await _steps.upsert_pending_step(
        flow,
        run_id=ctx.run_id,
        step_name=qualname,
        step_qualname=qualname,
        step_index=step_index,
        args_hash=args_hash,
        max_attempts=1,  # signal waits don't retry; they poll until match.
    )

    if status == "completed":
        # Cached signal_id; refetch payload by id so the same value
        # surfaces on replay even if the signal row's payload was
        # mutated externally (it shouldn't be — table is append-only).
        sid = (cached_output or {}).get("signal_id")
        payload = await _signals.fetch_consumed_signal(flow, sid) if sid is not None else None
        return {
            "name": signal_name,
            "payload": payload,
            "signal_id": sid,
        }

    await _steps.mark_step_running(flow, step_id)

    deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
    heartbeat_task = asyncio.create_task(_heartbeat_loop(flow, step_id))
    try:
        while True:
            consumed = await _signals.consume_signal(flow, ctx.run_id, signal_name)
            if consumed is not None:
                signal_id, signal_payload = consumed
                output = {"signal_id": signal_id, "name": signal_name}
                await _steps.mark_step_completed(flow, step_id, output)
                await _runs.emit_run_event(
                    flow,
                    ctx.run_id,
                    "signal_received",
                    {"name": signal_name, "signal_id": signal_id},
                )
                return {
                    "name": signal_name,
                    "payload": signal_payload,
                    "signal_id": signal_id,
                }
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                await _steps.mark_step_failed(flow, step_id, f"signal {signal_name!r} timed out", final=True)
                raise SignalTimeout(f"signal {signal_name!r} timed out after {timeout}s")
            await asyncio.sleep(poll_interval)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        except Exception:
            # The loop handles its own errors; reaching here on teardown is
            # unexpected but must not mask the step's own outcome — log, don't raise.
            logger.debug("heartbeat task raised on teardown", exc_info=True)


async def _read_step_attempts(flow: Flow, step_id: UUID) -> int:
    """Return the current ``attempts`` value for a step row. Used by
    the retry-vs-final-fail decision after a step body raises."""
    from sqlalchemy import select

    async with flow.session() as session:
        attempts = (await session.execute(select(StepRun.attempts).where(StepRun.id == step_id))).scalar_one()
        return attempts or 0


async def execute_run(
    flow: Flow,
    run_id: UUID,
    workflow_name: str,
    workflow_version: int,
) -> None:
    """Drive a single run through its workflow function. Catches
    terminal exceptions and marks the run accordingly."""
    defn = flow._registry.get_definition(workflow_name, version=workflow_version)
    if defn is not None:
        run_info = await _runs.fetch_run(flow, run_id)
        if run_info is None:
            return  # row deleted between claim and dispatch
        await execute_graph_run(flow, run_id, defn, run_info)
        return

    # No definition found — mark the run as failed with a clear message.
    await _runs.update_run_status(
        flow,
        run_id,
        status="failed",
        error=(
            f"workflow {workflow_name!r} version {workflow_version} not registered "
            "in this process — refusing to dispatch"
        ),
    )


async def _dispatch_node(
    flow: Flow,
    ctx: Ctx,
    node_def: NodeDef,
    current_state: dict[str, Any],
) -> dict[str, Any]:
    """Durable node boundary for graph execution.

    Cache hit (already completed) → return cached partial update dict.
    when_fn=False → skip (record as skipped, return {}).
    Cache miss → run node body, record output.

    step_qualname = "__node__:{graph_name}:{node_name}"
    This is distinct from old @flow.step qualnames so both can coexist.
    """
    step_qualname = f"__node__:{ctx.workflow_name}:{node_def.name}"
    step_index = _step_index_counter_for(ctx)
    args_hash = _hash_args((current_state,), {}, call_ordinal=step_index)

    step_id, status, cached_output = await _steps.upsert_pending_step(
        flow,
        run_id=ctx.run_id,
        step_name=node_def.name,
        step_qualname=step_qualname,
        step_index=step_index,
        args_hash=args_hash,
        max_attempts=node_def.max_attempts,
    )

    if status == "completed":
        # Cache hit — check if it was previously skipped.
        if isinstance(cached_output, dict) and cached_output.get("__skipped__") is True:
            await _runs.emit_run_event(
                flow,
                ctx.run_id,
                "node_skipped",
                {"node": node_def.name},
            )
            return {}
        # Return the cached partial state update.
        return cached_output if isinstance(cached_output, dict) else {}

    # when_fn guard: if present and returns False, skip the node.
    if node_def.when_fn is not None and not node_def.when_fn(current_state):
        await _steps.mark_step_running(flow, step_id)
        await _steps.mark_step_skipped(flow, step_id)
        await _runs.emit_run_event(
            flow,
            ctx.run_id,
            "node_skipped",
            {"node": node_def.name},
        )
        return {}

    await _steps.mark_step_running(flow, step_id)
    attempts = await _read_step_attempts(flow, step_id)
    ctx.node_name = node_def.name
    ctx.attempt = attempts

    await _runs.emit_run_event(
        flow,
        ctx.run_id,
        "node_started",
        {"node": node_def.name, "step_id": str(step_id)},
    )

    heartbeat_task = asyncio.create_task(_heartbeat_loop(flow, step_id))
    try:
        result = await node_def.fn(ctx, current_state)
        # Node functions return a partial state update dict (or None/empty).
        if result is None:
            result = {}
    except WorkflowCancelled:
        await _steps.mark_step_failed(flow, step_id, "cancelled", final=True)
        raise
    except Exception as e:
        tb = traceback.format_exc()
        attempts = await _read_step_attempts(flow, step_id)
        final = attempts >= node_def.max_attempts
        if final:
            await _steps.mark_step_failed(flow, step_id, tb, final=True)
            await _runs.emit_run_event(
                flow,
                ctx.run_id,
                "node_failed",
                {"node": node_def.name, "attempts": attempts, "error": str(e)},
            )
            raise StepFailed(f"node {node_def.name!r} failed after {attempts} attempts") from e
        idx = min(attempts - 1, len(node_def.backoff) - 1)
        next_at = await _steps.mark_step_failed(flow, step_id, tb, final=False, retry_in_seconds=node_def.backoff[idx])
        await _runs.emit_run_event(
            flow,
            ctx.run_id,
            "node_retried",
            {
                "node": node_def.name,
                "attempts": attempts,
                "next_attempt_at": None if next_at is None else to_iso(next_at),
                "error": str(e),
            },
        )
        raise _StepRetryRequested(
            f"node {node_def.name!r} retry scheduled (attempt {attempts}/{node_def.max_attempts})"
        ) from e
    else:
        await _steps.mark_step_completed(flow, step_id, result)
        await _runs.emit_run_event(
            flow,
            ctx.run_id,
            "node_completed",
            {"node": node_def.name, "step_id": str(step_id)},
        )
        return result
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        except Exception:
            # The loop handles its own errors; reaching here on teardown is
            # unexpected but must not mask the step's own outcome — log, don't raise.
            logger.debug("heartbeat task raised on teardown", exc_info=True)


async def _route(
    defn: WorkflowDefinition,
    from_node: str,
    state: dict[str, Any],
    ctx: Ctx,
) -> str:
    """Determine next node from the current node's outgoing edges.

    DirectEdge → return to_node directly.
    ConditionalEdge → call router_fn(state), map to next node name.
    WaitEdge → await dispatch_wait_signal via adapter, return to_node.

    Raises GraphStuckError if no matching edge.
    """

    edges = defn.edges.get(from_node, [])
    if not edges:
        if from_node == END:
            return END
        raise GraphStuckError(f"graph {defn.name!r}: node {from_node!r} has no outgoing edges")

    for edge in edges:
        if isinstance(edge, DirectEdge):
            return edge.to_node
        elif isinstance(edge, ConditionalEdge):
            result_key = edge.router_fn(state)
            next_node = edge.mapping.get(result_key)
            if next_node is not None:
                return next_node
            # Key not in mapping — try next edge.
        elif isinstance(edge, WaitEdge):
            # Adapter so dispatch_wait_signal can access ctx.flow
            # (it expects .flow, while Ctx stores it as ._flow).
            class _CtxAdapter:
                def __init__(self, c: Ctx) -> None:
                    self.run_id = c.run_id
                    self.flow = c._flow
                    self._step_counter = c._step_counter

            signal = await dispatch_wait_signal(
                _CtxAdapter(ctx),  # type: ignore[arg-type]
                edge.event_name,
                None,
                0.5,
            )
            # Merge signal payload into state so conditional edges after this
            # wait can route on signal data (e.g. wait_edge → conditional).
            payload = signal.get("payload")
            if isinstance(payload, dict):
                state.update(payload)
            return edge.to_node

    raise GraphStuckError(
        f"graph {defn.name!r}: node {from_node!r} has no matching outgoing edge (state keys: {list(state.keys())})"
    )


async def execute_graph_run(
    flow: Flow,
    run_id: UUID,
    defn: WorkflowDefinition,
    run_info: RunInfo,
) -> None:
    """Drive a workflow run through its graph definition.

    Called by execute_run when the registered entry is a WorkflowDefinition.
    Loops: current_node = _route(defn, current_node, state, ctx) until END.
    After each node: persists accumulated state to run.output (crash recovery).
    Exception handling mirrors execute_run's try/except block.
    """

    # Always start from run.input (initial state). Cache hits in _dispatch_node
    # rebuild the intermediate state correctly by replaying completed node outputs
    # in ordinal order — deterministic and replay-safe. run.output is written
    # after each node for observability but is NOT used as the resume point,
    # because starting from a merged state would produce different args_hashes
    # and break step cache lookups.
    current_state = dict(run_info.input or {})

    ctx = Ctx(
        run_id=run_id,
        workflow_name=defn.name,
        workflow_version=defn.version,
        namespace=defn.namespace,
        node_name="",
        attempt=0,
        metadata=dict(run_info.metadata),
        _flow=flow,
    )

    token = _current_ctx.set(ctx)  # type: ignore[arg-type]

    try:
        await _runs.emit_run_event(flow, run_id, "run_started", {})

        # Find the starting node (replay-safe: already-completed nodes cache-hit).
        current_node = defn.entry_node()

        while current_node != END:
            node_def = defn.nodes.get(current_node)
            if node_def is None:
                raise ValueError(f"graph {defn.name!r}: node {current_node!r} not found in definition")

            partial_update = await _dispatch_node(flow, ctx, node_def, current_state)

            current_state = apply_state_update(current_state, partial_update, defn.reducers)

            # Persist rolling state immediately (crash recovery).
            await _runs.update_run_output(flow, run_id, current_state)

            current_node = await _route(defn, current_node, current_state, ctx)

        await _runs.update_run_status(flow, run_id, status="completed", output=current_state)
        await _runs.emit_run_event(flow, run_id, "run_completed", {})

        for ext in flow.extensions:
            hook = getattr(ext, "after_complete", None)
            if hook is not None:
                try:
                    await hook(run_info, current_state)
                except Exception:
                    logger.debug("extension after_complete raised", exc_info=True)

    except _StepRetryRequested as e:
        await _runs.update_run_status(flow, run_id, status="pending")
        await _runs.emit_run_event(flow, run_id, "run_pending_retry", {"reason": str(e)})

    except WorkflowCancelled as e:
        await _runs.update_run_status(flow, run_id, status="cancelled", cancel_reason=str(e))
        await _runs.emit_run_event(flow, run_id, "run_cancelled", {"reason": str(e)})
        _call_extensions(flow, run_info, e, "after_fail")

    except StepFailed as e:
        await _runs.update_run_status(flow, run_id, status="failed", error=str(e))
        await _runs.emit_run_event(flow, run_id, "run_failed", {"error": str(e)})
        _call_extensions(flow, run_info, e, "after_fail")

    except Exception as e:
        tb = traceback.format_exc()
        await _runs.update_run_status(flow, run_id, status="failed", error=tb)
        await _runs.emit_run_event(flow, run_id, "run_failed", {"error": str(e)})
        _call_extensions(flow, run_info, e, "after_fail")

    finally:
        _current_ctx.reset(token)


def _call_extensions(flow: Flow, run_info: RunInfo, exc: Exception, hook_name: str) -> None:
    """Call extension hooks without blocking. Fire-and-forget pattern."""
    import asyncio as _asyncio

    for ext in flow.extensions:
        hook = getattr(ext, hook_name, None)
        if hook is not None:
            try:
                coro = hook(run_info, exc)
                if _asyncio.iscoroutine(coro):
                    _asyncio.get_event_loop().create_task(coro)
            except Exception:
                logger.debug("extension %s raised", hook_name, exc_info=True)
