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

"""``Bundle`` — bundle of related Grids with shared rich-content config.

The Bundle facade composes multiple ``Grid`` views under one namespace
and shared ``BundleConfig``. It owns the persisted ``GridSpace``
record so consumer code has a single place to declare 'these Grids
belong together' (cross-grid traversal scope, vector collection
prefix, storage bucket, edge vocabulary).

``Bundle`` provides construction, member-Grid binding, and persistence
of the ``GridSpace`` row. Cross-Grid graph traversal uses ``[graph]``
algebra over row-relations; VECTOR/FILE handler dispatch uses
``[vector]`` / ``[storage]``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.error import AlreadyExistsError, NotFoundError
from forktex_core.graph import subgraph_around
from forktex_core.grid import Grid
from forktex_core.grid.persist import GridSpace, GridTable
from forktex_core.space.config import BundleConfig, SyncSourceConfig
from forktex_core.space.traversal import bundle_to_graph

if TYPE_CHECKING:
    # Typing only: `graph` is imported lazily inside the methods below so `space`
    # does not require it at import time.
    from forktex_core.graph import Graph


async def _member_table(session: AsyncSession, grid: Grid) -> GridTable:
    """The persisted ``GridTable`` behind a member ``Grid`` handle.

    A Bundle bundles member tables by stamping their ``space_id``; the ``Grid``
    handle exposes a lightweight ``ref`` rather than the ORM row, so fetch it here.
    """
    table = await session.get(GridTable, grid.ref.id)
    if table is None:  # a live Grid handle always has its catalog row
        raise NotFoundError(f"grid table {grid.slug!r} was not found")
    return table


class Bundle(BaseModel):
    """Bundle of Grids sharing a namespace + config.

    Construct via :meth:`Bundle.declare` (creates the persisted row +
    binds the listed Grids) or :meth:`Bundle.bind` (loads an existing
    Bundle and re-binds its member Grids). Use :meth:`Bundle.grid` to
    fetch a member Grid by slug.

    Mutable on purpose: ``grids`` is the live binding map and consumers
    may add/remove members in-process during a materialisation flow.
    Persistence is explicit — call :meth:`Bundle.materialize` to
    reconcile member ``GridTable`` rows with their declared ``space_id`` FK.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: AsyncSession
    namespace: str
    record: GridSpace
    config: BundleConfig = Field(default_factory=BundleConfig)
    sync_sources: tuple[SyncSourceConfig, ...] = ()
    grids: dict[str, Grid] = Field(default_factory=dict)

    @property
    def slug(self) -> str:
        return self.record.slug

    @property
    def id(self) -> uuid.UUID:
        return self.record.id

    @classmethod
    async def declare(
        cls,
        session: AsyncSession,
        *,
        namespace: str,
        slug: str,
        label: str | None = None,
        config: BundleConfig | None = None,
        sync_sources: Iterable[SyncSourceConfig] = (),
        members: Iterable[Grid] = (),
    ) -> Bundle:
        """Create a Bundle row + bind member Grids.

        ``members`` are pre-existing ``Grid`` instances (typically
        created via ``Grid.declare``) to attach to the Bundle. Their
        ``GridTable.space_id`` is reconciled in :meth:`materialize`.
        """
        existing = await session.scalar(
            sa.select(GridSpace).where(
                GridSpace.namespace == namespace,
                GridSpace.slug == slug,
            )
        )
        if existing is not None:
            raise AlreadyExistsError(
                f"Bundle '{slug}' already exists",
                details={"field": "slug"},
            )
        record = GridSpace(
            namespace=namespace,
            slug=slug,
            label=label or slug,
            config={
                "space": (config or BundleConfig()).model_dump(),
                "sync_sources": [s.model_dump() for s in sync_sources],
            },
        )
        session.add(record)
        await session.flush()
        space = cls(
            session=session,
            namespace=namespace,
            record=record,
            config=config or BundleConfig(),
            sync_sources=tuple(sync_sources),
            grids={g.slug: g for g in members},
        )
        await space.materialize()
        return space

    @classmethod
    async def bind(
        cls,
        session: AsyncSession,
        *,
        namespace: str,
        slug: str,
    ) -> Bundle:
        """Load an existing Bundle + rebind its member Grids."""
        record = await session.scalar(
            sa.select(GridSpace).where(
                GridSpace.namespace == namespace,
                GridSpace.slug == slug,
            )
        )
        if record is None:
            raise NotFoundError(
                f"Bundle '{slug}' was not found",
                details={"resource": "register_space", "identifier": slug},
            )
        stored = record.config or {}
        config = BundleConfig.model_validate(stored.get("space") or {})
        sync_sources = tuple(SyncSourceConfig.model_validate(item) for item in (stored.get("sync_sources") or []))
        member_entities = (
            (
                await session.execute(
                    sa.select(GridTable).where(
                        GridTable.namespace == namespace,
                        GridTable.space_id == record.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        grids = {
            entity.slug: await Grid.open(session, slug=entity.slug, namespace=namespace) for entity in member_entities
        }
        return cls(
            session=session,
            namespace=namespace,
            record=record,
            config=config,
            sync_sources=sync_sources,
            grids=grids,
        )

    def grid(self, slug: str) -> Grid:
        """Fetch a member Grid by slug. Raises ``KeyError`` if missing."""
        try:
            return self.grids[slug]
        except KeyError as e:
            raise KeyError(f"Bundle {self.slug!r} has no member Grid {slug!r}") from e

    async def attach(self, grid: Grid) -> None:
        """Bring an existing Grid into this Bundle.

        Stamps ``GridTable.space_id`` and stages the change on the
        session. Idempotent — re-attaching an already-attached Grid
        is a no-op."""
        entity = await _member_table(self.session, grid)
        if entity.space_id == self.record.id:
            self.grids[grid.slug] = grid
            return
        entity.space_id = self.record.id
        self.session.add(entity)
        await self.session.flush()
        self.grids[grid.slug] = grid

    async def detach(self, grid_slug: str) -> None:
        """Remove a Grid from this Bundle (clears its ``space_id``)."""
        grid = self.grid(grid_slug)
        entity = await _member_table(self.session, grid)
        entity.space_id = None
        self.session.add(entity)
        await self.session.flush()
        del self.grids[grid_slug]

    async def materialize(self) -> None:
        """Idempotent reconciler — ensures every bound member's
        ``GridTable.space_id`` points at this Bundle's record id."""
        for grid in self.grids.values():
            entity = await _member_table(self.session, grid)
            if entity.space_id != self.record.id:
                entity.space_id = self.record.id
                self.session.add(entity)
        await self.session.flush()

    async def list_grids(self) -> list[Grid]:
        """All Grids attached to this Bundle, freshly fetched from the
        database (the in-memory ``grids`` map may lag if other writers
        attached members in a different session)."""
        result = await self.session.execute(
            sa.select(GridTable).where(
                GridTable.namespace == self.namespace,
                GridTable.space_id == self.record.id,
            )
        )
        entities = list(result.scalars().all())
        return [await Grid.open(self.session, slug=ent.slug, namespace=self.namespace) for ent in entities]

    async def to_graph(
        self,
        *,
        entity_slugs: Iterable[str] | None = None,
        include_inactive: bool = False,
    ) -> Graph:
        """Materialise an in-memory ``[graph].Graph`` snapshot of this
        Bundle's rows + relation edges.

        See :func:`forktex_core.space.traversal.bundle_to_graph` for
        the field shape — node ``kind`` is the entity slug; edge
        ``kind`` is the relation key.
        """

        return await bundle_to_graph(
            self,
            entity_slugs=entity_slugs,
            include_inactive=include_inactive,
        )

    async def traverse(
        self,
        start_row_id: uuid.UUID,
        *,
        max_depth: int = 3,
        edge_kind: str | None = None,
        direction: str = "both",
        entity_slugs: Iterable[str] | None = None,
    ) -> Graph:
        """Subgraph reachable from ``start_row_id`` within ``max_depth`` hops.

        Loads a snapshot via :meth:`to_graph` then delegates to
        ``[graph].subgraph_around``. Returns the resulting ``Graph``.
        """

        snapshot = await self.to_graph(entity_slugs=entity_slugs)
        return subgraph_around(
            snapshot,
            str(start_row_id),
            max_depth=max_depth,
            edge_kind=edge_kind,
            direction=direction,
        )


__all__ = ["Bundle"]
