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

"""DDL as SQLAlchemy constructs, not hand-built SQL strings.

The library creates and alters real Postgres objects at runtime (grid's
promoted-column sidecars and payload indexes, flow's extension columns).
Historically that meant f-string SQL guarded by an identifier regex. This
module replaces it, because SQLAlchemy constructs are strictly better here:

- **Quoting is the dialect's job.** The compiler's ``IdentifierPreparer``
  escapes and quotes every name, so a hostile identifier renders as data
  rather than syntax. A regex guard can only *reject*; quoting is correct even
  for names the guard would have to allow.
- **They compile without a database.** ``str(stmt.compile(dialect=...))`` is a
  pure function of the construct, so DDL is unit-testable with no container —
  which is the whole point of moving off strings.
- **They respect ``schema_translate_map``.** f-string SQL had to interpolate
  the runtime schema by hand, which is precisely why those f-strings existed.

SQLAlchemy 2.0 already covers most of the surface natively, so this module
mostly re-exports it and adds only what Core genuinely lacks: ``ALTER TABLE …
ADD/DROP COLUMN``, for which there is no built-in construct.
"""

from __future__ import annotations

from sqlalchemy import Column
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import (
    CreateIndex,
    CreateSchema,
    CreateTable,
    DDLElement,
    DropIndex,
    DropSchema,
    DropTable,
)
from sqlalchemy.sql.compiler import DDLCompiler

__all__ = [
    "AddColumn",
    "CreateIndex",
    "CreateSchema",
    "CreateTable",
    "DropColumn",
    "DropIndex",
    "DropSchema",
    "DropTable",
]


class AddColumn(DDLElement):
    """``ALTER TABLE <table> ADD COLUMN [IF NOT EXISTS] <col> <type> [NOT NULL]``.

    Core has ``CreateTable``/``CreateIndex`` but no ``ADD COLUMN`` construct —
    that lives in alembic's operations layer, which is a migration-authoring
    tool rather than a runtime dependency. This is the minimal equivalent.

    The column must be attached to a ``Table`` so the compiler can render its
    name and resolve the type; pass ``if_not_exists=True`` for the idempotent
    reconcile-style usage this library needs.
    """

    inherit_cache = False

    def __init__(self, column: Column, *, if_not_exists: bool = False) -> None:
        if column.table is None:  # pragma: no cover - guards a programming error
            raise ValueError("AddColumn requires a column attached to a Table")
        self.column = column
        self.if_not_exists = if_not_exists


class DropColumn(DDLElement):
    """``ALTER TABLE <table> DROP COLUMN [IF EXISTS] <col>``.

    Takes the column *name* rather than a ``Column``, because the reconcile
    path drops columns that are no longer declared — so by definition there is
    no live ``Column`` object for them.
    """

    inherit_cache = False

    def __init__(
        self,
        table_name: str,
        column_name: str,
        *,
        schema: str | None = None,
        if_exists: bool = False,
    ) -> None:
        self.table_name = table_name
        self.column_name = column_name
        self.schema = schema
        self.if_exists = if_exists


def _qualified(compiler: DDLCompiler, table_name: str, schema: str | None) -> str:
    preparer = compiler.preparer
    quoted = preparer.quote(table_name)
    return f"{preparer.quote_schema(schema)}.{quoted}" if schema else quoted


@compiles(AddColumn)
def _compile_add_column(element: AddColumn, compiler: DDLCompiler, **kw: object) -> str:
    column = element.column
    table = column.table
    target = _qualified(compiler, table.name, table.schema)
    exists = "IF NOT EXISTS " if element.if_not_exists else ""
    coltype = compiler.dialect.type_compiler.process(column.type)
    nullability = "" if column.nullable else " NOT NULL"
    return f"ALTER TABLE {target} ADD COLUMN {exists}{compiler.preparer.format_column(column)} {coltype}{nullability}"


@compiles(DropColumn)
def _compile_drop_column(element: DropColumn, compiler: DDLCompiler, **kw: object) -> str:
    target = _qualified(compiler, element.table_name, element.schema)
    exists = "IF EXISTS " if element.if_exists else ""
    return f"ALTER TABLE {target} DROP COLUMN {exists}{compiler.preparer.quote(element.column_name)}"
