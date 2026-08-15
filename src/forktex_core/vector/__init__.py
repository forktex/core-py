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

"""Multi-modal vector space embedding and search over Qdrant.

Qdrant-first with a thin abstraction layer that surfaces four search strategies:
dense (text), multimodal (CLIP/image), hybrid (dense+sparse RRF fusion),
and sparse (BM25/SPLADE keyword).

Collections are tenant-scoped by caller convention — the caller sets the
collection name (e.g. ``f"org-{org_id}--knowledge"``).

    vector = Vector(qdrant_url="http://qdrant:6333")

    # Create (idempotent)
    coll = vector.collection("org-abc:knowledge")
    await coll.create(dim=1536, multimodal_dim=512)

    # Upsert
    await coll.upsert([
        VectorPoint(
            id="chunk-001",
            vector=embed_text("The capital of France is Paris."),
            payload={"text": "The capital of France is Paris.", "source": "wiki"},
            multimodal_vector=embed_clip(image),  # optional
        )
    ])

    # Search — dense (default)
    hits = await coll.search(SearchQuery(vector=embed_text(query)).limit(10))

    # Search — hybrid
    hits = await coll.search(
        SearchQuery(vector=embed_text(query))
        .limit(10)
        .using("hybrid")
        .score_threshold(0.6)
    )

Requires: pip install qdrant-client
"""

from forktex_core.vector.collection import CollectionHandle
from forktex_core.vector.core import Vector
from forktex_core.vector.errors import (
    CollectionNotFoundError,
    DimensionMismatchError,
    VectorError,
)
from forktex_core.vector.types import (
    CollectionInfo,
    SearchHit,
    SearchQuery,
    SparseVector,
    VectorPoint,
)

# Mirrors ``forktex_core.storage``'s registry: register a named ``Vector``
# at startup, look it up by name from anywhere (e.g., the rich VECTOR
# field handler in [space]). ``"default"`` is the convention for
# single-client setups.

_clients: dict[str, Vector] = {}


class ClientNotRegisteredError(VectorError):
    """Raised by ``get_client`` when the requested name isn't registered."""


def register(name: str, qdrant_url: str, *, api_key: str | None = None) -> Vector:
    """Register a named ``Vector`` client and return it.

    Idempotent: calling with the same name replaces the previous client.
    """
    client = Vector(qdrant_url=qdrant_url, api_key=api_key)
    _clients[name] = client
    return client


def get_client(name: str = "default") -> Vector:
    """Return a registered ``Vector`` client by name."""
    try:
        return _clients[name]
    except KeyError as exc:
        registered = ", ".join(f'"{k}"' for k in _clients) or "(none)"
        raise ClientNotRegisteredError(f"Vector client {name!r} is not registered. Registered: {registered}") from exc


def deregister(name: str = "default") -> Vector | None:
    """Remove ``name`` from the registry and return the dropped client (or
    ``None`` if it wasn't registered). Idempotent.

    Symmetric with ``register`` and with ``forktex_core.storage.deregister``.
    Lets tests and dev tooling restore the registry to a known shape
    without hooking into ``_clients`` directly.
    """
    return _clients.pop(name, None)


__all__ = [
    "ClientNotRegisteredError",
    "CollectionHandle",
    "CollectionInfo",
    "CollectionNotFoundError",
    "DimensionMismatchError",
    "SearchHit",
    "SearchQuery",
    "SparseVector",
    "Vector",
    "VectorError",
    "VectorPoint",
    "deregister",
    "get_client",
    "register",
]
