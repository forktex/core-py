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

"""The field-type contract — one handler per ``grid_column.type_id``.

A handler is the single home for everything the substrate needs to know
about a column's *type*, cleanly split from what's configured per column:

- **Built-in, non-refutable capabilities** (:class:`Capabilities`) — whether
  the type is filterable / sortable / fuzzy-searchable, the operator
  vocabulary it supports, and the index kinds it can back. These live in
  code on the type and are the same for every column of that type. A column
  may *narrow* them (opt out) via ``capability_overrides`` but never widen
  them — see :func:`effective_capabilities`.
- **Per-column config** (:attr:`FieldTypeHandler.config_model`) — a Pydantic
  model validating ``grid_column.config`` (e.g. decimal precision/scale,
  enum options, text subtype).

Three codecs make the type usable everywhere:

- :meth:`normalize` — an API value → the canonical JSONB-storable form.
- :meth:`to_cell` / :meth:`from_cell` — canonical ⟷ a plain tabular cell.
  This is the generic spreadsheet-agnostic codec the ingestion layer uses;
  there is deliberately no ``.xlsx`` knowledge here. ``from_cell`` parses
  and raises :class:`ValueError` on unparseable input, so the (later)
  inference layer can try handlers in priority order.
- :meth:`sql_cast` / :meth:`promoted_type` — the SQL cast for a payload
  value (filter/sort) and the native column type when a column is promoted.
"""

from __future__ import annotations

import abc
import uuid
from typing import Any, ClassVar

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

# FilterOp is the shared operator vocabulary, owned by `database` so `flow` and
# any other consumer share one definition. Re-exported here (and from
# `forktex_core.grid`) because it is part of the field-type extension seam:
# a handler declares which ops it supports via `Capabilities.filter_ops`.
from forktex_core.database.filters import FilterOp
from forktex_core.types import BaseValueObject, JsonValue

# A tabular cell value: a JSON-ish scalar. The ingestion layer maps a row of
# these to/from canonical column values. Lists (MANY cardinality) are handled
# one level up, per element.
CellValue = str | int | float | bool | None

# The Postgres casts a handler may name via ``pg_cast``. The SQLAlchemy type
# each maps to must render to exactly the SQL the index DDL uses (e.g.
# ``BigInteger`` → ``BIGINT`` ⇔ ``::bigint``) so filter and index expressions
# match and the planner can use the index. Only IMMUTABLE casts belong here —
# Postgres rejects non-immutable casts (``text::date`` / ``text::timestamptz``)
# in index expressions, so temporal types project as canonical ISO text (whose
# lexicographic order equals chronological order) rather than a typed cast.
PG_CAST_TYPES: dict[str, sa.types.TypeEngine[Any]] = {
    "bigint": sa.BigInteger(),
    "numeric": sa.Numeric(),
    "boolean": sa.Boolean(),
    "uuid": sa.UUID(),
}


class Capabilities(BaseValueObject):
    """A type's built-in, non-refutable feature set.

    ``filterable`` / ``sortable`` / ``fuzzy`` are the headline axes the query
    engine consults. Cursor-browsing requires a deterministic total order, so
    it is available exactly when ``sortable`` is true. ``filter_ops`` is the
    operator vocabulary; ``index_kinds`` the index variants the type can back.
    """

    filterable: bool = True
    sortable: bool = False
    fuzzy: bool = False
    filter_ops: frozenset[FilterOp] = Field(default_factory=frozenset)
    index_kinds: frozenset[str] = Field(default_factory=frozenset)
    default_index_kind: str | None = None

    @property
    def cursor_browsable(self) -> bool:
        """A column can be keyset-browsed iff it has a deterministic order."""
        return self.sortable


class EmptyConfig(BaseModel):
    """Default per-column config for types that take none."""

    model_config = ConfigDict(extra="ignore")


class WriteContext(BaseValueObject):
    """Read-only context handed to a handler's lifecycle hooks.

    Carries the session, scope, row + column identity, and the before/after
    values straddling the mutation. Handlers that need write-time side effects
    (e.g. VECTOR auto-embed, FILE upload) read these and issue their own work
    inside the request transaction, so a failure rolls the row write back.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: AsyncSession
    namespace: str
    table_id: uuid.UUID
    table_slug: str
    column_key: str
    row_id: uuid.UUID
    before_value: JsonValue
    after_value: JsonValue


class FieldTypeHandler(abc.ABC):
    """Abstract base; one concrete subclass per built-in ``type_id``.

    Handlers are stateless singletons held in the registry and identified by
    :attr:`type_id` (the string persisted in ``grid_column.type_id``). Class
    layout may move freely across releases; only ``type_id`` is stable.
    """

    type_id: ClassVar[str]
    capabilities: ClassVar[Capabilities]
    config_model: ClassVar[type[BaseModel]] = EmptyConfig
    #: The canonical Postgres cast applied to the JSONB ``->>`` text extraction,
    #: e.g. ``"bigint"`` / ``"numeric"`` / ``"date"`` / ``"timestamptz"`` /
    #: ``"boolean"`` / ``"uuid"`` (``None`` = leave as text). This is the SINGLE
    #: source of truth for the type's SQL projection: :meth:`sql_cast` (query
    #: side) and the index reconciler (DDL side) both derive from it, so a
    #: filter's expression always matches its index's expression.
    pg_cast: ClassVar[str | None] = None

    def validate_config(self, raw: dict[str, Any] | None) -> BaseModel:
        """Parse + validate a column's raw ``config`` JSON."""
        return self.config_model.model_validate(raw or {})

    @abc.abstractmethod
    def normalize(self, value: object, *, config: BaseModel) -> JsonValue:
        """Turn an API value into the canonical JSONB-storable form.

        Raises :class:`ValueError` for a value this type cannot represent.
        """

    @abc.abstractmethod
    def to_cell(self, value: JsonValue, *, config: BaseModel) -> CellValue:
        """Render a canonical value as a plain tabular cell."""

    @abc.abstractmethod
    def from_cell(self, cell: CellValue, *, config: BaseModel) -> JsonValue:
        """Parse a tabular cell into the canonical form.

        Raises :class:`ValueError` on unparseable input (the inference layer
        relies on this to reject candidate types).
        """

    def sql_cast(self, text_expr: sa.ColumnElement[Any]) -> sa.ColumnElement[Any]:
        """Cast a JSONB ``->>`` text extraction to the typed SQL value.

        Derived from :attr:`pg_cast` so it can never drift from the index DDL.
        A type with a non-standard projection may still override this, but must
        keep it consistent with ``pg_cast``.
        """
        if self.pg_cast is None:
            return text_expr
        return sa.cast(text_expr, PG_CAST_TYPES[self.pg_cast])

    @abc.abstractmethod
    def promoted_type(self, *, config: BaseModel) -> sa.types.TypeEngine[Any]:
        """The native Postgres column type used when this column is promoted."""

    # Fired by the write path after rows are staged on the session, before
    # commit. Single-row callers pass a 1-element list; bulk callers pass the
    # batch so a handler can amortise downstream work (e.g. one embedding
    # request for N rows). Raised exceptions roll back the transaction.

    async def on_rows_written(self, contexts: list[WriteContext], *, config: BaseModel) -> None:
        """Fired after one or more rows of this column type are created/updated."""
        return None

    async def on_rows_archived(self, contexts: list[WriteContext], *, config: BaseModel) -> None:
        """Fired when one or more rows are archived (symmetric to ``on_rows_written``)."""
        return None


def effective_capabilities(handler: FieldTypeHandler, overrides: dict[str, Any] | None) -> Capabilities:
    """Apply a column's ``capability_overrides`` to its type's built-ins.

    Overrides may only *narrow* (opt out): a column can disable filtering,
    sorting, or fuzzy search, but cannot enable a capability the type lacks.
    Unknown keys are ignored.
    """
    base = handler.capabilities
    if not overrides:
        return base
    return Capabilities(
        filterable=base.filterable and bool(overrides.get("filterable", True)),
        sortable=base.sortable and bool(overrides.get("sortable", True)),
        fuzzy=base.fuzzy and bool(overrides.get("fuzzy", True)),
        filter_ops=base.filter_ops,
        index_kinds=base.index_kinds,
        default_index_kind=base.default_index_kind,
    )


__all__ = [
    "PG_CAST_TYPES",
    "Capabilities",
    "CellValue",
    "EmptyConfig",
    "FieldTypeHandler",
    "FilterOp",
    "effective_capabilities",
]
