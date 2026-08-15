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

"""Promoted columns — materialise a virtual column into a real physical column.

The grid stores every logical table's rows in one shared ``grid_row`` JSONB
table, so a promoted column cannot become a native column *on* ``grid_row``
(that would leak into every table). Instead each logical table gets its own
**sidecar table** ``grid_promoted_<hash>`` keyed 1:1 to ``grid_row`` by row id,
with one native, correctly-typed column per promoted column.

Payload stays the source of truth; the sidecar is a maintained projection (the
same pattern as ``ref`` → ``grid_edge``). Promotion buys you real physical
columns: native ``UNIQUE`` / ``NOT NULL`` constraints, native indexes, and a
first-class target for external SQL, BI tools, or native foreign keys.

``reconcile_table_promoted`` creates/extends the sidecar (+ backfills existing
rows); the write path calls :func:`sync_promoted` to keep it in step. Temporal
types (``date``/``datetime``) are stored as canonical ISO text and are not
promotable (their ``text::date`` cast is not immutable); ``json`` / ``ref`` /
``derived`` are non-scalar and also excluded — see
:func:`forktex_core.grid.domain.fieldtypes.is_promotable`.

The sidecar's shape is per-table and known only at runtime, which is why this
used to be hand-built SQL. It is expressed instead as a Core ``Table`` assembled
on the fly (:func:`_sidecar_table`) and driven through
:mod:`forktex_core.database.ddl` / ``postgresql.insert``: identifiers are then
quoted by the dialect's preparer rather than by f-strings, and payload values
bind through their column's type instead of a hand-written ``CAST``.
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.database import ddl, reflect
from forktex_core.database.integrity import integrity_boundary
from forktex_core.grid.domain.enums import Materialization
from forktex_core.grid.domain.fieldtypes import get_field_type
from forktex_core.grid.identifiers import is_identifier, validate_schema
from forktex_core.grid.persist import GridColumn, GridRow, GridTable, resolve_schema
from forktex_core.log import get_logger

logger = get_logger(__name__)


def sidecar_table_name(table_id: object) -> str:
    """Deterministic per-table sidecar name (``≤63`` chars)."""
    digest = zlib.crc32(str(table_id).encode()) & 0xFFFFFFFF
    return f"grid_promoted_{digest:08x}"


def _sql_type(column: GridColumn) -> sa.types.TypeEngine[Any]:
    handler = get_field_type(column.type_id)
    config = handler.validate_config(column.config)
    return handler.promoted_type(config=config)


def _promoted_columns(columns: Sequence[GridColumn]) -> list[GridColumn]:
    return [c for c in columns if c.materialization == Materialization.promoted and c.promoted_column]


def _sidecar_table(schema: str, name: str, promoted: Sequence[GridColumn]) -> sa.Table:
    """The sidecar as a Core ``Table``: ``row_id`` PK plus one native column per
    promoted column.

    Built on a throwaway ``MetaData`` each call — the shape is derived from
    ``grid_column`` rows at runtime, so it must never join the mapped metadata.
    A stub ``grid_row`` joins that same ``MetaData`` purely so the ``row_id``
    foreign key has a resolvable target at compile time; it is never emitted.
    """
    metadata = sa.MetaData()
    sa.Table("grid_row", metadata, sa.Column("id", sa.UUID(as_uuid=True), primary_key=True), schema=schema)
    columns: list[sa.Column[Any]] = [
        sa.Column(
            "row_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey(f"{schema}.grid_row.id", ondelete="CASCADE"),
            primary_key=True,
        )
    ]
    for col in promoted:
        native = col.promoted_column
        if native is None:  # pragma: no cover - callers filter these out
            continue
        columns.append(sa.Column(native, _sql_type(col)))
    return sa.Table(name, metadata, *columns, schema=schema)


async def reconcile_table_promoted(
    session: AsyncSession,
    *,
    table: GridTable,
    schema: str = "forktex_grid",
) -> str | None:
    """Ensure the sidecar table + native columns exist, and backfill from payload.

    Idempotent and self-converging: native columns for columns that were archived
    or un-promoted are dropped, and a promoted column's unique index is dropped
    when ``is_unique`` is flipped off — so the sidecar never enforces a stale
    constraint. Returns the sidecar name, or ``None`` if nothing is promoted.
    """
    validate_schema(schema)
    all_columns = list(await session.scalars(sa.select(GridColumn).where(GridColumn.table_id == table.id)))
    promoted = [
        c
        for c in all_columns
        if c.archived_at is None and c.materialization == Materialization.promoted and c.promoted_column
    ]
    name = sidecar_table_name(table.id)
    sidecar = _sidecar_table(schema, name, promoted)
    exists = await reflect.has_table(session, name, schema=schema)

    if not promoted:
        # Nothing left to promote — drop a lingering sidecar rather than leave it.
        if exists:
            logger.info(
                "grid: dropping sidecar table (nothing promoted)",
                extra={"schema": schema, "sidecar": name, "table_id": str(table.id)},
            )
            await session.execute(ddl.DropTable(sidecar, if_exists=True))
            await session.flush()
        return None

    if not exists:
        logger.info(
            "grid: creating sidecar table",
            extra={"schema": schema, "sidecar": name, "table_id": str(table.id)},
        )
    # Create with the key column only; the promoted columns are added below via
    # the same ADD COLUMN path that extends an existing sidecar, so a fresh and
    # an evolving sidecar converge through one code path.
    await session.execute(ddl.CreateTable(_sidecar_table(schema, name, ()), if_not_exists=True))

    # Drop native columns that are no longer promoted (archived / un-promoted).
    # DROP COLUMN also removes that column's unique index.
    desired = {c.promoted_column for c in promoted}
    present = await reflect.columns(session, name, schema=schema) - {"row_id"}
    for stale in present - desired:
        # The names originate in our own schema; the preparer quotes them either
        # way, but a name we would never have written is a sign of drift, so skip
        # rather than alter something unexpected.
        if is_identifier(stale):
            logger.info(
                "grid: dropping stale promoted column from sidecar",
                extra={"schema": schema, "sidecar": name, "column": stale},
            )
            await session.execute(ddl.DropColumn(name, stale, schema=schema, if_exists=True))

    for col in promoted:
        native = col.promoted_column
        if native is None:  # (filtered above; narrows for the type checker)
            continue
        column = sidecar.c[native]
        if native not in present:
            logger.info(
                "grid: adding promoted column to sidecar",
                extra={
                    "schema": schema,
                    "sidecar": name,
                    "column": native,
                    "sql_type": reflect.type_ddl(column.type),
                    "key": col.key,
                },
            )
        await session.execute(ddl.AddColumn(column, if_not_exists=True))
        idx = sidecar_index_name(name, native, unique=True)
        if col.is_unique:
            await session.execute(ddl.CreateIndex(sa.Index(idx, column, unique=True), if_not_exists=True))
        else:  # flipped off → the sidecar must stop enforcing uniqueness
            await session.execute(ddl.DropIndex(sa.Index(idx, column), if_exists=True))

    # Backfill: pull each promoted value out of payload for every live row.
    # `payload ->> key` yields text, so each value is cast to its native column
    # type — the cast the previous string-built SQL spelled out by hand.
    selected = [GridRow.id] + [GridRow.payload[c.key].astext.cast(_sql_type(c)) for c in promoted if c.promoted_column]
    source = sa.select(*selected).where(GridRow.table_id == table.id, GridRow.archived_at.is_(None))
    targets = ["row_id"] + [c.promoted_column for c in promoted if c.promoted_column]
    backfill = pg_insert(sidecar).from_select(targets, source)
    await session.execute(
        backfill.on_conflict_do_update(
            index_elements=[sidecar.c.row_id],
            set_={n: backfill.excluded[n] for n in targets if n != "row_id"},
        )
    )
    await session.flush()
    return name


def sidecar_index_name(sidecar: str, native: str, *, unique: bool) -> str:
    digest = zlib.crc32(f"{sidecar}|{native}|{unique}".encode()) & 0xFFFFFFFF
    return f"{'spux' if unique else 'spix'}_{digest:08x}"


async def sync_promoted(
    session: AsyncSession,
    *,
    table: GridTable,
    rows: Sequence,
    columns: Sequence[GridColumn],
) -> None:
    """Dual-write the promoted values of ``rows`` into the sidecar (upsert).

    No-op when the table has no promoted columns or the sidecar isn't
    materialised yet (payload holds the values; ``reconcile_table_promoted``
    creates the sidecar and backfills them). The physical schema is resolved
    from the session so this works under ``schema_translate_map``.
    """
    promoted = _promoted_columns(columns)
    if not promoted or not rows:
        return
    schema = resolve_schema(session)
    name = sidecar_table_name(table.id)
    if not await reflect.has_table(session, name, schema=schema):
        return  # not reconciled yet

    sidecar = _sidecar_table(schema, name, promoted)
    natives = [c.promoted_column for c in promoted if c.promoted_column]
    # A native UNIQUE on a promoted column can fire here — translate it like the
    # rest of the write path (409, not a raw 500).
    async with integrity_boundary():
        for row in rows:
            # Values bind through each column's own type, so the explicit
            # `CAST(:v AS <type>)` the raw statement carried is unnecessary.
            values: dict[str, object] = {"row_id": row.id}
            for col in promoted:
                if col.promoted_column:
                    values[col.promoted_column] = row.payload.get(col.key)
            stmt = pg_insert(sidecar).values(**values)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[sidecar.c.row_id],
                    set_={n: stmt.excluded[n] for n in natives},
                )
            )


__all__ = ["reconcile_table_promoted", "sidecar_table_name", "sync_promoted"]
