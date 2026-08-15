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

"""The ``date`` and ``datetime`` field types — stored as canonical ISO 8601."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel

from forktex_core.grid.domain.enums import FieldType
from forktex_core.grid.domain.fieldtypes.base import Capabilities, CellValue, EmptyConfig, FieldTypeHandler, FilterOp
from forktex_core.iso import from_date_iso, from_iso, to_date_iso, to_iso
from forktex_core.types import JsonValue

_TEMPORAL_OPS = frozenset(
    {
        FilterOp.eq,
        FilterOp.ne,
        FilterOp.lt,
        FilterOp.lte,
        FilterOp.gt,
        FilterOp.gte,
        FilterOp.between,
        FilterOp.is_null,
    }
)


class DateType(FieldTypeHandler):
    type_id = FieldType.date.value
    config_model = EmptyConfig
    # Stored/compared as canonical ISO ``YYYY-MM-DD`` text: lexicographic order
    # equals chronological order, and (unlike ``text::date``) it is IMMUTABLE so
    # it can back an index. So ``pg_cast`` stays None (text).
    capabilities = Capabilities(
        filterable=True,
        sortable=True,
        filter_ops=_TEMPORAL_OPS,
        index_kinds=frozenset({"btree"}),
        default_index_kind="btree",
    )

    def normalize(self, value: object, *, config: BaseModel) -> JsonValue:
        if value is None:
            return None
        if isinstance(value, datetime):
            return to_date_iso(value.date())
        if isinstance(value, date):
            return to_date_iso(value)
        if isinstance(value, str):
            # Parse through `iso` too, not just format through it — `iso` is
            # the single place that decides how ISO text maps to a date.
            return to_date_iso(from_date_iso(value))
        raise ValueError(f"{value!r} is not a date")

    def to_cell(self, value: JsonValue, *, config: BaseModel) -> CellValue:
        return None if value is None else str(value)

    def from_cell(self, cell: CellValue, *, config: BaseModel) -> JsonValue:
        return None if cell is None else self.normalize(cell, config=config)

    def promoted_type(self, *, config: BaseModel) -> sa.types.TypeEngine[Any]:
        return sa.Date()


class DateTimeType(FieldTypeHandler):
    type_id = FieldType.datetime.value
    config_model = EmptyConfig
    # Stored/compared as canonical UTC ISO 8601 text (naive inputs are assumed
    # UTC). Normalizing to a single offset makes lexicographic order equal
    # chronological order, so a plain (IMMUTABLE) text btree index accelerates
    # range/sort — a ``text::timestamptz`` cast is not immutable and cannot be
    # indexed. So ``pg_cast`` stays None (text).
    capabilities = Capabilities(
        filterable=True,
        sortable=True,
        filter_ops=_TEMPORAL_OPS,
        index_kinds=frozenset({"btree"}),
        default_index_kind="btree",
    )

    def normalize(self, value: object, *, config: BaseModel) -> JsonValue:
        if value is None:
            return None
        if isinstance(value, str):
            # `from_iso` normalizes to UTC on the way in, matching what
            # `to_iso` does on the way out — a bare `datetime.fromisoformat`
            # left an offset-less string naive and skipped that step.
            value = from_iso(value)
        if isinstance(value, datetime):
            return to_iso(value)
        raise ValueError(f"{value!r} is not a datetime")

    def to_cell(self, value: JsonValue, *, config: BaseModel) -> CellValue:
        return None if value is None else str(value)

    def from_cell(self, cell: CellValue, *, config: BaseModel) -> JsonValue:
        return None if cell is None else self.normalize(cell, config=config)

    def promoted_type(self, *, config: BaseModel) -> sa.types.TypeEngine[Any]:
        return sa.DateTime(timezone=True)


__all__ = ["DateTimeType", "DateType"]
