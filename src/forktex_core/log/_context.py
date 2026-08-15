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

"""Contextvar storage for trace_id and structured log fields.

Uses ``contextvars.ContextVar`` so values are coroutine-scoped — no leakage
between concurrent async tasks even when running in the same thread.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

# Trace/request identifier — set by middleware or manually via set_trace_id()
_trace_id: ContextVar[str | None] = ContextVar("forktex_log.trace_id", default=None)

# Arbitrary structured fields per coroutine context. Values are opaque (``object``):
# the logger only serialises them, it never treats them as a specific type.
# Sentinel None avoids the shared-mutable-default problem: if we used
# default={}, all coroutines that never call log_context() would share the
# same dict object and mutations would leak across task boundaries.
_extra_fields: ContextVar[dict[str, object] | None] = ContextVar("forktex_log.extra", default=None)

# Stable across a whole chain of nested trace_context()/async_trace_context()
# calls (an HTTP request, a worker job and everything it calls) — set once by
# whichever scope enters first, unlike trace_id which is fresh per call.
_root_trace_id: ContextVar[str | None] = ContextVar("forktex_log.root_trace_id", default=None)


def get_trace_id() -> str | None:
    return _trace_id.get()


def get_root_trace_id() -> str | None:
    return _root_trace_id.get()


def set_trace_id(value: str | None) -> None:
    """Manually set the trace ID for the current context (non-async callers)."""
    _trace_id.set(value)


@contextmanager
def trace_context(value: str | None = None) -> Generator[str]:
    """Sync context manager — scope a trace_id to this block, then restore
    whatever was set before it (never leaves it dangling like a bare
    ``set_trace_id()`` can). Mints a time-ordered ``uuid7`` if ``value`` is
    omitted.

    The same primitive ``TraceIDMiddleware`` uses per-request — usable in any
    Python process (CLI script, worker job loop, background task), not just
    behind an ASGI app::

        with trace_context(f"job-{job_id}"):
            logger.info("processing")   # → {..."trace_id": "job-..."}
        # trace_id is restored to whatever it was before the block

    Whichever call is outermost (an HTTP request, or the outermost nested
    call in a worker/CLI process) also establishes ``root_trace_id`` — every
    nested ``trace_context()``/``async_trace_context()`` call gets its own
    fresh ``trace_id`` but shares that same ``root_trace_id``, so you can
    correlate one step (``trace_id``) or the whole chain (``root_trace_id``).
    """
    trace_id = value or str(uuid.uuid7())
    token = _trace_id.set(trace_id)
    root_token = _root_trace_id.set(trace_id) if _root_trace_id.get() is None else None
    try:
        yield trace_id
    finally:
        _trace_id.reset(token)
        if root_token is not None:
            _root_trace_id.reset(root_token)


@asynccontextmanager
async def async_trace_context(value: str | None = None) -> AsyncGenerator[str]:
    """Async counterpart of :func:`trace_context` (coroutine-scoped), with the
    same ``root_trace_id`` establishment behavior.

    async with async_trace_context() as trace_id:
        await process_job()
    """
    trace_id = value or str(uuid.uuid7())
    token = _trace_id.set(trace_id)
    root_token = _root_trace_id.set(trace_id) if _root_trace_id.get() is None else None
    try:
        yield trace_id
    finally:
        _trace_id.reset(token)
        if root_token is not None:
            _root_trace_id.reset(root_token)


def get_extra_fields() -> dict[str, object]:
    """Return the current structured log fields, never None."""
    return _extra_fields.get(None) or {}


@contextmanager
def log_context(**fields: object) -> Generator[None]:
    """Sync context manager — inject structured fields into all log records
    emitted within the block.

        with log_context(org_id=str(org_id), user_id=str(user_id)):
            logger.info("processing")   # → {..."org_id": "...", "user_id": "..."}
    """
    merged = {**get_extra_fields(), **fields}
    token = _extra_fields.set(merged)
    try:
        yield
    finally:
        _extra_fields.reset(token)


@asynccontextmanager
async def async_log_context(**fields: object) -> AsyncGenerator[None]:
    """Async context manager — inject structured fields into all log records
    emitted within the async block (coroutine-scoped).

        async with async_log_context(org_id=str(org_id)):
            await process()
    """
    merged = {**get_extra_fields(), **fields}
    token = _extra_fields.set(merged)
    try:
        yield
    finally:
        _extra_fields.reset(token)


__all__ = [
    "async_log_context",
    "async_trace_context",
    "get_extra_fields",
    "get_root_trace_id",
    "get_trace_id",
    "log_context",
    "set_trace_id",
    "trace_context",
]
