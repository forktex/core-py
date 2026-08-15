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

"""SQL identifier validation — one implementation, explicit profiles.

Three near-copies of this used to exist (``database.migrate``,
``flow.migrations._runner``, and grid's own copy) with *incompatible*
policies: the first two accepted lower-case only with no length limit and
raised bare ``ValueError``; grid accepted mixed case, capped length at 128, and
raised ``BadRequestError``. Collapsing them onto a single rule would have
either loosened the migration-runner checks or broken grid's mixed-case column
keys, so the differences are expressed as **named profiles** instead.

Validation is defence in depth, not the primary defence: identifiers that
reach DDL should travel through SQLAlchemy constructs (see
``forktex_core.database.ddl``), where the dialect's preparer quotes them
correctly. These validators exist to reject nonsense early, with a clear
error, and to keep hostile input out of the few places a name is still
interpolated.
"""

from __future__ import annotations

import re

from forktex_core.error import BadRequestError

# Postgres truncates identifiers at 63 bytes by default; 128 is the limit the
# grid catalog has always advertised for user-supplied keys, kept for compat.
MAX_IDENT = 128

#: A plain SQL identifier: column key, promoted column, host column/relation.
#: Mixed case is permitted because grid quotes every identifier it emits.
IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

#: A table slug — an identifier that may also carry hyphens.
SLUG_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_-]*$")

#: A physical schema name. Lower-case only, matching how Postgres folds
#: unquoted identifiers — the same rule the migration runners have always used.
SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def validate_identifier(name: str, what: str = "identifier") -> None:
    """A plain SQL identifier (mixed case allowed), ``≤MAX_IDENT`` chars."""
    if not name or len(name) > MAX_IDENT or not IDENT_RE.match(name):
        raise BadRequestError(f"invalid {what} {name!r}: must be a plain SQL identifier (≤{MAX_IDENT} chars)")


def validate_slug(slug: str) -> None:
    """A table slug — alphanumeric plus ``_`` and ``-``."""
    if not slug or len(slug) > MAX_IDENT or not SLUG_RE.match(slug):
        raise BadRequestError(f"invalid slug {slug!r}: must be alphanumeric/_/- (≤{MAX_IDENT} chars)")


def validate_schema(schema: str) -> None:
    """A physical schema name — lower-case identifier."""
    if not schema or len(schema) > MAX_IDENT or not SCHEMA_RE.match(schema):
        raise BadRequestError(f"unsafe schema name: {schema!r}")


def validate_relation(relation: str) -> None:
    """A ``schema.table`` (or bare ``table``) relation reference."""
    parts = relation.split(".")
    if len(parts) not in (1, 2) or not all(p and len(p) <= MAX_IDENT and IDENT_RE.match(p) for p in parts):
        raise BadRequestError(f"unsafe relation reference: {relation!r}")


def is_identifier(name: str) -> bool:
    """Predicate form of :func:`validate_identifier`.

    Needed where a non-matching name should be *skipped* rather than raised on
    — e.g. reconciling away columns whose names predate current validation.
    """
    return bool(name) and len(name) <= MAX_IDENT and IDENT_RE.match(name) is not None


__all__ = [
    "IDENT_RE",
    "MAX_IDENT",
    "SCHEMA_RE",
    "SLUG_RE",
    "is_identifier",
    "validate_identifier",
    "validate_relation",
    "validate_schema",
    "validate_slug",
]
