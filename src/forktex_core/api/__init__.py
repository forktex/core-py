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

"""Level-3 ``[api]`` extra — FastAPI factory for forktex services.

Boilerplate-elimination: ``create_app(AppConfig)`` returns a preconfigured
FastAPI instance with the cross-cutting concerns every consumer wires by
hand today — all **opt-in/opt-out** via ``AppConfig``:
  - trace-id propagation (``forktex_core.log.TraceIDMiddleware`` — one
    ``X-Request-ID`` header, correlated with logs and the envelope);
  - security headers;
  - ``AppError`` → ``ErrorEnvelope`` mapping (+ optional catch-all for
    unexpected errors);
  - CORS (only when ``cors_origins`` is set);
  - ASGI ``lifespan`` passthrough;
  - ``/health`` liveness + ``/health/ready`` readiness probes.

What this extra deliberately leaves to the consumer: rate limiting /
idempotency, auth (tokens/RBAC), and a per-request DB session — all
service-specific. An API-key-only service simply leaves ``cors_origins``
unset and gets no CORS.

Mandatory deps: ``[log]`` + ``[error]`` (level 0; ``[types]`` transitively
via the envelope's ``BaseAppModel``). FastAPI is the one new dep — declared
as ``forktex-core[api]`` in ``pyproject.toml`` so consumers opt in.
"""

from forktex_core.api.factory import (
    AppConfig,
    HealthProbe,
    LivenessResponse,
    ReadinessResponse,
    create_app,
)
from forktex_core.api.middleware import SecurityHeadersMiddleware

__all__ = [
    "AppConfig",
    "HealthProbe",
    "LivenessResponse",
    "ReadinessResponse",
    "SecurityHeadersMiddleware",
    "create_app",
]
