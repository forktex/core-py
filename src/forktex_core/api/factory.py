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

"""``create_app`` — preconfigured FastAPI instance.

Composes the opt-in middleware stack (trace-id, CORS, security headers), the
``AppError`` → ``ErrorEnvelope`` handler (+ optional catch-all), and the
``/health`` + ``/health/ready`` endpoints, from an :class:`AppConfig`. Returns
the FastAPI instance for consumers to extend with routers and auth.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from forktex_core.api.middleware import ExceptionEnvelopeMiddleware, SecurityHeadersMiddleware
from forktex_core.log import TraceIDMiddleware, get_logger
from forktex_core.types import BaseAppModel

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = get_logger(__name__)


class LivenessResponse(BaseAppModel):
    """``/health`` — the process is up and serving."""

    status: str = "ok"


class ReadinessResponse(BaseAppModel):
    """``/health/ready`` — every declared dependency answered.

    A ``BaseAppModel`` like every other body this library puts on the wire, so
    the health endpoints cannot drift from the error envelope's conventions —
    they used to be hand-built dicts with no schema at all.
    """

    status: str
    checks: dict[str, bool] = Field(default_factory=dict)


HealthProbe = Callable[[], Awaitable[bool]]
"""A coroutine returning ``True`` when the dependency is reachable.

Used by ``/health/ready``. The probe should be fast (≤200ms) and
side-effect-free; readiness checks fire on every probe call."""


class AppConfig(BaseModel):
    """Inputs to :func:`create_app`.

    Every capability is opt-in/opt-out so a service takes only what it needs
    (e.g. a purely API-key API leaves ``cors_origins`` unset and gets no CORS).
    Consumers can still attach further routers/middleware on the returned app.
    """

    title: str = "ForkTex Service"
    version: str = "0.1.0"
    description: str = ""
    debug: bool = False

    # Middleware toggles.
    enable_trace_id: bool = True  # wire forktex_core.log.TraceIDMiddleware
    enable_security_headers: bool = True
    enable_exception_handler: bool = True  # AppError → ErrorEnvelope
    handle_unexpected: bool = True  # also map uncaught Exception → 500 envelope

    # CORS — installed only when origins are provided (None ⇒ no CORS).
    cors_origins: list[str] | None = None
    cors_allow_credentials: bool = True
    cors_allow_methods: list[str] = Field(default_factory=lambda: ["*"])
    cors_allow_headers: list[str] = Field(default_factory=lambda: ["*"])

    # ASGI lifespan passed straight through to FastAPI (startup/shutdown).
    lifespan: Callable[[Any], Any] | None = None

    # Pluggable readiness checks. ``/health/ready`` returns 503 when any
    # named probe returns False or raises.
    readiness_probes: dict[str, HealthProbe] = Field(default_factory=dict)


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Build a preconfigured FastAPI application.

    Wires the opt-in cross-cutting middleware (trace-id, CORS, security
    headers), registers the ``AppError`` → ``ErrorEnvelope`` exception
    handler (plus an optional catch-all), mounts liveness + readiness
    endpoints, and returns the FastAPI instance for consumers to extend
    (routers, auth, more middleware).
    """
    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise ImportError("Install 'forktex-core[api]' (fastapi) to use forktex_core.api") from exc

    cfg = config or AppConfig()

    app = FastAPI(
        title=cfg.title,
        version=cfg.version,
        description=cfg.description,
        debug=cfg.debug,
        lifespan=cfg.lifespan,
    )

    # add_middleware applies in REVERSE (last added = outermost). Target stack,
    # outermost → innermost: security-headers · CORS · trace-id · envelope.
    # The envelope handler sits INSIDE trace-id so the log trace-id contextvar
    # is still live when it builds the error body (so `traceId` == the
    # `X-Request-ID` header == the log records). Security headers are outermost
    # so they stamp every response, including errors and CORS preflights.
    if cfg.enable_exception_handler:
        app.add_middleware(ExceptionEnvelopeMiddleware, handle_unexpected=cfg.handle_unexpected)
    if cfg.enable_trace_id:
        app.add_middleware(TraceIDMiddleware)
    if cfg.cors_origins is not None:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_credentials=cfg.cors_allow_credentials,
            allow_methods=cfg.cors_allow_methods,
            allow_headers=cfg.cors_allow_headers,
        )
    if cfg.enable_security_headers:
        app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/health", tags=["health"])
    async def _liveness() -> LivenessResponse:
        return LivenessResponse()

    # `response_model=None` keeps the `-> JSONResponse` annotation (for typing) from being
    # turned into an OpenAPI response schema, which pydantic can't build for a Response type.
    @app.get("/health/ready", tags=["health"], response_model=None)
    async def _readiness() -> JSONResponse:
        results: dict[str, bool] = {}
        for name, probe in cfg.readiness_probes.items():
            try:
                results[name] = bool(await probe())
            except Exception:
                # A raising probe and a probe returning False are both "not
                # ready", but only one of them is a bug — log so they are
                # distinguishable instead of collapsing into the same false.
                logger.exception("readiness probe raised", extra={"probe": name})
                results[name] = False
        ok = all(results.values())
        if not ok:
            logger.warning(
                "service not ready",
                extra={"failing": sorted(n for n, passed in results.items() if not passed)},
            )
        body = ReadinessResponse(status="ready" if ok else "not_ready", checks=results)
        return JSONResponse(status_code=200 if ok else 503, content=body.model_dump())

    return app


__all__ = ["AppConfig", "HealthProbe", "LivenessResponse", "ReadinessResponse", "create_app"]
