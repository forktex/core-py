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

"""Gapless monotonic sequence allocator backed by ``grid_row``.

A tenant-scoped counter (issued document numbers, ticket ids, batch counters —
anything needing strictly gapless ``1, 2, 3, …`` under concurrent load),
materialised as a virtual row so it lives in the same JSONB storage every other
row does. ``next_in_series`` does the load + allocate + save in one Postgres
statement, so concurrent issuers serialise on the row-level lock the upsert
acquires:

    INSERT INTO grid_row (id, table_id, namespace, payload) VALUES (...)
    ON CONFLICT (id) DO UPDATE
        SET payload = jsonb_set(grid_row.payload, '{current_sequence}',
                                to_jsonb((payload->>'current_sequence')::int + 1))
    RETURNING (payload->>'current_sequence')::int

Atomicity comes from a deterministic UUID derived from the series key
(``namespace + table_slug + key tuple``): same input → same UUID everywhere, so
the upsert always lands on the same row and the row lock serialises concurrent
calls. A ``pg_advisory_xact_lock`` on that id adds defense in depth.

Invariants: strictly monotonic, gapless (N concurrent calls yield a contiguous
range), no duplicates (unique PK on ``grid_row.id``).
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.database.connection import with_transactional_session
from forktex_core.database.locks import key_from_uuid, xact_lock
from forktex_core.error import NotFoundError
from forktex_core.grid.persist import GridRow, GridTable

# Stable namespace UUID — same input → same UUID across processes/machines.
_SERIES_UUID_NAMESPACE = uuid.UUID("c0c0c0c0-0000-0000-0000-000000000001")
_SEQUENCE_KEY = "current_sequence"


def derive_series_row_id(namespace: str, table_slug: str, key: tuple[str, ...]) -> uuid.UUID:
    """The deterministic ``GridRow.id`` for a series key (public for peek/reset)."""
    composite = ":".join((namespace, table_slug, *key))
    return uuid.uuid5(_SERIES_UUID_NAMESPACE, composite)


@with_transactional_session
async def next_in_series(
    session: AsyncSession,
    *,
    namespace: str,
    table_slug: str,
    series_key: tuple[str, ...],
    initial_payload: dict[str, Any] | None = None,
) -> int:
    """Atomically allocate the next sequence number for ``series_key``.

    First call creates the row with ``current_sequence=1`` (or
    ``initial_payload['current_sequence']``); later calls increment in place and
    return the new value. Concurrent callers serialise on the row lock + advisory
    lock, so the returned integers are strictly monotonic and gapless.
    """
    table_id = await _resolve_table_id(session, namespace=namespace, slug=table_slug)
    row_id = derive_series_row_id(namespace, table_slug, series_key)

    # Transaction-scoped, so it releases with the caller's transaction. The key
    # folding lives in `database.locks` — grid had its own copy of the same
    # arithmetic, alongside two other divergent lock derivations.
    await xact_lock(session, key_from_uuid(row_id))

    payload: dict[str, Any] = dict(initial_payload or {})
    payload.setdefault(_SEQUENCE_KEY, 1)

    stmt = (
        pg_insert(GridRow)
        .values(id=row_id, table_id=table_id, namespace=namespace, payload=payload, is_active=True)
        .on_conflict_do_update(
            index_elements=[GridRow.id],
            set_={
                "payload": sa.func.jsonb_set(
                    GridRow.payload,
                    sa.literal_column(f"'{{{_SEQUENCE_KEY}}}'"),
                    sa.func.to_jsonb(sa.cast(GridRow.payload[_SEQUENCE_KEY].astext, sa.BigInteger) + 1),
                ),
                "updated_at": sa.func.now(),
            },
        )
        .returning(sa.cast(GridRow.payload[_SEQUENCE_KEY].astext, sa.BigInteger))
    )
    result = await session.execute(stmt)
    return result.scalar_one()


@with_transactional_session
async def peek_series(
    session: AsyncSession, *, namespace: str, table_slug: str, series_key: tuple[str, ...]
) -> int | None:
    """Read the current value without allocating (``None`` if never allocated)."""
    row = await session.scalar(
        select(GridRow).where(GridRow.id == derive_series_row_id(namespace, table_slug, series_key))
    )
    if row is None or row.payload.get(_SEQUENCE_KEY) is None:
        return None
    return int(row.payload[_SEQUENCE_KEY])


@with_transactional_session
async def reset_series(
    session: AsyncSession, *, namespace: str, table_slug: str, series_key: tuple[str, ...], to_value: int = 0
) -> None:
    """Reset the counter (admin/test only — violates the gapless invariant)."""
    row = await session.scalar(
        select(GridRow).where(GridRow.id == derive_series_row_id(namespace, table_slug, series_key))
    )
    if row is None:
        return
    row.payload = {**row.payload, _SEQUENCE_KEY: to_value}
    await session.flush()


async def _resolve_table_id(session: AsyncSession, *, namespace: str, slug: str) -> uuid.UUID:
    table_id = await session.scalar(
        select(GridTable.id).where(GridTable.namespace == namespace, GridTable.slug == slug)
    )
    if table_id is None:
        raise NotFoundError(f"Table '{slug}' not found in namespace '{namespace}'", details={"slug": slug})
    return table_id


__all__ = ["derive_series_row_id", "next_in_series", "peek_series", "reset_series"]
