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

"""Cross-cutting Starlette middleware bundled with the ``[api]`` extra.

Request-id / trace-id propagation is NOT here — it is owned by
``forktex_core.log.TraceIDMiddleware`` (single source: it sets the log
trace-id contextvar so logs correlate with the ``X-Request-ID`` header and
the error-envelope ``trace_id``). ``create_app`` wires that one for you.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
except ImportError as exc:  # pragma: no cover - exercised by a clean-install check
    # The middleware classes subclass `BaseHTTPMiddleware`, so unlike the other
    # extras-gated packages this one genuinely needs its dependency at import
    # time. Name the extra rather than letting a bare
    # `ModuleNotFoundError: No module named 'starlette'` reach the caller.
    raise ImportError("Install 'forktex-core[api]' (fastapi) to use forktex_core.api") from exc

from forktex_core.error import AppError, AppErrorCode, to_envelope
from forktex_core.log import get_logger, get_trace_id

if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp


_HTTP_STATUS_BY_CODE: dict[AppErrorCode, int] = {
    AppErrorCode.NOT_FOUND: 404,
    AppErrorCode.ALREADY_EXISTS: 409,
    AppErrorCode.BAD_REQUEST: 400,
    AppErrorCode.VALIDATION: 422,
    AppErrorCode.UNAUTHORIZED: 401,
    AppErrorCode.FORBIDDEN: 403,
    AppErrorCode.CONFLICT: 409,
    AppErrorCode.RATE_LIMITED: 429,
    AppErrorCode.UNAVAILABLE: 503,
    AppErrorCode.TIMEOUT: 504,
    # A cancelled job/run isn't a server fault and isn't retryable as-is —
    # 409 matches "the resource's current state conflicts with your request".
    AppErrorCode.CANCELLED: 409,
    # The operation genuinely failed (e.g. a workflow exhausted its retries).
    # Distinct code from INTERNAL so a client can tell "your work failed"
    # apart from "our server broke", even though both render as 500.
    AppErrorCode.FAILED: 500,
    AppErrorCode.INTERNAL: 500,
}


def _http_status_for(error: AppError) -> int:
    """Map an ``AppError``'s ``code`` to an HTTP status.

    ``AppError.code`` has no notion of HTTP — this mapping is owned here,
    the one place that actually needs it. Falls back to 500 for a service's
    own custom code (``code`` is an open ``str``) that isn't one of the
    generic vocabulary's members.
    """
    try:
        return _HTTP_STATUS_BY_CODE[AppErrorCode(error.code)]
    except ValueError:
        return 500


class ExceptionEnvelopeMiddleware(BaseHTTPMiddleware):
    """Convert raised errors into the ``ErrorEnvelope`` wire shape.

    Runs *inside* ``TraceIDMiddleware`` so the log trace-id contextvar is
    still active when the envelope is built — the response body's ``traceId``
    matches the ``X-Request-ID`` header and the server logs. ``AppError``'s
    ``code`` maps to an HTTP status via ``_http_status_for`` (``AppError``
    itself carries no HTTP status — that's an HTTP-transport concern owned
    here); when ``handle_unexpected`` is set, any other exception is logged
    and returned as a generic 500 envelope (never leaking internals).
    FastAPI's own HTTPException/validation handling runs closer to the route
    and is left untouched.
    """

    def __init__(self, app: ASGIApp, *, handle_unexpected: bool = True) -> None:
        super().__init__(app)
        self._handle_unexpected = handle_unexpected

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        except AppError as exc:
            envelope = to_envelope(exc, trace_id=get_trace_id())
            return JSONResponse(status_code=_http_status_for(exc), content=envelope.model_dump(by_alias=True))
        except Exception:
            if not self._handle_unexpected:
                raise
            get_logger(__name__).exception("unhandled error")
            envelope = to_envelope(AppError("Internal Server Error"), trace_id=get_trace_id())
            return JSONResponse(status_code=500, content=envelope.model_dump(by_alias=True))


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add a defensive baseline of HTTP security headers.

    Targets app responses that don't otherwise opt in to security
    headers — the values are conservative defaults that won't break
    typical JSON APIs. Consumers needing CSP, HSTS, or per-route
    overrides should install their own middleware.
    """

    DEFAULT_HEADERS: ClassVar[dict[str, str]] = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    }

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for key, value in self.DEFAULT_HEADERS.items():
            response.headers.setdefault(key, value)
        return response


__all__ = ["ExceptionEnvelopeMiddleware", "SecurityHeadersMiddleware"]
