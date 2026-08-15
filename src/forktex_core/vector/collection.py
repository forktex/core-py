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

"""CollectionHandle — async operations on a single Qdrant collection."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, cast

from forktex_core.log import get_logger
from forktex_core.vector.errors import (
    CollectionNotFoundError,
    DimensionMismatchError,
    VectorError,
)
from forktex_core.vector.types import (
    CollectionInfo,
    SearchHit,
    SearchQuery,
    VectorPoint,
)

logger = get_logger(__name__)

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient


def _make_client(url: str, api_key: str | None) -> AsyncQdrantClient:
    """Create a fresh AsyncQdrantClient (stateless, close after use)."""
    try:
        from qdrant_client import AsyncQdrantClient
    except ImportError as exc:
        raise ImportError("Install 'forktex-core[vector]' (qdrant-client) to use forktex_core.vector") from exc
    return AsyncQdrantClient(url=url, api_key=api_key)


class CollectionHandle:
    """Scoped handle for a named Qdrant collection.

    Obtained via ``vector.collection("name")`` — not instantiated directly.
    Creates a fresh ``AsyncQdrantClient`` per operation (stateless, safe for
    concurrent async use). Explicitly closes each client after use.

    Named vector spaces per collection:
    - ``"dense"``: required text embedding.
    - ``"multimodal"``: optional CLIP/image embedding.
    - ``"sparse"``: optional sparse BM25/SPLADE vector for hybrid search.
    """

    def __init__(self, name: str, qdrant_url: str, api_key: str | None) -> None:
        self._name = name
        self._qdrant_url = qdrant_url
        self._api_key = api_key

    async def create(
        self,
        dim: int,
        distance: str = "cosine",
        *,
        multimodal_dim: int | None = None,
        sparse: bool = False,
    ) -> None:
        """Create the collection. No-op if it already exists.

        Args:
            dim: Dense vector dimension (e.g. 1536 for text-embedding-3-small).
            distance: ``"cosine"``, ``"dot"``, or ``"euclidean"``.
            multimodal_dim: Optional second vector space for CLIP embeddings.
            sparse: If True, adds a ``"sparse"`` space for BM25/SPLADE hybrid.
        """
        from qdrant_client.models import Distance, SparseVectorParams, VectorParams

        dist_map = {
            "cosine": Distance.COSINE,
            "dot": Distance.DOT,
            "euclidean": Distance.EUCLID,
        }
        qdrant_dist = dist_map.get(distance, Distance.COSINE)

        vectors_config: dict = {"dense": VectorParams(size=dim, distance=qdrant_dist)}
        if multimodal_dim is not None:
            vectors_config["multimodal"] = VectorParams(size=multimodal_dim, distance=qdrant_dist)

        sparse_config = {"sparse": SparseVectorParams()} if sparse else None

        q = _make_client(self._qdrant_url, self._api_key)
        try:
            exists = await q.collection_exists(self._name)
            if not exists:
                await q.create_collection(
                    collection_name=self._name,
                    vectors_config=vectors_config,
                    sparse_vectors_config=sparse_config,
                )
        finally:
            await q.close()

    async def delete(self) -> None:
        """Delete the collection and all its data."""
        q = _make_client(self._qdrant_url, self._api_key)
        try:
            await q.delete_collection(self._name)
        finally:
            await q.close()

    async def info(self) -> CollectionInfo:
        """Return metadata about the collection."""
        q = _make_client(self._qdrant_url, self._api_key)
        try:
            try:
                info = await q.get_collection(self._name)
            except Exception as exc:
                raise CollectionNotFoundError(self._name) from exc
        finally:
            await q.close()

        vectors_config = info.config.params.vectors
        dim = 0
        multimodal_dim = None
        has_sparse = False
        if isinstance(vectors_config, dict):
            if "dense" in vectors_config:
                dim = vectors_config["dense"].size
            if "multimodal" in vectors_config:
                multimodal_dim = vectors_config["multimodal"].size
        if info.config.params.sparse_vectors:
            has_sparse = "sparse" in info.config.params.sparse_vectors
        # v1.9+: points_count; v1.17+: vectors_count may not exist
        pts = getattr(info, "points_count", None) or getattr(info, "vectors_count", None) or 0
        return CollectionInfo(
            name=self._name,
            vectors_count=pts,
            dim=dim,
            multimodal_dim=multimodal_dim,
            has_sparse=has_sparse,
        )

    async def upsert(self, points: list[VectorPoint]) -> None:
        """Insert or update points. Existing IDs are overwritten."""
        if not points:
            return
        from qdrant_client.http.exceptions import UnexpectedResponse
        from qdrant_client.models import PointStruct
        from qdrant_client.models import SparseVector as QSparseVector

        qdrant_points = []
        for p in points:
            vectors: dict[str, Any] = {"dense": p.vector}
            if p.multimodal_vector is not None:
                vectors["multimodal"] = p.multimodal_vector
            if p.sparse_vector is not None:
                # PointStruct.vector is a plain {name: vector} dict — the named-vector
                # entry doesn't need a NamedSparseVector wrapper (that type was only
                # for the old top-level client.search() API, and no longer exists
                # in qdrant-client 1.19+); a bare SparseVector under "sparse" works.
                vectors["sparse"] = QSparseVector(
                    indices=p.sparse_vector.indices,
                    values=p.sparse_vector.values,
                )
            qdrant_points.append(PointStruct(id=p.id, vector=vectors, payload=p.payload))

        q = _make_client(self._qdrant_url, self._api_key)
        try:
            try:
                await q.upsert(collection_name=self._name, points=qdrant_points)
            except UnexpectedResponse as exc:
                if exc.status_code == 400 and "dimension" in str(exc).lower():
                    raise DimensionMismatchError(str(exc)) from exc
                raise
        finally:
            await q.close()

    async def delete_points(self, ids: list[str | int]) -> None:
        """Delete points by ID. No-op if ``ids`` is empty."""
        if not ids:
            return
        from qdrant_client.models import PointIdsList

        q = _make_client(self._qdrant_url, self._api_key)
        try:
            await q.delete(
                collection_name=self._name,
                points_selector=PointIdsList(points=cast(Any, ids)),
            )
        finally:
            await q.close()

    async def search(self, query: SearchQuery) -> list[SearchHit]:
        """Execute a vector search and return ranked hits."""
        from qdrant_client.models import Filter, Fusion, FusionQuery, Prefetch

        qdrant_filter = Filter(**query._payload_filter) if query._payload_filter else None
        strategy = query._strategy

        q = _make_client(self._qdrant_url, self._api_key)
        try:
            if strategy == "dense":
                result = await q.query_points(
                    collection_name=self._name,
                    query=query._vector,
                    using="dense",
                    limit=query._limit,
                    score_threshold=query._score_threshold,
                    query_filter=qdrant_filter,
                    with_payload=True,
                )
                hits = result.points

            elif strategy == "multimodal":
                vec = query._multimodal_vector or query._vector
                result = await q.query_points(
                    collection_name=self._name,
                    query=vec,
                    using="multimodal",
                    limit=query._limit,
                    score_threshold=query._score_threshold,
                    query_filter=qdrant_filter,
                    with_payload=True,
                )
                hits = result.points

            elif strategy in ("hybrid", "sparse"):
                prefetch = [Prefetch(query=query._vector, using="dense", limit=query._limit * 3)]
                result = await q.query_points(
                    collection_name=self._name,
                    prefetch=prefetch,
                    query=FusionQuery(fusion=Fusion.RRF),
                    limit=query._limit,
                    score_threshold=query._score_threshold,
                    query_filter=qdrant_filter,
                    with_payload=True,
                )
                hits = result.points

            else:
                raise VectorError(f"Unknown search strategy: {strategy!r}")
        finally:
            await q.close()

        return [
            SearchHit(
                id=str(h.id) if not isinstance(h.id, (str, int)) else h.id,
                score=h.score,
                payload=h.payload or {},
            )
            for h in hits
        ]

    async def rerank(
        self,
        query_vector: list[float],
        hits: list[SearchHit],
        top_k: int,
    ) -> list[SearchHit]:
        """Re-score hits using cosine similarity between ``query_vector`` and
        the hit vectors fetched from Qdrant.

        Retrieves the stored dense vectors for each hit ID, computes cosine
        similarity against the query, and returns the top-k results sorted by
        the new score.
        """
        if not hits:
            return []

        ids = [h.id for h in hits]
        q = _make_client(self._qdrant_url, self._api_key)
        try:
            records = await q.retrieve(
                collection_name=self._name,
                ids=ids,
                with_vectors=["dense"],
                with_payload=False,
            )
        finally:
            await q.close()

        stored: dict[Any, list[float]] = {}
        for rec in records:
            vec: Any = rec.vector
            if isinstance(vec, dict):
                vec = vec.get("dense", [])
            if isinstance(vec, list) and vec:
                stored[rec.id] = vec

        query_norm = math.sqrt(sum(v * v for v in query_vector)) or 1.0

        reranked: list[SearchHit] = []
        for hit in hits:
            sv = stored.get(hit.id)
            if sv is None:
                # Vector not retrievable — fall back to original search score
                logger.debug(
                    "rerank: vector not found for hit %r, keeping original score",
                    hit.id,
                )
                reranked.append(hit)
                continue
            dot = sum(a * b for a, b in zip(query_vector, sv, strict=True))
            sv_norm = math.sqrt(sum(v * v for v in sv)) or 1.0
            cosine = dot / (query_norm * sv_norm)
            reranked.append(
                SearchHit(
                    id=hit.id,
                    score=cosine,
                    payload=hit.payload,
                    collection=hit.collection,
                )
            )

        reranked.sort(key=lambda h: h.score, reverse=True)
        return reranked[:top_k]


__all__ = ["CollectionHandle"]
