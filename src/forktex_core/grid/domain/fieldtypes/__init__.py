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

"""The open field-type registry — the strategy boundary the rest of the design imitates.

Importing this package registers the built-in handlers exactly once. Extras (e.g. the
``vector`` handler in the ``[space]`` package) register themselves on their own import
via :func:`register_field_type`.

This package also owns the single *promotability* decision — "can a value of this type
be mirrored to a native sidecar column?" — via :func:`is_promotable`.
"""

from __future__ import annotations

from forktex_core.grid.domain.enums import PROMOTABLE_EXCLUDED
from forktex_core.grid.domain.fieldtypes.base import (
    Capabilities,
    CellValue,
    EmptyConfig,
    FieldTypeHandler,
    FilterOp,
    WriteContext,
    effective_capabilities,
)
from forktex_core.grid.domain.fieldtypes.boolean import BooleanType
from forktex_core.grid.domain.fieldtypes.choice import EnumType
from forktex_core.grid.domain.fieldtypes.derived import DerivedType
from forktex_core.grid.domain.fieldtypes.identifier import UUIDType
from forktex_core.grid.domain.fieldtypes.json_type import JsonType
from forktex_core.grid.domain.fieldtypes.numeric import DecimalType, IntegerType
from forktex_core.grid.domain.fieldtypes.reference import RefType
from forktex_core.grid.domain.fieldtypes.registry import (
    UnknownFieldType,
    all_field_types,
    get_field_type,
    is_registered,
    register_field_type,
)
from forktex_core.grid.domain.fieldtypes.temporal import DateTimeType, DateType
from forktex_core.grid.domain.fieldtypes.text import TextType

# The canonical built-in handlers, registered in a stable order.
_BUILTINS: tuple[FieldTypeHandler, ...] = (
    TextType(),
    IntegerType(),
    DecimalType(),
    BooleanType(),
    DateType(),
    DateTimeType(),
    UUIDType(),
    EnumType(),
    JsonType(),
    RefType(),
    DerivedType(),
)

for _handler in _BUILTINS:
    if not is_registered(_handler.type_id):
        register_field_type(_handler)


def is_promotable(type_id: str) -> bool:
    """Whether a value of ``type_id`` can be mirrored to a native sidecar column."""
    return type_id not in PROMOTABLE_EXCLUDED


__all__ = [
    "BooleanType",
    "Capabilities",
    "CellValue",
    "DateTimeType",
    "DateType",
    "DecimalType",
    "DerivedType",
    "EmptyConfig",
    "EnumType",
    "FieldTypeHandler",
    "FilterOp",
    "IntegerType",
    "JsonType",
    "RefType",
    "TextType",
    "UUIDType",
    "UnknownFieldType",
    "WriteContext",
    "all_field_types",
    "effective_capabilities",
    "get_field_type",
    "is_promotable",
    "is_registered",
    "register_field_type",
]
