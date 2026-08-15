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

"""STORY: a durable workflow reads from a Grid, mutates a row, completes.

Cross-module story for ``[flow]`` + ``[grid]``. Real Postgres
(testcontainer). The two substrates share one engine but live in
separate Postgres schemas — ``forktex_flow`` for the workflow tables,
a per-test ``ftf_story_<hex>`` for the Grid tables.

  Act 1. Stand up Postgres, init a ``Flow``, declare a ``leads`` Grid
         and seed one row (status="new").
  Act 2. Register a one-step pipeline that reads the Grid row by id,
         flips ``status`` to ``"qualified"``, and returns the new
         payload as the workflow output.
  Act 3. Start the driver, kick off a run with ``input={"lead_id": …}``,
         wait for completion.
  Act 4. Confirm the Grid row reflects the workflow's mutation +
         the workflow's output dict carries the new status.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from forktex_core.flow import Ctx, Flow, step
from forktex_core.grid import FieldType, Grid, TableSpec, apply_migrations

_GRID_SCHEMA_PREFIX = "forktex_grid"


async def _declare(session, *, namespace, slug, label, columns, is_system=False):
    return await Grid.declare(
        session,
        TableSpec.from_dicts(slug=slug, label=label, namespace=namespace, is_system=is_system, columns=columns),
    )


class FGState(BaseModel):
    """In-flight state across the four acts."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    flow: Flow | None = None
    flow_schema: str = ""
    grid_schema: str = ""
    engine: Any = None
    namespace: str = ""
    leads: Grid | None = None
    lead_id: UUID | None = None
    run_id: UUID | None = None


# Module-level state so the workflow body can read it. arq + flow both
# resolve callables by import path, so closures over fixture state are
# awkward — we use a module-global container instead.
_STATE: FGState = FGState()


@step
async def _qualify_lead(ctx: Ctx, state: dict[str, Any]) -> dict[str, Any]:
    """Read the Grid row by id; flip status; persist via Grid.patch."""
    lead_id = UUID(state["lead_id"])
    assert _STATE.leads is not None, "fixture must populate _STATE.leads"

    async with _STATE.engine.connect() as _:  # warm a session-less connection
        pass

    maker = async_sessionmaker(bind=_STATE.engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        # Rebind the Grid to this session — Grid is a thin facade and
        # accepts any AsyncSession that points at the same schema.
        leads = await Grid.open(session, slug=_STATE.leads.slug, namespace=_STATE.namespace)
        row = await leads.get(lead_id)
        assert row.values["status"] == "new", f"expected status=new, got {row.values['status']}"
        patched = await leads.patch(lead_id, {"status": "qualified"})
        await session.commit()

    return {**state, "new_status": patched.values["status"]}


@pytest.mark.asyncio(loop_scope="class")
class TestFlowGridSync:
    """Flow + Grid integration as one consumer journey."""

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def state(self, postgres_url) -> FGState:
        db_url = postgres_url.render_as_string(hide_password=False)

        # Two schemas: one for flow, one for grid.
        flow_schema = f"story_fg_flow_{uuid.uuid4().hex[:6]}"
        grid_schema = f"story_fg_grid_{uuid.uuid4().hex[:6]}"

        # Bring up Grid schema + tables.
        engine = create_async_engine(
            db_url,
            execution_options={"schema_translate_map": {_GRID_SCHEMA_PREFIX: grid_schema}},
        )
        await apply_migrations(engine, schema=grid_schema)

        # Bring up Flow + its schema via Flow.init().
        flow = Flow(database_url=db_url, schema=flow_schema)
        await flow.init()

        _STATE.flow = flow
        _STATE.flow_schema = flow_schema
        _STATE.grid_schema = grid_schema
        _STATE.engine = engine
        _STATE.namespace = str(uuid.uuid4())

        yield _STATE

        try:
            await flow.close()
        except Exception:
            pass
        try:
            await engine.dispose()
        except Exception:
            pass

    # ── Act 1 ────────────────────────────────────────────────────────

    async def test_act1_declare_grid_and_seed_lead(self, state: FGState):
        assert state.engine is not None
        maker = async_sessionmaker(bind=state.engine, expire_on_commit=False, class_=AsyncSession)
        async with maker() as session:
            leads = await _declare(
                session,
                namespace=state.namespace,
                slug="leads",
                label="Leads",
                columns=[
                    {"key": "title", "label": "Title", "type_id": FieldType.text.value},
                    {
                        "key": "status",
                        "label": "Status",
                        "type_id": FieldType.enum.value,
                        "config": {"options": ["new", "qualified", "won", "lost"]},
                    },
                ],
            )
            lead = await leads.create({"title": "ACME Corp", "status": "new"})
            await session.commit()

        state.leads = leads
        state.lead_id = lead.id
        assert lead.values["status"] == "new"

    # ── Act 2 ────────────────────────────────────────────────────────

    async def test_act2_register_qualify_pipeline(self, state: FGState):
        assert state.flow is not None

        @state.flow.pipeline("story.qualify_lead", version=1)
        class QualifyLead:
            steps = [_qualify_lead]

        # The pipeline is now in the in-process registry — act 3 will
        # actually exercise it. Just verify the @pipeline decorator
        # didn't raise, which means registration succeeded.
        assert hasattr(state.flow, "_registry") and state.flow._registry is not None

    # ── Act 3 ────────────────────────────────────────────────────────

    async def test_act3_run_pipeline_to_completion(self, state: FGState):
        assert state.flow is not None
        assert state.lead_id is not None

        await state.flow.start_driver()
        run_id = await state.flow.start(
            "story.qualify_lead",
            input={"lead_id": str(state.lead_id)},
        )
        state.run_id = run_id

        # Poll for completion.
        deadline = time.monotonic() + 30.0
        final_status: str | None = None
        while time.monotonic() < deadline:
            info = await state.flow.get(run_id)
            if info.status in ("completed", "failed"):
                final_status = info.status
                break
            await asyncio.sleep(0.2)
        assert final_status == "completed", f"run {run_id} did not complete in 30s — last status was {final_status!r}"

    # ── Act 4 ────────────────────────────────────────────────────────

    async def test_act4_grid_row_reflects_workflow_mutation(self, state: FGState):
        assert state.flow is not None
        assert state.run_id is not None
        assert state.leads is not None
        assert state.lead_id is not None

        # 1. The workflow output carries the new status.
        info = await state.flow.get(state.run_id)
        assert info.output.get("new_status") == "qualified", (
            f"workflow output missing new_status=qualified: {info.output}"
        )

        # 2. The Grid row was actually mutated on disk.
        maker = async_sessionmaker(bind=state.engine, expire_on_commit=False, class_=AsyncSession)
        async with maker() as session:
            leads = await Grid.open(session, slug=state.leads.slug, namespace=state.namespace)
            row = await leads.get(state.lead_id)
            assert row.values["status"] == "qualified"
            assert row.values["title"] == "ACME Corp"

        # 3. Stop the driver so the test class teardown is clean.
        await state.flow.stop_driver()
