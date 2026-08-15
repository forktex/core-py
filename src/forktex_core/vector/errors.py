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

"""Vector module errors.

``AppError`` subclasses, so an HTTP transport renders them with a real
status (404 for a missing collection, 422 for a bad dimension) instead of
a masked 500 — see ``forktex_core.error``.
"""

from __future__ import annotations

from forktex_core.error import AppError, AppErrorCode


class VectorError(AppError):
    """Base class for vector module errors."""

    code = AppErrorCode.INTERNAL


class CollectionNotFoundError(VectorError):
    """Raised when a referenced collection does not exist in Qdrant."""

    code = AppErrorCode.NOT_FOUND


class DimensionMismatchError(VectorError):
    """Raised when an upserted vector's dimension doesn't match the collection.

    ``VALIDATION`` — the input is well-formed but semantically wrong for
    this collection, the same distinction ``UnprocessableEntityError``
    draws against ``BadRequestError``.
    """

    code = AppErrorCode.VALIDATION


__all__ = ["CollectionNotFoundError", "DimensionMismatchError", "VectorError"]
