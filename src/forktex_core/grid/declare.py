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

"""Decorator front-door — declare grid tables from classes (Spring/Hibernate/flow flavour).

OPTIONAL sugar, not a second interface: each decorator reads a declarative class, compiles it to
the *same* ``TableSpec`` / binding the dynamic path uses, registers it, and returns the class
unchanged. ``Registry.apply(space)`` reconciles every declared table through the one ``Namespace.apply``
engine — so decorated tables and runtime ``space.apply(...)`` changes converge identically.

    reg = Registry()

    @reg.table("people")
    class People:
        name = Column("text", required=True)
        age = Column("integer")

    @reg.extension("client_ext", "public.client_record")
    class ClientExt:
        tier = Column("text")

    @field_type(replace=True)
    class Money(FieldTypeHandler):
        type_id = "money"
        ...

    await reg.apply(Namespace(session, "org1"), prune=True)

``field_type`` registers a custom column *type* (behaviour) into the shared field-type registry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from forktex_core.grid.domain.binding import Extension, Overlay
from forktex_core.grid.domain.enums import Cardinality, Materialization
from forktex_core.grid.domain.fieldtypes import FieldTypeHandler, register_field_type
from forktex_core.grid.domain.schema import Schema
from forktex_core.grid.domain.spec import ColumnSpec, TableSpec
from forktex_core.grid.namespace import Namespace
from forktex_core.types import BaseValueObject

_T = TypeVar("_T", bound=type)


class Column(BaseValueObject):
    """A declarative column on a decorated class; its attribute name becomes the column key."""

    type_id: str
    label: str | None = None
    required: bool = False
    unique: bool = False
    cardinality: Cardinality = Cardinality.one
    materialization: Materialization = Materialization.payload
    ref: str | None = None  # relation key, for a `ref` column
    default: Any = None
    config: Mapping[str, Any] | None = None

    def __init__(self, type_id: str, **kwargs: object) -> None:
        """Accept ``type_id`` positionally — the DSL usage is ``Column("text", required=True)``."""
        super().__init__(type_id=type_id, **kwargs)  # type: ignore[call-arg]


def _table_spec(cls: type, slug: str, label: str | None, *, binding: Overlay | Extension | None) -> TableSpec:
    columns = tuple(
        ColumnSpec(
            key=name,
            label=col.label or name.replace("_", " ").title(),
            type_id=col.type_id,
            is_required=col.required,
            is_unique=col.unique,
            cardinality=col.cardinality,
            materialization=col.materialization,
            relation_ref=col.ref,
            default_value=col.default,
            config=dict(col.config or {}),
            display_order=order,
        )
        for order, (name, col) in enumerate((n, v) for n, v in vars(cls).items() if isinstance(v, Column))
    )
    return TableSpec(slug=slug, label=label or slug.replace("_", " ").title(), binding=binding, columns=columns)


class Registry:
    """Collects decorated table declarations, then applies them through a ``Namespace``."""

    def __init__(self) -> None:
        self._tables: dict[str, TableSpec] = {}

    def table(self, slug: str, *, label: str | None = None) -> Callable[[_T], _T]:
        """Declare an owned grid table from a class of :class:`Column` attributes."""

        def wrap(cls: _T) -> _T:
            self._tables[slug] = _table_spec(cls, slug, label, binding=None)
            return cls

        return wrap

    def overlay(
        self,
        slug: str,
        physical_relation: str,
        *,
        primary_key: str = "id",
        namespace_column: str | None = None,
        column_map: Mapping[str, str] | None = None,
        label: str | None = None,
    ) -> Callable[[_T], _T]:
        """Bind an existing physical table as a read-only overlay grid table."""
        binding = Overlay(
            physical_relation=physical_relation,
            primary_key=primary_key,
            namespace_column=namespace_column,
            column_map=dict(column_map or {}),
        )

        def wrap(cls: _T) -> _T:
            self._tables[slug] = _table_spec(cls, slug, label, binding=binding)
            return cls

        return wrap

    def extension(
        self,
        slug: str,
        physical_relation: str,
        *,
        primary_key: str = "id",
        label: str | None = None,
    ) -> Callable[[_T], _T]:
        """Declare an owned extension table whose rows link 1:1 to a host table's rows."""
        binding = Extension(physical_relation=physical_relation, primary_key=primary_key)

        def wrap(cls: _T) -> _T:
            self._tables[slug] = _table_spec(cls, slug, label, binding=binding)
            return cls

        return wrap

    def schema(self, namespace: str = "") -> Schema:
        """The registered tables as a :class:`Schema` (namespace-stamped)."""
        return Schema(
            namespace=namespace,
            tables={slug: spec.model_copy(update={"namespace": namespace}) for slug, spec in self._tables.items()},
        )

    async def apply(self, space: Namespace, *, prune: bool = False, allow_destructive: bool = False) -> dict[str, Any]:
        """Reconcile every registered table through ``Namespace.apply`` (the one engine)."""
        return await space.apply(self.schema(space.namespace), prune=prune, allow_destructive=allow_destructive)


def field_type(
    cls: type[FieldTypeHandler] | None = None, *, replace: bool = False
) -> type[FieldTypeHandler] | Callable[[type[FieldTypeHandler]], type[FieldTypeHandler]]:
    """Register a custom column-type handler (``@field_type`` or ``@field_type(replace=True)``).

    Returns the handler class when used bare, and a class decorator when used with
    arguments — the two shapes a parameterisable decorator has to support.
    """

    def wrap(handler_cls: type[FieldTypeHandler]) -> type[FieldTypeHandler]:
        register_field_type(handler_cls(), replace=replace)
        return handler_cls

    return wrap if cls is None else wrap(cls)


__all__ = ["Column", "Registry", "field_type"]
