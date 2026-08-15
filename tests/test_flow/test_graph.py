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

"""End-to-end coverage for the graph/state-machine primitive.

A graph instance is a workflow run; each node visit is a step_run.
These tests exercise the graph runtime against a real Postgres
testcontainer — auto-advancing transitions, conditional routing,
manual-advance states with external signals, and replay determinism.
"""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa

from forktex_core.flow import (
    END,
    START,
    Ctx,
    Flow,
    conditional,
    edge,
    step,
    wait_edge,
)
from forktex_core.flow.persist.models import Signal

from .conftest import wait_for_status

pytestmark = pytest.mark.asyncio


# ── Linear auto-advance ──────────────────────────────────────────────


async def test_linear_graph_completes(flow: Flow):
    @step
    async def lin_a(ctx: Ctx, state: dict) -> dict:
        return {**state, "a": True}

    @step
    async def lin_b(ctx: Ctx, state: dict) -> dict:
        return {**state, "b": True}

    @step
    async def lin_c(ctx: Ctx, state: dict) -> dict:
        return {**state, "c": True}

    @flow.graph("lin", version=1)
    class LinGraph:
        nodes = {"a": lin_a, "b": lin_b, "c": lin_c}
        topology = [
            edge(START, "a"),
            edge("a", "b"),
            edge("b", "c"),
            edge("c", END),
        ]

    await flow.start_driver()
    run_id = await flow.start("lin", input={"start": True})
    await wait_for_status(flow, run_id, until={"completed"})

    info = await flow.get(run_id)
    assert info.status == "completed"
    assert info.output.get("a") is True
    assert info.output.get("b") is True
    assert info.output.get("c") is True


# ── Terminal state semantics ─────────────────────────────────────────


async def test_single_node_graph_completes(flow: Flow):
    """A graph with one node connected START → node → END completes after
    one node visit."""

    @step
    async def only_single(ctx: Ctx, state: dict) -> dict:
        return {**state, "ran": True}

    @flow.graph("single", version=1)
    class SingleGraph:
        nodes = {"only": only_single}
        topology = [
            edge(START, "only"),
            edge("only", END),
        ]

    await flow.start_driver()
    run_id = await flow.start("single", input={})
    await wait_for_status(flow, run_id, until={"completed"})

    info = await flow.get(run_id)
    assert info.output.get("ran") is True


# ── Conditional transitions ──────────────────────────────────────────


async def test_conditional_transitions_route_correctly(flow: Flow):
    @step
    async def score_branchy(ctx: Ctx, state: dict) -> dict:
        return {**state, "score": state["raw"]}

    @step
    async def approved_branchy(ctx: Ctx, state: dict) -> dict:
        return {**state, "outcome": "approved"}

    @step
    async def rejected_branchy(ctx: Ctx, state: dict) -> dict:
        return {**state, "outcome": "rejected"}

    def _route(state: dict) -> str:
        return "approved" if state.get("score", 0) >= 7 else "rejected"

    @flow.graph("branchy", version=1)
    class BranchyGraph:
        nodes = {
            "score": score_branchy,
            "approved": approved_branchy,
            "rejected": rejected_branchy,
        }
        topology = [
            edge(START, "score"),
            conditional("score", _route, {"approved": "approved", "rejected": "rejected"}),
            edge("approved", END),
            edge("rejected", END),
        ]

    await flow.start_driver()
    high = await flow.start("branchy", input={"raw": 9})
    low = await flow.start("branchy", input={"raw": 3})
    await wait_for_status(flow, high, until={"completed"})
    await wait_for_status(flow, low, until={"completed"})

    info_h = await flow.get(high)
    info_l = await flow.get(low)
    assert info_h.output["outcome"] == "approved"
    assert info_l.output["outcome"] == "rejected"


# ── Manual-advance state via signal ──────────────────────────────────


async def test_manual_state_waits_for_signal(flow: Flow):
    state_visits: dict[str, int] = {"pending": 0, "done": 0}

    @step
    async def pending_manual(ctx: Ctx, state: dict) -> dict:
        state_visits["pending"] += 1
        return {**state, "pending_ran": True}

    @step
    async def done_manual(ctx: Ctx, state: dict) -> dict:
        state_visits["done"] += 1
        return {**state, "done_ran": True}

    @flow.graph("manual_test", version=1)
    class ManualGraph:
        nodes = {"pending": pending_manual, "done": done_manual}
        topology = [
            edge(START, "pending"),
            wait_edge("pending", "done", on="advance"),
            edge("done", END),
        ]

    await flow.start_driver()
    run_id = await flow.start("manual_test", input={"start": True})

    # Run shouldn't complete on its own — manual gate (wait_edge).
    await asyncio.sleep(2.0)
    info = await flow.get(run_id)
    assert info.status not in {"completed", "failed"}, f"manual run completed without signal: status={info.status}"

    # Signal advance.
    await flow.send(run_id, event="advance", payload={"approved_by": "tester"})
    await wait_for_status(flow, run_id, until={"completed"}, timeout=15)

    info = await flow.get(run_id)
    assert info.status == "completed"
    assert info.output.get("done_ran") is True
    assert state_visits["done"] >= 1


# ── Signal payload merge ─────────────────────────────────────────────


async def test_signal_payload_merges_into_state(flow: Flow):
    """Signal payload merges over the state dict BEFORE transition
    predicates evaluate, so guards can route on signal data."""

    @step
    async def gate_payload(ctx: Ctx, state: dict) -> dict:
        return state

    @step
    async def yes_payload(ctx: Ctx, state: dict) -> dict:
        return {"chosen": "yes"}

    @step
    async def no_payload(ctx: Ctx, state: dict) -> dict:
        return {"chosen": "no"}

    def _route_payload(state: dict) -> str:
        return "yes" if state.get("decision") == "yes" else "no"

    @step
    async def route_payload(ctx: Ctx, state: dict) -> dict:
        return {}  # pass-through: routing happens via conditional edge below

    @flow.graph("payload_test", version=1)
    class PayloadGraph:
        nodes = {
            "gate": gate_payload,
            "route": route_payload,
            "yes": yes_payload,
            "no": no_payload,
        }
        topology = [
            edge(START, "gate"),
            wait_edge("gate", "route", on="advance"),  # suspends; signal payload merges into state
            conditional("route", _route_payload, {"yes": "yes", "no": "no"}),
            edge("yes", END),
            edge("no", END),
        ]

    await flow.start_driver()
    run_id = await flow.start("payload_test", input={})
    await flow.send(run_id, event="advance", payload={"decision": "yes"})
    await wait_for_status(flow, run_id, until={"completed"}, timeout=15)

    info = await flow.get(run_id)
    assert info.output["chosen"] == "yes"


# ── send persists in DB ───────────────────────────────────────────────


async def test_send_persists_in_signal_table(flow: Flow):
    @step
    async def wait_sig(ctx: Ctx, state: dict) -> dict:
        return state

    @step
    async def end_sig(ctx: Ctx, state: dict) -> dict:
        return state

    @flow.graph("sig_persist", version=1)
    class SigPersistGraph:
        nodes = {"wait": wait_sig, "end": end_sig}
        topology = [
            edge(START, "wait"),
            wait_edge("wait", "end", on="advance"),
            edge("end", END),
        ]

    await flow.start_driver()
    run_id = await flow.start("sig_persist")

    # Allow the wait node to enter; then send + verify persistence.
    await asyncio.sleep(1.0)
    sig_id = await flow.send(run_id, event="advance", payload={"k": "v"})
    assert isinstance(sig_id, int)

    async with flow.session() as session:
        rows = (await session.execute(sa.select(Signal).where(Signal.run_id == run_id))).scalars().all()
    assert len(rows) >= 1
    assert any(r.signal_name == "advance" and r.payload == {"k": "v"} for r in rows)


# ── consume marks consumed_at ────────────────────────────────────────


async def test_consume_marks_signal_consumed_at(flow: Flow):
    @step
    async def g_consume(ctx: Ctx, state: dict) -> dict:
        return state

    @step
    async def done_consume(ctx: Ctx, state: dict) -> dict:
        return state

    @flow.graph("consume_test", version=1)
    class ConsumeGraph:
        nodes = {"g": g_consume, "done": done_consume}
        topology = [
            edge(START, "g"),
            wait_edge("g", "done", on="advance"),
            edge("done", END),
        ]

    await flow.start_driver()
    run_id = await flow.start("consume_test")
    await asyncio.sleep(1.0)
    await flow.send(run_id, event="advance", payload={"x": 1})
    await wait_for_status(flow, run_id, until={"completed"}, timeout=15)

    async with flow.session() as session:
        sig = (await session.execute(sa.select(Signal).where(Signal.run_id == run_id))).scalar_one()
    assert sig.consumed_at is not None


# ── Graph runs are introspectable like normal workflow runs ─────────


async def test_graph_runs_show_in_flow_list(flow: Flow):
    @step
    async def x_listed(ctx: Ctx, state: dict) -> dict:
        return state

    @flow.graph("listed", version=1)
    class ListedGraph:
        nodes = {"x": x_listed}
        topology = [
            edge(START, "x"),
            edge("x", END),
        ]

    await flow.start_driver()
    run_id = await flow.start("listed")
    await wait_for_status(flow, run_id, until={"completed"})

    runs = await flow.list(workflow_name="listed")
    assert any(r.run_id == run_id for r in runs)


# ── Replay correctness for graphs ────────────────────────────────────


async def test_graph_replay_caches_node_outputs(flow: Flow):
    """Forcing a graph run to resume must not re-run already-completed
    node handlers — same contract as @step caching."""
    counter = {"a": 0, "b": 0}

    @step
    async def a_replay(ctx: Ctx, state: dict) -> dict:
        counter["a"] += 1
        return {**state, "a_ran": counter["a"]}

    @step
    async def b_replay(ctx: Ctx, state: dict) -> dict:
        counter["b"] += 1
        return {**state, "b_ran": counter["b"]}

    @flow.graph("replay_g", version=1)
    class ReplayGraph:
        nodes = {"a": a_replay, "b": b_replay}
        topology = [
            edge(START, "a"),
            edge("a", "b"),
            edge("b", END),
        ]

    await flow.start_driver()
    run_id = await flow.start("replay_g")
    await wait_for_status(flow, run_id, until={"completed"})
    baseline = dict(counter)

    # Force a replay — direct execute_run invocation against the
    # already-completed run (after re-flagging it running).
    from forktex_core.flow.runtime.replay import execute_run

    from .conftest import force_resume

    await force_resume(flow, run_id)
    info = await flow.get(run_id)
    await execute_run(flow, run_id, "replay_g", info.workflow_version)

    assert counter == baseline, f"node handler bodies re-ran on replay: {counter} vs {baseline}"
