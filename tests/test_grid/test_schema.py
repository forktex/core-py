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

"""Namespace-wide schema hydration — load the whole configuration into memory.

The first step of the JSON management surface: after declaring a small CRM schema, the
whole namespace hydrates into one ``Schema`` that (a) passes referential ``check()`` and
(b) survives the JSON round-trip ``from_document(to_document())`` unchanged — the property
the bidirectional interface stands on.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid import ColumnSpec, Grid, RelationShape, RelationSpec, TableSpec, declare_relation
from forktex_core.grid.errors import BadRequestError
from forktex_core.grid.domain.schema import Schema
from forktex_core.grid.persist.schema_repo import hydrate

NS = "acme"


async def _crm(session: AsyncSession) -> None:
    """company + client, a many_to_one 'employer' relation, and client's ref column."""
    await Grid.declare(
        session,
        TableSpec(
            slug="company",
            label="Company",
            namespace=NS,
            columns=(ColumnSpec(key="name", label="Name", type_id="text", is_required=True),),
        ),
    )
    client = await Grid.declare(
        session,
        TableSpec(
            slug="client",
            label="Client",
            namespace=NS,
            columns=(ColumnSpec(key="name", label="Name", type_id="text"),),
        ),
    )
    await declare_relation(
        session,
        RelationSpec(key="employer", source="client", target="company", shape=RelationShape.many_to_one),
        namespace=NS,
    )
    await client.add_column(ColumnSpec(key="employer", label="Employer", type_id="ref", relation_ref="employer"))


async def test_hydrate_loads_the_whole_namespace(session: AsyncSession) -> None:
    await _crm(session)
    cat = await hydrate(session, NS)

    assert cat.namespace == NS
    assert set(cat.tables) == {"company", "client"}
    assert set(cat.relations) == {"employer"}
    # the ref column resolved back to its relation key (no per-column query)
    ref_col = next(c for c in cat.tables["client"].columns if c.key == "employer")
    assert ref_col.type_id == "ref" and ref_col.relation_ref == "employer"
    rel = cat.relations["employer"]
    assert (rel.source, rel.target, rel.shape) == ("client", "company", RelationShape.many_to_one)

    cat.check()  # referential integrity holds


async def test_hydrated_catalog_json_roundtrips(session: AsyncSession) -> None:
    await _crm(session)
    cat = await hydrate(session, NS)

    doc = cat.to_document()
    rebuilt = Schema.from_document(doc)
    assert rebuilt == cat
    assert rebuilt.to_document() == doc  # document is a stable fixed point


async def test_empty_namespace_hydrates_to_empty_catalog(session: AsyncSession) -> None:
    cat = await hydrate(session, "nonexistent")
    assert cat.tables == {} and cat.relations == {} and cat.indexes == {}
    cat.check()


def test_check_rejects_ref_to_unknown_relation() -> None:
    cat = Schema(
        namespace=NS,
        tables={
            "client": TableSpec(
                slug="client",
                label="Client",
                namespace=NS,
                columns=(ColumnSpec(key="employer", label="E", type_id="ref", relation_ref="ghost"),),
            )
        },
    )
    with pytest.raises(BadRequestError, match="unknown relation"):
        cat.check()


def test_check_rejects_relation_with_missing_endpoint() -> None:
    cat = Schema(
        namespace=NS,
        tables={"client": TableSpec(slug="client", label="Client", namespace=NS)},
        relations={
            "employer": RelationSpec(key="employer", source="client", target="company", shape=RelationShape.many_to_one)
        },
    )
    with pytest.raises(BadRequestError, match="target table 'company'"):
        cat.check()
