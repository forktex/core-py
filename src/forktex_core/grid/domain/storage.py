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

"""How a table's rows are stored/read/written — one strategy per ownership.

Replaces the hand-written ``if ownership == bound`` branches this used to need. The strategy is
selected once from the table's binding (:func:`select_storage`); everything else asks
the strategy. An ``Extension`` table is ``OwnedStorage`` — the binding is only
metadata, which removes the bound/extension collision.

(The async ``read``/``write`` and the query ``row_source``/``resolver`` are added with
the persistence + read layers; the write-gate + column-acceptance surface is defined now.)
"""

from __future__ import annotations

import abc
from typing import ClassVar

from forktex_core.grid.domain.binding import Binding, Overlay
from forktex_core.grid.domain.enums import Materialization
from forktex_core.grid.domain.spec import ColumnSpec
from forktex_core.grid.errors import BadRequestError, ReadOnlyStorage


class StorageStrategy(abc.ABC):
    writable: ClassVar[bool]
    # Names the read-layer query source + write-layer sink for this storage, so the
    # read/write layers dispatch by data (a dict lookup), never by `isinstance`.
    kind: ClassVar[str]

    @abc.abstractmethod
    def accept_column(self, spec: ColumnSpec) -> None:
        """Raise if this storage cannot back the given column (invariant hook)."""

    def ensure_writable(self) -> None:
        if not self.writable:
            raise ReadOnlyStorage("this table is a read-only overlay; write to the host table directly")


class OwnedStorage(StorageStrategy):
    """Rows live in the shared ``grid_row.payload``; fully writable."""

    writable = True
    kind = "owned"

    def accept_column(self, spec: ColumnSpec) -> None:
        return None  # owned tables accept every kind of column


class BoundOverlayStorage(StorageStrategy):
    """Read-only projection of an existing physical table."""

    writable = False
    kind = "overlay"

    def __init__(self, binding: Overlay) -> None:
        self.binding = binding

    def accept_column(self, spec: ColumnSpec) -> None:
        if spec.type_id == "ref" or spec.materialization is not Materialization.payload:
            raise BadRequestError(
                "a bound (overlay) table projects host columns only — no ref/derived/promoted columns"
            )


def select_storage(binding: Binding) -> StorageStrategy:
    """The ONE place table ownership is decided. Overlay ⇒ read-only host storage;
    everything else (owned, or an extension's metadata binding) ⇒ owned storage."""
    if isinstance(binding, Overlay):
        return BoundOverlayStorage(binding)
    return OwnedStorage()


__all__ = ["BoundOverlayStorage", "OwnedStorage", "StorageStrategy", "select_storage"]
