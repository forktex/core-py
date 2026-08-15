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

"""The ``Table`` aggregate root and its ``Column``s — where invariants live ONCE.

A ``Table``/``Column`` cannot be constructed in an illegal configuration: the
constructors resolve the storage + materialization strategies and run every invariant.
There is exactly one place a bad shape can be born, so ``create`` and ``alter`` (which
both build these aggregates) cannot drift the way a procedural ``create_column`` /
``alter_column`` did. The ORM + DB CHECKs remain as defence-in-depth (asserted equal to
these invariants by a conformance test), not as an independent second specification.
"""

from __future__ import annotations

from forktex_core.grid.domain.fieldtypes import FieldTypeHandler, UnknownFieldType, get_field_type
from forktex_core.grid.domain.materialization import MaterializationStrategy, select_materialization
from forktex_core.grid.domain.spec import ColumnSpec, TableSpec
from forktex_core.grid.domain.storage import StorageStrategy, select_storage
from forktex_core.grid.errors import BadRequestError


class Column:
    """A validated column: its spec, its type handler, and its value strategy."""

    __slots__ = ("handler", "spec", "value")

    def __init__(self, spec: ColumnSpec) -> None:
        self.spec = spec
        self.handler = _handler_for(spec)
        # select_materialization enforces the registry-aware invariant (promotability).
        self.value: MaterializationStrategy = select_materialization(spec)

    @property
    def key(self) -> str:
        return self.spec.key


class Table:
    """The aggregate root: a table with its columns, storage strategy, and invariants."""

    __slots__ = ("columns", "spec", "storage")

    def __init__(self, spec: TableSpec) -> None:
        self.spec = spec
        self.storage: StorageStrategy = select_storage(spec.binding)
        self.columns: tuple[Column, ...] = tuple(Column(cs) for cs in spec.columns)
        # The storage strategy is the one authority on which columns it can back
        # (an overlay refuses ref/derived/promoted). No `if ownership == ...` here.
        for col in self.columns:
            self.storage.accept_column(col.spec)

    @classmethod
    def declare(cls, spec: TableSpec) -> Table:
        return cls(spec)

    @property
    def writable(self) -> bool:
        return self.storage.writable

    def column(self, key: str) -> Column:
        for col in self.columns:
            if col.key == key:
                return col
        raise BadRequestError(f"unknown column '{key}'")


def _handler_for(spec: ColumnSpec) -> FieldTypeHandler:
    try:
        return get_field_type(spec.type_id)
    except UnknownFieldType:
        raise BadRequestError(f"unknown field type '{spec.type_id}'") from None


__all__ = ["Column", "Table"]
