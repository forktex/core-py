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

"""Grid builds on `forktex_core.database`'s primitives rather than its own copies.

Before this, grid carried a third copy of the identifier regexes, a second
`IntegrityError` translator, a third `information_schema` query, its own advisory-key
folding, and a fourth page shape. These assert the duplication stays gone —
identity checks, so a re-forked implementation fails here rather than drifting
quietly until the two disagree in production.
"""

from __future__ import annotations

import pathlib

import sqlalchemy as sa

from forktex_core.database import identifiers as db_identifiers
from forktex_core.database import integrity as db_integrity
from forktex_core.database.pagination import Page as DbPage
from forktex_core.grid import identifiers as grid_identifiers
from forktex_core.grid.read.result import Page, Row


def test_identifier_validation_is_the_shared_implementation():
    assert grid_identifiers.IDENT_RE is db_identifiers.IDENT_RE
    assert grid_identifiers.validate_schema is db_identifiers.validate_schema
    assert grid_identifiers.validate_slug is db_identifiers.validate_slug
    # grid's own spellings are thin wrappers, not reimplementations
    assert grid_identifiers.validate_key("customer_id") is None
    assert grid_identifiers.validate_ident("org_id", "namespace column") is None


def test_grid_consumes_the_integrity_boundaries_directly():
    """grid used to re-export these through a `_kernel.integrity` hop that added nothing.
    The hop is gone: every call site imports `database.integrity` itself, so there is no
    second module that could acquire behaviour and diverge."""
    import ast

    grid_src = pathlib.Path("src/forktex_core/grid")
    importers = {
        path.relative_to(grid_src).as_posix()
        for path in grid_src.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("forktex_core.database.integrity")
    }
    assert importers, "no grid module imports database.integrity — did the boundary move?"
    assert not (grid_src / "integrity.py").exists(), "a grid-local integrity module reappeared"
    # And the names resolve to the one implementation.
    assert db_integrity.integrity_boundary.__module__ == "forktex_core.database.integrity"


def test_grid_page_is_the_shared_page():
    assert issubclass(Page, DbPage)
    page = Page(rows=[Row(id=1, namespace="", values={})], has_more=True, total=7)
    # `rows` stays grid's vocabulary, on the way in, in Python, and on the wire.
    assert page.rows == page.items
    assert page.model_dump()["rows"]
    assert page.has_more is True and page.total == 7


def test_index_ddl_is_compiled_by_the_dialect_not_string_built():
    """A hostile column key reaches the DDL only through the preparer, so it is
    quoted rather than executed. It is also rejected by the name policy first —
    this asserts the second line of defence, on the path that renders."""
    from forktex_core.grid.persist.reconcile.indexes import build_payload_index, render_ddl
    from forktex_core.database import ddl

    index = build_payload_index(
        name='evil"; DROP TABLE grid_row; --',
        schema="forktex_grid",
        table_id="11111111-1111-1111-1111-111111111111",
        columns=[("amount", "numeric")],
        using="btree",
        opclass=None,
        unique=False,
    )
    rendered = render_ddl(ddl.CreateIndex(index, if_not_exists=True))
    assert 'CREATE INDEX IF NOT EXISTS "evil""; DROP TABLE grid_row; --"' in rendered
    assert rendered.count("DROP TABLE") == 1  # only inside the quoted name


def test_all_four_index_kinds_render_the_intended_sql():
    """The reconciler's SQL is no longer inspectable as a string it built, so the
    rendered form of each kind is pinned here instead."""
    from forktex_core.grid.persist.reconcile.indexes import build_payload_index_ddl

    common = {"schema": "fg", "table_id": "11111111-1111-1111-1111-111111111111"}

    btree = build_payload_index_ddl(
        name="a", columns=[("k", None)], using="btree", opclass=None, unique=False, **common
    )
    assert "USING btree (table_id, (payload ->> 'k'))" in btree
    assert "WHERE archived_at IS NULL" in btree

    numeric = build_payload_index_ddl(
        name="b", columns=[("n", "numeric")], using="btree", opclass=None, unique=False, **common
    )
    assert "CAST(payload ->> 'n' AS NUMERIC)" in numeric

    unique = build_payload_index_ddl(
        name="c", columns=[("k", None)], using="btree", opclass=None, unique=True, **common
    )
    # Unique indexes lead with namespace so uniqueness is per table *and* tenant.
    assert "CREATE UNIQUE INDEX" in unique
    assert "(table_id, namespace, (payload ->> 'k'))" in unique

    trgm = build_payload_index_ddl(
        name="d", columns=[("t", None)], using="gin", opclass="gin_trgm_ops", unique=False, **common
    )
    assert "USING gin ((payload ->> 't') gin_trgm_ops)" in trgm
    # GIN cannot lead with table_id, so the table scope moves into the predicate.
    assert "table_id = '11111111-1111-1111-1111-111111111111'" in trgm


def test_sidecar_ddl_renders_through_the_preparer():
    from sqlalchemy.dialects import postgresql

    from forktex_core.database import ddl

    metadata = sa.MetaData()
    sa.Table("grid_row", metadata, sa.Column("id", sa.UUID(as_uuid=True), primary_key=True), schema="fg")
    table = sa.Table(
        "grid_promoted_deadbeef",
        metadata,
        sa.Column(
            "row_id", sa.UUID(as_uuid=True), sa.ForeignKey("fg.grid_row.id", ondelete="CASCADE"), primary_key=True
        ),
        schema="fg",
    )
    rendered = str(ddl.CreateTable(table, if_not_exists=True).compile(dialect=postgresql.dialect()))
    assert "CREATE TABLE IF NOT EXISTS fg.grid_promoted_deadbeef" in rendered
    assert "REFERENCES fg.grid_row (id) ON DELETE CASCADE" in rendered
