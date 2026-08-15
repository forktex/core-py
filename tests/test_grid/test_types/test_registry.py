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

"""The field-type registry seeds the built-ins and validates type ids."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import BaseModel

from forktex_core.grid.domain.enums import FieldType
from forktex_core.grid.domain.fieldtypes import (
    Capabilities,
    FieldTypeHandler,
    TextType,
    UnknownFieldType,
    all_field_types,
    get_field_type,
    is_registered,
    register_field_type,
)
from forktex_core.grid.domain.fieldtypes.base import CellValue

# Every built-in except ``vector`` (which ships in the [space] extra).
CORE_BUILTINS = {t.value for t in FieldType} - {"vector"}


def test_all_core_builtins_registered() -> None:
    registered = set(all_field_types())
    assert CORE_BUILTINS <= registered
    # ``vector`` is not seeded by [grid] itself — it ships in the [space] extra.
    # The registry is process-global, so if [space] has been imported elsewhere
    # (e.g. earlier in a full test run) ``vector`` will be present; when it is,
    # assert it is provided by a forktex_core.space handler, not core.
    if "vector" in registered:
        assert get_field_type("vector").__class__.__module__.startswith("forktex_core.space")


def test_get_field_type_returns_singleton() -> None:
    handler = get_field_type("text")
    assert isinstance(handler, TextType)
    assert handler.type_id == "text"


def test_unknown_type_raises() -> None:
    with pytest.raises(UnknownFieldType):
        get_field_type("does_not_exist")


def test_duplicate_registration_rejected() -> None:
    with pytest.raises(ValueError):
        register_field_type(TextType())  # 'text' already registered


def test_replace_and_custom_registration() -> None:
    class CustomType(FieldTypeHandler):
        type_id = "custom_demo"
        capabilities = Capabilities()

        def normalize(self, value: Any, *, config: BaseModel) -> Any:
            return value

        def to_cell(self, value: Any, *, config: BaseModel) -> CellValue:
            return value

        def from_cell(self, cell: CellValue, *, config: BaseModel) -> Any:
            return cell

        def sql_cast(self, text_expr: sa.ColumnElement[Any]) -> sa.ColumnElement[Any]:
            return text_expr

        def promoted_type(self, *, config: BaseModel) -> sa.types.TypeEngine[Any]:
            return sa.Text()

    assert not is_registered("custom_demo")
    register_field_type(CustomType())
    assert is_registered("custom_demo")
    with pytest.raises(ValueError):
        register_field_type(CustomType())
    register_field_type(CustomType(), replace=True)  # explicit replace is allowed
