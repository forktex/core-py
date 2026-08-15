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

"""End-to-end coverage for ``FlowExtension`` lifecycle hooks.

The library ships the Protocol; consumers wire their own concrete
extensions. This module verifies the runtime fires every hook at the
documented point so consumer extensions can rely on the contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from forktex_core.flow import ColumnDef, Ctx, Flow, step

from .conftest import wait_for_status

pytestmark = pytest.mark.asyncio


class _RecordingExtension:
    """Fake extension that records every hook invocation. Tests
    assert on the recording list to verify ordering + payloads."""

    def __init__(self, abort_on_dispatch: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.abort_on_dispatch = abort_on_dispatch

    def extra_run_columns(self) -> list[ColumnDef]:
        import sqlalchemy as sa

        return [ColumnDef(name="trace_id", type_=sa.String(64), nullable=True)]

    def extra_step_run_columns(self) -> list[ColumnDef]:
        return []

    async def before_start(
        self,
        name: str,
        version: int,
        input: dict[str, Any],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("before_start", {"name": name, "version": version}))
        return {"trace_id": f"trace-{name}-{version}"}

    async def before_dispatch(self, run: Any, step: Any) -> None:
        self.calls.append(("before_dispatch", {"workflow": run.workflow_name}))
        if self.abort_on_dispatch:
            raise PermissionError("aborted by test extension")

    async def after_complete(self, run: Any, output: Any) -> None:
        self.calls.append(("after_complete", {"output": output}))

    async def after_fail(self, run: Any, error: BaseException) -> None:
        self.calls.append(("after_fail", {"error_class": type(error).__name__}))


def _make_flow_with(db_url: str, schema: str, ext: _RecordingExtension) -> Flow:
    return Flow(database_url=db_url, schema=schema, extensions=[ext])


# ── Schema column injection ──────────────────────────────────────────


async def test_extension_columns_show_up_in_metadata(db_url: str, fresh_schema: str):
    """An extension's ``before_start`` returns dict gets merged into
    the run's metadata so subsequent reads via ``flow.get`` see it.
    The ``extra_run_columns`` declaration is what makes the column
    available in the schema; the ``before_start`` value populates it."""
    ext = _RecordingExtension()
    flow = _make_flow_with(db_url, fresh_schema, ext)
    try:
        await flow.init()

        @step
        async def trivial_ext(ctx: Ctx, state: dict) -> dict:
            return {**state, "r": 1}

        @flow.pipeline("ext_test", version=1)
        class ExtTest:
            steps = [trivial_ext]

        await flow.start_driver()
        run_id = await flow.start("ext_test")
        await wait_for_status(flow, run_id, until={"completed"})

        info = await flow.get(run_id)
        # before_start injected trace_id into metadata.
        assert info.metadata.get("trace_id") == "trace-ext_test-1"
    finally:
        await flow.close()


# ── before_start fires once per run ──────────────────────────────────


async def test_before_start_is_called_with_workflow_identity(db_url: str, fresh_schema: str):
    ext = _RecordingExtension()
    flow = _make_flow_with(db_url, fresh_schema, ext)
    try:
        await flow.init()

        @step
        async def trivial_bs(ctx: Ctx, state: dict) -> dict:
            return {**state, "r": 1}

        @flow.pipeline("bs_test", version=2)
        class BsTest:
            steps = [trivial_bs]

        await flow.start_driver()
        run_id = await flow.start("bs_test")
        await wait_for_status(flow, run_id, until={"completed"})

        bs_calls = [c for c in ext.calls if c[0] == "before_start"]
        assert len(bs_calls) == 1
        assert bs_calls[0][1] == {"name": "bs_test", "version": 2}
    finally:
        await flow.close()


# ── after_complete fires on success ──────────────────────────────────


async def test_after_complete_fires_on_successful_run(db_url: str, fresh_schema: str):
    ext = _RecordingExtension()
    flow = _make_flow_with(db_url, fresh_schema, ext)
    try:
        await flow.init()

        @step
        async def double_ac(ctx: Ctx, state: dict) -> dict:
            return {**state, "r": state.get("x", 0) * 2}

        @flow.pipeline("ac_test", version=1)
        class AcTest:
            steps = [double_ac]

        await flow.start_driver()
        run_id = await flow.start("ac_test", input={"x": 21})
        await wait_for_status(flow, run_id, until={"completed"})
        # Hook is fired from the run-completion path; allow a tick for it.
        import asyncio

        for _ in range(20):
            ac_calls = [c for c in ext.calls if c[0] == "after_complete"]
            if ac_calls:
                break
            await asyncio.sleep(0.1)

        assert any(c[0] == "after_complete" for c in ext.calls), f"after_complete never fired; recorded={ext.calls}"
    finally:
        await flow.close()


# ── after_fail fires on terminal failure ─────────────────────────────


async def test_after_fail_fires_on_run_failure(db_url: str, fresh_schema: str):
    ext = _RecordingExtension()
    flow = _make_flow_with(db_url, fresh_schema, ext)
    try:
        await flow.init()

        @step(max_attempts=1)
        async def boom_af(ctx: Ctx, state: dict) -> dict:
            raise RuntimeError("kaboom")

        @flow.pipeline("af_test", version=1)
        class AfTest:
            steps = [boom_af]

        await flow.start_driver()
        run_id = await flow.start("af_test")
        await wait_for_status(flow, run_id, until={"failed"}, timeout=20)
        import asyncio

        for _ in range(20):
            if any(c[0] == "after_fail" for c in ext.calls):
                break
            await asyncio.sleep(0.1)

        assert any(c[0] == "after_fail" for c in ext.calls), f"after_fail never fired; recorded={ext.calls}"
    finally:
        await flow.close()
