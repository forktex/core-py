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

"""The single savepoint seam for atomic multi-step writes.

Encapsulates the rule hardening taught: run the mutation inside a
``begin_nested()`` savepoint and roll it back on *any* exception, so a caller that
catches the error cannot commit a half-written row. Callers ``add``/mutate rows
*inside* the ``with`` block — ``begin_nested`` autoflushes pending state before opening
the savepoint, so anything staged before it would escape the rollback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.database.integrity import integrity_boundary


@asynccontextmanager
async def atomic(session: AsyncSession) -> AsyncIterator[None]:
    """A savepoint that rolls back on any error; DB constraint errors become typed."""
    async with integrity_boundary():
        sp = await session.begin_nested()
        try:
            yield
        except BaseException:
            await sp.rollback()
            raise
        await sp.commit()


__all__ = ["atomic"]
