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

"""Grid as a CRM substrate — the two headline use-cases, end-to-end.

1. **Standalone virtual CRM** — owned ``clients``/``companies``/``persons``/``addresses``
   interconnected by ref relations, with derived read-through, traversal, and cascade.
2. **Upgrading existing entities** — overlay pre-existing physical CRM tables read-only,
   then attach extension rows (custom fields linked 1:1 to a host row via ``external_ref``)
   and interconnect *those* with relations across the boundary.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid import (
    ColumnSpec,
    Grid,
    Materialization,
    OnDelete,
    Overlay,
    ReadOnlyStorage,
    RelationShape,
    RelationSpec,
    TableSpec,
    declare_relation,
)


def _cols(*specs: tuple[str, str]) -> tuple[ColumnSpec, ...]:
    return tuple(ColumnSpec(key=k, label=k.title(), type_id=t) for k, t in specs)


# ── 1. Standalone virtual CRM ─────────────────────────────────────────────────


async def test_standalone_virtual_crm(session: AsyncSession) -> None:
    companies = await Grid.declare(
        session, TableSpec(slug="companies", label="Companies", columns=_cols(("name", "text"), ("tier", "text")))
    )
    persons = await Grid.declare(session, TableSpec(slug="persons", label="Persons", columns=_cols(("name", "text"))))
    clients = await Grid.declare(session, TableSpec(slug="clients", label="Clients", columns=_cols(("name", "text"))))
    addresses = await Grid.declare(
        session, TableSpec(slug="addresses", label="Addresses", columns=_cols(("city", "text")))
    )

    # The interconnection: person→company, client→company, client→person, address→company.
    # Archiving a company nulls the back-refs (set_null) but cascades to its addresses.
    await declare_relation(
        session,
        RelationSpec(
            key="employer",
            source="persons",
            target="companies",
            shape=RelationShape.many_to_one,
            on_delete=OnDelete.set_null,
        ),
    )
    await declare_relation(
        session,
        RelationSpec(
            key="account",
            source="clients",
            target="companies",
            shape=RelationShape.many_to_one,
            on_delete=OnDelete.set_null,
        ),
    )
    await declare_relation(
        session,
        RelationSpec(
            key="contact",
            source="clients",
            target="persons",
            shape=RelationShape.many_to_one,
            on_delete=OnDelete.set_null,
        ),
    )
    await declare_relation(
        session,
        RelationSpec(
            key="located_at",
            source="addresses",
            target="companies",
            shape=RelationShape.many_to_one,
            on_delete=OnDelete.cascade,
        ),
    )

    await persons.add_column(ColumnSpec(key="employer", label="Employer", type_id="ref", relation_ref="employer"))
    await persons.add_column(
        ColumnSpec(
            key="employer_tier",
            label="Employer Tier",
            type_id="text",
            materialization=Materialization.derived,
            derived_source="employer.tier",
        )
    )
    await clients.add_column(ColumnSpec(key="account", label="Account", type_id="ref", relation_ref="account"))
    await clients.add_column(ColumnSpec(key="contact", label="Contact", type_id="ref", relation_ref="contact"))
    await addresses.add_column(
        ColumnSpec(key="located_at", label="Located At", type_id="ref", relation_ref="located_at")
    )

    acme = await companies.create({"name": "Acme", "tier": "gold"})
    ann = await persons.create({"name": "Ann", "employer": str(acme.id)})
    deal = await clients.create({"name": "Deal-1", "account": str(acme.id), "contact": str(ann.id)})
    await addresses.create({"city": "Berlin", "located_at": str(acme.id)})

    # Derived read-through: the person's employer tier resolves via the ref auto-join.
    ann_row = next(r for r in (await persons.query()).rows if r.id == ann.id)
    assert ann_row.values["employer_tier"] == "gold"

    # Relations list + graph traversal over the interconnected web.
    assert [r.values["name"] for r in await clients.related("account", deal.id)] == ["Acme"]
    assert [r.values["name"] for r in await clients.related("contact", deal.id)] == ["Ann"]
    depth = await clients.traverse(deal.id, direction="outbound", depth=3)
    assert acme.id in depth and ann.id in depth  # reaches both the account and the contact

    # Archiving the company cascades to its address, nulls the back-refs, keeps the rest.
    await companies.archive(acme.id)
    assert (await addresses.query()).rows == []  # cascade
    assert (await companies.query()).rows == []
    assert (await persons.get(ann.id)).values.get("employer") is None  # set_null
    assert (await clients.get(deal.id)).values.get("account") is None  # set_null


# ── 2. Upgrading existing physical entities: overlay + extension ───────────────


async def _make_host_crm(session: AsyncSession, schema: str, org: uuid.UUID) -> dict[str, uuid.UUID]:
    """A pre-existing physical CRM the grid does not own: client_record + company_record."""
    await session.execute(
        sa.text(f'CREATE TABLE "{schema}".company_record (id uuid PRIMARY KEY, org_id uuid, name text, tier text)')
    )
    await session.execute(
        sa.text(f'CREATE TABLE "{schema}".client_record (id uuid PRIMARY KEY, org_id uuid, name text, score integer)')
    )
    ids = {"acme": uuid.uuid4(), "beta": uuid.uuid4(), "deal": uuid.uuid4()}
    await session.execute(
        sa.text(f'INSERT INTO "{schema}".company_record VALUES (:i, :o, :n, :t)'),
        {"i": ids["acme"], "o": org, "n": "Acme", "t": "gold"},
    )
    await session.execute(
        sa.text(f'INSERT INTO "{schema}".company_record VALUES (:i, :o, :n, :t)'),
        {"i": ids["beta"], "o": org, "n": "Beta", "t": "silver"},
    )
    await session.execute(
        sa.text(f'INSERT INTO "{schema}".client_record VALUES (:i, :o, :n, :s)'),
        {"i": ids["deal"], "o": org, "n": "Deal-1", "s": 42},
    )
    return ids


async def test_overlay_existing_crm_tables(session: AsyncSession, grid_schema: str) -> None:
    org = uuid.uuid4()
    ids = await _make_host_crm(session, grid_schema, org)

    companies = await Grid.declare(
        session,
        TableSpec(
            slug="companies",
            label="Companies",
            namespace=str(org),
            binding=Overlay(physical_relation=f"{grid_schema}.company_record", namespace_column="org_id"),
            columns=_cols(("name", "text"), ("tier", "text")),
        ),
    )

    assert not companies.writable
    with pytest.raises(ReadOnlyStorage):
        await companies.create({"name": "nope"})

    # Read the host through the shared compiler: filter + sort.
    page = await companies.query(filter={"column": "tier", "op": "eq", "value": "gold"})
    assert [r.values["name"] for r in page.rows] == ["Acme"]
    ordered = await companies.query(sort=[{"column": "name"}])
    assert [r.values["name"] for r in ordered.rows] == ["Acme", "Beta"]
    assert {r.id for r in ordered.rows} == {ids["acme"], ids["beta"]}  # host PKs preserved


async def test_extension_upgrades_and_interconnects_host_entities(session: AsyncSession, grid_schema: str) -> None:
    org = uuid.uuid4()
    ids = await _make_host_crm(session, grid_schema, org)
    ns = str(org)

    # Extension tables: owned grid tables carrying custom fields, each row linked 1:1 to a
    # host row via external_ref. This is how you "upgrade" a physical entity you don't own.
    company_ext = await Grid.declare(
        session,
        TableSpec(slug="company_ext", label="Company Ext", namespace=ns, columns=_cols(("health_score", "integer"))),
    )
    client_ext = await Grid.declare(
        session,
        TableSpec(slug="client_ext", label="Client Ext", namespace=ns, columns=_cols(("stage", "text"))),
    )

    acme_ext = await company_ext.create({"health_score": 90}, external_ref=ids["acme"])
    deal_ext = await client_ext.create({"stage": "won"}, external_ref=ids["deal"])

    # The 1:1 link back to the host row.
    assert (await company_ext.get_by_external_ref(ids["acme"])).values["health_score"] == 90
    # And it is enforced unique per host row.
    with pytest.raises(Exception):  # noqa: B017 — AlreadyExistsError on duplicate external_ref
        await company_ext.create({"health_score": 1}, external_ref=ids["acme"])

    # Interconnect the upgraded entities: a relation between the extension tables (bound
    # host endpoints can't be related directly, so the extension rows carry the graph).
    await declare_relation(
        session,
        RelationSpec(key="client_of", source="client_ext", target="company_ext", shape=RelationShape.many_to_one),
        ns,
    )
    await client_ext.relate("client_of", deal_ext.id, acme_ext.id)

    linked = await client_ext.related("client_of", deal_ext.id)
    assert [r.values["health_score"] for r in linked] == [90]
    depth = await client_ext.traverse(deal_ext.id, direction="outbound", depth=2)
    assert acme_ext.id in depth
