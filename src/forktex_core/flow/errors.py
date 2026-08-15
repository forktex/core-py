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

"""Exception hierarchy for forktex_core.flow.

Every class here is an ``AppError`` subclass, so a transport that already
knows how to render an ``AppError`` — notably
``forktex_core.api.ExceptionEnvelopeMiddleware`` — renders these too, with
the right status instead of a masked 500. Each leaf sets the ``code`` that
carries its meaning across the wire; ``AppError.__init__(message, *,
details=None)`` is inherited unchanged.
"""

from __future__ import annotations

from forktex_core.error import AppError, AppErrorCode


class FlowError(AppError):
    """Base for any error originating from the flow library itself
    (registration, schema, driver, replay). Distinct from errors raised
    by user step code, which the library captures as ``StepFailed``."""

    code = AppErrorCode.INTERNAL


class StepFailed(FlowError):
    """A step's body raised an exception that exhausted retries.

    The original cause is chained via ``__cause__``. The step's row in
    ``forktex_flow.step_run`` carries the truncated traceback and the
    final attempt count.

    ``FAILED`` rather than ``INTERNAL``: the workflow failing is a real
    outcome the caller may need to distinguish from the server breaking.
    """

    code = AppErrorCode.FAILED


class WorkflowFailed(FlowError):
    """A workflow run reached terminal failed status (typically because a
    step inside it raised ``StepFailed``). Surfaced from ``flow.wait``
    when callers want a synchronous result."""

    code = AppErrorCode.FAILED


class WorkflowCancelled(FlowError):
    """A workflow run was cancelled — either via ``ctx.cancel()`` from
    inside the workflow or via ``flow.cancel(run_id)`` from outside."""

    code = AppErrorCode.CANCELLED


class GraphStuckError(FlowError):
    """A ``Graph`` instance reached a non-terminal state with no
    matching outgoing transition. Surface in operator logs so the
    blueprint can be fixed; the run is marked ``failed``."""

    code = AppErrorCode.FAILED


class SignalTimeout(FlowError):
    """``ctx.wait_signal(name, timeout=...)`` exceeded its timeout
    without receiving a matching signal."""

    code = AppErrorCode.TIMEOUT


__all__ = [
    "FlowError",
    "GraphStuckError",
    "SignalTimeout",
    "StepFailed",
    "WorkflowCancelled",
    "WorkflowFailed",
]
