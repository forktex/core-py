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

"""Typed query inputs — re-exported from :mod:`forktex_core.database.filters`.

The filter AST and sort spec used to be defined here. They now live in
``database`` so ``flow`` and any other consumer share one vocabulary instead of
hand-building ``WHERE`` clauses; only the *compilation* of a column name to SQL
stays grid-specific (see ``grid.read.query``'s ``QuerySource``).

This module remains as the import path grid's own code and its public API have
always used.
"""

from __future__ import annotations

from forktex_core.database.filters import (
    MAX_FILTER_DEPTH,
    MAX_IN_ITEMS,
    And,
    Comparison,
    FilterNode,
    FilterOp,
    Not,
    Or,
    SortDirection,
    SortKey,
    parse_filter,
)

__all__ = [
    "MAX_FILTER_DEPTH",
    "MAX_IN_ITEMS",
    "And",
    "Comparison",
    "FilterNode",
    "FilterOp",
    "Not",
    "Or",
    "SortDirection",
    "SortKey",
    "parse_filter",
]
