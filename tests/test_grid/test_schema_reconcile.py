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

"""The schema reconciler, end-to-end against Postgres.

Applies a whole desired schema from empty, proves the **bidirectional no-op** guarantee
(re-applying what ``hydrate`` reports changes nothing), and exercises partial patch, prune,
the destructive gate, and dry-run — all through the single ``SchemaReconciler`` core.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid.errors import BadRequestError
from forktex_core.grid.write.schema import SchemaReconciler, ReconcileOptions
from forktex_core.grid.domain.schema import Schema
from forktex_core.grid.domain.enums import Materialization, RelationShape
from forktex_core.grid.domain.spec import ColumnSpec, RelationSpec, TableSpec
from forktex_core.grid.persist.schema_repo import hydrate

NS = "acme"


def _crm() -> Schema:
    company = TableSpec(
        slug="company",
        label="Company",
        namespace=NS,
        columns=(ColumnSpec(key="name", label="Name", type_id="text", display_order=0),),
    )
    client = TableSpec(
        slug="client",
        label="Client",
        namespace=NS,
        columns=(
            ColumnSpec(key="name", label="Name", type_id="text", display_order=0),
            ColumnSpec(key="employer", label="Employer", type_id="ref", relation_ref="employer", display_order=1),
            ColumnSpec(
                key="emp_name",
                label="Employer name",
                type_id="text",
                materialization=Materialization.derived,
                derived_source="employer.name",
                display_order=2,
            ),
        ),
    )
    rel = RelationSpec(key="employer", source="client", target="company", shape=RelationShape.many_to_one)
    return Schema(namespace=NS, tables={"company": company, "client": client}, relations={"employer": rel})


async def test_apply_whole_catalog_from_empty(session: AsyncSession) -> None:
    desired = _crm()
    report = await SchemaReconciler().reconcile(session, desired, options=ReconcileOptions(prune=True))

    assert not report.plan.is_empty()
    assert await hydrate(session, NS) == desired  # actual now equals desired exactly


async def test_apply_of_describe_is_a_noop(session: AsyncSession) -> None:
    """The bidirectionality law: re-applying what hydrate reports produces no change."""
    await SchemaReconciler().reconcile(session, _crm(), options=ReconcileOptions(prune=True))

    current = await hydrate(session, NS)
    report = await SchemaReconciler().reconcile(session, current, options=ReconcileOptions(prune=True, dry_run=True))
    assert report.plan.is_empty()


async def test_reapplying_the_same_catalog_is_idempotent(session: AsyncSession) -> None:
    r = SchemaReconciler()
    await r.reconcile(session, _crm(), options=ReconcileOptions(prune=True))
    again = await r.reconcile(session, _crm(), options=ReconcileOptions(prune=True))
    assert again.plan.is_empty() and again.applied == ()


async def test_partial_patch_adds_column_without_touching_others(session: AsyncSession) -> None:
    await SchemaReconciler().reconcile(session, _crm(), options=ReconcileOptions(prune=True))

    # a PARTIAL desired: just the company table, now with an extra column
    patch = Schema(
        namespace=NS,
        tables={
            "company": TableSpec(
                slug="company",
                label="Company",
                namespace=NS,
                columns=(
                    ColumnSpec(key="name", label="Name", type_id="text", display_order=0),
                    ColumnSpec(key="tier", label="Tier", type_id="text", display_order=1),
                ),
            )
        },
    )
    await SchemaReconciler().reconcile(session, patch, options=ReconcileOptions(prune=False))

    cat = await hydrate(session, NS)
    assert {c.key for c in cat.tables["company"].columns} == {"name", "tier"}
    assert set(cat.tables) == {"company", "client"}  # client untouched
    assert set(cat.relations) == {"employer"}  # relation untouched


async def test_prune_drops_absent_entities_with_permission(session: AsyncSession) -> None:
    await SchemaReconciler().reconcile(session, _crm(), options=ReconcileOptions(prune=True))

    # authoritative desired = only company (client + relation should be pruned)
    company_only = Schema(namespace=NS, tables={"company": _crm().tables["company"]})
    await SchemaReconciler().reconcile(
        session, company_only, options=ReconcileOptions(prune=True, allow_destructive=True)
    )

    cat = await hydrate(session, NS)
    assert set(cat.tables) == {"company"} and cat.relations == {}


async def test_destructive_changes_are_gated(session: AsyncSession) -> None:
    await SchemaReconciler().reconcile(session, _crm(), options=ReconcileOptions(prune=True))
    company_only = Schema(namespace=NS, tables={"company": _crm().tables["company"]})

    with pytest.raises(BadRequestError, match="destructive"):
        await SchemaReconciler().reconcile(session, company_only, options=ReconcileOptions(prune=True))


async def test_dry_run_mutates_nothing(session: AsyncSession) -> None:
    report = await SchemaReconciler().reconcile(session, _crm(), options=ReconcileOptions(prune=True, dry_run=True))
    assert report.dry_run and not report.plan.is_empty()
    assert (await hydrate(session, NS)).tables == {}  # nothing was created
