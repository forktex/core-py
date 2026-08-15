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

"""Grid 4.0 — pure domain unit tests (no DB). Milestone 1.

Proves every configuration invariant is enforced at aggregate construction, and that
the two storage axes resolve to the right strategy — the whole point of the rewrite.
"""

from __future__ import annotations

import pytest

from forktex_core.grid.errors import BadRequestError, ReadOnlyStorage
from forktex_core.grid.domain.binding import Extension, Overlay
from forktex_core.grid.domain.enums import Materialization, RelationShape
from forktex_core.grid.domain.materialization import (
    DerivedValue,
    PayloadValue,
    PromotedValue,
    select_materialization,
)
from forktex_core.grid.domain.spec import ColumnSpec, RelationSpec, TableSpec
from forktex_core.grid.domain.storage import BoundOverlayStorage, OwnedStorage, select_storage
from forktex_core.grid.domain.table import Table


def _col(key="c", type_id="text", **kw) -> ColumnSpec:
    return ColumnSpec(key=key, label=key.title(), type_id=type_id, **kw)


# ── RelationShape carries the endpoint-uniqueness rule (one source of truth) ──


def test_relation_shape_unique_endpoints() -> None:
    assert RelationShape.one_to_one.unique_endpoints() == ("source", "target")
    assert RelationShape.one_to_many.unique_endpoints() == ("target",)
    assert RelationShape.many_to_one.unique_endpoints() == ("source",)
    assert RelationShape.many_to_many.unique_endpoints() == ()
    assert RelationShape.many_to_many.needs_through and not RelationShape.one_to_one.needs_through


# ── Typed binding validation ─────────────────────────────────────────────────


def test_binding_validates_identifiers() -> None:
    Overlay(physical_relation="public.client_record", namespace_column="org_id")
    Extension(physical_relation="public.client_record")
    with pytest.raises(BadRequestError):
        Overlay(physical_relation="public.clients; drop table x")
    with pytest.raises(BadRequestError):
        Overlay(physical_relation="public.clients", namespace_column="org id")


# ── ColumnSpec single-object invariants ──────────────────────────────────────


def test_ref_requires_relation_ref_and_vice_versa() -> None:
    ColumnSpec(key="company", label="Co", type_id="ref", relation_ref="company")
    with pytest.raises(BadRequestError):
        ColumnSpec(key="company", label="Co", type_id="ref")  # ref without relation_ref
    with pytest.raises(BadRequestError):
        ColumnSpec(key="x", label="X", type_id="text", relation_ref="company")  # relation_ref on non-ref


def test_derived_requires_source_and_vice_versa() -> None:
    ColumnSpec(
        key="tier",
        label="Tier",
        type_id="text",
        materialization=Materialization.derived,
        derived_source="company.loyalty_tier",
    )
    with pytest.raises(BadRequestError):
        ColumnSpec(key="tier", label="Tier", type_id="text", materialization=Materialization.derived)
    with pytest.raises(BadRequestError):
        ColumnSpec(key="tier", label="Tier", type_id="text", derived_source="company.tier")  # source w/o derived


def test_promoted_column_defaults_and_is_gated() -> None:
    spec = _col("amount", "integer", materialization=Materialization.promoted)
    assert spec.promoted_column == "amount"  # defaults to key
    with pytest.raises(BadRequestError):  # promoted_column only valid for promoted
        _col("amount", "integer", promoted_column="amt")


# ── RelationSpec + TableSpec invariants ──────────────────────────────────────


def test_relation_m2m_requires_through() -> None:
    RelationSpec(key="tags", source="post", target="tag", shape=RelationShape.many_to_many, through="post_tag")
    with pytest.raises(BadRequestError):
        RelationSpec(key="tags", source="post", target="tag", shape=RelationShape.many_to_many)
    with pytest.raises(BadRequestError):
        RelationSpec(key="c", source="a", target="b", shape=RelationShape.many_to_one, through="x")


def test_table_spec_rejects_bad_slug_and_dup_columns() -> None:
    with pytest.raises(BadRequestError):
        TableSpec(slug="a b", label="X")
    with pytest.raises(BadRequestError):
        TableSpec(slug="t", label="T", columns=(_col("x"), _col("x")))


# ── Strategy selection — the core move ───────────────────────────────────────


def test_select_materialization() -> None:
    assert isinstance(select_materialization(_col()), PayloadValue)
    assert isinstance(
        select_materialization(_col("a", "integer", materialization=Materialization.promoted)), PromotedValue
    )
    assert isinstance(
        select_materialization(_col("d", "text", materialization=Materialization.derived, derived_source="r.f")),
        DerivedValue,
    )


@pytest.mark.parametrize("type_id", ["json", "date", "datetime"])
def test_non_promotable_types_refused(type_id: str) -> None:
    with pytest.raises(BadRequestError):
        select_materialization(_col("c", type_id, materialization=Materialization.promoted))


def test_derived_value_is_read_only() -> None:
    strat = DerivedValue()
    assert strat.accepts_write() is False
    with pytest.raises(BadRequestError):
        strat.store({}, "c", "v")
    assert PayloadValue().accepts_write() and PromotedValue().needs_reconcile


def test_select_storage() -> None:
    assert isinstance(select_storage(None), OwnedStorage)
    assert isinstance(select_storage(Extension(physical_relation="public.h")), OwnedStorage)  # extension is owned
    ov = select_storage(Overlay(physical_relation="public.h"))
    assert isinstance(ov, BoundOverlayStorage) and not ov.writable


def test_overlay_is_read_only() -> None:
    OwnedStorage().ensure_writable()  # no raise
    with pytest.raises(ReadOnlyStorage):
        BoundOverlayStorage(Overlay(physical_relation="public.h")).ensure_writable()


# ── Table aggregate: invariants enforced at construction ─────────────────────


def test_owned_table_accepts_all_column_kinds() -> None:
    t = Table.declare(
        TableSpec(
            slug="deal",
            label="Deal",
            columns=(
                _col("name", "text"),
                _col("amount", "integer", materialization=Materialization.promoted),
                ColumnSpec(key="co", label="Co", type_id="ref", relation_ref="company"),
            ),
        )
    )
    assert t.writable and {c.key for c in t.columns} == {"name", "amount", "co"}
    assert t.column("amount").value.needs_reconcile


@pytest.mark.parametrize(
    "bad",
    [
        ColumnSpec(key="co", label="Co", type_id="ref", relation_ref="company"),
        ColumnSpec(key="amt", label="Amt", type_id="integer", materialization=Materialization.promoted),
        ColumnSpec(key="t", label="T", type_id="text", materialization=Materialization.derived, derived_source="co.x"),
    ],
)
def test_bound_overlay_rejects_non_projection_columns(bad: ColumnSpec) -> None:
    spec = TableSpec(slug="ext", label="Ext", binding=Overlay(physical_relation="public.host"), columns=(bad,))
    with pytest.raises(BadRequestError):
        Table.declare(spec)


def test_unknown_field_type_rejected() -> None:
    with pytest.raises(BadRequestError):
        Table.declare(TableSpec(slug="t", label="T", columns=(_col("c", "nope"),)))
