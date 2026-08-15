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

"""How a column's value is stored/read/written — one strategy per Materialization.

This is the abstraction whose absence would force every call site to re-branch on ``materialization``
at ~8 call sites. A column is resolved to exactly one strategy at construction
(:func:`select_materialization`); the write path asks the strategy, it never switches
on the enum. Adding a materialization = a new subclass + one line here.

(Read-side ``expr(resolver, col)`` — which SQL expression to filter/sort on — is added
with the read layer in a later milestone; the write/reconcile surface is defined now.)
"""

from __future__ import annotations

import abc
from typing import ClassVar

from forktex_core.grid.domain.enums import Materialization
from forktex_core.grid.domain.fieldtypes import is_promotable
from forktex_core.grid.domain.spec import ColumnSpec
from forktex_core.grid.errors import BadRequestError
from forktex_core.types import JsonValue


class MaterializationStrategy(abc.ABC):
    materialization: ClassVar[Materialization]
    needs_reconcile: ClassVar[bool]  # promoted → True (a physical sidecar column must exist)

    @abc.abstractmethod
    def accepts_write(self) -> bool:
        """Whether a caller may set this column's value on write."""

    def store(self, payload: dict[str, JsonValue], key: str, value: JsonValue) -> None:
        """Place the (already-normalized) value into the row payload."""
        payload[key] = value


class PayloadValue(MaterializationStrategy):
    materialization = Materialization.payload
    needs_reconcile = False

    def accepts_write(self) -> bool:
        return True


class PromotedValue(MaterializationStrategy):
    """Value lives in payload (source of truth) AND is mirrored to a native column."""

    materialization = Materialization.promoted
    needs_reconcile = True

    def accepts_write(self) -> bool:
        return True


class DerivedValue(MaterializationStrategy):
    """Not stored — computed read-side by projecting a related row's field."""

    materialization = Materialization.derived
    needs_reconcile = False

    def accepts_write(self) -> bool:
        return False

    def store(self, payload: dict[str, JsonValue], key: str, value: JsonValue) -> None:
        raise BadRequestError(f"column '{key}' is derived and read-only")


def select_materialization(spec: ColumnSpec) -> MaterializationStrategy:
    """The ONE place column materialization is decided. Promotability is enforced here
    (a non-promotable type cannot get a ``PromotedValue``), replacing scattered
    ``PROMOTABLE_EXCLUDED`` checks."""
    match spec.materialization:
        case Materialization.payload:
            return PayloadValue()
        case Materialization.derived:
            return DerivedValue()
        case Materialization.promoted:
            if not is_promotable(spec.type_id):
                raise BadRequestError(f"'{spec.type_id}' columns cannot be promoted to a native column")
            return PromotedValue()
    raise BadRequestError(f"unknown materialization {spec.materialization!r}")  # pragma: no cover


__all__ = [
    "DerivedValue",
    "MaterializationStrategy",
    "PayloadValue",
    "PromotedValue",
    "select_materialization",
]
