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

"""Tests for the level-0 [error] extra: the AppError class hierarchy itself —
the code hierarchy itself, message/details storage, exception chaining."""

from __future__ import annotations

import pytest

from forktex_core.error import (
    AlreadyExistsError,
    AppError,
    AppErrorCode,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    TooManyRequestsError,
    UnauthorizedError,
    UnprocessableEntityError,
)

_LEAF_SUBCLASSES = [
    (NotFoundError, AppErrorCode.NOT_FOUND),
    (AlreadyExistsError, AppErrorCode.ALREADY_EXISTS),
    (BadRequestError, AppErrorCode.BAD_REQUEST),
    (UnprocessableEntityError, AppErrorCode.VALIDATION),
    (UnauthorizedError, AppErrorCode.UNAUTHORIZED),
    (ForbiddenError, AppErrorCode.FORBIDDEN),
    (ConflictError, AppErrorCode.CONFLICT),
    (TooManyRequestsError, AppErrorCode.RATE_LIMITED),
    (ServiceUnavailableError, AppErrorCode.UNAVAILABLE),
]


@pytest.mark.parametrize("cls,expected_code", _LEAF_SUBCLASSES)
def test_leaf_subclass_code(cls, expected_code):
    error = cls("boom")
    assert error.code == expected_code
    assert isinstance(error, AppError)


def test_already_exists_and_conflict_are_distinct_codes():
    """Both would map to HTTP 409 in an HTTP transport — but AppError itself
    carries no HTTP status; `code` is the only distinguishing signal."""
    already_exists = AlreadyExistsError("dup")
    conflict = ConflictError("dup")
    assert already_exists.code != conflict.code


def test_bad_request_vs_unprocessable_entity():
    """Malformed/missing input vs. well-formed but semantically invalid — distinguished by code."""
    bad_request = BadRequestError("missing field")
    unprocessable = UnprocessableEntityError("value fails a business rule")
    assert bad_request.code != unprocessable.code


def test_bare_apperror_defaults_to_internal():
    error = AppError("unexpected")
    assert error.code == AppErrorCode.INTERNAL


def test_message_and_details_stored():
    error = NotFoundError("user not found", details={"user_id": "abc"})
    assert error.message == "user not found"
    assert error.details == {"user_id": "abc"}
    assert str(error) == "user not found"  # Exception.__init__(message) still works


def test_details_defaults_to_empty_dict_not_none():
    error = BadRequestError("bad")
    assert error.details == {}


def test_exception_chaining_preserved():
    original = ValueError("root cause")
    try:
        try:
            raise original
        except ValueError as exc:
            raise BadRequestError("wrapped") from exc
    except BadRequestError as error:
        assert error.__cause__ is original
