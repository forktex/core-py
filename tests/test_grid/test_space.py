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

"""The ``Namespace`` facade — the one interface, driven the way every consumer would.

A consumer scopes a ``Namespace`` to a namespace and drives everything through it: build a schema
at runtime (typed or plain JSON) and ``apply`` it, ``describe`` it back, ``batch`` schema+data
in one transaction, and open ``Grid`` handles for row work. The headline guarantee:
``apply(describe())`` changes nothing (bidirectional round-trip).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid import RowOp, Namespace
from forktex_core.grid.errors import BadRequestError

NS = "shop"

# A whole schema as plain JSON — exactly what a network/agentic caller would send.
SCHEMA_DOC = {
    "namespace": NS,
    "tables": {
        "category": {
            "slug": "category",
            "label": "Category",
            "columns": [{"key": "name", "label": "Name", "type_id": "text", "is_required": True}],
        },
        "product": {
            "slug": "product",
            "label": "Product",
            "columns": [
                {"key": "name", "label": "Name", "type_id": "text", "is_required": True},
                {"key": "price", "label": "Price", "type_id": "decimal"},
                {"key": "category", "label": "Category", "type_id": "ref", "relation_ref": "cat"},
            ],
        },
    },
    "relations": {"cat": {"key": "cat", "source": "product", "target": "category", "shape": "many_to_one"}},
}


async def test_apply_from_json_creates_the_schema(session: AsyncSession) -> None:
    space = Namespace(session, NS)
    report = await space.apply(SCHEMA_DOC, prune=True)
    json.dumps(report)  # the report is plain JSON
    assert report["plan"]["changes"], "expected a non-empty plan"
    assert report["dry_run"] is False

    described = await space.describe()  # typed Schema
    assert set(described.tables) == {"category", "product"}
    assert set(described.relations) == {"cat"}


async def test_apply_of_describe_is_a_noop(session: AsyncSession) -> None:
    """The bidirectionality law through the facade."""
    space = Namespace(session, NS)
    await space.apply(SCHEMA_DOC, prune=True)

    described = await space.describe()
    report = await space.apply(described, prune=True, dry_run=True)
    assert report["plan"]["changes"] == []


async def test_partial_json_patch_adds_a_column(session: AsyncSession) -> None:
    space = Namespace(session, NS)
    await space.apply(SCHEMA_DOC, prune=True)

    patch = {
        "tables": {
            "category": {
                "slug": "category",
                "label": "Category",
                "columns": [
                    {"key": "name", "label": "Name", "type_id": "text", "is_required": True},
                    {"key": "blurb", "label": "Blurb", "type_id": "text"},
                ],
            }
        }
    }
    await space.apply(patch, prune=False)  # partial: product untouched

    described = await space.describe()
    assert {c.key for c in described.tables["category"].columns} == {"name", "blurb"}
    assert set(described.tables) == {"category", "product"}


async def test_batch_schema_then_data(session: AsyncSession) -> None:
    space = Namespace(session, NS)
    report = await space.batch(
        SCHEMA_DOC,
        rows=[
            RowOp(op="create", table="category", values={"name": "Books"}),
            RowOp(op="create_many", table="product", rows=[{"name": "A"}, {"name": "B"}]),
        ],
        prune=True,
    )
    json.dumps(report)
    assert report["reconcile"]["plan"]["changes"]
    assert [r["op"] for r in report["results"]] == ["create", "create_many"]

    products = await space.table("product")
    assert len((await products.query()).rows) == 2


async def test_destructive_apply_is_gated(session: AsyncSession) -> None:
    space = Namespace(session, NS)
    await space.apply(SCHEMA_DOC, prune=True)
    shrunk = {"namespace": NS, "tables": {"category": SCHEMA_DOC["tables"]["category"]}}
    with pytest.raises(BadRequestError, match="destructive"):
        await space.apply(shrunk, prune=True)
