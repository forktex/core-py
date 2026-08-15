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

"""STORY: multi-tenant isolation.

Two tenants share one Postgres schema, one Qdrant instance, and one
MinIO bucket. The substrate's tenant model relies on:

  - the ``namespace`` column on every Grid table for row-level
    isolation,
  - namespace-prefixed Qdrant collection names for vector isolation,
  - namespace-scoped ``Bundle.to_graph()`` snapshots for cross-Grid
    traversal isolation.

Acts:

  Act 1. Provision two tenants ``acme`` and ``beta``; each declares a
         Bundle ``crm`` with one ``leads`` Grid (TEXT title + remote
         VECTOR embedding).
  Act 2. Each tenant writes one row to their own Grid.
  Act 3. Verify ``Grid.query()`` is namespace-isolated.
  Act 4. Verify Qdrant collection naming isolates the two tenants.
  Act 5. Verify ``Bundle.to_graph()`` only sees the caller's rows.
  Act 6. Archive in tenant A; verify tenant B is untouched.

Real Postgres + real Qdrant. No mocks. Cleanup of both tenants'
collections runs in the class-scoped tracker so an early-act failure
doesn't leak.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import forktex_core.space  # noqa: F401  side-effect: register rich VECTOR
from forktex_core.grid import FieldType, Grid, TableSpec, apply_migrations
from forktex_core.space import Bundle
from forktex_core.space.types.vector import VECTOR_TYPE_ID
from forktex_core.vector import SearchQuery, register as register_vector

_SCHEMA = "forktex_grid"


async def _declare(session, *, namespace, slug, label, columns, is_system=False):
    return await Grid.declare(
        session,
        TableSpec.from_dicts(slug=slug, label=label, namespace=namespace, is_system=is_system, columns=columns),
    )


class TenantBundle(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    namespace: str
    leads: Grid
    space: Bundle


class IsoState(BaseModel):
    """In-flight state across the multi-tenant story acts."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: AsyncSession
    vector_client_name: str
    acme: TenantBundle | None = None
    beta: TenantBundle | None = None
    acme_lead: object | None = None
    beta_lead: object | None = None
    acme_collection: str | None = None
    beta_collection: str | None = None
    collections_created: list[tuple[str, str]] = Field(default_factory=list)

    def require(self, field: str) -> object:
        """The value act 1 stored, or a diagnosis of why it is missing.

        These acts share one class-scoped `IsoState` and only make sense in
        order. Reaching a later act with an empty state means the class-scoped
        fixture was rebuilt (or the acts ran out of order) — a suite-level
        problem that otherwise surfaces as a bare
        ``AttributeError: 'NoneType' object has no attribute 'leads'`` several
        frames away from the cause.
        """
        value = getattr(self, field)
        if value is None:
            raise AssertionError(
                f"IsoState.{field} is unset: act 1 did not populate this state object. "
                "These acts share class-scoped state and must run in order."
            )
        return value


async def _provision_tenant(session: AsyncSession, namespace: str, *, vector_client_name: str) -> TenantBundle:
    leads = await _declare(
        session,
        namespace=namespace,
        slug="leads",
        label="Leads",
        columns=[
            {"key": "title", "label": "Title", "type_id": FieldType.text.value},
            {
                "key": "embedding",
                "label": "Embedding",
                "type_id": VECTOR_TYPE_ID,
                "config": {"storage_mode": "remote", "dimensions": 4, "client_name": vector_client_name},
            },
        ],
    )
    space = await Bundle.declare(session, namespace=namespace, slug="crm", members=[leads])
    return TenantBundle(namespace=namespace, leads=leads, space=space)


@pytest.mark.asyncio(loop_scope="class")
class TestMultiTenantIsolation:
    """Six acts proving the namespace/collection/snapshot tenant model.
    Class-scoped state carries both tenants across the acts."""

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def state(self, postgres_url, qdrant_url: str):
        fresh_schema = f"story_mt_{uuid.uuid4().hex[:8]}"
        engine = create_async_engine(
            postgres_url.render_as_string(hide_password=False),
            execution_options={"schema_translate_map": {_SCHEMA: fresh_schema}},
        )
        await apply_migrations(engine, schema=fresh_schema)
        maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

        vector_client_name = f"story-mt-vector-{uuid.uuid4().hex[:6]}"
        register_vector(name=vector_client_name, qdrant_url=qdrant_url)

        async with maker() as session:
            iso = IsoState(session=session, vector_client_name=vector_client_name)
            yield iso

        # Class-scope cleanup of any tracked collections.
        from forktex_core.vector import get_client

        for client_name, coll in iso.collections_created:
            try:
                await get_client(client_name).collection(coll).delete()
            except Exception:
                pass
        await engine.dispose()

    # ── Act 1 ────────────────────────────────────────────────────────────

    async def test_act1_provision_two_tenants_in_same_schema(self, state: IsoState):
        ns_a = f"acme-{uuid.uuid4().hex[:8]}"
        ns_b = f"beta-{uuid.uuid4().hex[:8]}"
        state.acme = await _provision_tenant(state.session, ns_a, vector_client_name=state.vector_client_name)
        state.beta = await _provision_tenant(state.session, ns_b, vector_client_name=state.vector_client_name)
        state.acme_collection = f"{ns_a}--leads--embedding"
        state.beta_collection = f"{ns_b}--leads--embedding"

        assert state.acme.space.slug == state.beta.space.slug == "crm"
        assert state.acme.namespace != state.beta.namespace

    # ── Act 2 ────────────────────────────────────────────────────────────

    async def test_act2_each_tenant_writes_their_own_row(self, state: IsoState):
        state.acme_lead = await state.require("acme").leads.create(
            {"title": "ACME deal", "embedding": [1.0, 0.0, 0.0, 0.0]}
        )
        state.beta_lead = await state.require("beta").leads.create(
            {"title": "Beta opportunity", "embedding": [0.0, 1.0, 0.0, 0.0]}
        )
        # Track collections for class-scope cleanup.
        state.collections_created.append((state.vector_client_name, state.require("acme_collection")))
        state.collections_created.append((state.vector_client_name, state.require("beta_collection")))
        await state.session.commit()
        assert state.require("acme_lead").values["title"] == "ACME deal"
        assert state.require("beta_lead").values["title"] == "Beta opportunity"

    # ── Act 3 ────────────────────────────────────────────────────────────

    async def test_act3_grid_query_is_namespace_isolated(self, state: IsoState):
        acme_titles = sorted(r.values["title"] for r in (await state.require("acme").leads.query()).rows)
        beta_titles = sorted(r.values["title"] for r in (await state.require("beta").leads.query()).rows)
        assert acme_titles == ["ACME deal"]
        assert beta_titles == ["Beta opportunity"]

    # ── Act 4 ────────────────────────────────────────────────────────────

    async def test_act4_qdrant_collections_are_namespace_prefixed(self, state: IsoState):
        from forktex_core.vector import get_client as get_vector

        vc = get_vector(state.vector_client_name)
        acme_handle = vc.collection(state.require("acme_collection"))
        beta_handle = vc.collection(state.require("beta_collection"))

        # Searching ACME's collection — even with BETA's query vector —
        # never returns BETA's point: the data physically lives elsewhere.
        acme_hits = await acme_handle.search(SearchQuery(vector=[0.0, 1.0, 0.0, 0.0]).limit(5))
        assert all(str(h.id) != str(state.require("beta_lead").id) for h in acme_hits)

        beta_hits = await beta_handle.search(SearchQuery(vector=[1.0, 0.0, 0.0, 0.0]).limit(5))
        assert all(str(h.id) != str(state.require("acme_lead").id) for h in beta_hits)

    # ── Act 5 ────────────────────────────────────────────────────────────

    async def test_act5_space_to_graph_is_namespace_scoped(self, state: IsoState):
        acme_graph = await state.require("acme").space.to_graph()
        beta_graph = await state.require("beta").space.to_graph()
        assert {n.id for n in acme_graph.nodes} == {str(state.require("acme_lead").id)}
        assert {n.id for n in beta_graph.nodes} == {str(state.require("beta_lead").id)}

    # ── Act 6 ────────────────────────────────────────────────────────────

    async def test_act6_archive_in_one_tenant_leaves_the_other_untouched(self, state: IsoState):
        from forktex_core.vector import get_client as get_vector

        await state.require("acme").leads.archive(state.require("acme_lead").id)
        await state.session.commit()

        # ACME's view is empty; ACME's vector point is gone.
        assert (await state.require("acme").leads.query()).rows == []
        vc = get_vector(state.vector_client_name)
        acme_handle = vc.collection(state.require("acme_collection"))
        acme_post = await acme_handle.search(SearchQuery(vector=[1.0, 0.0, 0.0, 0.0]).limit(5))
        assert all(str(h.id) != str(state.require("acme_lead").id) for h in acme_post)

        # BETA is untouched.
        beta_after = await state.require("beta").leads.query()
        assert sorted(r.values["title"] for r in beta_after.rows) == ["Beta opportunity"]
        beta_handle = vc.collection(state.require("beta_collection"))
        beta_post = await beta_handle.search(SearchQuery(vector=[0.0, 1.0, 0.0, 0.0]).limit(5))
        assert any(str(h.id) == str(state.require("beta_lead").id) for h in beta_post)
