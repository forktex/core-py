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

"""The read-side value types: a presented row and a page of them.

Frozen Pydantic models (like the specs) so results serialise uniformly via
``model_dump(mode="json")`` — no bespoke encoders for the network/agentic surface.
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from forktex_core.database.pagination import Page as DbPage
from forktex_core.types import JsonValue


class Row(BaseModel):
    """A presented row: its id (grid row id, or host PK for an overlay) + values.

    ``values`` is the column-keyed mapping (payload for owned rows, mapped host
    columns for an overlay, plus any resolved derived columns).
    """

    model_config = ConfigDict(frozen=True)

    id: Any
    namespace: str
    values: dict[str, Any] = Field(default_factory=dict)

    def __getitem__(self, key: str) -> JsonValue:
        return self.values[key]

    def get(self, key: str, default: JsonValue = None) -> JsonValue:
        return self.values.get(key, default)


class Page(DbPage[Row]):
    """A page of :class:`Row` — the library-wide
    :class:`forktex_core.database.pagination.Page`, specialised to grid's row type.

    It used to be a parallel shape with its own ``rows``/``next_cursor``/``total``
    fields and no ``has_more``, one of four page models that disagreed. Grid's
    ``rows`` spelling is kept as a read-only alias so existing call sites and
    serialised payloads still work; ``items`` is the canonical name.
    """

    model_config = ConfigDict(frozen=True)

    #: Redeclared purely to alias the name: ``rows`` is grid's domain vocabulary
    #: (a page *of rows*) and is what the `ops` agent surface and its consumers
    #: already read, so it stays the wire name and an accepted input. ``items`` is
    #: the canonical Python name, shared with every other page in the library.
    items: list[Row] = Field(
        default_factory=list,
        validation_alias=AliasChoices("items", "rows"),
        serialization_alias="rows",
    )

    @property
    def rows(self) -> list[Row]:
        """Alias of :attr:`items`, for grid's row-centric call sites."""
        return self.items


# NOTE: the self-describing shape of a table is now the round-trippable ``TableSpec`` itself
# (returned by ``Grid.describe``), and a whole namespace is ``Schema`` (``Namespace.describe``).
# The former lossy ``TableInfo``/``ColumnInfo``/``RelationInfo`` DTOs were superseded by those.

__all__ = ["Page", "Row"]
