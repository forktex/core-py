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

"""Postgres-native durable workflow execution for the ForkTex ecosystem.

Two declaration tracks:

Platform track (code-defined, registered at startup):
    flow = Flow(database_url="postgresql+asyncpg://...")

    @flow.scheduled("cloud.backup.create", version=1, cron="0 2 * * *", state=BackupState)
    async def backup_create(ctx: Ctx, state: BackupState) -> dict: ...

    @flow.pipeline("cloud.deploy.up", version=4, state=DeployState)
    class DeployUp:
        steps = [provision, configure, health_check]

    @flow.graph("user.onboarding", version=1, state=OnboardingState)
    class UserOnboarding:
        entry = "email_pending"
        terminal = "verified"
        topology = [wait_edge("email_pending", "verified", on="email.verified")]

    @flow.step_template("network.reroute_traffic")
    async def reroute_traffic(ctx: Ctx, state: dict) -> dict: ...

Namespace track (config-defined, created at runtime):
    await flow.define(name="link_failure_response", namespace="org-abc", version=1,
                      config={"type": "pipeline", "steps": ["network.reroute_traffic"]})

Both dispatch via:
    instance = await flow.run("cloud.deploy.up", state={...}, metadata={...})
    instance = await flow.run("link_failure_response", namespace="org-abc", state={...})

Query:
    page = await flow.query().workflow("cloud.deploy.up").status("running").fetch()
"""

from forktex_core.flow.audit import AuditReport, audit_workflows
from forktex_core.flow.domain.definition import (
    END,
    START,
    ConditionalEdge,
    DirectEdge,
    NodeDef,
    StepTemplateDef,
    WaitEdge,
    WorkflowDefinition,
)
from forktex_core.flow.domain.node import ParallelGroup, StepSpec, node, parallel, step
from forktex_core.flow.domain.types import RunInfo, RunUpdate, StepRunInfo
from forktex_core.flow.errors import (
    FlowError,
    GraphStuckError,
    SignalTimeout,
    StepFailed,
    WorkflowCancelled,
    WorkflowFailed,
)
from forktex_core.flow.extension import ColumnDef, FlowExtension
from forktex_core.flow.flow import Flow
from forktex_core.flow.persist.migrations import apply_migrations
from forktex_core.flow.read.instance import (
    InstancePage,
    InstanceQuery,
    InstanceSummary,
    NodeInstance,
    WorkflowInstance,
)
from forktex_core.flow.runtime.compiler import (
    conditional,
    edge,
    wait_edge,
)
from forktex_core.flow.runtime.ctx import Ctx

__all__ = [
    "END",
    "START",
    "AuditReport",
    "ColumnDef",
    "ConditionalEdge",
    "Ctx",
    "DirectEdge",
    "Flow",
    "FlowError",
    "FlowExtension",
    "GraphStuckError",
    "InstancePage",
    "InstanceQuery",
    "InstanceSummary",
    "NodeDef",
    "NodeInstance",
    "ParallelGroup",
    # Row shapes: what the persistence layer returns and the wire carries.
    "RunInfo",
    "RunUpdate",
    "SignalTimeout",
    "StepFailed",
    "StepRunInfo",
    "StepSpec",
    "StepTemplateDef",
    "WaitEdge",
    "WorkflowCancelled",
    "WorkflowDefinition",
    "WorkflowFailed",
    "WorkflowInstance",
    "apply_migrations",
    "audit_workflows",
    "conditional",
    "edge",
    "node",
    "parallel",
    "step",
    "wait_edge",
]
