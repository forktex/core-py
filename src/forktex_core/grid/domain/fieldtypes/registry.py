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

"""The open field-type registry.

``grid_column.type_id`` is a free string validated here against the set of
registered handlers — so adding a type is a code-only change (register a
handler) with no enum and no migration. The built-ins are registered when
``forktex_core.grid.domain.fieldtypes`` is imported; extras (e.g. the ``vector`` handler
in the ``[space]`` package) register themselves on import.
"""

from __future__ import annotations

from forktex_core.grid.domain.fieldtypes.base import FieldTypeHandler

_REGISTRY: dict[str, FieldTypeHandler] = {}


class UnknownFieldType(KeyError):
    """Raised when a ``type_id`` has no registered handler."""


def register_field_type(handler: FieldTypeHandler, *, replace: bool = False) -> None:
    """Register a handler under its ``type_id``.

    Rejects duplicate registration unless ``replace=True`` (used by extras
    that swap a richer handler in for a built-in, e.g. a storage-backed file
    type over a bare string one).
    """
    type_id = handler.type_id
    if not replace and type_id in _REGISTRY:
        raise ValueError(f"Field type {type_id!r} is already registered")
    _REGISTRY[type_id] = handler


def get_field_type(type_id: str) -> FieldTypeHandler:
    """Return the handler for ``type_id`` or raise :class:`UnknownFieldType`."""
    try:
        return _REGISTRY[type_id]
    except KeyError:
        raise UnknownFieldType(type_id) from None


def is_registered(type_id: str) -> bool:
    return type_id in _REGISTRY


def all_field_types() -> dict[str, FieldTypeHandler]:
    """A copy of the registry keyed by ``type_id``."""
    return dict(_REGISTRY)


__all__ = [
    "UnknownFieldType",
    "all_field_types",
    "get_field_type",
    "is_registered",
    "register_field_type",
]
