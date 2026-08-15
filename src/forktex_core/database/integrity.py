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

"""Translate database constraint violations into typed ``AppError``s.

One implementation, replacing two that disagreed: ``crud.create`` mapped
*every* ``IntegrityError`` to ``ConflictError`` and leaked the raw driver
message into the user-facing error, while grid's own copy mapped
unique violations to ``AlreadyExistsError`` and everything else to
``BadRequestError`` with fixed messages.

Detection is by **SQLSTATE**, not by substring-matching the driver's message.
The old ``"unique" in str(exc).lower()`` test was locale- and driver-dependent;
the codes below are defined by the SQL standard and exposed by asyncpg.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import DBAPIError, IntegrityError

from forktex_core.error import AlreadyExistsError, BadRequestError
from forktex_core.log import get_logger

logger = get_logger(__name__)

# SQLSTATE class 23 — integrity constraint violation.
SQLSTATE_UNIQUE_VIOLATION = "23505"
SQLSTATE_FOREIGN_KEY_VIOLATION = "23503"
SQLSTATE_NOT_NULL_VIOLATION = "23502"
SQLSTATE_CHECK_VIOLATION = "23514"

# SQLSTATE class 22 — data exception (e.g. a stored value that will not cast to
# the type a query asks for).
_SQLSTATE_DATA_EXCEPTION_CLASS = "22"


def _sqlstate(exc: BaseException) -> str:
    """The driver's SQLSTATE for ``exc``, or ``""`` when unavailable."""
    return str(getattr(getattr(exc, "orig", None), "sqlstate", "") or "")


@asynccontextmanager
async def integrity_boundary() -> AsyncIterator[None]:
    """Translate an ``IntegrityError`` into a typed ``AppError``.

    - unique violation → :class:`AlreadyExistsError` (the row already exists)
    - foreign key / not-null / check violation → :class:`BadRequestError`

    Messages are fixed and non-leaking: a driver message can quote the offending
    values, which may be user data or otherwise sensitive, so it is chained via
    ``__cause__`` for the logs rather than surfaced to the caller.

    Rollback stays the caller's responsibility — this only maps the exception.
    """
    try:
        yield
    except IntegrityError as exc:
        state = _sqlstate(exc)
        logger.debug("integrity violation", extra={"sqlstate": state or "unknown"})
        if state == SQLSTATE_UNIQUE_VIOLATION:
            raise AlreadyExistsError("resource already exists") from exc
        if state == SQLSTATE_FOREIGN_KEY_VIOLATION:
            raise BadRequestError("write references a row that does not exist") from exc
        if state == SQLSTATE_NOT_NULL_VIOLATION:
            raise BadRequestError("a required value is missing") from exc
        if state == SQLSTATE_CHECK_VIOLATION:
            raise BadRequestError("write violates a database check constraint") from exc
        if not state:
            # No SQLSTATE (an unusual driver, or a SQLAlchemy-level
            # IntegrityError with no orig). Fall back to the pre-SQLSTATE
            # substring heuristic rather than mislabelling it.
            detail = str(getattr(exc, "orig", None) or exc).lower()
            if "unique" in detail or "duplicate key" in detail:
                raise AlreadyExistsError("resource already exists") from exc
        raise BadRequestError("write violates a database constraint") from exc


@asynccontextmanager
async def read_boundary() -> AsyncIterator[None]:
    """Translate a read-time data exception into :class:`BadRequestError`.

    A stored value that cannot be cast to the type a query compares against is
    the caller's problem (they asked for an incompatible comparison), not a 500.
    Anything outside SQLSTATE class ``22`` re-raises untouched.
    """
    try:
        yield
    except DBAPIError as exc:
        if _sqlstate(exc).startswith(_SQLSTATE_DATA_EXCEPTION_CLASS):
            raise BadRequestError("a stored value is not valid for its column's declared type") from exc
        raise


__all__ = [
    "SQLSTATE_CHECK_VIOLATION",
    "SQLSTATE_FOREIGN_KEY_VIOLATION",
    "SQLSTATE_NOT_NULL_VIOLATION",
    "SQLSTATE_UNIQUE_VIOLATION",
    "integrity_boundary",
    "read_boundary",
]
