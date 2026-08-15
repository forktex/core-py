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

"""Integration tests for forktex_core.vector — requires Qdrant container.

Note: Qdrant point IDs must be unsigned integers or valid UUIDs — not arbitrary
strings. Tests use integers or uuid.uuid4() strings.
Collection names cannot contain ':' — use '--' as tenant separator instead.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

pytest.importorskip("qdrant_client", reason="qdrant-client not installed")

from forktex_core.vector import (
    ClientNotRegisteredError,
    SearchQuery,
    SparseVector,
    Vector,
    VectorPoint,
    deregister,
    get_client,
    register,
)
from forktex_core.vector.errors import DimensionMismatchError

DIM = 4  # tiny vectors for speed in tests
COLLECTION_PREFIX = "test-core-py--"


@pytest_asyncio.fixture
async def vector(qdrant_url: str) -> Vector:
    return Vector(qdrant_url=qdrant_url)


@pytest_asyncio.fixture
async def collection_name() -> str:
    return f"{COLLECTION_PREFIX}{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def coll(vector: Vector, collection_name: str):
    handle = vector.collection(collection_name)
    await handle.create(dim=DIM, distance="cosine")
    yield handle
    try:
        await handle.delete()
    except Exception:
        pass


def _vec(vals: list[float]) -> list[float]:
    """Pad/trim to DIM."""
    return (vals + [0.0] * DIM)[:DIM]


@pytest.mark.asyncio
async def test_create_and_info(coll):
    info = await coll.info()
    assert info.dim == DIM


@pytest.mark.asyncio
async def test_upsert_and_search_dense(coll):
    # Use integer IDs — Qdrant requires uint or UUID
    points = [
        VectorPoint(id=1, vector=_vec([1, 0, 0, 0]), payload={"text": "apple"}),
        VectorPoint(id=2, vector=_vec([0, 1, 0, 0]), payload={"text": "banana"}),
        VectorPoint(id=3, vector=_vec([0, 0, 1, 0]), payload={"text": "cherry"}),
    ]
    await coll.upsert(points)
    hits = await coll.search(SearchQuery(vector=_vec([1, 0, 0, 0])).limit(3))
    assert len(hits) > 0
    # Point 1 ([1,0,0,0]) is most similar to query [1,0,0,0]
    assert hits[0].id == 1


@pytest.mark.asyncio
async def test_upsert_with_uuid_ids(coll):
    uid = str(uuid.uuid4())
    await coll.upsert([VectorPoint(id=uid, vector=_vec([1, 0, 0, 0]))])
    hits = await coll.search(SearchQuery(vector=_vec([1, 0, 0, 0])).limit(1))
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_delete_points(coll):
    await coll.upsert([VectorPoint(id=100, vector=_vec([0.5, 0.5, 0, 0]))])
    await coll.delete_points([100])
    hits = await coll.search(SearchQuery(vector=_vec([0.5, 0.5, 0, 0])).limit(5))
    assert all(h.id != 100 for h in hits)


@pytest.mark.asyncio
async def test_score_threshold_filters(coll):
    await coll.upsert(
        [
            VectorPoint(id=10, vector=_vec([1, 0, 0, 0])),
            VectorPoint(id=11, vector=_vec([0, 0, 0, 1])),
        ]
    )
    hits = await coll.search(SearchQuery(vector=_vec([1, 0, 0, 0])).limit(10).score_threshold(0.9))
    ids = [h.id for h in hits]
    assert 10 in ids
    assert 11 not in ids


@pytest.mark.asyncio
async def test_payload_stored_and_returned(coll):
    await coll.upsert([VectorPoint(id=20, vector=_vec([1, 0, 0, 0]), payload={"key": "value", "n": 42})])
    hits = await coll.search(SearchQuery(vector=_vec([1, 0, 0, 0])).limit(1))
    assert len(hits) == 1
    assert hits[0].payload.get("key") == "value"
    assert hits[0].payload.get("n") == 42


@pytest.mark.asyncio
async def test_list_collections(vector: Vector, collection_name: str, coll):
    names = await vector.list_collections()
    assert collection_name in names


@pytest.mark.asyncio
async def test_list_collections_with_prefix(vector: Vector, collection_name: str, coll):
    names = await vector.list_collections(prefix=COLLECTION_PREFIX)
    assert all(n.startswith(COLLECTION_PREFIX) for n in names)
    assert collection_name in names


@pytest.mark.asyncio
async def test_search_across(vector: Vector):
    names = []
    for i in range(2):
        name = f"{COLLECTION_PREFIX}cross--{uuid.uuid4().hex[:6]}"
        h = vector.collection(name)
        await h.create(dim=DIM)
        await h.upsert([VectorPoint(id=i + 1, vector=_vec([float(i + 1), 0, 0, 0]), payload={"i": i})])
        names.append(name)
    try:
        hits = await vector.search_across(names, SearchQuery(vector=_vec([1, 0, 0, 0])).limit(4))
        assert len(hits) >= 1
        assert all(h.collection in names for h in hits)
        # SearchHit is a frozen BaseValueObject — search_across() must rebuild each
        # hit via model_copy(update={"collection": name}) rather than mutate in
        # place; confirm each hit's payload still matches the collection it was
        # tagged with (i.e. collection tagging didn't get scrambled across collections).
        for h in hits:
            i = names.index(h.collection)
            assert h.payload == {"i": i}
    finally:
        for n in names:
            try:
                await vector.collection(n).delete()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_rerank_returns_top_k(coll):
    points = [VectorPoint(id=i + 1, vector=_vec([float(i + 1), 0, 0, 0])) for i in range(5)]
    await coll.upsert(points)
    query_vec = _vec([5, 0, 0, 0])
    initial = await coll.search(SearchQuery(vector=query_vec).limit(5))
    reranked = await coll.rerank(query_vec, initial, top_k=3)
    assert len(reranked) <= 3
    assert reranked[0].score >= reranked[-1].score


@pytest.mark.asyncio
async def test_multimodal_collection(vector: Vector):
    name = f"{COLLECTION_PREFIX}mm--{uuid.uuid4().hex[:6]}"
    h = vector.collection(name)
    await h.create(dim=DIM, multimodal_dim=DIM)
    try:
        await h.upsert(
            [
                VectorPoint(
                    id=1,
                    vector=_vec([1, 0, 0, 0]),
                    multimodal_vector=_vec([0, 1, 0, 0]),
                )
            ]
        )
        hits = await h.search(SearchQuery(vector=_vec([0, 1, 0, 0])).using("multimodal").limit(1))
        assert len(hits) >= 1
    finally:
        await h.delete()


@pytest.mark.asyncio
async def test_sparse_vector_upsert_and_hybrid_search(vector: Vector):
    name = f"{COLLECTION_PREFIX}sparse--{uuid.uuid4().hex[:6]}"
    h = vector.collection(name)
    await h.create(dim=DIM, sparse=True)
    try:
        await h.upsert(
            [
                VectorPoint(
                    id=1,
                    vector=_vec([1, 0, 0, 0]),
                    payload={"text": "apple"},
                    sparse_vector=SparseVector(indices=[1, 3], values=[0.5, 0.8]),
                ),
                VectorPoint(id=2, vector=_vec([0, 1, 0, 0]), payload={"text": "banana"}),
            ]
        )
        info = await h.info()
        assert info.has_sparse is True

        hits = await h.search(SearchQuery(vector=_vec([1, 0, 0, 0])).using("hybrid").limit(5))
        assert len(hits) >= 1
    finally:
        await h.delete()


@pytest.mark.asyncio
async def test_upsert_dimension_mismatch_raises_typed_error(coll):
    with pytest.raises(DimensionMismatchError):
        await coll.upsert([VectorPoint(id=1, vector=[1.0, 0.0])])  # DIM is 4, this is length 2


def test_register_get_client_deregister_roundtrip():
    try:
        registered = register("test-registry-client", qdrant_url="http://localhost:6333")
        assert get_client("test-registry-client") is registered
        dropped = deregister("test-registry-client")
        assert dropped is registered
        assert deregister("test-registry-client") is None  # idempotent
    finally:
        deregister("test-registry-client")


def test_get_client_unregistered_raises():
    with pytest.raises(ClientNotRegisteredError):
        get_client("definitely-not-registered")
