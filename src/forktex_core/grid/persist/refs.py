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

"""``TableRef`` — pairs a persisted table's identity with its hydrated domain aggregate.

The domain :class:`~forktex_core.grid.domain.table.Table` is DB-free (it has no row id).
A ``TableRef`` carries the DB ``id`` + ``namespace`` alongside the aggregate so the
read/write layers have both the identity to query and the strategies to dispatch on.
"""

from __future__ import annotations

import uuid

from pydantic import ConfigDict

from forktex_core.grid.domain.table import Table
from forktex_core.types import BaseValueObject


class TableRef(BaseValueObject):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: uuid.UUID
    namespace: str
    domain: Table

    @property
    def writable(self) -> bool:
        return self.domain.writable

    @property
    def kind(self) -> str:
        return self.domain.storage.kind


__all__ = ["TableRef"]
