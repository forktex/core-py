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

"""Integration tests for forktex_core.store — requires MongoDB container."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

pytest.importorskip("pymongo", reason="pymongo not installed")

from forktex_core.store import (
    ClientNotRegisteredError,
    StoreClient,
    get_client,
    register,
)


@pytest_asyncio.fixture
async def client(mongo_url: str) -> StoreClient:
    c = register("test", url=mongo_url, database=f"testdb_{uuid.uuid4().hex[:8]}")
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_insert_and_find_one(client: StoreClient):
    doc_id = await client.insert_one("widgets", {"name": "alpha", "score": 10})
    assert isinstance(doc_id, str)

    found = await client.find_one("widgets", {"_id": doc_id})
    assert found is not None
    assert found["name"] == "alpha"
    assert found["score"] == 10
    assert isinstance(found["_id"], str)  # ObjectId normalized to str


@pytest.mark.asyncio
async def test_insert_without_id_assigns_a_real_object_id(client: StoreClient):
    """No id= given → MongoDB assigns a real ObjectId (the idiomatic
    default), not a generated uuid — returned as its 24-hex-char string
    form, and round-trippable through find_one/update_one/delete_one."""
    from bson import ObjectId

    doc_id = await client.insert_one("widgets", {"name": "gen-id"})
    assert ObjectId.is_valid(doc_id)

    assert await client.find_one("widgets", {"_id": doc_id}) is not None
    assert await client.update_one("widgets", {"_id": doc_id}, {"score": 1})
    assert await client.delete_one("widgets", {"_id": doc_id})
    assert await client.find_one("widgets", {"_id": doc_id}) is None


@pytest.mark.asyncio
async def test_insert_with_caller_supplied_id(client: StoreClient):
    custom_id = str(uuid.uuid4())
    doc_id = await client.insert_one("widgets", {"name": "beta"}, id=custom_id)
    assert doc_id == custom_id

    found = await client.find_one("widgets", {"_id": custom_id})
    assert found is not None
    assert found["_id"] == custom_id


@pytest.mark.asyncio
async def test_find_one_missing_returns_none(client: StoreClient):
    result = await client.find_one("widgets", {"_id": str(uuid.uuid4())})
    assert result is None


@pytest.mark.asyncio
async def test_find_with_filter_and_limit(client: StoreClient):
    for i in range(5):
        await client.insert_one("items", {"kind": "test-find", "n": i})
    await client.insert_one("items", {"kind": "other"})

    results = await client.find("items", {"kind": "test-find"}, limit=3)
    assert len(results) == 3
    assert all(r["kind"] == "test-find" for r in results)
    assert all(isinstance(r["_id"], str) for r in results)


@pytest.mark.asyncio
async def test_update_one_modifies_existing(client: StoreClient):
    doc_id = await client.insert_one("widgets", {"name": "gamma", "score": 1})
    updated = await client.update_one("widgets", {"_id": doc_id}, {"score": 99})
    assert updated is True

    found = await client.find_one("widgets", {"_id": doc_id})
    assert found["score"] == 99


@pytest.mark.asyncio
async def test_update_one_no_match_without_upsert_returns_false(client: StoreClient):
    updated = await client.update_one("widgets", {"_id": str(uuid.uuid4())}, {"score": 1})
    assert updated is False


@pytest.mark.asyncio
async def test_update_one_upsert_inserts_new_document(client: StoreClient):
    new_id = str(uuid.uuid4())
    updated = await client.update_one("widgets", {"_id": new_id}, {"score": 5}, upsert=True)
    assert updated is True

    found = await client.find_one("widgets", {"_id": new_id})
    assert found is not None
    assert found["score"] == 5


@pytest.mark.asyncio
async def test_delete_one(client: StoreClient):
    doc_id = await client.insert_one("widgets", {"name": "delta"})
    deleted = await client.delete_one("widgets", {"_id": doc_id})
    assert deleted is True
    assert await client.find_one("widgets", {"_id": doc_id}) is None


@pytest.mark.asyncio
async def test_delete_one_missing_returns_false(client: StoreClient):
    deleted = await client.delete_one("widgets", {"_id": str(uuid.uuid4())})
    assert deleted is False


@pytest.mark.asyncio
async def test_count(client: StoreClient):
    suffix = uuid.uuid4().hex[:6]
    for i in range(4):
        await client.insert_one("counted", {"batch": suffix, "n": i})

    assert await client.count("counted", {"batch": suffix}) == 4
    assert await client.count("counted", {"batch": "no-such-batch"}) == 0


@pytest.mark.asyncio
async def test_multi_client_registry(mongo_url: str):
    c1 = register("multi-a", url=mongo_url, database="multi_a_db")
    c2 = register("multi-b", url=mongo_url, database="multi_b_db")
    assert get_client("multi-a") is c1
    assert get_client("multi-b") is c2
    assert c1 is not c2
    await c1.close()
    await c2.close()


@pytest.mark.asyncio
async def test_get_unregistered_client_raises():
    with pytest.raises(ClientNotRegisteredError):
        get_client("not-registered-xyz-abc")


@pytest.mark.asyncio
async def test_module_level_convenience_functions(mongo_url: str):
    import forktex_core.store as store

    await store.init(url=mongo_url, database=f"convenience_{uuid.uuid4().hex[:8]}")
    try:
        doc_id = await store.insert_one("notes", {"text": "hello"})
        found = await store.find_one("notes", {"_id": doc_id})
        assert found["text"] == "hello"
        assert await store.count("notes") == 1
        assert await store.update_one("notes", {"_id": doc_id}, {"text": "updated"})
        assert await store.delete_one("notes", {"_id": doc_id})
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Transactions — require the MongoDB deployment to be a replica set
# (tests/_containers.py::start_mongo configures a single-node one).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transaction_commits_across_collections(client: StoreClient):
    async with client.transaction() as session:
        order_id = await client.insert_one("orders", {"sku": "widget-1", "qty": 2}, session=session)
        await client.insert_one("inventory", {"sku": "widget-1", "stock": 8}, session=session)

    order = await client.find_one("orders", {"_id": order_id})
    inventory = await client.find_one("inventory", {"sku": "widget-1"})
    assert order is not None
    assert inventory is not None
    assert inventory["stock"] == 8


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_exception(client: StoreClient):
    with pytest.raises(ValueError, match="boom"):
        async with client.transaction() as session:
            await client.insert_one("orders", {"sku": "widget-2"}, id="order-rollback", session=session)
            raise ValueError("boom")

    assert await client.find_one("orders", {"_id": "order-rollback"}) is None


@pytest.mark.asyncio
async def test_operation_without_session_does_not_see_uncommitted_writes(
    client: StoreClient,
):
    """Reading your own writes inside an open transaction requires passing
    the same session explicitly — this is real Mongo isolation semantics,
    not a store-specific quirk, and confirms session= is actually wired
    through to the underlying pymongo call rather than silently ignored."""
    async with client.transaction() as session:
        doc_id = await client.insert_one("orders", {"sku": "widget-3"}, session=session)
        # No session= passed here — must not observe the uncommitted insert.
        assert await client.find_one("orders", {"_id": doc_id}) is None
        # With the session, the write is visible (read-your-own-writes).
        assert await client.find_one("orders", {"_id": doc_id}, session=session) is not None

    # After commit, visible without a session too.
    assert await client.find_one("orders", {"_id": doc_id}) is not None
