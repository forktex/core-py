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

"""The ``json`` field type — an opaque structured blob.

Opaque to the typed filter/sort path (it declares no filter operators). In a
tabular cell it is carried as a JSON-encoded string.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel

from forktex_core.grid.domain.enums import FieldType
from forktex_core.grid.domain.fieldtypes.base import Capabilities, CellValue, EmptyConfig, FieldTypeHandler
from forktex_core.types import JsonValue


class JsonType(FieldTypeHandler):
    type_id = FieldType.json.value
    config_model = EmptyConfig
    capabilities = Capabilities(
        filterable=False,
        sortable=False,
        filter_ops=frozenset(),
        index_kinds=frozenset(),
        default_index_kind=None,
    )

    def normalize(self, value: object, *, config: BaseModel) -> JsonValue:
        if value is None:
            return None
        try:
            # allow_nan=False rejects NaN/Infinity, which are invalid JSON and
            # would otherwise be written verbatim and rejected later by JSONB.
            encoded = json.dumps(value, allow_nan=False)
        except TypeError, ValueError:
            raise ValueError("value is not JSON-serializable") from None
        # Return the *decoded* value rather than the input. The round-trip is
        # what proves the value is JSON, and it also canonicalises containers
        # JSON has no distinct form for — a tuple used to be stored as an array
        # and read back as a list, so the canonical form differed by direction.
        return json.loads(encoded)

    def to_cell(self, value: JsonValue, *, config: BaseModel) -> CellValue:
        return None if value is None else json.dumps(value, separators=(",", ":"))

    def from_cell(self, cell: CellValue, *, config: BaseModel) -> JsonValue:
        if cell is None:
            return None
        if isinstance(cell, str):
            try:
                return json.loads(cell)
            except json.JSONDecodeError:
                raise ValueError("cell is not valid JSON") from None
        return self.normalize(cell, config=config)

    def promoted_type(self, *, config: BaseModel) -> sa.types.TypeEngine[Any]:
        from sqlalchemy.dialects.postgresql import JSONB

        return JSONB()


__all__ = ["JsonType"]
