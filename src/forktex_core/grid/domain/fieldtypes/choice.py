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

"""The ``enum`` field type — a value constrained to a configured option set.

Single- vs multi-select is the column's *cardinality*, not a separate type:
a ``many`` enum is a multi-select.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field

from forktex_core.grid.domain.enums import FieldType
from forktex_core.grid.domain.fieldtypes.base import Capabilities, CellValue, FieldTypeHandler, FilterOp
from forktex_core.types import JsonValue


class EnumConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    options: list[str] = Field(min_length=1)


class EnumType(FieldTypeHandler):
    type_id = FieldType.enum.value
    config_model = EnumConfig
    capabilities = Capabilities(
        filterable=True,
        sortable=True,
        filter_ops=frozenset({FilterOp.eq, FilterOp.ne, FilterOp.in_, FilterOp.not_in, FilterOp.is_null}),
        index_kinds=frozenset({"btree"}),
        default_index_kind="btree",
    )

    def normalize(self, value: object, *, config: BaseModel) -> JsonValue:
        if value is None:
            return None
        assert isinstance(config, EnumConfig)
        token = str(value)
        if token not in config.options:
            raise ValueError(f"{token!r} is not one of {config.options}")
        return token

    def to_cell(self, value: JsonValue, *, config: BaseModel) -> CellValue:
        return None if value is None else str(value)

    def from_cell(self, cell: CellValue, *, config: BaseModel) -> JsonValue:
        return None if cell is None else self.normalize(str(cell), config=config)

    def promoted_type(self, *, config: BaseModel) -> sa.types.TypeEngine[Any]:
        return sa.Text()


__all__ = ["EnumConfig", "EnumType"]
