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

"""Tests for forktex_core.api: factory + middleware + envelope + trace-id."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forktex_core.api import AppConfig, create_app
from forktex_core.error import AppError, NotFoundError, TooManyRequestsError
from forktex_core.log import TraceIDMiddleware, get_trace_id


def test_create_app_returns_fastapi_instance():
    app = create_app()
    assert isinstance(app, FastAPI)
    assert app.title == "ForkTex Service"
    assert app.version == "0.1.0"


def test_app_metadata_threads_through_config():
    app = create_app(AppConfig(title="Intelligence", version="1.2.3", description="Smart things"))
    assert app.title == "Intelligence"
    assert app.version == "1.2.3"
    assert app.description == "Smart things"


def test_openapi_schema_builds():
    # Guards the /health/ready no-annotation workaround: a TYPE_CHECKING-only
    # forward-ref return type would make .openapi() raise for consumers.
    app = create_app()
    schema = app.openapi()
    assert "/health" in schema["paths"]
    assert "/health/ready" in schema["paths"]


def test_liveness_endpoint():
    client = TestClient(create_app())
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_readiness_endpoint_no_probes():
    client = TestClient(create_app())
    res = client.get("/health/ready")
    assert res.status_code == 200
    assert res.json() == {"status": "ready", "checks": {}}


def test_readiness_endpoint_failing_probe_returns_503():
    async def bad_probe() -> bool:
        return False

    async def good_probe() -> bool:
        return True

    async def raising_probe() -> bool:
        raise RuntimeError("boom")

    app = create_app(AppConfig(readiness_probes={"db": good_probe, "cache": bad_probe, "vector": raising_probe}))
    res = TestClient(app).get("/health/ready")
    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == {"db": True, "cache": False, "vector": False}


def test_trace_id_header_stamped_and_inbound_propagated():
    client = TestClient(create_app())
    res = client.get("/health")
    assert len(res.headers.get("X-Request-ID", "")) >= 8
    res = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert res.headers["X-Request-ID"] == "abc-123"


def test_security_headers_present():
    res = TestClient(create_app()).get("/health")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Permissions-Policy" in res.headers


def test_app_error_renders_envelope_with_trace_id():
    app = create_app()

    @app.get("/missing")
    async def _route():
        raise NotFoundError("user not found", details={"user_id": "abc"})

    res = TestClient(app).get("/missing", headers={"X-Request-ID": "trace-xyz"})
    assert res.status_code == 404
    body = res.json()
    assert body["code"] == "not_found"
    assert body["message"] == "user not found"
    assert body["details"] == {"user_id": "abc"}
    # envelope traceId is correlated with the response header (and the logs)
    assert body["traceId"] == "trace-xyz" == res.headers["X-Request-ID"]


def test_security_headers_present_on_error_response():
    app = create_app()

    @app.get("/missing")
    async def _route():
        raise NotFoundError("nope")

    res = TestClient(app).get("/missing")
    assert res.status_code == 404
    assert res.headers["X-Content-Type-Options"] == "nosniff"


def test_app_error_maps_code_to_http_status_via_middleware():
    """AppError itself carries no HTTP status — forktex_core.api.middleware
    owns the AppErrorCode -> HTTP-status mapping and applies it here."""
    app = create_app()

    @app.get("/rate-limited")
    async def _route():
        raise TooManyRequestsError("slow down")

    res = TestClient(app).get("/rate-limited")
    assert res.status_code == 429
    assert res.json()["code"] == "rate_limited"


def test_app_error_with_custom_code_falls_back_to_500():
    """A service's own custom `code` (an open str, not in the generic
    AppErrorCode vocabulary) isn't in the HTTP-status mapping — falls back
    to 500 rather than raising a KeyError."""
    app = create_app()

    class WidgetLockedError(AppError):
        code = "widget_locked"

    @app.get("/locked")
    async def _route():
        raise WidgetLockedError("widget is locked")

    res = TestClient(app).get("/locked")
    assert res.status_code == 500
    assert res.json()["code"] == "widget_locked"


@pytest.mark.parametrize(
    "exc_factory,expected_status,expected_code",
    [
        # flow — were previously masked 500s because FlowError derived from
        # bare Exception, so `except AppError` in the middleware never matched.
        (lambda: _flow_errors().SignalTimeout("no signal"), 504, "timeout"),
        (lambda: _flow_errors().WorkflowCancelled("cancelled"), 409, "cancelled"),
        (lambda: _flow_errors().StepFailed("retries exhausted"), 500, "failed"),
        (lambda: _flow_errors().GraphStuckError("no outgoing edge"), 500, "failed"),
        # vector — same root cause via VectorError.
        (lambda: _vector_errors().CollectionNotFoundError("nope"), 404, "not_found"),
        (lambda: _vector_errors().DimensionMismatchError("dim 4 != 2"), 422, "validation"),
    ],
)
def test_downstream_package_errors_render_with_real_status_not_masked_500(exc_factory, expected_status, expected_code):
    """Regression guard for the masked-500 bug.

    `flow` and `vector` maintained their own hierarchies rooted at bare
    `Exception`, so `ExceptionEnvelopeMiddleware`'s `except AppError` could
    not match them and every one surfaced as a generic 500 with the body
    masked to "Internal Server Error". Now that they subclass `AppError`,
    each carries a real code and maps to a real status.
    """
    app = create_app()

    @app.get("/boom")
    async def _route():
        raise exc_factory()

    res = TestClient(app).get("/boom")
    assert res.status_code == expected_status
    body = res.json()
    assert body["code"] == expected_code
    # The real message survives — it is not masked as "Internal Server Error".
    assert body["message"] != "Internal Server Error"


def _flow_errors():
    from forktex_core.flow import errors

    return errors


def _vector_errors():
    from forktex_core.vector import errors

    return errors


def test_unexpected_exception_becomes_500_envelope():
    app = create_app()

    @app.get("/boom")
    async def _route():
        raise RuntimeError("kaboom")

    logging.disable(logging.CRITICAL)  # silence the expected .exception() log line
    try:
        res = TestClient(app, raise_server_exceptions=False).get("/boom", headers={"X-Request-ID": "t-1"})
    finally:
        logging.disable(logging.NOTSET)
    assert res.status_code == 500
    body = res.json()
    assert body["code"] == "internal"
    assert body["message"] == "Internal Server Error"  # no internals leaked
    assert body["details"] == {}
    assert body["traceId"] == "t-1"


def test_handle_unexpected_off_propagates():
    app = create_app(AppConfig(handle_unexpected=False))

    @app.get("/boom")
    async def _route():
        raise RuntimeError("kaboom")

    logging.disable(logging.CRITICAL)
    try:
        res = TestClient(app, raise_server_exceptions=False).get("/boom")
    finally:
        logging.disable(logging.NOTSET)
    assert res.status_code == 500
    # generic ASGI 500, NOT our envelope
    assert "code" not in res.json() if res.headers.get("content-type", "").startswith("application/json") else True


def test_cors_installed_only_when_configured():
    # off by default
    res = TestClient(create_app()).get("/health", headers={"Origin": "https://app.test"})
    assert "access-control-allow-origin" not in res.headers
    # on when origins provided
    app = create_app(AppConfig(cors_origins=["https://app.test"]))
    res = TestClient(app).get("/health", headers={"Origin": "https://app.test"})
    assert res.headers["access-control-allow-origin"] == "https://app.test"


def test_lifespan_passthrough_runs():
    import contextlib

    events: list[str] = []

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        events.append("startup")
        yield
        events.append("shutdown")

    app = create_app(AppConfig(lifespan=lifespan))
    with TestClient(app) as client:
        client.get("/health")
    assert events == ["startup", "shutdown"]


def test_disabling_middleware_drops_their_headers():
    app = create_app(AppConfig(enable_trace_id=False, enable_security_headers=False))
    res = TestClient(app).get("/health")
    assert "X-Request-ID" not in res.headers
    assert "X-Content-Type-Options" not in res.headers


def test_trace_id_middleware_covers_background_tasks():
    # The pure-ASGI TraceIDMiddleware scopes the trace id over the WHOLE ASGI call,
    # so a background task (runs after the response is sent) still sees it — the
    # case BaseHTTPMiddleware got wrong (its finally-reset fired first → None).
    from starlette.background import BackgroundTask
    from starlette.responses import JSONResponse

    seen: dict[str, str | None] = {}
    app = FastAPI()
    app.add_middleware(TraceIDMiddleware)

    @app.get("/bg")
    async def _bg() -> JSONResponse:
        return JSONResponse(
            {"in_request": get_trace_id()},
            background=BackgroundTask(lambda: seen.__setitem__("bg", get_trace_id())),
        )

    res = TestClient(app).get("/bg", headers={"X-Request-ID": "trace-BG"})
    assert res.headers["X-Request-ID"] == "trace-BG"
    assert res.json()["in_request"] == "trace-BG"
    assert seen["bg"] == "trace-BG"
