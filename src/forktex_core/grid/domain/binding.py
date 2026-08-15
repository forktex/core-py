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

"""Typed binding to a pre-existing host table, discriminated by ``kind``.

The collision this avoids (bound *and* extension both storing a ``binding`` dict, opposite
ownership) is resolved by two distinct types:

- :class:`Overlay` — the host table is presented as a **read-only** grid entity
  (its rows are the host's rows). Selects ``BoundOverlayStorage``.
- :class:`Extension` — pure metadata linking an **owned** grid table's rows 1:1
  to host rows by ``external_ref``. Storage stays ``OwnedStorage`` — the binding does
  not change how rows are stored, only records what they extend.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from forktex_core.grid.domain.enums import Ownership
from forktex_core.grid.identifiers import validate_ident, validate_relation


class Overlay(BaseModel):
    """Read-only overlay of an existing physical table."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["overlay"] = "overlay"
    physical_relation: str
    primary_key: str = "id"
    namespace_column: str | None = None
    column_map: dict[str, str] = Field(default_factory=dict)
    # Reflected host ``udt_name`` per column, cached at bind time (filled by persistence).
    column_types: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> Overlay:
        validate_relation(self.physical_relation)
        validate_ident(self.primary_key, "primary_key")
        if self.namespace_column is not None:
            validate_ident(self.namespace_column, "namespace_column")
        for host_col in self.column_map.values():
            validate_ident(host_col, "column_map value")
        return self


class Extension(BaseModel):
    """Metadata: an owned table whose rows extend a host table's rows 1:1."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["extension"] = "extension"
    physical_relation: str
    primary_key: str = "id"

    @model_validator(mode="after")
    def _validate(self) -> Extension:
        validate_relation(self.physical_relation)
        validate_ident(self.primary_key, "primary_key")
        return self


# The discriminated union that a persisted/JSON binding parses into (``kind`` selects the arm).
BoundBinding = Annotated[Overlay | Extension, Field(discriminator="kind")]
Binding = Overlay | Extension | None

_BINDING_ADAPTER: TypeAdapter[Overlay | Extension] = TypeAdapter(BoundBinding)


def binding_to_json(binding: Binding) -> dict[str, Any] | None:
    """The persisted/JSON form of a binding (``None`` for an owned table)."""
    return None if binding is None else binding.model_dump(mode="json")


def binding_from_json(data: Mapping[str, object] | None) -> Binding:
    """Rehydrate a binding from its persisted/JSON form; the inverse of :func:`binding_to_json`."""
    if not data:
        return None
    return _BINDING_ADAPTER.validate_python(data)


def ownership_of(binding: Binding) -> Ownership:
    """An overlay is a read-only bound table; everything else (owned, or an extension's
    metadata binding) is owned."""
    return Ownership.bound if isinstance(binding, Overlay) else Ownership.owned


__all__ = [
    "Binding",
    "BoundBinding",
    "Extension",
    "Overlay",
    "binding_from_json",
    "binding_to_json",
    "ownership_of",
]
