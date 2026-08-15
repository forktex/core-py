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

"""Built-in field-type handlers: codecs, capabilities, casts, integrity."""

from __future__ import annotations

import sqlalchemy as sa
import pytest
from pydantic import ValidationError

from forktex_core.grid.domain.fieldtypes import (
    BooleanType,
    DateType,
    DateTimeType,
    DecimalType,
    DerivedType,
    EnumType,
    FieldTypeHandler,
    IntegerType,
    JsonType,
    RefType,
    TextType,
    UUIDType,
    effective_capabilities,
    get_field_type,
)

_UUID = "12345678-1234-5678-1234-567812345678"

# (handler, raw_config, input_value, canonical_stored_value)
ROUND_TRIP = [
    (TextType(), {}, "hello", "hello"),
    (TextType(), {"max_length": 10}, "hi", "hi"),
    (IntegerType(), {}, 42, 42),
    (IntegerType(), {}, "42", 42),
    (IntegerType(), {}, 7.0, 7),
    (DecimalType(), {}, "9.99", "9.99"),
    (DecimalType(), {}, 3, "3"),
    (BooleanType(), {}, "true", True),
    (BooleanType(), {}, 0, False),
    (DateType(), {}, "2026-01-02", "2026-01-02"),
    # Naive datetimes are canonicalized to UTC so ISO text sorts chronologically.
    (DateTimeType(), {}, "2026-01-02T03:04:05", "2026-01-02T03:04:05+00:00"),
    (DateTimeType(), {}, "2026-01-02T05:04:05+02:00", "2026-01-02T03:04:05+00:00"),
    (UUIDType(), {}, _UUID, _UUID),
    (EnumType(), {"options": ["a", "b"]}, "a", "a"),
    (JsonType(), {}, {"k": 1}, {"k": 1}),
    (RefType(), {}, _UUID, _UUID),
]


@pytest.mark.parametrize("handler, raw, value, canonical", ROUND_TRIP)
def test_normalize_and_cell_round_trip(handler, raw, value, canonical) -> None:
    config = handler.validate_config(raw)
    assert handler.normalize(value, config=config) == canonical
    cell = handler.to_cell(canonical, config=config)
    assert handler.from_cell(cell, config=config) == canonical


@pytest.mark.parametrize("handler", [t for t in (TextType(), IntegerType(), DecimalType(), BooleanType(), JsonType())])
def test_none_passes_through(handler: FieldTypeHandler) -> None:
    config = handler.validate_config({})
    assert handler.normalize(None, config=config) is None
    assert handler.to_cell(None, config=config) is None
    assert handler.from_cell(None, config=config) is None


INVALID = [
    (IntegerType(), {}, "abc"),
    (IntegerType(), {}, 1.5),
    (IntegerType(), {}, True),
    (DecimalType(), {}, "x"),
    (BooleanType(), {}, "maybe"),
    (DateType(), {}, "not-a-date"),
    (UUIDType(), {}, "nope"),
    (EnumType(), {"options": ["a", "b"]}, "z"),
    (TextType(), {"max_length": 2}, "toolong"),
]


@pytest.mark.parametrize("handler, raw, value", INVALID)
def test_invalid_values_rejected(handler, raw, value) -> None:
    config = handler.validate_config(raw)
    with pytest.raises(ValueError):
        handler.normalize(value, config=config)


def test_capabilities_are_type_intrinsic() -> None:
    assert TextType().capabilities.fuzzy is True
    assert IntegerType().capabilities.sortable is True
    assert IntegerType().capabilities.fuzzy is False
    assert UUIDType().capabilities.sortable is False
    assert JsonType().capabilities.filterable is False
    assert DerivedType().capabilities.filterable is False
    # cursor-browse is derived from a deterministic sort order
    assert TextType().capabilities.cursor_browsable is True
    assert UUIDType().capabilities.cursor_browsable is False


def test_effective_capabilities_only_narrow() -> None:
    text = TextType()
    narrowed = effective_capabilities(text, {"fuzzy": False, "filterable": True})
    assert narrowed.fuzzy is False
    assert narrowed.filterable is True
    # A column cannot widen a capability the type lacks.
    widened = effective_capabilities(JsonType(), {"filterable": True})
    assert widened.filterable is False


def test_decimal_config_validation() -> None:
    cfg = DecimalType().validate_config({"precision": 8, "scale": 2})
    assert (cfg.precision, cfg.scale) == (8, 2)  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        DecimalType().validate_config({"precision": 2, "scale": 5})


def test_enum_requires_options() -> None:
    with pytest.raises(ValidationError):
        EnumType().validate_config({})


@pytest.mark.parametrize(
    "handler",
    [TextType(), IntegerType(), DecimalType(), BooleanType(), DateType(), DateTimeType(), UUIDType(), RefType()],
)
def test_sql_cast_and_promoted_type(handler: FieldTypeHandler) -> None:
    config = handler.validate_config({"options": ["a"]} if isinstance(handler, EnumType) else {})
    expr = handler.sql_cast(sa.literal_column("data ->> 'k'"))
    assert isinstance(expr, sa.ColumnElement)
    assert isinstance(handler.promoted_type(config=config), sa.types.TypeEngine)


def test_decimal_promoted_type_honours_config() -> None:
    cfg = DecimalType().validate_config({"precision": 12, "scale": 4})
    promoted = DecimalType().promoted_type(config=cfg)
    assert isinstance(promoted, sa.Numeric)
    assert (promoted.precision, promoted.scale) == (12, 4)


def test_derived_is_read_only() -> None:
    derived = DerivedType()
    config = derived.validate_config({})
    with pytest.raises(ValueError):
        derived.normalize("x", config=config)
    with pytest.raises(ValueError):
        derived.from_cell("x", config=config)
    with pytest.raises(NotImplementedError):
        derived.promoted_type(config=config)


def test_registry_handlers_match_classes() -> None:
    assert isinstance(get_field_type("decimal"), DecimalType)
    assert isinstance(get_field_type("ref"), RefType)
