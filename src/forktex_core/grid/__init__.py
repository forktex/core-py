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

"""forktex_core[grid] — the layered, strategy-based virtual database.

The single source of truth for the grid: a dynamic tabular tier over Postgres,
built as strict layers with the storage/materialization decisions resolved once by
strategy objects. See ``docs/grid.md`` and ``docs/grid-binding-design.md``.

Layers, named after the role each plays, with dependencies pointing strictly downward::

    grid.py / namespace.py  →  the curated public façades (Grid, Namespace)
    write/                  →  mutation services (rows, relations, schema, batch, tx)
    read/                   →  query pipeline, derived columns, graph, numbering
    persist/                →  ORM models, migrations, repositories, physical reconcilers
    domain/                 →  specs, Table/Column aggregates, strategies, field types (pure)
    errors.py               →  the typed error vocabulary
    identifiers.py          →  grid's names for `database.identifiers`' profiles

Integrity boundaries come straight from :mod:`forktex_core.database.integrity`; grid keeps no
module of its own for them. `ops.py` and `declare.py` are opt-in front doors (an agentic tool
surface and a decorator DSL) deliberately kept out of this namespace so ``__all__`` stays lean.

The one law: *no module branches on ``ownership`` or ``materialization``* — those
decisions are made once, by a strategy object.
"""

from forktex_core.grid.domain.binding import Extension, Overlay
from forktex_core.grid.domain.enums import (
    BrowseMode,
    Cardinality,
    FieldType,
    Materialization,
    OnDelete,
    RelationShape,
)
from forktex_core.grid.domain.fieldtypes import (
    Capabilities,
    FieldTypeHandler,
    FilterOp,
    is_registered,
    register_field_type,
)
from forktex_core.grid.domain.fieldtypes.base import CellValue, WriteContext
from forktex_core.grid.domain.schema import Schema
from forktex_core.grid.domain.spec import ColumnSpec, IndexSpec, RelationSpec, TableSpec
from forktex_core.grid.errors import ReadOnlyStorage
from forktex_core.grid.grid import Grid, declare_relation
from forktex_core.grid.namespace import Namespace
from forktex_core.grid.persist import apply_migrations
from forktex_core.grid.read.result import Page, Row
from forktex_core.grid.write.batch import RowOp

__all__ = [
    "BrowseMode",
    "Capabilities",
    "Cardinality",
    "CellValue",
    "ColumnSpec",
    "Extension",
    "FieldType",
    "FieldTypeHandler",
    "FilterOp",
    "Grid",
    "IndexSpec",
    "Materialization",
    "Namespace",
    "OnDelete",
    "Overlay",
    "Page",
    "ReadOnlyStorage",
    "RelationShape",
    "RelationSpec",
    "Row",
    "RowOp",
    "Schema",
    "TableSpec",
    "WriteContext",
    "apply_migrations",
    "declare_relation",
    "is_registered",
    "register_field_type",
]
