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

"""Level-0 ``[types]`` extra — base Pydantic models + frozen value objects.

The wire-shape opinion lives in ``BaseAppModel`` (snake↔camel). The
domain-shape opinion lives in ``BaseValueObject`` (frozen, hashable,
equality on values not identity). Together they cover the two model
shapes consumer code hits hardest:

  - cross-boundary payloads → ``BaseAppModel``
  - immutable in-process values (UUIDs, currency amounts, percentages,
    timestamps) → ``BaseValueObject``

This is a Level-0 primitive: it depends only on ``forktex_core.iso`` (for
``UtcDateTime``'s canonical UTC serialization) — a Level-0 sibling, not a
facade or substrate module, same as ``forktex_core.log``'s dependency on
``iso``. Specific value types (``Currency``, ``Percentage``, etc.) ship as
separate modules under ``forktex_core.types.*`` once they're consolidated
from consumer code.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainSerializer
from pydantic.alias_generators import to_camel

from forktex_core.iso import to_date_iso, to_iso

#: A JSON scalar — the leaves of any JSON document.
type JsonScalar = str | int | float | bool | None

#: Any value that survives a JSON round-trip.
#:
#: The honest type for the many places this library moves caller-defined data
#: through a JSON/JSONB boundary — a grid cell's canonical form, a flow run's
#: state and metadata, a queue payload, an extension's config. Those were all
#: annotated ``Any``, which said "unknown" where the truth is "JSON-shaped":
#: a `dict[str, Any]` payload could hold a socket, and nothing would object.
#:
#: Use ``object`` instead where a value is genuinely arbitrary and only
#: inspected or passed through (e.g. what a caller hands to a normaliser
#: *before* it becomes JSON).
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

#: A JSON object — the common case of :data:`JsonValue` at a payload boundary.
type JsonObject = dict[str, JsonValue]

UtcDateTime = Annotated[datetime, PlainSerializer(to_iso, return_type=str)]
"""Datetime field type for ``BaseAppModel``/``BaseWireValueObject`` subclasses.

Pydantic's default ``datetime`` serialization emits a ``Z``-suffixed,
non-UTC-forced, offset-preserving string — it disagrees with every other
timestamp `forktex_core` produces (``log``'s JSON, ``grid``'s stored text,
``flow``'s cursors, ``database``'s JSON columns), all of which go through
``forktex_core.iso.to_iso()``. Use ``UtcDateTime`` instead of a plain
``datetime`` annotation on any wire-facing model field to get the same
canonical UTC text everywhere::

    class Invoice(BaseAppModel):
        created_at: UtcDateTime
"""

UtcDate = Annotated[date, PlainSerializer(to_date_iso, return_type=str)]
"""Date field type for ``BaseAppModel``/``BaseWireValueObject`` subclasses —
the ``date``-only counterpart of ``UtcDateTime``. Plain ``date`` fields
already serialize as ``YYYY-MM-DD`` by default in Pydantic (there's no
timezone/offset ambiguity to normalize the way there is for ``datetime``,
and Pydantic's own validator already rejects a ``datetime`` with a nonzero
time component passed where a ``date`` is expected), so ``UtcDate`` exists
for naming symmetry and consistency with ``UtcDateTime`` rather than to
fix a live bug::

    class Invoice(BaseAppModel):
        due_date: UtcDate
"""


class BaseAppModel(BaseModel):
    """App-wide model base: accept snake+camel input, emit camel aliases.

    The wire-shape opinion every forktex API surface inherits: snake_case
    in Python, camelCase on the wire, with both input shapes accepted so
    curl-from-the-shell and TypeScript clients can each use their native
    conventions. Use it for any model that crosses a service boundary
    (HTTP body, JSON storage, queue payload); internal-only domain models
    can use plain ``BaseModel``.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        # Without this, the camelCase promise held only for callers that
        # remembered `by_alias=True` — so a plain `model_dump()` emitted
        # snake_case and contradicted the error envelope on the same wire.
        serialize_by_alias=True,
    )


class BaseValueObject(BaseModel):
    """Immutable, hashable value object base.

    Pydantic-validated like ``BaseModel`` but with ``frozen=True`` so
    instances are read-only after construction and ``__hash__`` is
    defined on the model fields. Use this for domain primitives where
    *equality is structural* — two values with the same fields are the
    same value (UUID wrappers, monetary amounts, timestamps with
    timezone, etc.).

    Wire-format opinions (camelCase aliases) live on ``BaseAppModel``;
    value objects are internal and use plain Python field names.

    The frozen/hashable guarantee covers this class's own immutability
    contract (you can't reassign a field after construction) — it doesn't
    make a subclass's field *values* immutable. A subclass that adds a
    ``list``/``dict`` field can still construct an instance whose hash
    changes if that mutable value is mutated in place, or whose fields are
    shared (aliased) across instances despite ``frozen=True``. Prefer
    ``tuple``/``frozenset``/nested ``BaseValueObject`` fields over
    ``list``/``dict`` when a value object needs to actually be safe to hash
    and share.
    """

    model_config = ConfigDict(frozen=True)


class BaseWireValueObject(BaseValueObject):
    """Frozen value object that *also* speaks the wire-shape convention.

    Use when a value object crosses a service boundary (e.g., a typed
    ``Money`` field appearing in an HTTP response). Combines
    ``BaseValueObject``'s frozen+hashable semantics with
    ``BaseAppModel``'s snake↔camel alias generator.
    """

    model_config = ConfigDict(
        frozen=True,
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,  # same reason as `BaseAppModel`'s
    )


__all__ = [
    "BaseAppModel",
    "BaseValueObject",
    "BaseWireValueObject",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "UtcDate",
    "UtcDateTime",
]
