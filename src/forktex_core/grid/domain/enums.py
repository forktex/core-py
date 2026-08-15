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

"""The grid vocabulary — the small set of enums a caller actually names.

Deliberately small: ``Ownership`` is not here (it is the storage strategy,
selected from the binding); ``RelationDirection`` is gone (orientation is implied by
source/target); ``RelationType`` is renamed :class:`RelationShape` and carries the
endpoint-uniqueness rule so the app-check and the physical cardinality index read one
source of truth.
"""

from __future__ import annotations

import enum


class Cardinality(enum.StrEnum):
    one = "one"
    many = "many"


class Materialization(enum.StrEnum):
    """How a column's value is stored/read — resolved to a strategy object.

    ``payload`` — in the shared ``grid_row.payload`` JSONB (default, zero-DDL).
    ``promoted`` — mirrored to a native column in the table's sidecar.
    ``derived`` — not stored; computed read-side by projecting a related row's field.
    """

    payload = "payload"
    promoted = "promoted"
    derived = "derived"


class OnDelete(enum.StrEnum):
    restrict = "restrict"
    cascade = "cascade"
    set_null = "set_null"


class BrowseMode(enum.StrEnum):
    page = "page"  # offset/limit
    cursor = "cursor"  # keyset


class RelationShape(enum.StrEnum):
    """Relationship cardinality — with the endpoint-uniqueness rule attached."""

    one_to_one = "one_to_one"
    one_to_many = "one_to_many"
    many_to_one = "many_to_one"
    many_to_many = "many_to_many"

    def unique_endpoints(self) -> tuple[str, ...]:
        """Which endpoint(s) must be unique for this shape (source/target).

        The single source of truth read by BOTH the application-level cardinality
        check and the physical partial-unique edge index.
        """
        return {
            RelationShape.one_to_one: ("source", "target"),
            RelationShape.one_to_many: ("target",),  # each target belongs to one source
            RelationShape.many_to_one: ("source",),  # each source references one target
            RelationShape.many_to_many: (),
        }[self]

    @property
    def needs_through(self) -> bool:
        return self is RelationShape.many_to_many


class Ownership(enum.StrEnum):
    """How a table relates to its physical Postgres relation."""

    owned = "owned"
    bound = "bound"


class IndexState(enum.StrEnum):
    """Reconciliation state of a declared index against physical Postgres."""

    pending = "pending"
    building = "building"
    live = "live"
    invalid = "invalid"


class FieldType(enum.StrEnum):
    """Canonical built-in field-type ids (storage primitives).

    Not a closed set: ``grid_column.type_id`` is a free ``VARCHAR(64)`` validated against
    the handler registry, so consumers may register additional types. This enum seeds the
    registry with the built-ins and names them for readability.
    """

    text = "text"
    integer = "integer"
    decimal = "decimal"
    boolean = "boolean"
    date = "date"
    datetime = "datetime"
    uuid = "uuid"
    enum = "enum"
    json = "json"
    ref = "ref"
    vector = "vector"
    derived = "derived"


PROMOTABLE_EXCLUDED: frozenset[str] = frozenset(
    {FieldType.date.value, FieldType.datetime.value, FieldType.json.value, FieldType.ref.value, FieldType.derived.value}
)


__all__ = ["PROMOTABLE_EXCLUDED", "FieldType", "IndexState", "Ownership"]


__all__ = [
    "BrowseMode",
    "Cardinality",
    "FieldType",
    "IndexState",
    "Materialization",
    "OnDelete",
    "Ownership",
    "RelationShape",
]
