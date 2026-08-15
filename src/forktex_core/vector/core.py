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

"""Vector entry point — mirrors Flow's design: one instance, scoped handles."""

from __future__ import annotations

from forktex_core.log import get_logger
from forktex_core.vector.collection import CollectionHandle, _make_client
from forktex_core.vector.types import SearchHit, SearchQuery

logger = get_logger(__name__)


class Vector:
    """Entry point for the vector module.

    Typically created once at app startup and shared across the process:

        vector = Vector(qdrant_url=settings.qdrant_url)

    Does NOT connect eagerly — a fresh ``AsyncQdrantClient`` is created per
    operation (stateless, safe for concurrent async use).

    Args:
        qdrant_url: Full URL of the Qdrant service (e.g. ``"http://qdrant:6333"``).
        api_key: Optional Qdrant API key for Qdrant Cloud deployments.
    """

    def __init__(self, qdrant_url: str, api_key: str | None = None) -> None:
        self._qdrant_url = qdrant_url
        self._api_key = api_key

    def collection(self, name: str) -> CollectionHandle:
        """Return a handle scoped to the named collection.

        The collection need not exist yet — call ``await handle.create(...)``
        first (idempotent).
        """
        return CollectionHandle(name=name, qdrant_url=self._qdrant_url, api_key=self._api_key)

    async def list_collections(self, *, prefix: str | None = None) -> list[str]:
        """List all collection names in Qdrant.

        Args:
            prefix: If provided, only return names that start with this string.
                    Use to enumerate tenant collections, e.g. ``prefix="org-abc--"``.
        """
        q = _make_client(self._qdrant_url, self._api_key)
        try:
            result = await q.get_collections()
        finally:
            await q.close()
        names = [c.name for c in result.collections]
        if prefix:
            names = [n for n in names if n.startswith(prefix)]
        return names

    async def search_across(
        self,
        collection_names: list[str],
        query: SearchQuery,
    ) -> list[SearchHit]:
        """Fan-out search across multiple collections, merge results by score.

        Each collection is searched independently with the same ``query``.
        Results are merged and sorted by score descending; the top
        ``query._limit`` overall hits are returned.

        ``SearchHit.collection`` is set to the source collection name for
        each result so callers can attribute hits.

        Typical use: cross-collection knowledge-base search for an org.

            hits = await vector.search_across(
                await vector.list_collections(prefix=f"org-{org_id}:"),
                SearchQuery(vector=embed(q)).limit(10),
            )
        """
        import asyncio

        async def _search_one(name: str) -> list[SearchHit]:
            handle = self.collection(name)
            hits = await handle.search(query)
            return [h.model_copy(update={"collection": name}) for h in hits]

        results_per_coll = await asyncio.gather(
            *(_search_one(name) for name in collection_names),
            return_exceptions=True,
        )

        merged: list[SearchHit] = []
        for i, result in enumerate(results_per_coll):
            if isinstance(result, Exception):
                logger.warning(
                    "search_across: collection %r failed: %s",
                    collection_names[i],
                    result,
                )
            elif isinstance(result, list):
                merged.extend(result)

        merged.sort(key=lambda h: h.score, reverse=True)
        return merged[: query._limit]


__all__ = ["Vector"]
