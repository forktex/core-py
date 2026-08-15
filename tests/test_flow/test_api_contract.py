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

"""The never-break guardrail for `flow`'s public surface — grid's contract, applied here.

`grid` has had a frozen-API test since 4.0; `flow` had none, so its surface could
drift silently. This closes that asymmetry, and pins the two conventions the
substrate packages now share:

- no leading-underscore names in `__all__` (`_ParallelGroup` / `_PipelineStepSpec`
  were exported as public API while spelled private; they are `ParallelGroup` and
  `StepSpec` now);
- one `apply_migrations(engine, schema=..., ...)` per substrate, exported from the
  package root, so both are brought up the same way.
"""

from __future__ import annotations

import inspect

import forktex_core.flow as flow

FROZEN_PUBLIC_API: frozenset[str] = frozenset(
    {
        # entry point
        "Flow",
        "Ctx",
        # authoring — pipeline + graph
        "step",
        "node",
        "parallel",
        "edge",
        "conditional",
        "wait_edge",
        "START",
        "END",
        "StepSpec",
        "ParallelGroup",
        # declarative definition (namespace track)
        "WorkflowDefinition",
        "NodeDef",
        "StepTemplateDef",
        "DirectEdge",
        "ConditionalEdge",
        "WaitEdge",
        # query API
        "InstanceQuery",
        "InstancePage",
        "InstanceSummary",
        "WorkflowInstance",
        "NodeInstance",
        # extension seam
        "FlowExtension",
        "ColumnDef",
        # errors
        "FlowError",
        "StepFailed",
        "WorkflowFailed",
        "WorkflowCancelled",
        "SignalTimeout",
        "GraphStuckError",
        # row shapes (still used by cloud's flow_api routes)
        "RunInfo",
        "RunUpdate",
        "StepRunInfo",
        # infra
        "apply_migrations",
        # the workflow version-drift check, as a library function (was a CLI)
        "audit_workflows",
        "AuditReport",
    }
)


def test_public_api_is_frozen() -> None:
    assert set(flow.__all__) == FROZEN_PUBLIC_API


def test_all_names_are_importable() -> None:
    for name in flow.__all__:
        assert hasattr(flow, name), f"{name} is in __all__ but not importable"


def test_no_private_names_are_exported() -> None:
    """A name in `__all__` is public by definition, so spelling it with a leading
    underscore tells the reader the opposite of the truth."""
    private = sorted(n for n in flow.__all__ if n.startswith("_"))
    assert private == [], f"private-looking names exported as public API: {private}"


def test_migrations_entry_point_matches_grid() -> None:
    """`flow` and `grid` are brought up the same way — same name, same first two
    parameters, same defaulting — so a consumer's alembic hook can drive either."""
    from forktex_core.grid import apply_migrations as grid_apply

    flow_sig = inspect.signature(flow.apply_migrations)
    grid_sig = inspect.signature(grid_apply)

    assert list(flow_sig.parameters)[:2] == list(grid_sig.parameters)[:2] == ["engine", "schema"]
    assert flow_sig.parameters["schema"].default == "forktex_flow"
    assert grid_sig.parameters["schema"].default == "forktex_grid"
    # Every parameter beyond the shared two must be optional, or the shapes are
    # not interchangeable from a caller's point of view.
    for sig in (flow_sig, grid_sig):
        for name, param in list(sig.parameters.items())[2:]:
            assert param.default is not inspect.Parameter.empty, f"{name} has no default"


def test_both_substrates_run_on_the_shared_migration_runner() -> None:
    """Neither package hand-rolls migration mechanics: the advisory lock, the
    version tracking and the file ordering all live in `database.migrate`."""
    from forktex_core.flow.persist.migrations import _runner as flow_runner
    from forktex_core.grid.persist import migrations as grid_migrations

    for module in (flow_runner, grid_migrations):
        assert "SchemaMigrationRunner(" in inspect.getsource(module)


def test_both_substrates_own_their_declarative_registry() -> None:
    """`substrate_base` per schema, so a consumer's `BaseDBModel.metadata.create_all()`
    never tries to build either substrate."""
    from forktex_core.database.models import BaseDBModel
    from forktex_core.flow.persist.models import Run
    from forktex_core.grid.persist.models import GridRow

    for model, schema in ((Run, "forktex_flow"), (GridRow, "forktex_grid")):
        assert model.__table__.schema == schema
        assert model.metadata is not BaseDBModel.metadata
