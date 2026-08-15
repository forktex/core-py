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

"""Identifier validation — a re-export shim over :mod:`forktex_core.database.identifiers`.

The implementation moved down to ``database``, where ``migrate``, ``flow`` and
``grid`` all reach it, replacing three copies of the same regexes that had drifted
into *incompatible* policies. Grid's names are kept as aliases because they read
better at grid's call sites (a ``key`` and a ``slug`` are different things here)
and because they are part of grid's internal vocabulary.
"""

from __future__ import annotations

from forktex_core.database.identifiers import (
    IDENT_RE,
    MAX_IDENT,
    SCHEMA_RE,
    SLUG_RE,
    is_identifier,
    validate_identifier,
    validate_relation,
    validate_schema,
    validate_slug,
)

__all__ = [
    "IDENT_RE",
    "MAX_IDENT",
    "SCHEMA_RE",
    "SLUG_RE",
    "is_identifier",
    "validate_ident",
    "validate_identifier",
    "validate_key",
    "validate_relation",
    "validate_schema",
    "validate_slug",
]


def validate_key(key: str) -> None:
    """A column key / native column name."""
    validate_identifier(key)


def validate_ident(name: str, what: str) -> None:
    """A host identifier (primary key / namespace column / ``column_map`` value)."""
    validate_identifier(name, what)
