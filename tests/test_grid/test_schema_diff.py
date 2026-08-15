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

"""The pure schema diff engine — exhaustive, DB-free.

Covers create/alter/drop across every entity kind, partial (``prune=False``) vs authoritative
(``prune=True``), the dependency ordering (ref/derived columns deferred past relations), the
destructive-change flags, and idempotency (``diff(cat, cat)`` is empty).
"""

from __future__ import annotations

import json

from forktex_core.grid.domain.schema import Schema
from forktex_core.grid.domain.schema_diff import ChangeOp, diff
from forktex_core.grid.domain.enums import Materialization, OnDelete, RelationShape
from forktex_core.grid.domain.spec import ColumnSpec, IndexSpec, RelationSpec, TableSpec

NS = "acme"


def _col(key: str, type_id: str = "text", **kw) -> ColumnSpec:
    return ColumnSpec(key=key, label=key.title(), type_id=type_id, **kw)


def _table(slug: str, *cols: ColumnSpec, **kw) -> TableSpec:
    return TableSpec(slug=slug, label=slug.title(), namespace=NS, columns=cols, **kw)


def _crm() -> Schema:
    """company + client with a ref column projecting a relation + a derived column."""
    company = _table("company", _col("name"))
    client = _table(
        "client",
        _col("name"),
        _col("employer", "ref", relation_ref="employer"),
        _col("emp_name", materialization=Materialization.derived, derived_source="employer.name"),
    )
    rel = RelationSpec(key="employer", source="client", target="company", shape=RelationShape.many_to_one)
    return Schema(namespace=NS, tables={"company": company, "client": client}, relations={"employer": rel})


def _ops(changeset) -> list[ChangeOp]:
    return [c.op for c in changeset.changes]


# ── empty + idempotency ───────────────────────────────────────────────────────
def test_empty_diff_is_empty() -> None:
    assert diff(Schema(namespace=NS), Schema(namespace=NS)).is_empty()


def test_diff_of_identical_catalog_is_empty() -> None:
    cat = _crm()
    assert diff(cat, cat).is_empty()
    assert diff(cat, cat, prune=True).is_empty()


# ── create ordering: ref/derived columns deferred past relations ────────────────
def test_create_orders_tables_then_relations_then_ref_then_derived() -> None:
    cs = diff(Schema(namespace=NS), _crm())
    changes = cs.changes
    assert _ops(cs) == [
        ChangeOp.create_table,  # company
        ChangeOp.create_table,  # client
        ChangeOp.create_relation,  # employer
        ChangeOp.add_column,  # client.employer (ref)
        ChangeOp.add_column,  # client.emp_name (derived)
    ]
    # the created client table carries ONLY its base column; ref/derived deferred to add_column
    client_create = next(c for c in changes if c.op is ChangeOp.create_table and c.key == "client")
    assert [col.key for col in client_create.desired.columns] == ["name"]
    ref_add = next(c for c in changes if c.op is ChangeOp.add_column and c.key == "employer")
    derived_add = next(c for c in changes if c.op is ChangeOp.add_column and c.key == "emp_name")
    assert changes.index(ref_add) < changes.index(derived_add)


# ── prune governs actual-only entities ──────────────────────────────────────────
def test_prune_false_leaves_extra_entities_untouched() -> None:
    actual = _crm()
    desired = Schema(namespace=NS, tables={"company": actual.tables["company"]})
    assert diff(actual, desired, prune=False).is_empty()  # client/relation untouched


def test_prune_true_drops_extra_entities_in_reverse_order() -> None:
    actual = _crm()
    desired = Schema(namespace=NS, tables={"company": actual.tables["company"]})
    cs = diff(actual, desired, prune=True)
    ops = _ops(cs)
    # relation dropped before the table it references
    assert ops == [ChangeOp.drop_relation, ChangeOp.drop_table]
    assert all(c.destructive for c in cs.changes)
    assert cs.destructive == cs.changes


# ── column alters + destructive flags ──────────────────────────────────────────
def test_alter_column_label_is_non_destructive() -> None:
    actual = _table("t", _col("name"))
    desired = _table("t", ColumnSpec(key="name", label="Full name", type_id="text"))
    cs = diff(Schema(namespace=NS, tables={"t": actual}), Schema(namespace=NS, tables={"t": desired}))
    (change,) = cs.changes
    assert change.op is ChangeOp.alter_column and not change.destructive


def test_tightening_required_or_unique_is_destructive() -> None:
    actual = _table("t", _col("name"))
    desired = _table("t", _col("name", is_required=True))
    (change,) = diff(Schema(namespace=NS, tables={"t": actual}), Schema(namespace=NS, tables={"t": desired})).changes
    assert change.op is ChangeOp.alter_column and change.destructive


def test_adding_required_column_without_default_is_destructive() -> None:
    actual = _table("t", _col("name"))
    desired = _table("t", _col("name"), _col("age", "integer", is_required=True))
    add = next(
        c
        for c in diff(Schema(namespace=NS, tables={"t": actual}), Schema(namespace=NS, tables={"t": desired})).changes
        if c.op is ChangeOp.add_column
    )
    assert add.destructive


def test_adding_optional_column_is_not_destructive() -> None:
    actual = _table("t", _col("name"))
    desired = _table("t", _col("name"), _col("age", "integer"))
    add = next(
        c
        for c in diff(Schema(namespace=NS, tables={"t": actual}), Schema(namespace=NS, tables={"t": desired})).changes
        if c.op is ChangeOp.add_column
    )
    assert not add.destructive


# ── relations: on_delete alter vs structural change ─────────────────────────────
def test_relation_on_delete_change_is_non_destructive_alter() -> None:
    a = RelationSpec(key="r", source="x", target="y", shape=RelationShape.many_to_one)
    b = RelationSpec(key="r", source="x", target="y", shape=RelationShape.many_to_one, on_delete=OnDelete.cascade)
    tables = {"x": _table("x"), "y": _table("y")}
    (change,) = diff(
        Schema(namespace=NS, tables=tables, relations={"r": a}),
        Schema(namespace=NS, tables=tables, relations={"r": b}),
    ).changes
    assert change.op is ChangeOp.alter_relation and not change.destructive


def test_relation_shape_change_is_destructive_alter() -> None:
    a = RelationSpec(key="r", source="x", target="y", shape=RelationShape.many_to_one)
    b = RelationSpec(key="r", source="x", target="y", shape=RelationShape.one_to_one)
    tables = {"x": _table("x"), "y": _table("y")}
    (change,) = diff(
        Schema(namespace=NS, tables=tables, relations={"r": a}),
        Schema(namespace=NS, tables=tables, relations={"r": b}),
    ).changes
    assert change.op is ChangeOp.alter_relation and change.destructive


# ── indexes ─────────────────────────────────────────────────────────────────────
def test_index_create_and_drop() -> None:
    table = _table("t", _col("name"))
    idx = IndexSpec(column_keys=("name",), is_unique=True)
    no_idx = Schema(namespace=NS, tables={"t": table})
    with_idx = Schema(namespace=NS, tables={"t": table}, indexes={"t": (idx,)})

    (created,) = diff(no_idx, with_idx).changes
    assert created.op is ChangeOp.create_index and created.table == "t"
    (dropped,) = diff(with_idx, no_idx, prune=True).changes
    assert dropped.op is ChangeOp.drop_index and dropped.destructive


# ── the plan summary is JSON-able (dry-run surface for agents/UIs) ──────────────
def test_changeset_summary_is_json_serialisable() -> None:
    cs = diff(Schema(namespace=NS), _crm())
    payload = cs.summary()
    json.dumps(payload)
    # tables (order-independent at creation) come first, then the relation, then ref/derived cols
    assert {"op": "create_table", "target": "table:company", "destructive": False} in payload[:2]
    assert {"op": "create_table", "target": "table:client", "destructive": False} in payload[:2]
    assert {"op": "add_column", "target": "column:client.employer", "destructive": False} in payload
