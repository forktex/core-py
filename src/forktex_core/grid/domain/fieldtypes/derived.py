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

"""The ``derived`` field type — a read-only, query-time computed projection.

Derived columns are never stored and never ingested: they are resolved by the
query layer (:func:`forktex_core.grid.read.derived.resolve_derived`). The handler
therefore rejects writes and has no payload cast or promoted type.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel

from forktex_core.grid.domain.enums import FieldType
from forktex_core.grid.domain.fieldtypes.base import Capabilities, CellValue, EmptyConfig, FieldTypeHandler
from forktex_core.types import JsonValue


class DerivedType(FieldTypeHandler):
    type_id = FieldType.derived.value
    config_model = EmptyConfig
    capabilities = Capabilities(
        filterable=False,
        sortable=False,
        filter_ops=frozenset(),
        index_kinds=frozenset(),
        default_index_kind=None,
    )

    def normalize(self, value: object, *, config: BaseModel) -> JsonValue:
        raise ValueError("derived columns are read-only and cannot be written")

    def to_cell(self, value: JsonValue, *, config: BaseModel) -> CellValue:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    def from_cell(self, cell: CellValue, *, config: BaseModel) -> JsonValue:
        raise ValueError("derived columns cannot be ingested")

    def sql_cast(self, text_expr: sa.ColumnElement[Any]) -> sa.ColumnElement[Any]:
        raise NotImplementedError("derived columns are resolved at query time, not from payload")

    def promoted_type(self, *, config: BaseModel) -> sa.types.TypeEngine[Any]:
        raise NotImplementedError("derived columns are never stored and cannot be promoted")


__all__ = ["DerivedType"]
