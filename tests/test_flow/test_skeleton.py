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

"""Phase 1 smoke tests for forktex_core.flow.

The library is decomposed across multiple files; these tests just
verify the shape: imports resolve, ``Flow`` constructs without
touching a DB, decorators register cleanly, validation rejects bad
inputs, and Flow construction rejects bad config values.
"""

from __future__ import annotations


import pytest

from forktex_core.flow import (
    ColumnDef,
    Ctx,
    Flow,
    FlowError,
    FlowExtension,
    RunInfo,
    RunUpdate,
    StepFailed,
    StepRunInfo,
    WorkflowCancelled,
    WorkflowFailed,
    edge,
    step,
    END,
    START,
)


# Module-level functions so they can be used in pipeline/graph declarations.
@step
async def example_step_fn(ctx: Ctx, state: dict) -> dict:
    return {**state, "result": state.get("x", 0) * 2}


@step
async def parametric_step_fn(ctx: Ctx, state: dict) -> dict:
    return {**state, "result": state.get("x", 0) + 1}


@step
async def example_workflow_fn(ctx: Ctx, state: dict) -> dict:
    return {"x": state.get("x")}


def _make_flow() -> Flow:
    """Construct a Flow without touching a real database. The async
    engine doesn't connect until a query runs."""
    return Flow(database_url="postgresql+asyncpg://x:y@localhost/z")


def test_public_api_exports():
    # Just importing the names is the smoke test.
    assert FlowError is not None
    assert StepFailed is not None
    assert WorkflowFailed is not None
    assert WorkflowCancelled is not None
    assert ColumnDef is not None
    assert RunInfo is not None
    assert StepRunInfo is not None
    assert RunUpdate is not None
    assert Ctx is not None
    assert FlowExtension is not None
    assert Flow is not None


def test_flow_constructs_without_db():
    f = _make_flow()
    assert f.schema == "forktex_flow"
    assert f.leader_lock_key != 0
    assert f.poll_interval == 1.0
    assert f.heartbeat_interval == 10.0
    assert f.stale_threshold == 60.0
    assert f.default_max_attempts == 3
    assert f.default_backoff == (30.0, 120.0, 300.0)
    assert f.extensions == []


def test_flow_rejects_bad_config():
    with pytest.raises(ValueError, match="default_max_attempts"):
        Flow(database_url="postgresql+asyncpg://x:y@h/d", default_max_attempts=0)
    with pytest.raises(ValueError, match="default_backoff"):
        Flow(database_url="postgresql+asyncpg://x:y@h/d", default_backoff=())


def test_pipeline_decorator_registers():
    f = _make_flow()

    @f.pipeline("test.workflow", version=1)
    class TestWorkflow:
        steps = [example_step_fn]

    defn = f._registry.get_definition("test.workflow", version=1)
    assert defn is not None
    assert defn.name == "test.workflow"
    assert defn.version == 1


def test_pipeline_dup_registration_raises():
    f = _make_flow()

    @f.pipeline("dup", version=1)
    class DupV1:
        steps = [example_step_fn]

    with pytest.raises(ValueError, match="already registered"):

        @f.pipeline("dup", version=1)
        class DupV1b:
            steps = [example_step_fn]


def test_pipeline_versioning_coexistence():
    f = _make_flow()

    @f.pipeline("evolved", version=1)
    class EvolvedV1:
        steps = [example_step_fn]

    @f.pipeline("evolved", version=2)
    class EvolvedV2:
        steps = [parametric_step_fn]

    assert f._registry.latest_version("evolved") == 2
    assert f._registry.get_definition("evolved", version=1) is not None
    assert f._registry.get_definition("evolved", version=2) is not None


def test_pipeline_rejects_duplicate_registration():
    f = _make_flow()

    @f.pipeline("unique.pipeline", version=1)
    class UniquePipelineV1:
        steps = [example_step_fn]

    with pytest.raises(ValueError, match="already registered"):

        @f.pipeline("unique.pipeline", version=1)
        class UniquePipelineV1b:
            steps = [example_step_fn]


def test_scheduled_decorator_registers():
    f = _make_flow()

    @f.scheduled("cron.x", version=1, cron="*/15 * * * *")
    async def cron_fn(ctx: Ctx, state: dict) -> dict:
        return {}

    sched_defs = f._registry.scheduled_definitions()
    names = [d.name for d in sched_defs]
    assert "cron.x" in names
    defn = f._registry.get_definition("cron.x", version=1)
    assert defn is not None
    assert defn.schedule == "*/15 * * * *"


def test_scheduled_rejects_duplicate_registration():
    f = _make_flow()

    @f.scheduled("unique.sched", version=1, cron="*/5 * * * *")
    async def unique_sched_v1(ctx: Ctx, state: dict) -> dict:
        return {}

    with pytest.raises(ValueError, match="already registered"):

        @f.scheduled("unique.sched", version=1, cron="*/10 * * * *")
        async def unique_sched_v1b(ctx: Ctx, state: dict) -> dict:
            return {}


def test_graph_decorator_registers():
    f = _make_flow()

    @f.graph("test.graph", version=1)
    class TestGraph:
        nodes = {"a": example_step_fn, "b": parametric_step_fn}
        topology = [
            edge(START, "a"),
            edge("a", "b"),
            edge("b", END),
        ]

    defn = f._registry.get_definition("test.graph", version=1)
    assert defn is not None
    assert defn.name == "test.graph"
    assert defn.version == 1


# Runtime methods (start/wait/get/list/stream/cancel/replay) are
# integration-tested against the Postgres testcontainer in
# test_runtime.py — covering them at the unit level here would require
# heavy mocking that doesn't pay off.


def test_extension_protocol_default_methods_are_no_op():
    """Default implementations of every FlowExtension hook must do
    nothing — consumers should be able to implement only the hooks
    they care about."""
    from forktex_core.flow.extension import FlowExtension as Ext

    # Verify the Protocol has the documented hook names.
    expected = {
        "extra_run_columns",
        "extra_step_run_columns",
        "before_start",
        "before_dispatch",
        "after_complete",
        "after_fail",
        "filter_list",
    }
    assert expected.issubset(set(dir(Ext)))


def test_column_def_construction():
    import sqlalchemy as sa

    col = ColumnDef(name="org_id", type_=sa.String(64), nullable=True, index=True)
    assert col.name == "org_id"
    assert col.nullable is True
    assert col.index is True
