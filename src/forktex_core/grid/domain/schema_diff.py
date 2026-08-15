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

"""The schema diff engine — a pure function of two ``Schema`` states.

``diff(actual, desired, prune=...)`` returns a **topologically ordered** ``ChangeSet`` that
converges ``actual`` to ``desired`` in one pass. It reads no database, so the whole thing is
exhaustively unit-testable offline.

Ordering is by dependency, which lets the applier run the tuple front-to-back with no
per-entity graph:

    create table (base cols) → create relation → add ref cols → add derived cols →
    alter table/column → create index → (drops in reverse) drop index → drop column →
    drop relation → drop table

The table↔relation cycle (a ``ref`` column needs its relation, which needs its endpoint
tables, which may carry ref columns) is broken by **deferring ref/derived columns** out of
``create_table`` to after relations exist — the same manual order callers use today.

Not inferred here (a structural diff cannot read intent): **rename** and **retype**. Both are
explicit imperative operations elsewhere; a diff only ever sees drop-old + create-new.
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict

from forktex_core.grid.domain.enums import Materialization
from forktex_core.grid.domain.schema import Schema
from forktex_core.grid.domain.spec import ColumnSpec, IndexSpec, RelationSpec, TableSpec

_REF_TYPE = "ref"


class ChangeOp(enum.StrEnum):
    create_table = "create_table"
    alter_table = "alter_table"
    drop_table = "drop_table"
    add_column = "add_column"
    alter_column = "alter_column"
    drop_column = "drop_column"
    create_relation = "create_relation"
    alter_relation = "alter_relation"
    drop_relation = "drop_relation"
    create_index = "create_index"
    drop_index = "drop_index"


# Dependency-ordered apply priority (ascending). Creates ascend, drops descend.
_PRIORITY: dict[ChangeOp, int] = {
    ChangeOp.create_table: 20,
    ChangeOp.alter_table: 22,
    ChangeOp.add_column: 26,  # base columns; ref/derived get a per-change bump (see _column_priority)
    ChangeOp.create_relation: 30,
    ChangeOp.alter_relation: 32,
    ChangeOp.alter_column: 50,
    ChangeOp.create_index: 60,
    ChangeOp.drop_index: 70,
    ChangeOp.drop_column: 80,
    ChangeOp.drop_relation: 90,
    ChangeOp.drop_table: 100,
}
_REF_COLUMN_PRIORITY = 40  # ref columns: after relations exist
_DERIVED_COLUMN_PRIORITY = 44  # derived columns: after the ref columns they project through

Spec = TableSpec | ColumnSpec | RelationSpec | IndexSpec


class Change(BaseModel):
    """One atomic step. ``desired``/``actual`` carry the typed specs the applier needs;
    ``table`` is the owning table slug for column/index ops."""

    model_config = ConfigDict(frozen=True)

    op: ChangeOp
    key: str  # entity identity: table/space slug, relation/column key, or index signature
    desired: Spec | None = None
    actual: Spec | None = None
    table: str | None = None
    destructive: bool = False

    @property
    def target(self) -> str:
        """A stable human/JSON reference, e.g. ``column:client.employer``."""
        kind = self.op.value.split("_", 1)[1]
        return f"{kind}:{self.table}.{self.key}" if self.table else f"{kind}:{self.key}"

    def summary(self) -> dict[str, Any]:
        """A JSON-able one-line description (for dry-run plans shown to agents/UIs)."""
        return {"op": self.op.value, "target": self.target, "destructive": self.destructive}


def _priority(change: Change) -> int:
    if change.op is ChangeOp.add_column and isinstance(change.desired, ColumnSpec):
        col = change.desired
        if col.type_id == _REF_TYPE:
            return _REF_COLUMN_PRIORITY
        if col.materialization is Materialization.derived:
            return _DERIVED_COLUMN_PRIORITY
    return _PRIORITY[change.op]


class ChangeSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    changes: tuple[Change, ...] = ()

    def is_empty(self) -> bool:
        return not self.changes

    @property
    def destructive(self) -> tuple[Change, ...]:
        return tuple(c for c in self.changes if c.destructive)

    def summary(self) -> list[dict[str, Any]]:
        return [c.summary() for c in self.changes]


def _is_deferred_column(col: ColumnSpec) -> bool:
    """A ref/derived column is created after its table (once relations exist)."""
    return col.type_id == _REF_TYPE or col.materialization is Materialization.derived


def _base_columns(table: TableSpec) -> tuple[ColumnSpec, ...]:
    return tuple(c for c in table.columns if not _is_deferred_column(c))


def _column_is_destructive(desired: ColumnSpec, actual: ColumnSpec | None) -> bool:
    """A column change that can fail against existing rows / loses information."""
    if actual is None:  # a freshly-required column with no default may reject existing rows
        return desired.is_required and desired.default_value is None
    return (
        desired.type_id != actual.type_id
        or desired.materialization != actual.materialization
        or desired.cardinality != actual.cardinality
        or (desired.is_required and not actual.is_required)
        or (desired.is_unique and not actual.is_unique)
    )


def diff(actual: Schema, desired: Schema, *, prune: bool = False) -> ChangeSet:
    """The ordered change set converging ``actual`` to ``desired``.

    ``prune`` controls entities present in ``actual`` but absent from ``desired``: dropped
    when ``True`` (authoritative), left untouched when ``False`` (partial patch).
    """
    changes: list[Change] = []

    for slug, spec in desired.tables.items():
        cur = actual.tables.get(slug)
        if cur is None:
            # Create with base columns only; ref/derived columns are deferred (added below).
            base = spec.model_copy(update={"columns": _base_columns(spec)})
            changes.append(Change(op=ChangeOp.create_table, key=slug, desired=base))
            for col in spec.columns:
                if _is_deferred_column(col):
                    changes.append(Change(op=ChangeOp.add_column, key=col.key, desired=col, table=slug))
        else:
            changes.extend(_diff_table_attrs(slug, spec, cur))
            changes.extend(_diff_columns(slug, spec, cur, prune=prune))
    if prune:
        for slug, cur in actual.tables.items():
            if slug not in desired.tables:
                changes.append(Change(op=ChangeOp.drop_table, key=slug, actual=cur, destructive=True))

    for key, spec in desired.relations.items():
        cur = actual.relations.get(key)
        if cur is None:
            changes.append(Change(op=ChangeOp.create_relation, key=key, desired=spec))
        elif cur != spec:
            # endpoint/shape/through changes are structural (recreate); on_delete alone is an alter
            structural = (cur.source, cur.target, cur.through, cur.shape) != (
                spec.source,
                spec.target,
                spec.through,
                spec.shape,
            )
            changes.append(
                Change(op=ChangeOp.alter_relation, key=key, desired=spec, actual=cur, destructive=structural)
            )
    if prune:
        for key, cur in actual.relations.items():
            if key not in desired.relations:
                changes.append(Change(op=ChangeOp.drop_relation, key=key, actual=cur, destructive=True))

    for slug in desired.tables:
        want = {_index_key(i): i for i in desired.indexes.get(slug, ())}
        have = {_index_key(i): i for i in actual.indexes.get(slug, ())} if slug in actual.tables else {}
        for sig, spec in want.items():
            if sig not in have:
                changes.append(Change(op=ChangeOp.create_index, key=sig, desired=spec, table=slug))
        for sig, cur in have.items():
            if sig not in want and (prune or slug in desired.tables):
                changes.append(Change(op=ChangeOp.drop_index, key=sig, actual=cur, table=slug, destructive=True))

    changes.sort(key=lambda c: (_priority(c), c.table or "", c.key))
    return ChangeSet(changes=tuple(changes))


def _diff_table_attrs(slug: str, desired: TableSpec, actual: TableSpec) -> list[Change]:
    """Table-level attribute changes (label/binding/scope/natural_key/is_system), columns aside."""
    fields = ("label", "namespace", "binding", "scope_predicate", "natural_key", "is_system")
    if any(getattr(desired, f) != getattr(actual, f) for f in fields):
        destructive = desired.binding != actual.binding  # rebinding an overlay is structural
        return [Change(op=ChangeOp.alter_table, key=slug, desired=desired, actual=actual, destructive=destructive)]
    return []


def _diff_columns(slug: str, desired: TableSpec, actual: TableSpec, *, prune: bool) -> list[Change]:
    want = {c.key: c for c in desired.columns}
    have = {c.key: c for c in actual.columns}
    out: list[Change] = []
    for key, col in want.items():
        cur = have.get(key)
        if cur is None:
            out.append(
                Change(
                    op=ChangeOp.add_column,
                    key=key,
                    desired=col,
                    table=slug,
                    destructive=_column_is_destructive(col, None),
                )
            )
        elif cur != col:
            out.append(
                Change(
                    op=ChangeOp.alter_column,
                    key=key,
                    desired=col,
                    actual=cur,
                    table=slug,
                    destructive=_column_is_destructive(col, cur),
                )
            )
    if prune:
        for key, cur in have.items():
            if key not in want:
                out.append(Change(op=ChangeOp.drop_column, key=key, actual=cur, table=slug, destructive=True))
    return out


def _index_key(idx: IndexSpec) -> str:
    """A stable identity for an index within a table (its columns + kind)."""
    return f"{idx.index_kind}:{','.join(idx.column_keys)}"


__all__ = ["Change", "ChangeOp", "ChangeSet", "diff"]
