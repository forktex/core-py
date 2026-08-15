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

"""Unit tests for forktex_core.database.ddl — **no container required**.

That's the point of the module: DDL is now a SQLAlchemy construct, so it
compiles to a string as a pure function and can be asserted without a database.
The previous f-string DDL could only be tested against live Postgres.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from forktex_core.database.ddl import (
    AddColumn,
    CreateIndex,
    CreateTable,
    DropColumn,
    DropIndex,
    DropTable,
)

_PG = postgresql.dialect()


def _sql(stmt) -> str:
    return " ".join(str(stmt.compile(dialect=_PG)).split())


@pytest.fixture
def table() -> sa.Table:
    md = sa.MetaData(schema="forktex_grid")
    return sa.Table(
        "grid_promoted_abc",
        md,
        sa.Column("row_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(50)),
    )


# ---------------------------------------------------------------------------
# ADD / DROP COLUMN — the constructs Core lacks
# ---------------------------------------------------------------------------


def test_add_column_renders_schema_qualified_with_type(table: sa.Table):
    col = sa.Column("score", sa.Numeric())
    table.append_column(col)
    assert _sql(AddColumn(col)) == ("ALTER TABLE forktex_grid.grid_promoted_abc ADD COLUMN score NUMERIC")


def test_add_column_if_not_exists_and_not_null(table: sa.Table):
    col = sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False)
    table.append_column(col)
    assert _sql(AddColumn(col, if_not_exists=True)) == (
        "ALTER TABLE forktex_grid.grid_promoted_abc ADD COLUMN IF NOT EXISTS org_id UUID NOT NULL"
    )


def test_add_column_quotes_a_hostile_identifier(table: sa.Table):
    """The dialect's preparer escapes the name, so injected SQL becomes a quoted
    identifier rather than syntax. A regex guard could only reject this; quoting
    is correct regardless."""
    col = sa.Column('foo"; DROP TABLE users; --', sa.String(8))
    table.append_column(col)
    rendered = _sql(AddColumn(col))
    assert '"foo""; DROP TABLE users; --"' in rendered
    # the payload never escapes its quotes into executable position
    assert not rendered.rstrip().endswith("--")


def test_add_column_requires_an_attached_column():
    with pytest.raises(ValueError, match="attached to a Table"):
        AddColumn(sa.Column("orphan", sa.Integer))


def test_drop_column_takes_a_name_because_the_column_is_already_gone():
    stmt = DropColumn("grid_promoted_abc", "stale_col", schema="forktex_grid", if_exists=True)
    assert _sql(stmt) == ("ALTER TABLE forktex_grid.grid_promoted_abc DROP COLUMN IF EXISTS stale_col")


def test_drop_column_without_schema_or_if_exists():
    assert _sql(DropColumn("t", "c")) == "ALTER TABLE t DROP COLUMN c"


def test_drop_column_quotes_a_hostile_name():
    rendered = _sql(DropColumn("t", 'c"; DROP TABLE users; --'))
    assert '"c""; DROP TABLE users; --"' in rendered


# ---------------------------------------------------------------------------
# The natively-supported constructs we re-export (guard the assumption)
# ---------------------------------------------------------------------------


def test_create_and_drop_table_support_if_exists_natively(table: sa.Table):
    assert "CREATE TABLE IF NOT EXISTS forktex_grid.grid_promoted_abc" in _sql(CreateTable(table, if_not_exists=True))
    assert _sql(DropTable(table, if_exists=True)) == ("DROP TABLE IF EXISTS forktex_grid.grid_promoted_abc")


def test_create_and_drop_index_support_if_exists_natively(table: sa.Table):
    idx = sa.Index("ix_title", table.c.title)
    assert _sql(CreateIndex(idx, if_not_exists=True)) == (
        "CREATE INDEX IF NOT EXISTS ix_title ON forktex_grid.grid_promoted_abc (title)"
    )
    assert _sql(DropIndex(idx, if_exists=True)) == ("DROP INDEX IF EXISTS forktex_grid.ix_title")


# ---------------------------------------------------------------------------
# grid's four payload-index shapes must all be expressible in Core
# ---------------------------------------------------------------------------


@pytest.fixture
def grid_row() -> sa.Table:
    md = sa.MetaData(schema="forktex_grid")
    return sa.Table(
        "grid_row",
        md,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("table_id", postgresql.UUID(as_uuid=True)),
        sa.Column("payload", postgresql.JSONB),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
    )


def test_btree_payload_index_partial(grid_row: sa.Table):
    idx = sa.Index(
        "gix_title",
        grid_row.c.table_id,
        grid_row.c.payload["title"].astext,
        postgresql_where=grid_row.c.archived_at.is_(None),
    )
    assert _sql(CreateIndex(idx, if_not_exists=True)) == (
        "CREATE INDEX IF NOT EXISTS gix_title ON forktex_grid.grid_row "
        "(table_id, (payload ->> 'title')) WHERE archived_at IS NULL"
    )


def test_numeric_cast_payload_index(grid_row: sa.Table):
    idx = sa.Index("gix_score", grid_row.c.payload["score"].astext.cast(sa.Numeric))
    assert "CAST(payload ->> 'score' AS NUMERIC)" in _sql(CreateIndex(idx))


def test_gin_trigram_opclass_on_a_payload_expression(grid_row: sa.Table):
    """The trickiest of grid's index kinds: a GIN index with an operator class
    applied to a JSONB-extracted expression. Expressed by labelling the
    expression and keying postgresql_ops by that label."""
    expr = grid_row.c.payload["title"].astext.label("title_txt")
    idx = sa.Index(
        "gix_trgm",
        expr,
        postgresql_using="gin",
        postgresql_ops={"title_txt": "gin_trgm_ops"},
    )
    assert _sql(CreateIndex(idx, if_not_exists=True)) == (
        "CREATE INDEX IF NOT EXISTS gix_trgm ON forktex_grid.grid_row USING gin ((payload ->> 'title') gin_trgm_ops)"
    )


def test_unique_partial_payload_index(grid_row: sa.Table):
    idx = sa.Index(
        "gux_email",
        grid_row.c.table_id,
        grid_row.c.payload["email"].astext,
        unique=True,
        postgresql_where=grid_row.c.archived_at.is_(None),
    )
    assert _sql(CreateIndex(idx)) == (
        "CREATE UNIQUE INDEX gux_email ON forktex_grid.grid_row "
        "(table_id, (payload ->> 'email')) WHERE archived_at IS NULL"
    )
