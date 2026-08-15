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

"""The index reconciler — declarative metadata → physical Postgres indexes.

This turns the schema's *declared intent* into real, integrity-bearing
Postgres indexes, idempotently:

- ``grid_index`` rows → btree / btree_numeric / trgm(GIN) indexes on
  ``grid_row.payload`` expressions; the row's ``state``/``physical_name`` are
  reconciled to ``live``.
- ``grid_column.is_unique`` (payload columns) → a partial **unique** index on
  ``(table_id, namespace, payload->>'key')`` so "declared unique" becomes
  "enforced unique" — per table and namespace, never schema-wide.
- ``grid_relation`` cardinality → partial unique indexes on ``grid_edge``
  endpoints (1:1 → both ends unique; 1:N → target end unique; N:1 → source end
  unique), giving DB-level cardinality on top of the application checks in
  ``relations``.

The index-kind registry (:func:`register_index_kind`) is the extension seam.
Indexes are scoped per table (btree leads with ``table_id``; GIN scopes via a
partial ``WHERE table_id = …``), never schema-wide. Names are deterministic
(crc32-hashed, ≤63 chars) so creation is idempotent (``CREATE INDEX IF NOT
EXISTS``). Indexes are standard (transactional); promoted-column sidecar tables
are handled separately in :mod:`forktex_core.grid.persist.reconcile.sidecar`.
"""

from __future__ import annotations

import zlib
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.database import ddl
from forktex_core.database.models import UtcDateTime
from forktex_core.error import BadRequestError
from forktex_core.grid.domain.enums import IndexState, Materialization
from forktex_core.grid.domain.fieldtypes import get_field_type
from forktex_core.grid.domain.fieldtypes.base import PG_CAST_TYPES
from forktex_core.grid.identifiers import validate_key, validate_schema
from forktex_core.grid.persist import GridColumn, GridIndex, GridTable
from forktex_core.log import get_logger
from forktex_core.types import BaseValueObject

logger = get_logger(__name__)


def index_name(prefix: str, *parts: object) -> str:
    """A deterministic, ≤63-char index name from its identity parts."""
    digest = zlib.crc32("|".join(str(p) for p in parts).encode()) & 0xFFFFFFFF
    return f"{prefix}_{digest:08x}"


class IndexKindSpec(BaseValueObject):
    """How to materialise one index kind over a payload expression.

    ``using`` is the access method (``btree`` / ``gin``); ``cast`` an optional
    SQL cast applied to the extracted text (e.g. ``numeric``); ``opclass`` an
    optional operator class (e.g. ``gin_trgm_ops``).
    """

    kind: str
    using: str = "btree"
    cast: str | None = None
    opclass: str | None = None

    def __init__(self, kind: str, **kwargs: object) -> None:
        """Accept ``kind`` positionally — the registry usage is ``IndexKindSpec("btree")``."""
        super().__init__(kind=kind, **kwargs)  # type: ignore[call-arg]


_KINDS: dict[str, IndexKindSpec] = {}


def register_index_kind(spec: IndexKindSpec, *, replace: bool = False) -> None:
    if not replace and spec.kind in _KINDS:
        raise ValueError(f"Index kind {spec.kind!r} already registered")
    _KINDS[spec.kind] = spec


def get_index_kind(kind: str) -> IndexKindSpec:
    try:
        return _KINDS[kind]
    except KeyError:
        raise BadRequestError(f"Unknown index kind '{kind}'") from None


def all_index_kinds() -> dict[str, IndexKindSpec]:
    return dict(_KINDS)


for _spec in (
    IndexKindSpec("btree"),
    IndexKindSpec("btree_numeric", cast="numeric"),
    IndexKindSpec("trgm", using="gin", opclass="gin_trgm_ops"),
):
    register_index_kind(_spec)


def _casts_token(columns: list[tuple[str, str | None]]) -> str:
    """A stable token of the per-column casts, so a type (cast) change → a new name."""
    return ",".join(cast or "text" for _key, cast in columns)


def _grid_row_for(schema: str) -> sa.Table:
    """A minimal Core ``grid_row`` carrying just the columns an index touches.

    Deliberately *not* ``GridRow.__table__``: the reconciler is handed an explicit
    physical ``schema``, and the out-of-band ``CONCURRENTLY`` path may run on a
    connection with no ``schema_translate_map`` at all. Naming the schema on a
    throwaway table keeps that behaviour exact while still letting the dialect's
    preparer do the quoting.
    """
    return sa.Table(
        "grid_row",
        sa.MetaData(),
        sa.Column("table_id", sa.UUID(as_uuid=True)),
        sa.Column("namespace", sa.String(255)),
        sa.Column("payload", JSONB),
        sa.Column("archived_at", UtcDateTime),
        schema=schema,
    )


def _payload_expr(row: sa.Table, key: str, cast: str | None) -> sa.ColumnElement[Any]:
    """``payload ->> 'key'``, optionally cast to the column type's SQL type.

    The cast comes from :data:`PG_CAST_TYPES` — the same mapping
    :meth:`FieldTypeHandler.sql_cast` uses on the query side — so the index
    expression and the filter expression cannot drift apart. That replaces a
    regex-guarded cast *name* interpolated into the DDL string.
    """
    validate_key(key)
    expr: sa.ColumnElement[Any] = row.c.payload[key].astext
    if cast is None:
        return expr
    try:
        sql_type = PG_CAST_TYPES[cast]
    except KeyError:
        raise BadRequestError(f"Unsafe index cast: {cast!r}") from None
    return sa.cast(expr, sql_type)


def build_payload_index(
    *,
    name: str,
    schema: str,
    table_id: object,
    columns: list[tuple[str, str | None]],
    using: str,
    opclass: str | None,
    unique: bool,
    concurrently: bool = False,
) -> sa.Index:
    """The ``Index`` construct for a payload index (nothing is executed).

    ``columns`` is ``[(key, cast), …]`` where ``cast`` is the column type's
    canonical Postgres cast (``None`` = text). The same cast is what
    :meth:`FieldTypeHandler.sql_cast` applies on the query side, so the index
    expression matches the filter/sort expression and the planner can use it.
    ``concurrently`` marks the index ``CREATE INDEX CONCURRENTLY`` (which must run
    outside a transaction — see :func:`reconcile_table_indexes`).
    """
    validate_schema(schema)
    if using not in ("btree", "gin"):
        raise BadRequestError(f"Unsupported index access method: {using!r}")
    if unique and using != "btree":
        raise BadRequestError("unique indexes must use a btree-compatible access method")

    row = _grid_row_for(schema)
    where: list[sa.ColumnElement[bool]] = [row.c.archived_at.is_(None)]
    exprs: list[sa.ColumnElement[Any]] = []
    ops: dict[str, str] = {}

    if using == "gin":
        # GIN indexes are not table-scoped by a leading column, so the scope goes
        # into the partial predicate. `table_id` binds as a parameter and is
        # rendered as a literal by DDL compilation — no manual `'…'::uuid`.
        where.append(row.c.table_id == table_id)
    else:
        exprs.extend([row.c.table_id, row.c.namespace] if unique else [row.c.table_id])

    for i, (key, cast) in enumerate(columns):
        expr = _payload_expr(row, key, cast)
        if opclass:
            # An operator class on an expression has to be keyed by a label,
            # which is the only reason these get named at all.
            label = f"expr_{i}"
            expr = expr.label(label)
            ops[label] = opclass
        exprs.append(expr)

    return sa.Index(
        name,
        *exprs,
        unique=unique,
        postgresql_using=using,
        postgresql_where=sa.and_(*where),
        postgresql_concurrently=concurrently,
        postgresql_ops=ops,
    )


def render_ddl(element: ddl.CreateIndex | ddl.DropIndex) -> str:
    """Compile a DDL construct to Postgres SQL text.

    Used for logging, for tests that assert the rendered statement, and for the
    ``CONCURRENTLY`` path where the caller runs the statement on its own
    AUTOCOMMIT connection.
    """
    return str(element.compile(dialect=postgresql.dialect())).strip()


def build_payload_index_ddl(**kwargs: object) -> str:
    """Render the ``CREATE INDEX`` DDL for a payload index (no execution).

    Kept as the string-returning form for callers driving the ``CONCURRENTLY``
    path by hand; :func:`build_payload_index` is the construct it renders.
    """
    index = build_payload_index(**kwargs)  # type: ignore[arg-type]
    return render_ddl(ddl.CreateIndex(index, if_not_exists=True))


def build_drop_index(*, name: str, schema: str, concurrently: bool = False) -> ddl.DropIndex:
    """``DROP INDEX [CONCURRENTLY] IF EXISTS`` for a reconciled index.

    The index is reconstructed as a bare named ``Index`` on ``grid_row`` purely so
    the compiler can schema-qualify it; only its name and schema are emitted.
    """
    validate_schema(schema)
    index = sa.Index(name, _grid_row_for(schema).c.table_id, postgresql_concurrently=concurrently)
    return ddl.DropIndex(index, if_exists=True)


def build_drop_index_ddl(*, name: str, schema: str, concurrently: bool = False) -> str:
    """Render ``DROP INDEX [CONCURRENTLY] IF EXISTS`` for a reconciled index."""
    return render_ddl(build_drop_index(name=name, schema=schema, concurrently=concurrently))


def _create_index(**kwargs: object) -> ddl.CreateIndex:
    """``CREATE INDEX IF NOT EXISTS`` for a payload index."""
    return ddl.CreateIndex(build_payload_index(**kwargs), if_not_exists=True)  # type: ignore[arg-type]


def _column_casts(
    keys: list[str], columns_by_key: dict[str, GridColumn], *, using: str
) -> list[tuple[str, str | None]]:
    """Resolve each index column key to its type's canonical cast.

    GIN (trigram) indexes operate on the raw text, so the cast is dropped there;
    btree indexes use the column type's ``pg_cast`` so they match the query.
    """
    out: list[tuple[str, str | None]] = []
    for key in keys:
        cast: str | None = None
        col = columns_by_key.get(key)
        if using != "gin" and col is not None:
            cast = get_field_type(col.type_id).pg_cast
        out.append((key, cast))
    return out


async def reconcile_table_indexes(
    session: AsyncSession,
    *,
    table: GridTable,
    schema: str = "forktex_grid",
) -> list[str]:
    """Materialise declared + per-column-unique indexes; drop archived ones.

    Idempotent. Returns the physical index names ensured. Runs transactionally.
    To build without a write lock on a large live table, emit the DDL with
    :func:`build_payload_index_ddl` (``concurrently=True``) and run it on an
    AUTOCOMMIT connection *outside* any transaction (Postgres forbids
    ``CREATE INDEX CONCURRENTLY`` inside a transaction block, and it also waits
    on any concurrent transaction — so it cannot share the reconcile session).
    """
    created: list[str] = []
    creates: list[ddl.CreateIndex] = []
    drops: list[ddl.DropIndex] = []
    marks: list[tuple[GridIndex, str]] = []

    columns_by_key = {
        c.key: c
        for c in await session.scalars(
            sa.select(GridColumn).where(GridColumn.table_id == table.id, GridColumn.archived_at.is_(None))
        )
    }
    all_columns = list(await session.scalars(sa.select(GridColumn).where(GridColumn.table_id == table.id)))

    declared = await session.scalars(
        sa.select(GridIndex).where(GridIndex.table_id == table.id, GridIndex.archived_at.is_(None))
    )
    for gi in declared:
        spec = get_index_kind(gi.index_kind)
        casts = _column_casts(list(gi.column_keys), columns_by_key, using=spec.using)
        # The cast is part of the identity: if a column's type (hence cast) changes,
        # the name changes → a new index is built and the stale one is dropped below,
        # rather than a same-named ``IF NOT EXISTS`` no-op leaving the old expression.
        name = index_name(
            "gux" if gi.is_unique else "gix",
            gi.namespace,
            gi.table_id,
            ",".join(gi.column_keys),
            gi.index_kind,
            gi.is_unique,
            _casts_token(casts),
        )
        creates.append(
            _create_index(
                name=name,
                schema=schema,
                table_id=gi.table_id,
                columns=casts,
                using=spec.using,
                opclass=spec.opclass,
                unique=gi.is_unique,
            )
        )
        if gi.physical_name and gi.physical_name != name:
            drops.append(build_drop_index(name=gi.physical_name, schema=schema))
        marks.append((gi, name))
        created.append(name)

    # Implicit per-column unique indexes: drop every column's implicit index, then
    # recreate only for columns that are still active + unique + payload. This makes
    # the reconcile converge when ``is_unique`` is flipped off or the column is
    # archived (the constraint must stop enforcing) or its type changes (fresh
    # expression). The implicit index is a plain btree, so rebuild is cheap.
    for column in all_columns:
        drops.append(
            build_drop_index(
                name=index_name("gux", column.namespace, column.table_id, column.key, "btree", True), schema=schema
            )
        )
    for column in all_columns:
        if column.archived_at is None and column.is_unique and column.materialization == Materialization.payload:
            name = index_name("gux", column.namespace, column.table_id, column.key, "btree", True)
            creates.append(
                _create_index(
                    name=name,
                    schema=schema,
                    table_id=column.table_id,
                    columns=_column_casts([column.key], columns_by_key, using="btree"),
                    using="btree",
                    opclass=None,
                    unique=True,
                )
            )
            created.append(name)

    # Drop the physical index of any archived declaration.
    dropped: list[GridIndex] = []
    archived = await session.scalars(
        sa.select(GridIndex).where(
            GridIndex.table_id == table.id,
            GridIndex.archived_at.is_not(None),
            GridIndex.physical_name.is_not(None),
        )
    )
    for gi in archived:
        if gi.physical_name is None:  # (the query already filters these out)
            continue
        drops.append(build_drop_index(name=gi.physical_name, schema=schema))
        dropped.append(gi)

    # Drops before creates: an implicit index's drop and (re)create share a name.
    if drops or creates:
        logger.info(
            "grid: reconciling table indexes",
            extra={
                "schema": schema,
                "table_id": str(table.id),
                "table_slug": table.slug,
                "dropping": len(drops),
                "creating": len(creates),
            },
        )
    for statement in [*drops, *creates]:
        logger.debug("grid: index DDL", extra={"ddl": render_ddl(statement)})
        await session.execute(statement)

    for gi, name in marks:
        gi.physical_name = name
        gi.state = IndexState.live
    for gi in dropped:
        gi.physical_name = None
        gi.state = IndexState.pending

    await session.flush()
    return created


__all__ = [
    "IndexKindSpec",
    "all_index_kinds",
    "build_drop_index",
    "build_drop_index_ddl",
    "build_payload_index",
    "build_payload_index_ddl",
    "get_index_kind",
    "index_name",
    "reconcile_table_indexes",
    "register_index_kind",
    "render_ddl",
]
