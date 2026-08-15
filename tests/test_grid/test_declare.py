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

"""The decorator front-door — declared classes compile to the same IR the dynamic path uses."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid.declare import Column, Registry, field_type
from forktex_core.grid.domain.fieldtypes import FieldTypeHandler, get_field_type, is_registered
from forktex_core.grid.domain.fieldtypes.text import TextType
from forktex_core.grid.namespace import Namespace

NS = "hr"


def _registry() -> Registry:
    reg = Registry()

    @reg.table("people")
    class People:  # noqa: D401
        name = Column("text", required=True)
        age = Column("integer")

    @reg.table("team", label="Team")
    class Team:
        name = Column("text")

    return reg


def test_decorator_compiles_to_tablespec() -> None:
    schema = _registry().schema(NS)
    assert set(schema.tables) == {"people", "team"}
    people = schema.tables["people"]
    assert [c.key for c in people.columns] == ["name", "age"]  # class-definition order preserved
    name = people.columns[0]
    assert name.type_id == "text" and name.is_required and name.label == "Name"
    assert schema.tables["team"].label == "Team"


async def test_apply_registered_reconciles_through_the_engine(session: AsyncSession) -> None:
    reg = _registry()
    report = await reg.apply(Namespace(session, NS), prune=True)
    assert report["plan"]["changes"]

    # the tables are really there — decorated declaration == dynamic apply
    described = await Namespace(session, NS).describe()
    assert set(described.tables) == {"people", "team"}
    assert {c.key for c in described.tables["people"].columns} == {"name", "age"}


def test_field_type_decorator_registers_a_custom_type() -> None:
    @field_type
    class Money(TextType):  # a trivial custom type reusing text behaviour
        type_id = "money_decl_test"

    assert is_registered("money_decl_test")
    assert isinstance(get_field_type("money_decl_test"), FieldTypeHandler)
