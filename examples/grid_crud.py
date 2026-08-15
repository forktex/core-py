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

"""``Grid`` end-to-end CRUD against Postgres — pure tabular.

Declares a ``leads`` and ``notes`` Grid, exercises create/patch/query/archive
on rows, declares a relation, and demonstrates single-Grid neighbor traversal.
Set ``POSTGRES_URL`` to a SQLAlchemy asyncpg URL before running. The schema
is created on the fly and lives in a unique-per-run namespace, so repeated
runs against the same database don't collide.

Run with ``POSTGRES_URL=postgresql+asyncpg://… python examples/grid_crud.py``.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forktex_core.grid import (
    FieldType,
    Grid,
    RelationShape,
    RelationSpec,
    TableSpec,
    apply_migrations,
    declare_relation,
)

_SCHEMA = "forktex_grid"


async def main() -> None:
    postgres_url = os.environ.get(
        "POSTGRES_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres",
    )

    fresh_schema = f"grid_demo_{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(
        postgres_url,
        execution_options={"schema_translate_map": {_SCHEMA: fresh_schema}},
    )
    await apply_migrations(engine, schema=fresh_schema)

    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    namespace = str(uuid.uuid4())

    async with maker() as session:
        # ── 1. Declare two Grids ──────────────────────────────────────
        leads = await Grid.declare(
            session,
            TableSpec.from_dicts(
                slug="leads",
                label="Leads",
                namespace=namespace,
                columns=[
                    {"key": "title", "label": "Title", "type_id": FieldType.text.value},
                    {
                        "key": "status",
                        "label": "Status",
                        "type_id": FieldType.enum.value,
                        "config": {"options": ["open", "won", "lost"]},
                    },
                ],
            ),
        )
        notes = await Grid.declare(
            session,
            TableSpec.from_dicts(
                slug="notes",
                label="Notes",
                namespace=namespace,
                columns=[{"key": "body", "label": "Body", "type_id": FieldType.text.value}],
            ),
        )
        print(f"grids: {leads.slug}, {notes.slug}")

        # ── 2. Create + patch + archive lifecycle on a row ────────────
        lead_a = await leads.create({"title": "ACME Corp", "status": "open"})
        print(f"row created: {lead_a.values}")

        patched = await leads.patch(lead_a.id, {"status": "won"})
        assert patched.values["status"] == "won"
        assert patched.values["title"] == "ACME Corp"
        print(f"row patched: {patched.values}")

        # ── 3. Sibling row + related note, then a relation ────────────
        lead_b = await leads.create({"title": "Beta Inc", "status": "open"})
        note_for_a = await notes.create({"body": "Initial outreach"})

        # Declare the relation, then link the two rows through the curated API.
        await declare_relation(
            session,
            RelationSpec(key="has_note", source="leads", target="notes", shape=RelationShape.one_to_many),
            namespace,
        )
        await leads.relate("has_note", lead_a.id, note_for_a.id)

        # ── 4. Query + related traversal ──────────────────────────────
        page = await leads.query()
        print(f"active leads: {sorted(r.values['title'] for r in page.rows)}")

        related_notes = await leads.related("has_note", lead_a.id)
        assert note_for_a.id in {r.id for r in related_notes}
        print(f"lead_a notes: {[r.values['body'] for r in related_notes]}")

        # ── 5. Archive lead_b — query excludes it ─────────────────────
        await leads.archive(lead_b.id)
        page_after = await leads.query()
        active_after = sorted(r.values["title"] for r in page_after.rows)
        assert "Beta Inc" not in active_after
        print(f"active leads after archive: {active_after}")

        await session.commit()

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
