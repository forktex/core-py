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

"""Spec serialization symmetry — the frozen Pydantic specs round-trip through JSON.

``model_validate(model_dump(mode="json"))`` must reproduce the exact spec (the inverse
law the whole JSON management surface stands on) and stay JSON-clean (strings for enums,
lists for tuples) so a network/agentic consumer sees a stable wire form.
"""

from __future__ import annotations

import json

import pytest

from forktex_core.grid.errors import BadRequestError
from forktex_core.grid.domain.binding import (
    Extension,
    Overlay,
    binding_from_json,
    binding_to_json,
)
from forktex_core.grid.domain.enums import Materialization, OnDelete, RelationShape
from forktex_core.grid.domain.spec import (
    ColumnSpec,
    IndexSpec,
    RelationSpec,
    TableSpec,
)


def _roundtrip(spec):
    """A spec, its JSON, and the spec rebuilt from that JSON (must equal the original)."""
    doc = spec.model_dump(mode="json")
    json.dumps(doc)  # must be JSON-serialisable (no enums/tuples/UUID leaking through)
    return doc, type(spec).model_validate(doc)


# ── per-spec round-trip ──────────────────────────────────────────────────────
def test_column_spec_roundtrips() -> None:
    col = ColumnSpec(key="name", label="Name", type_id="text", is_required=True, is_unique=True)
    doc, back = _roundtrip(col)
    assert back == col
    assert doc["cardinality"] == "one" and doc["materialization"] == "payload"


def test_relation_spec_roundtrips() -> None:
    rel = RelationSpec(key="tags", source="post", target="tag", shape=RelationShape.many_to_many, through="post_tag")
    doc, back = _roundtrip(rel)
    assert back == rel
    assert doc["shape"] == "many_to_many" and doc["on_delete"] == "restrict"


@pytest.mark.parametrize("shape", list(RelationShape))
@pytest.mark.parametrize("on_delete", list(OnDelete))
def test_relation_spec_all_enum_combos_roundtrip(shape: RelationShape, on_delete: OnDelete) -> None:
    through = "j" if shape.needs_through else None
    rel = RelationSpec(key="r", source="a", target="b", shape=shape, through=through, on_delete=on_delete)
    _, back = _roundtrip(rel)
    assert back == rel


def test_index_spec_roundtrips() -> None:
    idx = IndexSpec(column_keys=("a", "b"), index_kind="btree", is_unique=True)
    _, idx_back = _roundtrip(idx)
    assert idx_back == idx


def test_table_spec_full_tree_roundtrips() -> None:
    table = TableSpec(
        slug="client",
        label="Client",
        namespace="org1",
        binding=Overlay(physical_relation="public.client_record", namespace_column="org_id"),
        scope_predicate={"column": "active", "op": "eq", "value": True},
        natural_key=("name",),
        columns=(
            ColumnSpec(key="name", label="Name", type_id="text", is_required=True),
            ColumnSpec(key="amt", label="Amt", type_id="integer", materialization=Materialization.promoted),
            ColumnSpec(key="co", label="Co", type_id="ref", relation_ref="company"),
            ColumnSpec(
                key="co_name",
                label="Co name",
                type_id="text",
                materialization=Materialization.derived,
                derived_source="co.name",
            ),
        ),
        is_system=True,
    )
    doc, back = _roundtrip(table)
    assert back == table
    # tuple fields serialise to JSON lists; the binding carries its discriminator
    assert isinstance(doc["columns"], list) and isinstance(doc["natural_key"], list)
    assert doc["binding"]["kind"] == "overlay"


# ── promoted-column defaulting survives the round-trip ───────────────────────
def test_promoted_column_defaults_to_key_and_persists() -> None:
    col = ColumnSpec(key="amt", label="Amt", type_id="integer", materialization=Materialization.promoted)
    assert col.promoted_column == "amt"
    _, back = _roundtrip(col)
    assert back.promoted_column == "amt"


# ── binding discriminated union round-trips via the free functions ───────────
def test_binding_json_roundtrip() -> None:
    for binding in (
        Overlay(physical_relation="public.host", column_map={"name": "display_name"}),
        Extension(physical_relation="public.host"),
    ):
        assert binding_from_json(binding_to_json(binding)) == binding
    assert binding_to_json(None) is None
    assert binding_from_json(None) is None


# ── invariants still raise the domain error (not pydantic's ValidationError) ──
def test_spec_invariants_raise_bad_request_from_json() -> None:
    with pytest.raises(BadRequestError):
        ColumnSpec.model_validate({"key": "x", "label": "X", "type_id": "ref"})  # ref w/o relation_ref
    with pytest.raises(BadRequestError):
        TableSpec.model_validate({"slug": "a b", "label": "X"})  # illegal slug


def test_from_dicts_covers_every_spec_field():
    """`from_dicts` is the JSON-boundary constructor, so a field it drops is a
    field a consumer declaring from JSON cannot set. It used to silently discard
    `binding`, `scope_predicate` and `natural_key` — which made a bound table
    impossible to declare that way at all."""
    from forktex_core.grid import FieldType, Overlay, TableSpec

    spec = TableSpec.from_dicts(
        slug="clients",
        label="Clients",
        namespace="org",
        columns=[{"key": "name", "label": "Name", "type_id": FieldType.text.value}],
        binding=Overlay(
            physical_relation="public.client_record",
            primary_key="id",
            namespace_column="org_id",
            column_map={"name": "display_name"},
        ),
        scope_predicate={"column": "name", "op": "is_null", "value": None},
        natural_key=["name"],
    )
    assert spec.binding is not None
    assert spec.binding.physical_relation == "public.client_record"
    assert spec.natural_key == ("name",)
    assert spec.scope_predicate == {"column": "name", "op": "is_null", "value": None}

    # Every declared field is reachable, so `from_dicts` and the constructor agree.
    assert set(TableSpec.model_fields) <= set(spec.model_dump())


def test_from_dicts_accepts_a_binding_as_plain_json():
    """The whole point of the dict constructor: a binding arriving over the wire."""
    from forktex_core.grid import Overlay, TableSpec

    spec = TableSpec.from_dicts(
        slug="c2",
        label="C2",
        columns=[{"key": "n", "label": "N", "type_id": "text"}],
        binding={"kind": "overlay", "physical_relation": "public.x", "primary_key": "id"},
    )
    assert isinstance(spec.binding, Overlay)
