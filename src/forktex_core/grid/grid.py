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

"""``Grid`` — the curated entry point. A session-bound handle over one table that
covers the 90% flows so consumers never reach into ``app`` / ``persist`` / ``read``."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, PrivateAttr
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid.domain.enums import BrowseMode
from forktex_core.grid.domain.spec import ColumnSpec, RelationSpec, TableSpec
from forktex_core.grid.persist import GridRelation, reconcile, repos, schema_repo
from forktex_core.grid.persist.refs import TableRef
from forktex_core.grid.read.filters import FilterNode, SortKey, parse_filter
from forktex_core.grid.read.graph import Direction as _Direction
from forktex_core.grid.read.graph import traverse as _traverse
from forktex_core.grid.read.numbering import next_in_series as _next_in_series
from forktex_core.grid.read.query import run_query
from forktex_core.grid.read.result import Page, Row
from forktex_core.grid.write.write import RowWriter


class Grid(BaseModel):
    """A handle to one table in one namespace, bound to a session."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: AsyncSession
    ref: TableRef
    _writer: RowWriter = PrivateAttr(default_factory=RowWriter)

    @classmethod
    async def declare(cls, session: AsyncSession, spec: TableSpec) -> Grid:
        return cls(session=session, ref=await repos.create_table(session, spec))

    @classmethod
    async def open(cls, session: AsyncSession, *, slug: str, namespace: str = "") -> Grid:
        return cls(session=session, ref=await repos.load_table(session, slug, namespace))

    async def add_column(self, spec: ColumnSpec) -> None:
        """Evolve the schema: add a column, re-hydrating this handle's aggregate."""
        self.ref = await repos.add_column(self.session, self.ref, spec)

    async def alter_column(self, spec: ColumnSpec) -> None:
        """Update a column's non-type attributes (retype is not supported)."""
        await schema_repo.alter_column(self.session, table_id=self.ref.id, spec=spec)
        await self._refresh_physical()

    async def rename_column(self, key: str, new_key: str) -> None:
        """Rename a column, migrating its key inside every live row's payload."""
        await schema_repo.rename_column(self.session, table_id=self.ref.id, key=key, new_key=new_key)
        await self._refresh_physical()

    async def drop_column(self, key: str) -> None:
        """Soft-drop a column (payload data retained; its physical index/sidecar reconciled away)."""
        await schema_repo.archive_column(self.session, table_id=self.ref.id, key=key)
        await self._refresh_physical()

    async def _refresh_physical(self) -> None:
        await reconcile.reconcile_table(self.session, self.ref)
        self.ref = await repos.load_table(self.session, self.slug, self.namespace)

    @property
    def namespace(self) -> str:
        return self.ref.namespace

    @property
    def slug(self) -> str:
        return self.ref.domain.spec.slug

    @property
    def writable(self) -> bool:
        return self.ref.writable

    async def create(self, values: dict[str, Any], *, external_ref: uuid.UUID | None = None) -> Row:
        """Create a row. ``external_ref`` links an extension row 1:1 to a host row's PK."""
        return (await self._writer.create(self.session, self.ref, [values], [external_ref]))[0]

    async def create_many(self, rows: list[dict[str, Any]]) -> list[Row]:
        return await self._writer.create(self.session, self.ref, rows)

    async def get_by_external_ref(self, external_ref: uuid.UUID) -> Row | None:
        """The extension row linked to ``external_ref`` (a host row's PK), or ``None``."""
        row = await repos.get_row_by_external_ref(self.session, self.ref.id, external_ref)
        return None if row is None else Row(id=row.id, namespace=row.namespace, values=dict(row.payload))

    async def patch(self, row_id: uuid.UUID, values: dict[str, Any]) -> Row:
        return await self._writer.patch(self.session, self.ref, row_id, values)

    async def archive(self, row_id: uuid.UUID) -> None:
        await self._writer.archive(self.session, row_id)

    async def get(self, row_id: uuid.UUID) -> Row:
        row = await repos.get_row(self.session, row_id)
        return Row(id=row.id, namespace=row.namespace, values=dict(row.payload))

    async def query(
        self,
        *,
        filter: FilterNode | Mapping[str, Any] | None = None,
        sort: Sequence[SortKey | Mapping[str, str]] | None = None,
        mode: BrowseMode = BrowseMode.page,
        limit: int = 50,
        offset: int = 0,
        cursor: str | None = None,
        include_total: bool = False,
    ) -> Page:
        """Query rows. ``filter``/``sort`` accept the typed :class:`FilterNode`/:class:`SortKey`
        forms or their plain-dict wire equivalents (coerced here at the boundary)."""
        return await run_query(
            self.session,
            self.ref,
            filter=None if filter is None else parse_filter(filter),
            sort=None if sort is None else [SortKey.parse(k) for k in sort],
            mode=mode,
            limit=limit,
            offset=offset,
            cursor=cursor,
            include_total=include_total,
        )

    async def relate(self, rel_key: str, source_id: uuid.UUID, target_id: uuid.UUID) -> None:
        await self._writer.relate(self.session, rel_key, source_id, target_id, self.ref.namespace)

    async def unrelate(self, rel_key: str, source_id: uuid.UUID, target_id: uuid.UUID) -> None:
        await self._writer.unrelate(self.session, rel_key, source_id, target_id, self.ref.namespace)

    async def related(self, rel_key: str, source_id: uuid.UUID) -> list[Row]:
        return await self._writer.related(self.session, rel_key, source_id, self.ref.namespace)

    async def describe(self) -> TableSpec:
        """This table's round-trippable :class:`TableSpec` (columns + config). For the whole
        namespace including relations, use ``Namespace.describe``."""
        return self.ref.domain.spec

    async def traverse(
        self, start_id: uuid.UUID, *, direction: _Direction | str = _Direction.both, depth: int = 3
    ) -> dict[uuid.UUID, int]:
        """Breadth-first reachable rows → ``{row_id: shortest-path depth}``."""
        result = await _traverse(
            self.session,
            start_row_id=start_id,
            namespace=self.namespace,
            direction=_Direction(direction),
            max_depth=depth,
        )
        return dict(result.depth)

    async def next_number(self, series_key: str) -> int:
        """Allocate the next strictly-gapless counter in a named series."""
        return await _next_in_series(
            self.session, namespace=self.namespace, table_slug=self.ref.domain.spec.slug, series_key=(series_key,)
        )

    async def reconcile(self) -> None:
        await reconcile.reconcile_table(self.session, self.ref)


async def declare_relation(session: AsyncSession, spec: RelationSpec, namespace: str = "") -> GridRelation:
    """Declare a relation between two owned tables; reconcile its cardinality index."""
    relation = await repos.create_relation(session, spec, namespace)
    await reconcile.reconcile_relation(session, relation)
    return relation


__all__ = ["Grid", "declare_relation"]
