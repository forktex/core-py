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

"""Structured logging for ForkTex Python services.

Zero extra dependencies — stdlib only. Works everywhere: FastAPI services,
CLI workers, forktex-py scripts, background processes.

Features:
- JSON output (Loki-compatible) in production; human-readable in dev
- Coroutine-safe trace_id via ``contextvars``, scoped to any block of code —
  HTTP request, worker job, CLI run — via ``trace_context()``/``async_trace_context()``
- Structured extra fields via ``log_context()`` / ``async_log_context()``
- One-call ``setup_logging()`` replaces every service's bespoke logging_config.py
- Optional pure-ASGI ``TraceIDMiddleware`` for FastAPI/Starlette/any ASGI app (zero deps),
  sanitizes the inbound trace id before trusting it
- ``$FORKTEX_LOG_LEVEL`` / ``$FORKTEX_LOG_DEBUG`` / ``$FORKTEX_LOG_JSON`` env vars override
  ``setup_logging()``'s defaults when the caller leaves the argument unset
- ``setup_logging(queue=True)`` moves log I/O to a background thread
  (stdlib ``QueueHandler``/``QueueListener``, no extra dependency)
- ``setup_logging(handlers=[...])`` to plug in any transport (file, syslog, …)
  while still getting the context filter + formatter + level applied

Quickstart::

    from forktex_core.log import setup_logging, get_logger

    setup_logging(service="network")          # JSON to stdout, INFO level
    log = get_logger(__name__)
    log.info("service started")
    # → {"timestamp":"...","level":"INFO","logger":"...","service":"network","message":"service started"}

    setup_logging(service="network", debug=True)
    # → 2026-05-02 14:30:00 | INFO     | network.crm | service started

FastAPI middleware::

    from forktex_core.log import setup_logging, TraceIDMiddleware
    app.add_middleware(TraceIDMiddleware, header="X-Request-ID")

Structured context::

    from forktex_core.log import async_log_context, get_logger
    log = get_logger(__name__)

    async with async_log_context(org_id=str(org_id), user_id=str(user_id)):
        log.info("processing")
        # → {..."org_id": "...", "user_id": "..."}

Scoped trace_id — any process, not just HTTP (CLI / workers)::

    from forktex_core.log import trace_context
    with trace_context(f"job-{job_id}"):
        log.info("job started")    # → {..."trace_id": "job-..."}
    # trace_id is restored automatically on exit — nothing to clear manually
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from collections.abc import Awaitable, Callable
from queue import SimpleQueue
from typing import Any

from forktex_core.log._context import (
    async_log_context,
    async_trace_context,
    get_extra_fields,
    get_root_trace_id,
    get_trace_id,
    log_context,
    set_trace_id,
    trace_context,
)
from forktex_core.log._decorators import traced
from forktex_core.log._formatter import HumanFormatter, JsonFormatter

# Minimal ASGI protocol aliases — so the middleware needs no starlette/asgiref dep.
_Scope = dict[str, Any]
_Message = dict[str, Any]
_Receive = Callable[[], Awaitable[_Message]]
_Send = Callable[[_Message], Awaitable[None]]
_ASGIApp = Callable[[_Scope, _Receive, _Send], Awaitable[None]]

# An inbound X-Request-ID is untrusted client input. Restrict it to a safe
# charset before trusting it as trace_id — otherwise a client can splice
# control characters (e.g. "\n") into it and forge log lines wherever a
# formatter (HumanFormatter) writes trace_id into a raw text line.
_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class _ContextFilter(logging.Filter):
    """Injects contextvar state into every LogRecord before formatting."""

    def __init__(self, service: str | None = None) -> None:
        super().__init__()
        self._service = service

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id()
        record.root_trace_id = get_root_trace_id()
        record.service = self._service
        record._forktex_extra = get_extra_fields()
        return True


class _InProcessQueueHandler(logging.handlers.QueueHandler):
    """``QueueHandler`` that hands records to the queue untouched.

    The stdlib default ``prepare()`` pre-formats the message and strips
    ``exc_info``/``args`` — a precaution for handlers backed by a
    multiprocessing queue that has to pickle records across a process
    boundary. Our queue is an in-process ``queue.SimpleQueue``, so there is no
    pickling boundary and stripping would only destroy the exception info
    ``JsonFormatter``/``HumanFormatter`` need on the listener side.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return record


#: Third-party loggers that are noisy at INFO by default. Extended (not
#: replaced) by ``setup_logging(quiet=...)``; pass ``quiet_defaults=False`` to
#: opt out of these entirely (e.g. a worker that wants ``httpx`` debug output).
DEFAULT_QUIET_LOGGERS: list[str] = [
    "uvicorn.access",
    "uvicorn.error",
    "sqlalchemy.engine",
    "httpx",
    "httpcore",
    "asyncio",
]


def _env_bool(name: str) -> bool | None:
    val = os.getenv(name)
    if val is None:
        return None
    return val.strip().lower() in {"1", "true", "yes", "on"}


def setup_logging(
    *,
    service: str | None = None,
    level: int | str | None = None,
    debug: bool | None = None,
    json: bool | None = None,
    quiet: list[str] | None = None,
    quiet_level: int = logging.WARNING,
    quiet_defaults: bool = True,
    fmt: str | None = None,
    datefmt: str | None = None,
    handlers: list[logging.Handler] | None = None,
    queue: bool = False,
) -> None:
    """Configure the root logger for a ForkTex Python process.

    Call once at process startup (e.g. in ``main.py`` or FastAPI lifespan).
    Safe to call multiple times — idempotent (clears existing handlers first;
    with ``queue=True`` each call still starts a new listener thread, so
    prefer calling it once).

    Args:
        service: Service name included in every log record (e.g. ``"network"``).
                 Appears as a Loki label and in the JSON ``service`` field.
        level: Root log level. Default ``INFO`` (or ``$FORKTEX_LOG_LEVEL`` if set).
               Overridden to ``DEBUG`` when ``debug`` is true.
        debug: If True, sets level to DEBUG and switches to human-readable
               format (unless ``json=True`` forces JSON in debug mode).
               Default: ``$FORKTEX_LOG_DEBUG`` if set, else ``False``.
        json: Force JSON output (True) or human-readable output (False).
              Default: ``$FORKTEX_LOG_JSON`` if set, else JSON when not debug,
              human-readable when debug.
        quiet: Additional logger names to set to ``quiet_level``.
        quiet_level: Level for quietened loggers. Default ``WARNING``.
        quiet_defaults: If False, skip silencing ``DEFAULT_QUIET_LOGGERS``
               (only ``quiet`` is applied). Default ``True``.
        fmt: Override ``HumanFormatter``'s line format (human/debug mode only).
        datefmt: Override ``HumanFormatter``'s date format (human/debug mode only).
        handlers: Attach these handlers instead of the default
               ``StreamHandler(stdout)``. ``setup_logging`` still applies the
               context filter, level, and formatter to each — callers only
               supply the transport (e.g. a ``RotatingFileHandler``).
        queue: If True, route records through a stdlib
               ``QueueHandler``/``QueueListener`` pair so log I/O happens on a
               background thread instead of blocking the caller (useful under
               load in an async FastAPI process). No extra dependency.
    """
    if debug is None:
        debug = _env_bool("FORKTEX_LOG_DEBUG") or False
    if json is None:
        json = _env_bool("FORKTEX_LOG_JSON")
    if level is None:
        level = os.getenv("FORKTEX_LOG_LEVEL", logging.INFO)

    if debug:
        level = logging.DEBUG

    effective_json = (not debug) if json is None else json

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    formatter = JsonFormatter() if effective_json else HumanFormatter(fmt=fmt, datefmt=datefmt)
    target_handlers = handlers if handlers is not None else [logging.StreamHandler(sys.stdout)]
    for h in target_handlers:
        h.setLevel(level)
        h.setFormatter(formatter)

    if queue:
        log_queue: SimpleQueue[logging.LogRecord] = SimpleQueue()
        queue_handler = _InProcessQueueHandler(log_queue)
        queue_handler.setLevel(level)
        queue_handler.addFilter(_ContextFilter(service=service))
        root.addHandler(queue_handler)

        listener = logging.handlers.QueueListener(log_queue, *target_handlers, respect_handler_level=True)
        listener.start()
    else:
        for h in target_handlers:
            h.addFilter(_ContextFilter(service=service))
            root.addHandler(h)

    quiet_names = (DEFAULT_QUIET_LOGGERS if quiet_defaults else []) + (quiet or [])
    for name in quiet_names:
        logging.getLogger(name).setLevel(quiet_level)


def get_logger(name: str) -> logging.Logger:
    """Return a standard ``logging.Logger`` for ``name``.

    Thin wrapper over ``logging.getLogger`` — exists so consumers can import
    from one place::

        from forktex_core.log import get_logger
        log = get_logger(__name__)
    """
    return logging.getLogger(name)


class TraceIDMiddleware:
    """Pure-ASGI middleware that binds a trace id for the whole request.

    Reads ``header`` (default ``X-Request-ID``) from the request or mints a
    time-ordered ``uuid7``, stores it in the log trace-id contextvar for the *entire* ASGI
    call — so the endpoint, a streaming body, and background tasks all log with
    it — and echoes it back on the response. Pure ASGI: works with any ASGI app
    (FastAPI, Starlette, raw) and adds no third-party dependency.

    Usage (FastAPI/Starlette; optional — plain Python callers don't need it)::

        from forktex_core.log import TraceIDMiddleware
        app.add_middleware(TraceIDMiddleware)                    # X-Request-ID
        app.add_middleware(TraceIDMiddleware, header="X-Trace-ID")
    """

    def __init__(self, app: _ASGIApp, *, header: str = "X-Request-ID") -> None:
        self._app = app
        self._header = header.lower().encode()

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        inbound = dict(scope["headers"]).get(self._header)
        candidate = inbound.decode(errors="replace") if inbound else None
        valid = candidate if candidate and _TRACE_ID_RE.match(candidate) else None

        async def send_with_header(message: _Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((self._header, trace_id.encode()))
            await send(message)

        with trace_context(valid) as trace_id:
            await self._app(scope, receive, send_with_header)


__all__ = [
    "DEFAULT_QUIET_LOGGERS",
    "TraceIDMiddleware",
    "async_log_context",
    "async_trace_context",
    "get_extra_fields",
    "get_logger",
    "get_root_trace_id",
    "get_trace_id",
    "log_context",
    "set_trace_id",
    "setup_logging",
    "trace_context",
    "traced",
]
