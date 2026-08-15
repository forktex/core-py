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

"""Tests for the level-0 [error] extra: AppError → ErrorEnvelope mapping."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forktex_core.error import (
    AppErrorCode,
    BadRequestError,
    ErrorEnvelope,
    NotFoundError,
    ServiceUnavailableError,
    TooManyRequestsError,
    UnauthorizedError,
    to_envelope,
)


def test_to_envelope_projects_apperror_fields():
    error = NotFoundError("user not found", details={"user_id": "abc"})
    envelope = to_envelope(error)

    assert envelope.code == AppErrorCode.NOT_FOUND
    assert envelope.message == "user not found"
    assert envelope.details == {"user_id": "abc"}


def test_to_envelope_handles_empty_details():
    error = UnauthorizedError("token expired")
    envelope = to_envelope(error)

    assert envelope.code == AppErrorCode.UNAUTHORIZED
    assert envelope.message == "token expired"
    assert envelope.details == {}


def test_envelope_emits_camel_case_keys():
    """ErrorEnvelope inherits BaseAppModel — keys cross the wire as camelCase."""
    envelope = ErrorEnvelope(
        code=AppErrorCode.BAD_REQUEST,
        message="missing field",
        details={"field": "email"},
        trace_id="abc123",
    )
    dumped = envelope.model_dump(by_alias=True)
    assert dumped == {
        "code": "bad_request",
        "message": "missing field",
        "details": {"field": "email"},
        "traceId": "abc123",
    }


def test_to_envelope_carries_trace_id():
    envelope = to_envelope(NotFoundError("x"), trace_id="t-9")
    assert envelope.trace_id == "t-9"
    assert to_envelope(NotFoundError("x")).trace_id is None


def test_envelope_code_is_open_for_service_codes():
    # code is an open str so a service's own error code round-trips through THE envelope
    env = ErrorEnvelope.model_validate({"code": "widget_locked", "message": "x", "details": {}})
    assert env.code == "widget_locked"
    # core producers still pass AppErrorCode members (StrEnum → serialise as their string)
    assert to_envelope(NotFoundError("x")).code == "not_found" == AppErrorCode.NOT_FOUND


def test_too_many_requests_and_service_unavailable_round_trip_through_envelope():
    rate_limited = to_envelope(TooManyRequestsError("slow down"))
    assert rate_limited.code == AppErrorCode.RATE_LIMITED
    assert rate_limited.message == "slow down"

    unavailable = to_envelope(ServiceUnavailableError("maintenance window"))
    assert unavailable.code == AppErrorCode.UNAVAILABLE
    assert unavailable.message == "maintenance window"


def test_apperror_subclass_code_preserved():
    """code is transport-agnostic — never HTTP status, kept on the exception and mirrored on the envelope."""
    error = NotFoundError("missing")
    assert error.code == AppErrorCode.NOT_FOUND


def test_envelope_handles_non_ascii_message():
    error = BadRequestError("Ungültige Eingabe — 请检查输入")
    envelope = to_envelope(error)
    assert envelope.message == "Ungültige Eingabe — 请检查输入"
    dumped = envelope.model_dump(mode="json", by_alias=True)
    assert dumped["message"] == "Ungültige Eingabe — 请检查输入"


def test_envelope_validation_error_on_missing_required_field():
    with pytest.raises(ValidationError):
        ErrorEnvelope.model_validate({"message": "x"})  # missing required `code`


def test_to_envelope_preserves_exception_chain_on_the_original_error():
    """to_envelope() only projects fields — it doesn't touch __cause__/__context__
    on the AppError instance it was given, so upstream chaining survives untouched."""
    try:
        try:
            raise ValueError("root cause")
        except ValueError as exc:
            raise NotFoundError("not found") from exc
    except NotFoundError as error:
        envelope = to_envelope(error)
        assert envelope.message == "not found"
        assert isinstance(error.__cause__, ValueError)


def test_envelope_details_with_non_serializable_value_falls_back_to_str_in_json_mode():
    """An error-reporting path must never itself crash on a value pydantic
    doesn't know how to serialize (e.g. a raw exception, a custom object)."""

    class Unserializable:
        def __str__(self) -> str:
            return "unserializable-repr"

    envelope = ErrorEnvelope(code="internal", message="x", details={"cause": Unserializable()})
    dumped = envelope.model_dump(mode="json")
    assert dumped["details"] == {"cause": "unserializable-repr"}


def test_envelope_details_normal_values_unaffected_by_fallback_serializer():
    envelope = ErrorEnvelope(code="internal", message="x", details={"user_id": "abc", "count": 3})
    assert envelope.model_dump(mode="json")["details"] == {"user_id": "abc", "count": 3}
