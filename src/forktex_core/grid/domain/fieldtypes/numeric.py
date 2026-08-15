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

"""The ``integer`` and ``decimal`` numeric field types.

``integer`` stores a native int; ``decimal`` stores a canonical
Decimal-as-string so arbitrary precision survives the JSONB round-trip
(the per-column precision/scale is honoured by the SQL cast).
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, model_validator

from forktex_core.grid.domain.enums import FieldType
from forktex_core.grid.domain.fieldtypes.base import Capabilities, CellValue, EmptyConfig, FieldTypeHandler, FilterOp
from forktex_core.types import JsonValue

_NUMERIC_OPS = frozenset(
    {
        FilterOp.eq,
        FilterOp.ne,
        FilterOp.lt,
        FilterOp.lte,
        FilterOp.gt,
        FilterOp.gte,
        FilterOp.in_,
        FilterOp.not_in,
        FilterOp.between,
        FilterOp.is_null,
    }
)


class IntegerType(FieldTypeHandler):
    type_id = FieldType.integer.value
    config_model = EmptyConfig
    pg_cast = "bigint"
    capabilities = Capabilities(
        filterable=True,
        sortable=True,
        filter_ops=_NUMERIC_OPS,
        index_kinds=frozenset({"btree"}),
        default_index_kind="btree",
    )

    def normalize(self, value: object, *, config: BaseModel) -> JsonValue:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("boolean is not a valid integer")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("NaN/Infinity is not a valid integer")
            if value != int(value):
                raise ValueError("value is not an integer")
            return int(value)
        try:
            return int(str(value).strip())
        except TypeError, ValueError:
            raise ValueError(f"{value!r} is not an integer") from None

    def to_cell(self, value: JsonValue, *, config: BaseModel) -> CellValue:
        # `normalize` only ever produces a scalar for this type, so a container
        # here means something wrote a raw non-numeric straight into payload.
        # Surfacing it beats emitting a list/dict where a cell is expected.
        if isinstance(value, list | dict):
            raise ValueError("{value!r} is not a numeric cell")
        return value

    def from_cell(self, cell: CellValue, *, config: BaseModel) -> JsonValue:
        return None if cell is None else self.normalize(cell, config=config)

    def promoted_type(self, *, config: BaseModel) -> sa.types.TypeEngine[Any]:
        return sa.BigInteger()


class DecimalConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    precision: int = 18
    scale: int = 6

    @model_validator(mode="after")
    def _check(self) -> DecimalConfig:
        if self.precision < 1:
            raise ValueError("precision must be >= 1")
        if not 0 <= self.scale <= self.precision:
            raise ValueError("scale must be between 0 and precision")
        return self


class DecimalType(FieldTypeHandler):
    type_id = FieldType.decimal.value
    config_model = DecimalConfig
    pg_cast = "numeric"
    capabilities = Capabilities(
        filterable=True,
        sortable=True,
        filter_ops=_NUMERIC_OPS,
        index_kinds=frozenset({"btree"}),
        default_index_kind="btree",
    )

    def normalize(self, value: object, *, config: BaseModel) -> JsonValue:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("boolean is not a valid decimal")
        try:
            parsed = Decimal(str(value))
        except InvalidOperation, ValueError:
            raise ValueError(f"{value!r} is not a decimal") from None
        if not parsed.is_finite():
            raise ValueError("NaN/Infinity is not a valid decimal")
        # Reject values that don't fit the declared precision/scale, so the payload
        # never carries more precision than the promoted Numeric(p,s) sidecar can
        # hold (no silent rounding / divergence) and overflow is a clean 400 rather
        # than a raw DataError 500 at the sidecar cast.
        assert isinstance(config, DecimalConfig)
        _, digits, exponent = parsed.normalize().as_tuple()
        if not isinstance(exponent, int):  # 'n'/'N'/'F' — already excluded by is_finite
            raise ValueError("not a finite decimal")
        frac_digits = -exponent if exponent < 0 else 0
        if frac_digits > config.scale:
            raise ValueError(f"decimal has more than {config.scale} fractional digits")
        int_digits = max(len(digits) + exponent, 1)
        if int_digits > config.precision - config.scale:
            raise ValueError(f"decimal exceeds precision {config.precision} (scale {config.scale})")
        return str(parsed)

    def to_cell(self, value: JsonValue, *, config: BaseModel) -> CellValue:
        # Lossless: keep the canonical string representation.
        return None if value is None else str(value)

    def from_cell(self, cell: CellValue, *, config: BaseModel) -> JsonValue:
        return None if cell is None else self.normalize(cell, config=config)

    def promoted_type(self, *, config: BaseModel) -> sa.types.TypeEngine[Any]:
        assert isinstance(config, DecimalConfig)
        return sa.Numeric(config.precision, config.scale)


__all__ = ["DecimalConfig", "DecimalType", "IntegerType"]
