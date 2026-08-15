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

"""Public data types returned by ``Flow`` introspection methods.

These are ``BaseWireValueObject`` models (frozen + camelCase wire aliasing)
— consumer HTTP routes serialize them directly via ``model_dump()``; the
typed shape is the contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from forktex_core.types import BaseWireValueObject

RunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
StepStatus = Literal["pending", "running", "completed", "failed", "cancelled"]

# Statuses at which a run/step has reached a terminal state and acquires a
# ``finished_at``; used to decide when polling/streaming may stop.
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


class StepRunInfo(BaseWireValueObject):
    """One step invocation within a run."""

    step_id: UUID
    step_name: str
    step_index: int
    status: StepStatus
    output: Any | None
    error: str | None
    attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None


class RunInfo(BaseWireValueObject):
    """Full state of a workflow run, including per-step progress."""

    run_id: UUID
    workflow_name: str
    workflow_version: int
    status: RunStatus
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: str | None
    metadata: dict[str, Any]
    started_at: datetime
    finished_at: datetime | None
    steps: list[StepRunInfo] = Field(default_factory=list)


class RunUpdate(BaseWireValueObject):
    """Streaming update emitted via ``Flow.stream(run_id)`` whenever
    the run or any of its steps transitions state. Consumers feed
    these into Server-Sent Event responses for live progress UIs."""

    run_id: UUID
    timestamp: datetime
    event_type: Literal[
        "run_started",
        "step_started",
        "step_completed",
        "step_failed",
        "step_retried",
        "run_completed",
        "run_failed",
        "run_cancelled",
    ]
    payload: dict[str, Any]
