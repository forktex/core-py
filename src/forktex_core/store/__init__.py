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

"""Schemaless document store connector (MongoDB-first).

Thin async connector — no schema validation, no aggregation pipeline
builder. Distinct from ``forktex_core.storage``, which holds opaque binary
blobs; this holds structured BSON/JSON documents with query support.

Multi-database services use ``register`` + ``get_client``; single-database
services use the module-level convenience functions (``init`` / ``insert_one``
/ ``find`` / etc.) which operate on the ``"default"`` client.

## Single-database (default client)

    import forktex_core.store as store

    await store.init(url="mongodb://localhost:27017", database="app")

    doc_id = await store.insert_one("events", {"kind": "signup", "user_id": "u-1"})
    event = await store.find_one("events", {"_id": doc_id})
    events = await store.find("events", {"kind": "signup"}, limit=50)
    await store.close()

## Multi-database (named clients)

    from forktex_core.store import register, get_client

    register("analytics", url=..., database="analytics")
    register("audit",     url=..., database="audit")

    await get_client("audit").insert_one("log", {"actor": "u-1", "action": "delete"})

## Multi-document transactions

Requires the MongoDB deployment to be a replica set (or sharded cluster) —
a standalone ``mongod`` raises ``pymongo.errors.OperationFailure`` on
``start_transaction()``.

    async with store.transaction() as session:
        await store.insert_one("orders", order_doc, session=session)
        await store.update_one(
            "inventory", {"_id": sku}, {"stock": new_stock}, session=session
        )
    # commits automatically on clean exit; aborts on exception

Requires: pip install forktex-core[store]  (pymongo)
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any

from forktex_core.error import AppError, AppErrorCode
from forktex_core.log import get_logger
from forktex_core.types import BaseValueObject

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pymongo.asynchronous.client_session import AsyncClientSession
    from pymongo.asynchronous.mongo_client import AsyncMongoClient

logger = get_logger(__name__)


class StoreError(AppError):
    """Base class for store errors.

    An ``AppError`` so an HTTP transport renders it via the shared
    envelope rather than a masked 500 — see ``forktex_core.error``.
    """

    code = AppErrorCode.INTERNAL


class ClientNotRegisteredError(StoreError):
    """Raised when ``get_client(name)`` is called for an unregistered name.

    ``INTERNAL`` — a missing ``register()`` call is a wiring mistake, not
    caller-fixable.
    """

    code = AppErrorCode.INTERNAL


class StoreConfig(BaseValueObject):
    url: str
    """MongoDB connection URI, e.g. ``mongodb://localhost:27017``."""
    database: str
    """Logical database name within the MongoDB deployment."""


def _make_client(url: str) -> AsyncMongoClient:
    try:
        from pymongo import AsyncMongoClient
    except ImportError as exc:
        raise ImportError("Install 'forktex-core[store]' (pymongo) to use forktex_core.store") from exc
    return AsyncMongoClient(url)


def _to_query_id(value: object) -> object:
    """Coerce a string that looks like a valid ``ObjectId`` back to one.

    Auto-generated ids are stored as real ``ObjectId``s (the ordinary,
    idiomatic MongoDB default) — a string filter would never match one (a
    string and an ``ObjectId`` are different BSON types even when they
    print the same), so lookups must convert back. Caller-supplied custom
    ids that don't look like a 24-hex-char ``ObjectId`` are left untouched,
    since they were stored as literal strings.

    Edge case this can't disambiguate: a caller-supplied custom id that
    happens to *also* be valid ``ObjectId`` hex form gets coerced here too,
    which would mismatch the literal string it was actually stored as. Rare
    in practice — avoid raw 24-hex-char custom ids if this matters.
    """
    from bson import ObjectId

    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    return value


def _normalize_filter(filter: dict[str, Any]) -> dict[str, Any]:
    if "_id" in filter:
        filter = dict(filter)
        filter["_id"] = _to_query_id(filter["_id"])
    return filter


def _normalize(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert MongoDB's ``ObjectId`` ``_id`` to a plain string.

    Every document this module hands back has a string ``_id`` — matching
    the rest of the codebase's convention of string identifiers everywhere
    — instead of leaking a ``bson.ObjectId`` that isn't JSON-serializable
    by default.
    """
    if doc is None:
        return None
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc


class StoreClient:
    """Async MongoDB client scoped to a single logical database.

    Obtained via ``register(name, ...)`` + ``get_client(name)``, or constructed
    directly with a ``StoreConfig``. Holds one persistent ``AsyncMongoClient``
    for its lifetime — unlike ``forktex_core.storage``'s per-call clients,
    MongoDB's async client is designed to be constructed once and reused; it
    manages its own internal connection pool.
    """

    def __init__(self, config: StoreConfig) -> None:
        self._config = config
        self._client = _make_client(config.url)
        self._db = self._client[config.database]

    async def insert_one(
        self,
        collection: str,
        document: dict[str, Any],
        *,
        id: str | None = None,
        session: AsyncClientSession | None = None,
    ) -> str:
        """Insert ``document`` into ``collection``. Returns the document id.

        Pass ``id`` to store a caller-supplied string id (e.g. a natural
        key); otherwise MongoDB assigns a real ``ObjectId`` (returned as
        its string form).
        """
        payload = dict(document)
        if id is not None:
            payload["_id"] = id
        result = await self._db[collection].insert_one(payload, session=session)
        return str(result.inserted_id)

    async def find_one(
        self,
        collection: str,
        filter: dict[str, Any],
        *,
        session: AsyncClientSession | None = None,
    ) -> dict[str, Any] | None:
        """Return the first document matching ``filter``, or ``None``."""
        doc = await self._db[collection].find_one(_normalize_filter(filter), session=session)
        return _normalize(doc)

    async def find(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
        *,
        limit: int = 100,
        session: AsyncClientSession | None = None,
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` documents matching ``filter`` (default: all)."""
        query = _normalize_filter(filter) if filter else {}
        cursor = self._db[collection].find(query, session=session).limit(limit)
        return [doc for doc in [_normalize(d) async for d in cursor] if doc is not None]

    async def update_one(
        self,
        collection: str,
        filter: dict[str, Any],
        update: dict[str, Any],
        *,
        upsert: bool = False,
        session: AsyncClientSession | None = None,
    ) -> bool:
        """Set fields in ``update`` on the first document matching ``filter``.

        Returns ``True`` if a document was modified or (with ``upsert=True``)
        newly inserted.
        """
        result = await self._db[collection].update_one(
            _normalize_filter(filter), {"$set": update}, upsert=upsert, session=session
        )
        return result.modified_count > 0 or result.upserted_id is not None

    async def delete_one(
        self,
        collection: str,
        filter: dict[str, Any],
        *,
        session: AsyncClientSession | None = None,
    ) -> bool:
        """Delete the first document matching ``filter``. Returns ``True`` if
        a document was actually deleted."""
        result = await self._db[collection].delete_one(_normalize_filter(filter), session=session)
        return result.deleted_count > 0

    async def count(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
        *,
        session: AsyncClientSession | None = None,
    ) -> int:
        """Count documents matching ``filter`` (default: all in collection)."""
        query = _normalize_filter(filter) if filter else {}
        return await self._db[collection].count_documents(query, session=session)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncClientSession]:
        """Start a multi-document transaction. Commits on clean exit, aborts
        on exception.

        Requires the MongoDB deployment to be a replica set (or sharded
        cluster) — a standalone ``mongod`` raises
        ``pymongo.errors.OperationFailure`` immediately.

        Usage::

            async with client.transaction() as session:
                await client.insert_one("orders", order_doc, session=session)
                await client.update_one(
                    "inventory", {"_id": sku}, {"stock": new_stock}, session=session
                )
        """
        async with (
            self._client.start_session() as session,
            await session.start_transaction(),
        ):
            yield session

    async def close(self) -> None:
        """Close the underlying MongoDB client connection."""
        await self._client.close()


_clients: dict[str, StoreClient] = {}


def register(name: str, url: str, database: str) -> StoreClient:
    """Register a named ``StoreClient`` and return it.

    Idempotent: calling ``register`` with the same name replaces the previous
    client (useful for reconfiguration without a restart).

    Args:
        name: Logical name used with ``get_client(name)``.
              Use ``"default"`` if you only have one database.
    """
    cfg = StoreConfig(url=url, database=database)
    client = StoreClient(cfg)
    _clients[name] = client
    return client


def get_client(name: str = "default") -> StoreClient:
    """Return a registered ``StoreClient`` by name.

    Raises ``ClientNotRegisteredError`` if the name has not been registered.
    """
    try:
        return _clients[name]
    except KeyError:
        registered = ", ".join(f'"{k}"' for k in _clients) or "(none)"
        raise ClientNotRegisteredError(
            f"No store client named {name!r}. "
            f"Registered clients: {registered}. "
            f"Call store.register({name!r}, ...) at startup."
        ) from None


def deregister(name: str = "default") -> StoreClient | None:
    """Remove ``name`` from the registry and return the dropped client (or
    ``None`` if it wasn't registered). Idempotent."""
    return _clients.pop(name, None)


async def init(url: str, database: str) -> None:
    """Initialize the default store client. Equivalent to
    ``register("default", ...)``.
    """
    register("default", url, database)


async def close(name: str = "default") -> None:
    """Close and deregister a named client. Idempotent."""
    client = deregister(name)
    if client is not None:
        await client.close()


async def insert_one(
    collection: str,
    document: dict[str, Any],
    *,
    id: str | None = None,
    session: AsyncClientSession | None = None,
) -> str:
    return await get_client().insert_one(collection, document, id=id, session=session)


async def find_one(
    collection: str,
    filter: dict[str, Any],
    *,
    session: AsyncClientSession | None = None,
) -> dict[str, Any] | None:
    return await get_client().find_one(collection, filter, session=session)


async def find(
    collection: str,
    filter: dict[str, Any] | None = None,
    *,
    limit: int = 100,
    session: AsyncClientSession | None = None,
) -> list[dict[str, Any]]:
    return await get_client().find(collection, filter, limit=limit, session=session)


async def update_one(
    collection: str,
    filter: dict[str, Any],
    update: dict[str, Any],
    *,
    upsert: bool = False,
    session: AsyncClientSession | None = None,
) -> bool:
    return await get_client().update_one(collection, filter, update, upsert=upsert, session=session)


async def delete_one(
    collection: str,
    filter: dict[str, Any],
    *,
    session: AsyncClientSession | None = None,
) -> bool:
    return await get_client().delete_one(collection, filter, session=session)


async def count(
    collection: str,
    filter: dict[str, Any] | None = None,
    *,
    session: AsyncClientSession | None = None,
) -> int:
    return await get_client().count(collection, filter, session=session)


def transaction() -> AbstractAsyncContextManager[AsyncClientSession]:
    """Start a multi-document transaction on the default client. See
    ``StoreClient.transaction`` for usage and requirements."""
    return get_client().transaction()


__all__ = [
    "ClientNotRegisteredError",
    "StoreClient",
    "StoreConfig",
    "StoreError",
    "close",
    "count",
    "delete_one",
    "find",
    "find_one",
    "get_client",
    "init",
    "insert_one",
    "register",
    "transaction",
    "update_one",
]
