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

"""Configuration records for a Bundle.

The Bundle's job is to centralise rich-content config so multiple Grids
in the same bundle share defaults. A field-level setting always wins
over a Bundle-level default — Bundle defaults provide a sensible base,
not a hard ceiling.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from forktex_core.types import BaseValueObject

VectorStorageMode = Literal["none", "inline", "remote", "both"]
"""Where the vector for a VECTOR field's cell lives.

- ``none`` — no embedding stored (the field accepts a free-form value
  but no vector is computed).
- ``inline`` — embedding stored in the row's JSONB payload (small
  vectors, < ~256 dims).
- ``remote`` — embedding stored in the vector store ([vector] →
  qdrant); the cell carries only a back-reference (collection name +
  point id).
- ``both`` — embedding written to both locations (consistency at the
  cost of write amplification; useful during a vector-store migration).
"""


class VectorDefaults(BaseValueObject):
    """Shared VECTOR field defaults at the Bundle level.

    Individual VECTOR fields can override any of these. ``model`` is a
    free-form tag the consumer interprets — core ships no embedding-model
    registry, that lives on consumer tracks.

    ``collection_prefix`` is a *consumer-recommended* default — the
    Bundle facade does not auto-propagate it onto member Grids' VECTOR
    field ``config`` at declare time. Consumers that want every
    VECTOR field in the Bundle to share a Qdrant prefix should stamp it
    explicitly into each column dict's ``config={"collection_prefix":
    space.config.vector.collection_prefix, ...}``. The per-field
    handler reads ``VectorConfig.collection_prefix`` (in
    ``space/types/vector.py``); this Bundle-level field is the
    documented place to record the consumer's chosen default in one
    spot for stamping.
    """

    model: str | None = None
    dimensions: int | None = None
    storage_mode: VectorStorageMode = "remote"
    collection_prefix: str | None = None


class StorageDefaults(BaseValueObject):
    """Shared FILE field defaults at the Bundle level."""

    bucket: str | None = None
    prefix: str | None = None
    """Object-key prefix prepended to every uploaded blob — useful for
    grouping a Bundle's files under one logical folder in MinIO."""


class BundleConfig(BaseValueObject):
    """Aggregate of shared defaults a Bundle passes down to its Grids.

    Frozen + hashable (inherited from BaseValueObject) so a BundleConfig
    can be used as a dict key for caching or as part of a deterministic
    identity hash for Bundle-level fingerprinting.
    """

    vector: VectorDefaults = VectorDefaults()
    storage: StorageDefaults = StorageDefaults()
    edge_vocab: tuple[str, ...] = ()
    """Optional whitelist of edge ``kind`` strings allowed within this
    Bundle's cross-Grid edges. Empty tuple means 'no restriction' —
    any kind is allowed. Useful when a Bundle wants to model a small,
    auditable graph vocabulary (e.g., {"contains", "depends_on"})."""


class SyncSourceConfig(BaseValueObject):
    """Contract for consumer-defined sync drivers.

    A Bundle can declare 'I'm fed by these external sources' without
    coupling core to any particular driver. The actual driver code
    (codebase walkers, S3 watchers, REST pollers) lives on consumer
    tracks; core just holds the typed config that names + parameterises
    them.

    ``kind`` is the driver identifier (consumer-defined namespace,
    e.g. ``"intelligence:codebase"``). ``options`` is an opaque dict
    the driver interprets. ``schedule`` is an optional cron expression
    for periodic resync.
    """

    kind: str
    options: dict[str, Any] = Field(default_factory=dict)
    schedule: str | None = None


__all__ = [
    "BundleConfig",
    "StorageDefaults",
    "SyncSourceConfig",
    "VectorDefaults",
    "VectorStorageMode",
]
