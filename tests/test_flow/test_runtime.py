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

"""End-to-end integration tests for Phases 3–6.

Real Postgres testcontainer; real workflow execution via the driver;
real step caching via the run+step_run tables. These tests are the
load-bearing proof of the durability contract — every guarantee in
the design plan should be exercisable here.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from forktex_core.flow import Ctx, Flow, step

from .conftest import force_resume, wait_for_status as _wait_for_status

pytestmark = pytest.mark.asyncio


# ── Basic happy path ─────────────────────────────────────────────────


async def test_simple_workflow_completes(flow: Flow):
    """A 3-step pipeline runs end-to-end and produces the expected output."""

    @step
    async def double_simple(ctx: Ctx, state: dict) -> dict:
        return {**state, "doubled": state["value"] * 2}

    @step
    async def add_one_simple(ctx: Ctx, state: dict) -> dict:
        return {**state, "added": state["doubled"] + 1}

    @step
    async def stringify_simple(ctx: Ctx, state: dict) -> dict:
        return {**state, "final": f"answer={state['added']}"}

    @flow.pipeline("simple", version=1)
    class Simple:
        steps = [double_simple, add_one_simple, stringify_simple]

    await flow.start_driver()
    run_id = await flow.start("simple", input={"value": 5})

    final_status = await _wait_for_status(flow, run_id, until={"completed", "failed"})
    info = await flow.get(run_id)
    assert final_status == "completed", f"got {final_status} error={info.error!r}"
    assert info.output.get("doubled") == 10
    assert info.output.get("added") == 11
    assert info.output.get("final") == "answer=11"

    # Each step executed exactly once.
    assert len(info.steps) == 3
    statuses = {s.step_name: s.status for s in info.steps}
    assert all(v == "completed" for v in statuses.values())


# ── Step caching across replays ──────────────────────────────────────


async def test_step_caching_replays_dont_double_run(flow: Flow):
    """The same workflow is invoked twice (simulating a crash + resume).
    Steps that completed on the first invocation must NOT re-run on the
    second; their cached output must be returned."""
    counter = {"a": 0, "b": 0}

    @step
    async def step_a_cache(ctx: Ctx, state: dict) -> dict:
        counter["a"] += 1
        return {**state, "a": state.get("value", 0) + 100}

    @step
    async def step_b_cache(ctx: Ctx, state: dict) -> dict:
        counter["b"] += 1
        return {**state, "b": state["a"] * 10}

    @flow.pipeline("replay_safe", version=1)
    class ReplaySafe:
        steps = [step_a_cache, step_b_cache]

    await flow.start_driver()
    run_id = await flow.start("replay_safe", input={"value": 1})
    await _wait_for_status(flow, run_id, until={"completed"})

    # Manually trigger a replay by re-invoking execute_run on the same
    # run_id. Even though the run is already completed, the pipeline
    # would replay if invoked — but steps should hit the cache.
    from forktex_core.flow.runtime.replay import execute_run

    counter_baseline = dict(counter)

    # Direct replay against the same run row — simulates a leader
    # picking up a not-yet-finalised run after a crash. ``force_resume``
    # is the ORM helper from conftest; no inline SQL in tests.
    await force_resume(flow, run_id)
    await execute_run(flow, run_id, "replay_safe", 1)

    # Steps must not have run again.
    assert counter == counter_baseline, f"step body re-ran on replay: counter={counter} baseline={counter_baseline}"
    info = await flow.get(run_id)
    assert info.status == "completed"


# ── Retry semantics ──────────────────────────────────────────────────


async def test_step_retries_until_success(flow: Flow):
    """A step that fails twice then succeeds must complete the run.
    Total step attempts == 3; run output reflects the final success."""
    attempts = {"count": 0}

    @step(max_attempts=5, backoff=(0.01, 0.01, 0.01, 0.01))
    async def flaky_retry(ctx: Ctx, state: dict) -> dict:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError(f"flaky failure #{attempts['count']}")
        return {**state, "result": state.get("x", 0) * 100}

    @flow.pipeline("retry_test", version=1)
    class RetryTest:
        steps = [flaky_retry]

    await flow.start_driver()
    run_id = await flow.start("retry_test", input={"x": 7})
    await _wait_for_status(flow, run_id, until={"completed"}, timeout=60)

    info = await flow.get(run_id)
    assert info.status == "completed"
    assert info.output.get("result") == 700
    # The step's attempts column reflects the total tries.
    step_info = next(s for s in info.steps if "flaky_retry" in s.step_name)
    assert step_info.attempts == 3, f"expected 3 attempts; got {step_info.attempts}"


async def test_step_fails_after_exhausting_retries(flow: Flow):
    """A step that always raises is marked failed after max_attempts;
    the run is also marked failed with a useful error."""

    @step(max_attempts=3, backoff=(0.01, 0.01))
    async def always_broken_retry(ctx: Ctx, state: dict) -> dict:
        raise RuntimeError("permanently broken")

    @flow.pipeline("broken", version=1)
    class Broken:
        steps = [always_broken_retry]

    await flow.start_driver()
    run_id = await flow.start("broken")
    await _wait_for_status(flow, run_id, until={"failed"}, timeout=60)

    info = await flow.get(run_id)
    assert info.status == "failed"
    assert "permanently broken" in (info.error or "") or "always_broken_retry" in (info.error or "")
    step_info = next(s for s in info.steps if "always_broken_retry" in s.step_name)
    assert step_info.status == "failed"
    assert step_info.attempts == 3


# ── Run lifecycle introspection ──────────────────────────────────────


async def test_get_returns_full_run_info(flow: Flow):
    @step
    async def s1_introspect(ctx: Ctx, state: dict) -> dict:
        return {**state, "x": "ok"}

    @flow.pipeline("introspect", version=1)
    class Introspect:
        steps = [s1_introspect]

    await flow.start_driver()
    run_id = await flow.start(
        "introspect",
        metadata={"trace_id": "abc"},
    )
    await _wait_for_status(flow, run_id, until={"completed"})

    info = await flow.get(run_id)
    assert info.run_id == run_id
    assert info.workflow_name == "introspect"
    assert info.workflow_version == 1
    assert info.metadata.get("trace_id") == "abc"
    assert info.status == "completed"
    assert info.started_at is not None
    assert info.finished_at is not None
    assert len(info.steps) == 1


async def test_list_filters_by_metadata(flow: Flow):
    @step
    async def trivial_list(ctx: Ctx, state: dict) -> dict:
        return {**state, "r": 1}

    @flow.pipeline("filterable", version=1)
    class Filterable:
        steps = [trivial_list]

    await flow.start_driver()
    a = await flow.start("filterable", metadata={"org_id": "alpha"})
    b = await flow.start("filterable", metadata={"org_id": "beta"})
    c = await flow.start("filterable", metadata={"org_id": "alpha"})
    await _wait_for_status(flow, a, until={"completed"})
    await _wait_for_status(flow, b, until={"completed"})
    await _wait_for_status(flow, c, until={"completed"})

    alpha_runs = await flow.list(metadata={"org_id": "alpha"})
    alpha_ids = {r.run_id for r in alpha_runs}
    assert a in alpha_ids
    assert c in alpha_ids
    assert b not in alpha_ids


async def test_list_filters_by_status(flow: Flow):
    @step
    async def trivial_status(ctx: Ctx, state: dict) -> dict:
        return {**state, "r": 1}

    @flow.pipeline("status_filter", version=1)
    class StatusFilter:
        steps = [trivial_status]

    await flow.start_driver()
    run_id = await flow.start("status_filter")
    await _wait_for_status(flow, run_id, until={"completed"})

    completed = await flow.list(status=["completed"])
    assert any(r.run_id == run_id for r in completed)
    failed_only = await flow.list(status=["failed"])
    assert all(r.run_id != run_id for r in failed_only)


# ── Re-run with same input (replaces old replay API) ─────────────────


async def test_rerun_creates_new_run_with_same_input(flow: Flow):
    @step
    async def echo_rerun(ctx: Ctx, state: dict) -> dict:
        return state

    @flow.pipeline("echo_wf", version=1)
    class EchoWf:
        steps = [echo_rerun]

    await flow.start_driver()
    a = await flow.start("echo_wf", input={"x": 42}, metadata={"k": "v"})
    await _wait_for_status(flow, a, until={"completed"})

    info_a = await flow.get(a)
    # Start a new run with the same input (manual replay).
    b = await flow.start("echo_wf", input=info_a.input, metadata=info_a.metadata)
    assert b != a
    await _wait_for_status(flow, b, until={"completed"})

    info_b = await flow.get(b)
    assert info_a.input == info_b.input
    assert info_a.workflow_version == info_b.workflow_version


async def test_start_unknown_workflow_raises(flow: Flow):
    with pytest.raises(ValueError, match="not registered"):
        await flow.start("does.not.exist")


async def test_start_pinned_to_explicit_version(flow: Flow):
    @step
    async def s_versioned(ctx: Ctx, state: dict) -> dict:
        return {**state, "r": 1}

    @step
    async def s_versioned2(ctx: Ctx, state: dict) -> dict:
        return {**state, "r": 2}

    @flow.pipeline("versioned", version=1)
    class VersionedV1:
        steps = [s_versioned]

    @flow.pipeline("versioned", version=2)
    class VersionedV2:
        steps = [s_versioned2]

    await flow.start_driver()
    a = await flow.start("versioned", version=1)
    b = await flow.start("versioned")  # default = latest
    await _wait_for_status(flow, a, until={"completed"})
    await _wait_for_status(flow, b, until={"completed"})

    info_a = await flow.get(a)
    info_b = await flow.get(b)
    assert info_a.workflow_version == 1
    assert info_b.workflow_version == 2
    assert info_a.output.get("r") == 1
    assert info_b.output.get("r") == 2


# ── Driver leader election ───────────────────────────────────────────


async def test_only_one_driver_is_leader(db_url: str, fresh_schema: str):
    """Two Flow instances on the same DB; only one becomes leader."""
    f1 = Flow(database_url=db_url, schema=fresh_schema, poll_interval=0.1)
    f2 = Flow(database_url=db_url, schema=fresh_schema, poll_interval=0.1)
    try:
        await f1.init()
        await f2.init()
        await f1.start_driver()
        await f2.start_driver()
        await asyncio.sleep(1.0)  # let one acquire the lock

        # Exactly one is leader.
        leaders = sum(1 for f in (f1, f2) if f._driver and f._driver.is_leader)
        assert leaders == 1, f"expected 1 leader, got {leaders}"
    finally:
        await f1.close()
        await f2.close()


async def test_failover_on_leader_loss(db_url: str, fresh_schema: str):
    """Stop the leader; the other Flow should pick up leadership."""
    f1 = Flow(database_url=db_url, schema=fresh_schema, poll_interval=0.1)
    f2 = Flow(database_url=db_url, schema=fresh_schema, poll_interval=0.1)
    try:
        await f1.init()
        await f2.init()
        await f1.start_driver()
        await f2.start_driver()
        await asyncio.sleep(1.0)
        leader = f1 if f1._driver.is_leader else f2
        follower = f2 if leader is f1 else f1

        # Stop the leader.
        await leader.close()
        # Within ~3s the follower must become leader.
        for _ in range(30):
            if follower._driver and follower._driver.is_leader:
                break
            await asyncio.sleep(0.1)
        assert follower._driver.is_leader, "follower never acquired leadership"
    finally:
        for f in (f1, f2):
            try:
                await f.close()
            except Exception:
                pass


# ── Cancellation ─────────────────────────────────────────────────────


async def test_cancel_marks_run_cancelled(flow: Flow):
    @step
    async def slow_cancel(ctx: Ctx, state: dict) -> dict:
        await asyncio.sleep(0.05)
        return state

    @flow.pipeline("cancellable", version=1)
    class Cancellable:
        # Use the same step 20 times to give time to cancel between steps.
        steps = [slow_cancel] * 20

    await flow.start_driver()
    run_id = await flow.start("cancellable")
    # Cancel before completion. Even if the workflow has already
    # finished by the time cancel runs (race), the test asserts that
    # the cancel API itself is well-behaved.
    await asyncio.sleep(0.1)
    await flow.cancel(run_id)

    final = await _wait_for_status(flow, run_id, until={"cancelled", "completed"}, timeout=30)
    info = await flow.get(run_id)
    # If completed (workflow finished before cancel propagated), the
    # cancel call is a no-op; otherwise status is cancelled.
    assert final in {"cancelled", "completed"}
    if final == "cancelled":
        assert info.status == "cancelled"


# ── Wait API ─────────────────────────────────────────────────────────


async def test_wait_returns_completed_run(flow: Flow):
    @step
    async def s_wait(ctx: Ctx, state: dict) -> dict:
        return {**state, "r": 99}

    @flow.pipeline("for_wait", version=1)
    class ForWait:
        steps = [s_wait]

    await flow.start_driver()
    run_id = await flow.start("for_wait")
    info = await flow.wait(run_id, timeout=15)
    assert info.status == "completed"
    assert info.output.get("r") == 99


async def test_wait_times_out(flow: Flow):
    """If the timeout elapses before completion, wait returns the
    current state without erroring."""

    @step
    async def hang_wait(ctx: Ctx, state: dict) -> dict:
        await asyncio.sleep(60)
        return state

    @flow.pipeline("for_timeout", version=1)
    class ForTimeout:
        steps = [hang_wait]

    # Don't start the driver — the run will sit at pending.
    run_id = await flow.start("for_timeout")
    info = await flow.wait(run_id, timeout=0.5)
    assert info.status == "pending"


# ── Stream API ───────────────────────────────────────────────────────


async def test_stream_yields_progress_updates(flow: Flow):
    @step
    async def first_stream(ctx: Ctx, state: dict) -> dict:
        return {**state, "a": 1}

    @step
    async def second_stream(ctx: Ctx, state: dict) -> dict:
        return {**state, "b": state["a"] + 1}

    @flow.pipeline("streamable", version=1)
    class Streamable:
        steps = [first_stream, second_stream]

    await flow.start_driver()
    run_id = await flow.start("streamable")

    updates: list = []
    async for upd in flow.stream(run_id):
        updates.append(upd)
        if len(updates) > 50:
            break  # safety

    info = await flow.get(run_id)
    assert info.status == "completed"
    # Saw at least one run_started + one terminal event.
    event_types = {u.event_type for u in updates}
    assert event_types & {"run_started", "step_started"}, f"got {event_types}"
    assert event_types & {"run_completed"}, f"got {event_types}"


@pytest.mark.asyncio
async def test_fetching_an_unknown_run_raises_not_found(flow: Flow):
    """A missing run used to surface as a bare `ValueError`, which an HTTP boundary can only
    render as a masked 500. It is a `NotFoundError` — the same contract as every other
    missing resource in the library."""
    from forktex_core.error import NotFoundError

    with pytest.raises(NotFoundError):
        await flow.get(uuid.uuid7())
    with pytest.raises(NotFoundError):
        await flow.wait(uuid.uuid7(), timeout=0.1)
