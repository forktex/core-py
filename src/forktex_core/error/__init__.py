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

"""Level-0 ``[error]`` extra — AppError hierarchy + envelope + http mapping.

This module is the public, top-level home for the app exception
hierarchy that every forktex service raises and every transport
(HTTP, CLI, queue) translates::

    from forktex_core.error import AppError, NotFoundError, to_envelope

``AppError`` carries a ``code`` consumers switch on to build typed error
envelopes; it has no notion of HTTP or any other transport. The FastAPI
exception handler in ``forktex_core.api.middleware`` owns its own
``AppErrorCode`` → HTTP-status mapping, since HTTP is the one transport
that needs one. The envelope shape is deliberately tiny:
``{code, message, details}``; consumers extend it (e.g., adding
``request_id``) by subclassing ``ErrorEnvelope``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, field_serializer
from pydantic_core import to_jsonable_python

from forktex_core.types import BaseAppModel


class AppErrorCode(StrEnum):
    """Core's generic, cross-cutting error codes — the stable published vocabulary.

    Services SHOULD reuse these and add their own domain codes as needed; the wire
    envelope's ``code`` is an open ``str`` so any service code round-trips. Auth /
    domain specifics (e.g. invalid-credentials, email-taken) are service-owned.
    """

    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    BAD_REQUEST = "bad_request"
    VALIDATION = "validation"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERNAL = "internal"


class AppError(Exception):
    """Root of the app exception hierarchy.

    Subclasses set ``code``; consumers raise the leaf subclass and the
    handler reads it to build a typed response. ``details`` is an optional
    dict the handler can pass through to the client. This class has no
    notion of HTTP status — transports that need one (e.g. an HTTP
    exception handler) map ``code`` to their own status vocabulary
    themselves (see ``forktex_core.api.middleware``).
    """

    code: AppErrorCode = AppErrorCode.INTERNAL

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(AppError):
    code = AppErrorCode.NOT_FOUND


class AlreadyExistsError(AppError):
    code = AppErrorCode.ALREADY_EXISTS


class BadRequestError(AppError):
    code = AppErrorCode.BAD_REQUEST


class UnprocessableEntityError(AppError):
    """Input is well-formed but semantically invalid (e.g. failed
    business-rule validation), distinct from ``BadRequestError``
    (malformed/missing input)."""

    code = AppErrorCode.VALIDATION


class UnauthorizedError(AppError):
    code = AppErrorCode.UNAUTHORIZED


class ForbiddenError(AppError):
    code = AppErrorCode.FORBIDDEN


class ConflictError(AppError):
    code = AppErrorCode.CONFLICT


class TooManyRequestsError(AppError):
    """The caller is rate-limited; retry after backing off."""

    code = AppErrorCode.RATE_LIMITED


class ServiceUnavailableError(AppError):
    """The service is deliberately refusing requests right now (e.g.
    a maintenance window, a dependency circuit-breaker open), distinct from
    the bare ``AppError`` default (an unexpected failure)."""

    code = AppErrorCode.UNAVAILABLE


class ErrorEnvelope(BaseAppModel):
    """Wire shape for app-error responses — the one contract every service bridges onto.

    ``code`` is an open ``str`` (not the closed enum) so a service's own error code
    serialises through this same envelope; core producers pass ``AppErrorCode`` members,
    which are strings, so the published vocabulary stays the stable default. ``message`` is
    human-readable, ``details`` is free-form context, and ``trace_id`` (wire alias
    ``traceId``) correlates the response with the server logs. The HTTP status is conveyed
    by the response itself, not the envelope.
    """

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None

    @field_serializer("details", when_used="json")
    def _serialize_details(self, value: dict[str, Any]) -> dict[str, Any]:
        """``details`` is caller-supplied free-form data — an error-reporting
        path must never itself crash on a value pydantic doesn't know how to
        serialize (e.g. a raw exception object, a SQLAlchemy model). Anything
        pydantic recognizes (datetime, UUID, Decimal, …) still serializes
        normally; anything it doesn't falls back to ``str()``, mirroring
        ``forktex_core.log``'s ``json.dumps(doc, default=str)`` for the same
        reason."""
        return to_jsonable_python(value, fallback=str)


def to_envelope(error: AppError, *, trace_id: str | None = None) -> ErrorEnvelope:
    """Project an ``AppError`` onto the wire envelope shape.

    Used by transport layers (FastAPI exception handler, CLI top-level,
    queue dead-letter writer) to render a uniform error payload. Pass the
    active ``trace_id`` (e.g. ``forktex_core.log.get_trace_id()``) so the
    client can quote it back for support.
    """
    return ErrorEnvelope(code=str(error.code), message=error.message, details=error.details, trace_id=trace_id)


__all__ = [
    "AlreadyExistsError",
    "AppError",
    "AppErrorCode",
    "BadRequestError",
    "ConflictError",
    "ErrorEnvelope",
    "ForbiddenError",
    "NotFoundError",
    "ServiceUnavailableError",
    "TooManyRequestsError",
    "UnauthorizedError",
    "UnprocessableEntityError",
    "to_envelope",
]
