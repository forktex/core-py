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

"""Vector module value types."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from forktex_core.types import BaseValueObject

SearchStrategy = Literal["dense", "multimodal", "hybrid", "sparse"]


class SparseVector(BaseValueObject):
    """Sparse vector for BM25/SPLADE keyword-based search.

    ``indices`` and ``values`` must have the same length. Only non-zero
    dimensions are stored — Qdrant's native sparse format.
    """

    indices: list[int]
    values: list[float]


class VectorPoint(BaseValueObject):
    """A single point to upsert into a collection.

    Args:
        id: Unique identifier (str UUID or int).
        vector: Dense text embedding (required).
        payload: Arbitrary key/value metadata stored alongside the vector.
        multimodal_vector: Optional CLIP/image embedding for the
            ``"multimodal"`` named vector space.
        sparse_vector: Optional sparse vector (BM25/SPLADE) for the
            ``"sparse"`` named vector space, enabling hybrid search.
    """

    id: str | int
    vector: list[float]
    payload: dict[str, Any] = Field(default_factory=dict)
    multimodal_vector: list[float] | None = None
    sparse_vector: SparseVector | None = None


class SearchHit(BaseValueObject):
    """A single result from a vector search.

    ``collection`` is set by ``search_across`` to indicate which collection
    this hit came from — frozen instances get a new ``collection`` via
    ``.model_copy(update={"collection": name})`` rather than assignment.
    """

    id: str | int
    score: float
    payload: dict[str, Any]
    collection: str | None = None


class CollectionInfo(BaseValueObject):
    name: str
    vectors_count: int
    dim: int
    multimodal_dim: int | None = None
    has_sparse: bool = False
    distance: str = "cosine"


class SearchQuery:
    """Chainable builder for vector search queries.

    Mirrors flow's ``InstanceQuery`` pattern — build by chaining, execute
    by calling ``collection.search(query)`` or ``vector.search_across(...)``.

        results = await coll.search(
            SearchQuery(vector=embed(q))
            .limit(10)
            .using("hybrid")
            .score_threshold(0.7)
        )
    """

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self._multimodal_vector: list[float] | None = None
        self._limit: int = 10
        self._strategy: SearchStrategy = "dense"
        self._payload_filter: dict[str, Any] | None = None
        self._score_threshold: float | None = None

    def limit(self, k: int) -> SearchQuery:
        self._limit = k
        return self

    def using(self, strategy: SearchStrategy) -> SearchQuery:
        """Set the search strategy.

        - ``"dense"``: text embedding similarity (default).
        - ``"multimodal"``: CLIP/image vector space.
        - ``"hybrid"``: dense prefetch + sparse RRF fusion.
        - ``"sparse"``: sparse keyword vector (BM25/SPLADE only).
        """
        self._strategy = strategy
        return self

    def multimodal(self, vector: list[float]) -> SearchQuery:
        """Set the CLIP/image query vector for multimodal strategy."""
        self._multimodal_vector = vector
        return self

    def filter(self, payload_filter: dict[str, Any]) -> SearchQuery:
        """Apply a Qdrant payload filter dict."""
        self._payload_filter = payload_filter
        return self

    def score_threshold(self, threshold: float) -> SearchQuery:
        """Only return hits with score >= threshold."""
        self._score_threshold = threshold
        return self


__all__ = [
    "CollectionInfo",
    "SearchHit",
    "SearchQuery",
    "SearchStrategy",
    "SparseVector",
    "VectorPoint",
]
