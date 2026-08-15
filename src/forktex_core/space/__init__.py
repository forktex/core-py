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

"""Level-2 ``[space]`` extra — multi-Grid bundle with shared rich-content config.

A ``Bundle`` is the wrapper between ``[grid]`` and rich content
(VECTOR / FILE field types) plus cross-Grid traversal. Consumers
that need only tabular state stay on bare ``Grid``; consumers that
add VECTOR or FILE fields graduate to ``Bundle`` to centralise
backing-service config (vector collection prefix, storage bucket,
embedding model defaults).

This module ships the ``Bundle`` facade, ``BundleConfig`` shared-config
record, and ``SyncSourceConfig`` contract for consumer-defined sync
drivers. VECTOR / FILE handlers and cross-Grid traversal helpers
lazy-import ``[vector]`` / ``[storage]`` / ``[graph]`` so consumers
only pay for what they use.
"""

# Side-effect: register rich FILE/VECTOR handlers into [grid]'s registry.
# Consumers that import [space] opt in to the rich behavior; pure-tabular
# consumers using only [grid] keep the bare handlers.
from forktex_core.space import types as _types  # noqa: F401
from forktex_core.space.bundle import Bundle
from forktex_core.space.config import (
    BundleConfig,
    StorageDefaults,
    SyncSourceConfig,
    VectorDefaults,
)

__all__ = [
    "Bundle",
    "BundleConfig",
    "StorageDefaults",
    "SyncSourceConfig",
    "VectorDefaults",
]
