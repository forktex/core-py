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

"""The ``boolean`` field type (the ``checkbox`` render-hint maps here)."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel

from forktex_core.grid.domain.enums import FieldType
from forktex_core.grid.domain.fieldtypes.base import Capabilities, CellValue, EmptyConfig, FieldTypeHandler, FilterOp
from forktex_core.types import JsonValue

_TRUE = {"true", "t", "1", "yes", "y", "on"}
_FALSE = {"false", "f", "0", "no", "n", "off"}


class BooleanType(FieldTypeHandler):
    type_id = FieldType.boolean.value
    config_model = EmptyConfig
    pg_cast = "boolean"
    capabilities = Capabilities(
        filterable=True,
        sortable=True,
        filter_ops=frozenset({FilterOp.eq, FilterOp.ne, FilterOp.is_null}),
        index_kinds=frozenset({"btree"}),
        default_index_kind="btree",
    )

    def normalize(self, value: object, *, config: BaseModel) -> JsonValue:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            token = value.strip().lower()
            if token in _TRUE:
                return True
            if token in _FALSE:
                return False
        raise ValueError(f"{value!r} is not a boolean")

    def to_cell(self, value: JsonValue, *, config: BaseModel) -> CellValue:
        # `normalize` only ever produces a scalar for this type, so a container
        # here means something wrote a raw non-boolean straight into payload.
        # Surfacing it beats emitting a list/dict where a cell is expected.
        if isinstance(value, list | dict):
            raise ValueError("{value!r} is not a boolean cell")
        return value

    def from_cell(self, cell: CellValue, *, config: BaseModel) -> JsonValue:
        return None if cell is None else self.normalize(cell, config=config)

    def promoted_type(self, *, config: BaseModel) -> sa.types.TypeEngine[Any]:
        return sa.Boolean()


__all__ = ["BooleanType"]
