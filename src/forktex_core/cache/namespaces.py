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

"""Cache key namespace management.

Each consumer project should define its own CachePrefix enum extending
StrEnum. This module provides the base pattern and the ``key_for()`` builder.

Usage::

    from enum import StrEnum
    from forktex_core.cache.namespaces import key_for

    class CachePrefix(StrEnum):
        USER = "user"
        FEED = "feed"
        STATS = "stats"

    key = key_for(CachePrefix.USER, user_id)  # "user:abc-123"
"""

from enum import StrEnum


class CachePrefix(StrEnum):
    """Base cache prefix enum. Consumers should define their own."""

    pass


def key_for(prefix: str | CachePrefix, *parts: object) -> str:
    """Build a namespaced cache key: ``"prefix:part1:part2"``.

    Raises ``ValueError`` if any part is ``None`` — a ``None`` part almost
    always means an upstream ID was never resolved (e.g. ``user_id`` not
    yet set), and silently dropping it would collapse a per-entity key
    onto the bare, unscoped ``prefix`` key, which every other caller of
    that prefix also reads and writes.
    """
    if any(p is None for p in parts):
        raise ValueError(f"key_for({prefix!r}, ...): None part in {parts!r}")
    parts_str = ":".join(str(p) for p in parts if p != "")
    return f"{prefix}:{parts_str}" if parts_str else str(prefix)
