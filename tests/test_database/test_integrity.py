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

"""Unit tests for forktex_core.database.integrity — no container required.

Constraint violations are simulated by constructing a SQLAlchemy
``IntegrityError`` whose ``orig`` carries a SQLSTATE, which is exactly the shape
asyncpg produces.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from forktex_core.database.integrity import (
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_FOREIGN_KEY_VIOLATION,
    SQLSTATE_NOT_NULL_VIOLATION,
    SQLSTATE_UNIQUE_VIOLATION,
    integrity_boundary,
    read_boundary,
)
from forktex_core.error import AlreadyExistsError, BadRequestError


class _Orig(Exception):
    """Stand-in for a driver exception carrying a SQLSTATE."""

    def __init__(self, sqlstate: str | None, message: str = "driver detail") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def _integrity_error(sqlstate: str | None, message: str = "driver detail") -> IntegrityError:
    return IntegrityError("INSERT ...", {}, _Orig(sqlstate, message))


@pytest.mark.asyncio
async def test_unique_violation_becomes_already_exists():
    with pytest.raises(AlreadyExistsError) as exc_info:
        async with integrity_boundary():
            raise _integrity_error(SQLSTATE_UNIQUE_VIOLATION)
    assert exc_info.value.code == "already_exists"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sqlstate",
    [SQLSTATE_FOREIGN_KEY_VIOLATION, SQLSTATE_NOT_NULL_VIOLATION, SQLSTATE_CHECK_VIOLATION],
)
async def test_other_constraint_violations_become_bad_request(sqlstate):
    with pytest.raises(BadRequestError):
        async with integrity_boundary():
            raise _integrity_error(sqlstate)


@pytest.mark.asyncio
async def test_driver_message_is_not_leaked_into_the_user_facing_error():
    """A driver message can quote the offending values — chain it via __cause__
    for the logs, never surface it. The old crud.create did `str(exc)`."""
    secret = "user@example.com already registered"
    with pytest.raises(AlreadyExistsError) as exc_info:
        async with integrity_boundary():
            raise _integrity_error(SQLSTATE_UNIQUE_VIOLATION, secret)
    assert secret not in str(exc_info.value)
    assert exc_info.value.message == "resource already exists"
    assert isinstance(exc_info.value.__cause__, IntegrityError)  # available for logs


@pytest.mark.asyncio
async def test_missing_sqlstate_falls_back_to_the_substring_heuristic():
    """An unusual driver may not expose a SQLSTATE; don't mislabel it."""
    with pytest.raises(AlreadyExistsError):
        async with integrity_boundary():
            raise _integrity_error(None, "duplicate key value violates unique constraint")

    with pytest.raises(BadRequestError):
        async with integrity_boundary():
            raise _integrity_error(None, "some other constraint problem")


@pytest.mark.asyncio
async def test_non_integrity_exceptions_pass_through_untouched():
    with pytest.raises(ValueError, match="unrelated"):
        async with integrity_boundary():
            raise ValueError("unrelated")


@pytest.mark.asyncio
async def test_read_boundary_maps_data_exceptions_only():
    """SQLSTATE class 22 (data exception) is the caller's problem — they asked
    for an incompatible comparison. Anything else re-raises untouched."""
    with pytest.raises(BadRequestError):
        async with read_boundary():
            raise DBAPIError("SELECT ...", {}, _Orig("22P02"))  # invalid text representation

    with pytest.raises(DBAPIError):
        async with read_boundary():
            raise DBAPIError("SELECT ...", {}, _Orig("42P01"))  # undefined table
