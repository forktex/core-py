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

"""Redis-backed async cache: connection, operations, decorator, namespaces."""

from forktex_core.cache.connection import available, close, get_client, init
from forktex_core.cache.decorators import cached
from forktex_core.cache.namespaces import CachePrefix, key_for
from forktex_core.cache.ops import (
    delete,
    fetch_or_set,
    fetch_swr,
    get,
    invalidate_key,
    invalidate_prefix,
    set,
    shutdown_background_tasks,
)
from forktex_core.cache.serialization import deserialize, serialize

__all__ = [
    "CachePrefix",
    "available",
    "cached",
    "close",
    "delete",
    "deserialize",
    "fetch_or_set",
    "fetch_swr",
    "get",
    "get_client",
    "init",
    "invalidate_key",
    "invalidate_prefix",
    "key_for",
    "serialize",
    "set",
    "shutdown_background_tasks",
]
