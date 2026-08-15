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

"""Grid ORM models — the schema + data plane, in the ``forktex_grid`` schema.

The grid is a fully-dynamic virtual database: tables, columns, relations and indexes
are described by rows in these schema tables, not by code-defined ORM classes. Tenant
rows land in ``grid_row.payload`` (JSONB) by default; individual columns can be promoted
to native typed columns on a per-table sidecar.

The SQL that creates these tables lives in ``migrations/v0001__schema.sql`` and is applied
by ``migrations.apply_migrations`` — the ORM mapping below mirrors it 1:1.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.schema import SchemaItem

from forktex_core.database.models import AuditMixin, substrate_base
from forktex_core.grid.domain.enums import Cardinality, IndexState, Materialization, OnDelete, Ownership, RelationShape

_SCHEMA = "forktex_grid"

#: Grid's own declarative registry — every table defaults to ``_SCHEMA``.
#:
#: Separate from ``BaseDBModel.metadata`` on purpose: that registry belongs to
#: the consumer, and its ``create_all`` must not try to build grid's substrate.
#: Grid's tables come from ``migrations/v0001__schema.sql`` instead.
_GridBase = substrate_base(_SCHEMA)

_ACTIVE_ARCHIVE = "(is_active AND archived_at IS NULL) OR (NOT is_active AND archived_at IS NOT NULL)"


def _audit_args(
    tablename: str,
    *extra: SchemaItem,
    unique_active: tuple[str, ...] | None = None,
) -> tuple[Any, ...]:
    """``__table_args__`` for an audited grid table: the active/archive CHECK, plus
    an optional partial-unique index over active rows and any extra constraints.
    The schema comes from ``_GridBase``'s ``MetaData``, not from here."""
    args: list[SchemaItem] = list(extra)
    if unique_active:
        args.append(
            sa.Index(
                f"uq_{tablename}_active",
                *unique_active,
                unique=True,
                postgresql_where=sa.text("archived_at IS NULL"),
            )
        )
    args.append(sa.CheckConstraint(_ACTIVE_ARCHIVE, name=f"ck_{tablename}_active_archive"))
    return tuple(args)


def _fk(target: str, *, ondelete: str) -> sa.ForeignKey:
    return sa.ForeignKey(f"{_SCHEMA}.{target}", ondelete=ondelete)


class _GridAudited(_GridBase, AuditMixin):
    """Abstract base: UUID ``id`` PK + optional ``namespace`` scope (default ``''`` root),
    plus the ``AuditMixin`` timestamps / actor ids / soft-delete."""

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid.uuid7)
    namespace: Mapped[str] = mapped_column(sa.String(255), nullable=False, server_default="", default="", index=True)


class GridSpace(_GridAudited):
    """An optional bundle that owns a set of tables and shared config."""

    __tablename__ = "grid_space"
    __table_args__ = _audit_args("grid_space", unique_active=("namespace", "slug"))

    slug: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    label: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}", default=dict)


class GridTable(_GridAudited):
    """A logical table. ``ownership`` records whether the grid owns the physical relation
    (``owned``) or overlays a pre-existing one (``bound``, described by ``binding``).

    ``projection_predicate`` is a filter AST always AND-ed into the table's scope (so one
    physical table can back several logical tables). ``natural_key`` is the business key
    used for deterministic upsert during ingestion.
    """

    __tablename__ = "grid_table"
    __table_args__ = _audit_args(
        "grid_table",
        sa.CheckConstraint(
            "(ownership <> 'bound') OR (binding IS NOT NULL)",
            name="ck_grid_table_bound_requires_binding",
        ),
        unique_active=("namespace", "slug"),
    )

    space_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.UUID(as_uuid=True), _fk("grid_space.id", ondelete="SET NULL"), nullable=True, index=True
    )
    slug: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    label: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    ownership: Mapped[Ownership] = mapped_column(
        nullable=False, server_default=Ownership.owned, default=Ownership.owned
    )
    binding: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    projection_predicate: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    natural_key: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    is_system: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false(), default=False)

    columns: Mapped[list[GridColumn]] = relationship(
        back_populates="table", cascade="all, delete-orphan", foreign_keys="GridColumn.table_id"
    )
    indexes: Mapped[list[GridIndex]] = relationship(back_populates="table", cascade="all, delete-orphan")
    rows: Mapped[list[GridRow]] = relationship(back_populates="table", cascade="all, delete-orphan")


class GridColumn(_GridAudited):
    """A logical column. ``type_id`` is a registry string; ``materialization`` decides
    storage (``payload`` JSONB / ``promoted`` native sidecar column / ``derived`` computed
    from ``derived_source``). A ``ref`` column projects a :class:`GridRelation`."""

    __tablename__ = "grid_column"
    __table_args__ = _audit_args(
        "grid_column",
        sa.CheckConstraint(
            "((type_id = 'ref') AND relation_id IS NOT NULL) OR ((type_id <> 'ref') AND relation_id IS NULL)",
            name="ck_grid_column_ref_projects_relation",
        ),
        sa.CheckConstraint(
            "((materialization = 'promoted') AND promoted_column IS NOT NULL) "
            "OR ((materialization <> 'promoted') AND promoted_column IS NULL)",
            name="ck_grid_column_promoted_has_column",
        ),
        sa.CheckConstraint(
            "((materialization = 'derived') AND derived_source IS NOT NULL) "
            "OR ((materialization <> 'derived') AND derived_source IS NULL)",
            name="ck_grid_column_derived_has_source",
        ),
        unique_active=("table_id", "key"),
    )

    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), _fk("grid_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    label: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    type_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    cardinality: Mapped[Cardinality] = mapped_column(
        nullable=False, server_default=Cardinality.one, default=Cardinality.one
    )
    materialization: Mapped[Materialization] = mapped_column(
        nullable=False, server_default=Materialization.payload, default=Materialization.payload
    )
    promoted_column: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    derived_source: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    is_required: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false(), default=False)
    is_unique: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false(), default=False)
    default_value: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    relation_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.UUID(as_uuid=True), _fk("grid_relation.id", ondelete="SET NULL"), nullable=True, index=True
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}", default=dict)
    display_order: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0", default=0)

    table: Mapped[GridTable] = relationship(back_populates="columns", foreign_keys=[table_id])
    relation: Mapped[GridRelation | None] = relationship(foreign_keys=[relation_id])


class GridRelation(_GridAudited):
    """The single relationship substrate between two tables. ``relation_type`` is the
    cardinality shape; a many-to-many routes through ``through_table_id``. Edges live in
    :class:`GridEdge`; ``ref`` columns project a relation via ``GridColumn.relation_id``."""

    __tablename__ = "grid_relation"
    __table_args__ = _audit_args(
        "grid_relation",
        sa.CheckConstraint(
            "((relation_type = 'many_to_many') AND through_table_id IS NOT NULL) "
            "OR ((relation_type <> 'many_to_many') AND through_table_id IS NULL)",
            name="ck_grid_relation_m2m_has_through",
        ),
        unique_active=("source_table_id", "key"),
    )

    key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    source_table_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), _fk("grid_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_table_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), _fk("grid_table.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    through_table_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.UUID(as_uuid=True), _fk("grid_table.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    relation_type: Mapped[RelationShape] = mapped_column(nullable=False)
    on_delete: Mapped[OnDelete] = mapped_column(
        nullable=False, server_default=OnDelete.restrict, default=OnDelete.restrict
    )


class GridIndex(_GridAudited):
    """Declarative index intent, reconciled to a physical Postgres index. ``column_keys``
    is an ordered (composite-capable) list; ``index_kind`` is a registry string; ``state``
    tracks reconciliation."""

    __tablename__ = "grid_index"
    __table_args__ = _audit_args(
        "grid_index",
        sa.Index(
            "uq_grid_index_active",
            "namespace",
            "table_id",
            sa.text("(column_keys::text)"),
            "index_kind",
            unique=True,
            postgresql_where=sa.text("archived_at IS NULL"),
        ),
    )

    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), _fk("grid_table.id", ondelete="CASCADE"), nullable=False, index=True
    )
    column_keys: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    index_kind: Mapped[str] = mapped_column(sa.String(64), nullable=False, server_default="btree", default="btree")
    is_unique: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false(), default=False)
    physical_name: Mapped[str | None] = mapped_column(sa.String(63), nullable=True)
    state: Mapped[IndexState] = mapped_column(
        nullable=False, server_default=IndexState.pending, default=IndexState.pending
    )

    table: Mapped[GridTable] = relationship(back_populates="indexes")


class GridRow(_GridAudited):
    """A data row for an owned table. ``payload`` holds every payload-materialised column
    value keyed by column key. ``external_ref`` links an extension row 1:1 to a host row
    (the row's PK in a bound/overlaid physical table)."""

    __tablename__ = "grid_row"
    __table_args__ = _audit_args(
        "grid_row",
        sa.Index("ix_grid_row_table", "table_id"),
        sa.Index(
            "ix_grid_row_payload_gin", "payload", postgresql_using="gin", postgresql_ops={"payload": "jsonb_path_ops"}
        ),
        sa.Index(
            "uq_grid_row_external_ref",
            "table_id",
            "external_ref",
            unique=True,
            postgresql_where=sa.text("external_ref IS NOT NULL"),
        ),
    )

    table_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), _fk("grid_table.id", ondelete="CASCADE"), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}", default=dict)
    external_ref: Mapped[uuid.UUID | None] = mapped_column(sa.UUID(as_uuid=True), nullable=True)

    table: Mapped[GridTable] = relationship(back_populates="rows")


class GridEdge(_GridAudited):
    """A materialised edge between two rows under a :class:`GridRelation`. Each
    ``(relation_id, source_row_id, target_row_id)`` triple is unique; cardinality is
    enforced by partial unique indexes the reconciler emits per relation shape."""

    __tablename__ = "grid_edge"
    __table_args__ = _audit_args(
        "grid_edge",
        sa.UniqueConstraint("relation_id", "source_row_id", "target_row_id", name="uq_grid_edge_triple"),
        sa.Index("ix_grid_edge_source", "namespace", "source_row_id"),
        sa.Index("ix_grid_edge_target", "namespace", "target_row_id"),
    )

    relation_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), _fk("grid_relation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_row_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), _fk("grid_row.id", ondelete="CASCADE"), nullable=False
    )
    target_row_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), _fk("grid_row.id", ondelete="CASCADE"), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}", default=dict)


__all__ = [
    "GridColumn",
    "GridEdge",
    "GridIndex",
    "GridRelation",
    "GridRow",
    "GridSpace",
    "GridTable",
]
