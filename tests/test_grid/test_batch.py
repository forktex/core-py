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

"""The mutation batch end-to-end — schema + data in one atomic transaction."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid import Grid
from forktex_core.grid.errors import BadRequestError
from forktex_core.grid.write.schema import ReconcileOptions
from forktex_core.grid.write.batch import Batch, RowOp, apply_batch
from forktex_core.grid.domain.schema import Schema
from forktex_core.grid.domain.spec import ColumnSpec, TableSpec
from forktex_core.grid.persist.schema_repo import hydrate

NS = "acme"


def _two_tables() -> Schema:
    return Schema(
        namespace=NS,
        tables={
            "company": TableSpec(
                slug="company",
                label="Company",
                namespace=NS,
                columns=(ColumnSpec(key="name", label="Name", type_id="text"),),
            ),
            "person": TableSpec(
                slug="person",
                label="Person",
                namespace=NS,
                columns=(ColumnSpec(key="name", label="Name", type_id="text"),),
            ),
        },
    )


async def _count(session: AsyncSession, slug: str) -> int:
    grid = await Grid.open(session, slug=slug, namespace=NS)
    return len((await grid.query(include_total=True)).rows)


async def test_schema_and_data_apply_atomically(session: AsyncSession) -> None:
    batch = Batch(
        namespace=NS,
        desired=_two_tables(),
        options=ReconcileOptions(prune=True),
        mutations=(
            RowOp(op="create", table="company", values={"name": "Acme"}),
            RowOp(op="create_many", table="person", rows=[{"name": "Ann"}, {"name": "Bob"}]),
        ),
    )
    report = await apply_batch(session, batch)

    assert report.reconcile is not None and not report.reconcile.plan.is_empty()
    assert [r.op for r in report.results] == ["create", "create_many"]
    assert len(report.results[1].rows) == 2
    assert await _count(session, "company") == 1
    assert await _count(session, "person") == 2


async def test_cross_table_batch_over_existing_schema(session: AsyncSession) -> None:
    await apply_batch(session, Batch(namespace=NS, desired=_two_tables(), options=ReconcileOptions(prune=True)))

    batch = Batch(
        namespace=NS,
        mutations=(
            RowOp(op="create", table="company", values={"name": "Globex"}),
            RowOp(op="create", table="person", values={"name": "Cid"}),
        ),
    )
    report = await apply_batch(session, batch)
    assert len(report.results) == 2
    assert await _count(session, "company") == 1 and await _count(session, "person") == 1


async def test_batch_rolls_back_everything_on_failure(session: AsyncSession) -> None:
    batch = Batch(
        namespace=NS,
        desired=_two_tables(),
        options=ReconcileOptions(prune=True),
        mutations=(
            RowOp(op="create", table="company", values={"name": "Acme"}),
            RowOp(op="create", table="company", values={"nope": "x"}),  # unknown column → fails
        ),
    )
    with pytest.raises(BadRequestError):
        await apply_batch(session, batch)

    # the whole unit rolled back — even the schema the batch would have created is gone
    assert (await hydrate(session, NS)).tables == {}


async def test_dry_run_batch_reports_plan_without_writing(session: AsyncSession) -> None:
    batch = Batch(
        namespace=NS,
        desired=_two_tables(),
        options=ReconcileOptions(prune=True, dry_run=True),
        mutations=(RowOp(op="create", table="company", values={"name": "Acme"}),),
    )
    report = await apply_batch(session, batch)
    assert report.reconcile is not None and report.reconcile.dry_run
    assert report.results == ()
    assert (await hydrate(session, NS)).tables == {}


async def test_batch_report_is_json_serialisable() -> None:
    import json

    from forktex_core.grid.write.batch import BatchReport

    # the report is a Pydantic model — model_dump(mode="json") is the uniform serializer
    json.dumps(BatchReport().model_dump(mode="json"))
