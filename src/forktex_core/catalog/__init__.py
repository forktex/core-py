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

"""Architecture catalog — typed manifest of forktex_core's levels and extras.

A single source of truth for "what extras exist, what role each plays, what
they depend on, and where they sit in the four-level architecture (primitives,
abstractions, facades, bootstraps)."

The catalog ships as JSON (``catalog.json``), is validated against a Pydantic
schema (``models.py``), and renders to Markdown via ``render.py`` for the
README's architecture section. CI lints catalog ↔ README drift.

Programmatic access::

    from forktex_core.catalog import current

    grid = current.extra("grid")
    print(grid.depends_on)               # ["database", "log", "error", "types"]
    print(grid.tech)                     # None — grid is a facade, no tech binding

    for extra in current.extras_at_level(2):
        print(f"{extra.id}: {extra.role}")
"""

from forktex_core.catalog.loader import current, load_current
from forktex_core.catalog.models import (
    ArchitectureCatalog,
    CatalogPresentation,
    ExtraKind,
    ExtraSpec,
    Level,
    Relation,
    RelationKind,
    Status,
    TechBacking,
)

__all__ = [
    "ArchitectureCatalog",
    "CatalogPresentation",
    "ExtraKind",
    "ExtraSpec",
    "Level",
    "Relation",
    "RelationKind",
    "Status",
    "TechBacking",
    "current",
    "load_current",
]
